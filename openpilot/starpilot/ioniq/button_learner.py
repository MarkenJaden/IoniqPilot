"""3x-press passive CAN button learner and configurable action dispatcher for Hyundai Ioniq."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable

from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_json, write_json_atomic

BUTTON_ACTIONS: tuple[dict[str, str], ...] = (
  {'id': 'trigger_event_recording', 'label': 'Diagnose- & Event-Aufzeichnung speichern'},
  {'id': 'toggle_experimental_mode', 'label': 'Experimental Mode umschalten'},
  {'id': 'toggle_always_on_lateral', 'label': 'Always-On Lateral (AOL) umschalten'},
  {'id': 'cycle_personality', 'label': 'Fahrprofil / Abstand durchschalten'},
  {'id': 'toggle_speed_limit_control', 'label': 'Tempolimit-Assistent (SLC) umschalten'},
  {'id': 'mark_refuel', 'label': 'Tankstopp / Event-Marker setzen'},
  {'id': 'schedule_dtc_scan', 'label': 'Steuergeräte-Scan beim nächsten Start vormerken'},
)

VALID_ACTION_IDS: frozenset[str] = frozenset(item['id'] for item in BUTTON_ACTIONS)

# Common powertrain / wheel speed / radar frames that change rapidly during normal driving and should not
# be mistaken for dedicated button presses if baseline only captured stationary frames.
DEFAULT_IGNORE_ADDRESSES: frozenset[int] = frozenset((
  0x2B0,  # SAS11 steering angle
  0x316,  # EMS11 RPM/speed
  0x386,  # WHL_SPD11 wheel speeds
  0x420,  # SCC11
  0x421,  # SCC12
  0x251,  # MDPS12
))


@dataclass
class _BitTracker:
  bus: int
  address: int
  byte_idx: int
  bit_mask: int
  rest_value: int
  active_value: int | None = None
  is_pressed: bool = False
  press_started_s: float = 0.0
  last_edge_s: float = 0.0
  pulses: list[tuple[float, float]] = field(default_factory=list)
  transitions: list[float] = field(default_factory=list)
  noisy: bool = False


class ButtonLearnEngine:
  """Learns a clean CAN button signal after the user presses a button 3 consecutive times."""

  def __init__(
    self,
    *,
    baseline_seconds: float = 1.2,
    listen_timeout_seconds: float = 30.0,
    target_presses: int = 3,
    min_press_gap_s: float = 0.14,
    max_press_duration_s: float = 3.5,
    ignore_addresses: frozenset[int] = DEFAULT_IGNORE_ADDRESSES,
  ) -> None:
    self.baseline_seconds = baseline_seconds
    self.listen_timeout_seconds = listen_timeout_seconds
    self.target_presses = target_presses
    self.min_press_gap_s = min_press_gap_s
    self.max_press_duration_s = max_press_duration_s
    self.ignore_addresses = ignore_addresses

    self.state: str = 'idle'  # idle | sampling_baseline | listening | learned | timeout | cancelled
    self.label: str = ''
    self.action_id: str = 'trigger_event_recording'
    self.started_mono_s: float = 0.0
    self.baseline_end_s: float = 0.0
    self.listen_deadline_s: float = 0.0
    self.detected_presses: int = 0
    self.message: str = 'Bereit zum Anlernen.'
    self.learned_binding: dict[str, Any] | None = None
    self.candidates: list[dict[str, Any]] = []

    self._baseline_bytes: dict[tuple[int, int], bytearray] = {}
    self._baseline_flips: dict[tuple[int, int, int], list[int]] = {}
    self._volatile_masks: dict[tuple[int, int], bytearray] = {}
    self._last_byte_val: dict[tuple[int, int, int], int] = {}
    self._trackers: dict[tuple[int, int, int, int], _BitTracker] = {}

  def start(self, *, label: str, action_id: str, mono_s: float) -> dict[str, Any]:
    if action_id not in VALID_ACTION_IDS:
      raise ValueError(f'Unknown button action: {action_id}')
    clean_label = (label or 'Custom Knopf').strip()[:48] or 'Custom Knopf'
    self.state = 'sampling_baseline'
    self.label = clean_label
    self.action_id = action_id
    self.started_mono_s = mono_s
    self.baseline_end_s = mono_s + self.baseline_seconds
    self.listen_deadline_s = self.baseline_end_s + self.listen_timeout_seconds
    self.detected_presses = 0
    self.learned_binding = None
    self.candidates = []
    self._baseline_bytes.clear()
    self._baseline_flips.clear()
    self._volatile_masks.clear()
    self._last_byte_val.clear()
    self._trackers.clear()
    self.message = f'Kalibriere Ruhezustand ({self.baseline_seconds:.1f}s) — bitte jetzt keinen Knopf drücken...'
    return self.status(mono_s)

  def cancel(self) -> None:
    self.state = 'idle'
    self.message = 'Anlernvorgang abgebrochen.'
    self.detected_presses = 0

  def feed_frame(self, mono_s: float, bus: int, address: int, data: bytes | bytearray | memoryview) -> dict[str, Any] | None:
    if self.state not in ('sampling_baseline', 'listening'):
      return None
    if address in self.ignore_addresses or len(data) == 0:
      return None

    key = (bus, address)
    frame = bytes(data)

    if self.state == 'sampling_baseline':
      self._update_baseline(key, frame)
      if mono_s >= self.baseline_end_s:
        self._finalize_baseline()
        self.state = 'listening'
        self.message = f'Bitte "{self.label}" jetzt {self.target_presses}x kurz hintereinander drücken (0/{self.target_presses}).'
      return None

    # Listening phase
    if mono_s > self.listen_deadline_s:
      return self._finish_on_timeout(mono_s)

    if key not in self._baseline_bytes:
      # Unseen CAN ID after baseline: learn its initial baseline silently
      self._baseline_bytes[key] = bytearray(frame)
      self._volatile_masks[key] = bytearray(len(frame))
      return None

    base = self._baseline_bytes[key]
    volatile = self._volatile_masks[key]
    limit = min(len(frame), len(base))

    for byte_idx in range(limit):
      last_key = (bus, address, byte_idx)
      prev_val = self._last_byte_val.get(last_key, base[byte_idx])
      cur_val = frame[byte_idx]
      if cur_val == prev_val:
        continue
      self._last_byte_val[last_key] = cur_val

      stable_mask = (~volatile[byte_idx]) & 0xFF
      delta_from_base = (cur_val ^ base[byte_idx]) & stable_mask

      # Update existing trackers on this byte first
      for tkey in [k for k in self._trackers if k[:3] == (bus, address, byte_idx)]:
        tracker = self._trackers[tkey]
        if tracker.noisy:
          continue
        cur_masked = cur_val & tracker.bit_mask
        if tracker.is_pressed:
          if cur_masked == tracker.rest_value:
            duration = mono_s - tracker.press_started_s
            tracker.is_pressed = False
            if 0.01 <= duration <= self.max_press_duration_s:
              if not tracker.pulses or (tracker.press_started_s - tracker.pulses[-1][0]) >= self.min_press_gap_s:
                tracker.pulses.append((tracker.press_started_s, mono_s))
                tracker.transitions.append(mono_s)
            elif duration > self.max_press_duration_s:
              tracker.noisy = True
        else:
          if cur_masked != tracker.rest_value:
            if (mono_s - tracker.last_edge_s) < 0.05 and len(tracker.transitions) > 4:
              tracker.noisy = True
            else:
              tracker.is_pressed = True
              tracker.press_started_s = mono_s
              tracker.active_value = cur_masked
              if not tracker.transitions or (mono_s - tracker.transitions[-1]) >= self.min_press_gap_s:
                tracker.transitions.append(mono_s)
        tracker.last_edge_s = mono_s
        if len(tracker.transitions) > 12:
          tracker.noisy = True

      if delta_from_base != 0:
        tkey = (bus, address, byte_idx, delta_from_base)
        if tkey not in self._trackers:
          # Avoid creating redundant sub-trackers if an active identical tracker already tracks these bits
          tracker = _BitTracker(
            bus=bus,
            address=address,
            byte_idx=byte_idx,
            bit_mask=delta_from_base,
            rest_value=base[byte_idx] & delta_from_base,
            active_value=cur_val & delta_from_base,
            is_pressed=True,
            press_started_s=mono_s,
            last_edge_s=mono_s,
            transitions=[mono_s],
          )
          self._trackers[tkey] = tracker

    return self._evaluate_candidates(mono_s)

  def _update_baseline(self, key: tuple[int, int], frame: bytes) -> None:
    if key not in self._baseline_bytes:
      self._baseline_bytes[key] = bytearray(frame)
      self._volatile_masks[key] = bytearray(len(frame))
      return
    base = self._baseline_bytes[key]
    for idx in range(min(len(frame), len(base))):
      diff = frame[idx] ^ base[idx]
      if diff:
        flips = self._baseline_flips.setdefault((key[0], key[1], idx), [0] * 8)
        for bit in range(8):
          if diff & (1 << bit):
            flips[bit] += 1
        base[idx] = frame[idx]

  def _finalize_baseline(self) -> None:
    for (bus, addr, idx), flips in self._baseline_flips.items():
      mask = 0
      for bit, count in enumerate(flips):
        if count >= 1:
          mask |= (1 << bit)
      if (bus, addr) in self._volatile_masks and idx < len(self._volatile_masks[(bus, addr)]):
        self._volatile_masks[(bus, addr)][idx] = mask
    # Also mask common trailing byte checksum/alive nibbles when high flip frequency seen anywhere in frame

  def _evaluate_candidates(self, mono_s: float) -> dict[str, Any] | None:
    best_pulse_count = 0
    best_toggle_tracker: _BitTracker | None = None
    ready_momentary: list[_BitTracker] = []

    for tracker in self._trackers.values():
      if tracker.noisy:
        continue
      pulse_n = len(tracker.pulses)
      trans_n = len(tracker.transitions)
      best_pulse_count = max(best_pulse_count, pulse_n)
      if pulse_n >= self.target_presses:
        ready_momentary.append(tracker)
      elif trans_n == self.target_presses and (mono_s - tracker.last_edge_s) >= 0.85:
        best_toggle_tracker = tracker

    self.detected_presses = max(best_pulse_count, self.detected_presses)
    if self.detected_presses > 0 and not ready_momentary:
      self.message = (
        f'Erkannt: {min(self.detected_presses, self.target_presses)}/{self.target_presses} Tastendrücke '
        f'für "{self.label}"...'
      )

    if ready_momentary:
      # Prefer single-bit / cleaner masks, lowest noisy bit count
      ready_momentary.sort(key=lambda t: (t.bit_mask.bit_count(), abs(len(t.pulses) - self.target_presses), t.address))
      winner = ready_momentary[0]
      return self._accept_winner(winner, mode='momentary', alternatives=ready_momentary[1:], mono_s=mono_s)

    if best_toggle_tracker is not None:
      return self._accept_winner(best_toggle_tracker, mode='toggle', alternatives=[], mono_s=mono_s)

    return None

  def _finish_on_timeout(self, mono_s: float) -> dict[str, Any] | None:
    # Check if any toggle candidate reached target transitions just before timeout
    for tracker in self._trackers.values():
      if not tracker.noisy and len(tracker.transitions) == self.target_presses:
        return self._accept_winner(tracker, mode='toggle', alternatives=[], mono_s=mono_s)
    self.state = 'timeout'
    self.message = f'Zeitüberschreitung beim Anlernen von "{self.label}" ({self.detected_presses}/{self.target_presses} Drücke erkannt).'
    return None

  def _accept_winner(self, winner: _BitTracker, *, mode: str, alternatives: list[_BitTracker],
                     mono_s: float) -> dict[str, Any]:
    now = time.time()
    binding = {
      'id': f'btn_{int(now * 1000)}_{winner.address:03x}_{winner.byte_idx}_{winner.bit_mask:02x}',
      'name': self.label,
      'action': self.action_id,
      'bus': winner.bus,
      'address': winner.address,
      'addressHex': f'0x{winner.address:03X}',
      'byteIndex': winner.byte_idx,
      'bitMask': winner.bit_mask,
      'bitMaskHex': f'0x{winner.bit_mask:02X}',
      'restValue': winner.rest_value,
      'activeValue': winner.active_value if winner.active_value is not None else (winner.rest_value ^ winner.bit_mask),
      'mode': mode,
      'enabled': True,
      'triggerCount': 0,
      'lastTriggeredAt': None,
      'learnedAt': now,
    }
    self.learned_binding = binding
    self.candidates = [
      {
        'bus': alt.bus,
        'addressHex': f'0x{alt.address:03X}',
        'byteIndex': alt.byte_idx,
        'bitMaskHex': f'0x{alt.bit_mask:02X}',
      }
      for alt in alternatives[:4]
    ]
    self.detected_presses = self.target_presses
    self.state = 'learned'
    self.message = (
      f'Knopf "{self.label}" erfolgreich gelernt: Bus {winner.bus}, '
      f'0x{winner.address:03X} Byte {winner.byte_idx} Maske 0x{winner.bit_mask:02X} ({mode}).'
    )
    return binding

  def status(self, mono_s: float | None = None) -> dict[str, Any]:
    remaining = 0.0
    if self.state in ('sampling_baseline', 'listening') and mono_s is not None:
      remaining = max(0.0, self.listen_deadline_s - mono_s)
    return {
      'state': self.state,
      'label': self.label,
      'action': self.action_id,
      'detectedPresses': self.detected_presses,
      'targetPresses': self.target_presses,
      'remainingS': round(remaining, 1),
      'message': self.message,
      'learnedBinding': self.learned_binding,
      'alternatives': self.candidates,
    }


class ButtonMatcher:
  """Matches incoming CAN frames against saved learned button bindings with debouncing."""

  def __init__(self, *, cooldown_s: float = 0.65) -> None:
    self.cooldown_s = cooldown_s
    self._last_active: dict[str, bool] = {}
    self._last_masked_val: dict[str, int] = {}
    self._last_trigger_mono: dict[str, float] = {}

  def process_frame(self, mono_s: float, bus: int, address: int, data: bytes | bytearray | memoryview,
                    bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    triggered: list[dict[str, Any]] = []
    for binding in bindings:
      if not binding.get('enabled', True):
        continue
      if int(binding.get('bus', -1)) != bus or int(binding.get('address', -1)) != address:
        continue
      byte_idx = int(binding.get('byteIndex', 0))
      if byte_idx >= len(data):
        continue
      bid = str(binding.get('id', ''))
      mask = int(binding.get('bitMask', 0))
      masked = int(data[byte_idx]) & mask
      mode = binding.get('mode', 'momentary')

      if mode == 'toggle':
        prev = self._last_masked_val.get(bid)
        self._last_masked_val[bid] = masked
        if prev is not None and masked != prev:
          if (mono_s - self._last_trigger_mono.get(bid, -1e9)) >= self.cooldown_s:
            self._last_trigger_mono[bid] = mono_s
            triggered.append(binding)
      else:
        active_val = int(binding.get('activeValue', 0))
        is_active = masked == active_val
        was_active = self._last_active.get(bid, False)
        self._last_active[bid] = is_active
        if is_active and not was_active:
          if (mono_s - self._last_trigger_mono.get(bid, -1e9)) >= self.cooldown_s:
            self._last_trigger_mono[bid] = mono_s
            triggered.append(binding)
    return triggered


def buttons_path(root: Path | str | None = None) -> Path:
  return ioniq_storage_root(root) / 'buttons.json'


def read_buttons_state(root: Path | str | None = None) -> dict[str, Any]:
  default: dict[str, Any] = {
    'bindings': [],
    'learnRequest': None,
    'learnStatus': {'state': 'idle', 'detectedPresses': 0, 'targetPresses': 3, 'message': 'Bereit zum Anlernen.'},
    'lastTrigger': None,
  }
  data = read_json(buttons_path(root), default)
  if not isinstance(data, dict):
    return default
  for k, v in default.items():
    data.setdefault(k, v)
  if not isinstance(data.get('bindings'), list):
    data['bindings'] = []
  return data


def write_buttons_state(state: dict[str, Any], root: Path | str | None = None) -> dict[str, Any]:
  write_json_atomic(buttons_path(root), state)
  return state


def execute_button_action(action_id: str, params_obj: Any = None, *,
                          on_record: Callable[[str], Any] | None = None,
                          on_refuel_marker: Callable[[], Any] | None = None,
                          on_schedule_scan: Callable[[], Any] | None = None) -> dict[str, Any]:
  """Execute one mapped action cleanly via OpenPilot Params or Ioniq subsystems."""
  if action_id not in VALID_ACTION_IDS:
    raise ValueError(f'Unknown button action: {action_id}')

  if action_id == 'toggle_experimental_mode' and params_obj is not None:
    cur = bool(params_obj.get_bool('ExperimentalMode'))
    params_obj.put_bool('ExperimentalMode', not cur)
    return {'action': action_id, 'newValue': not cur, 'summary': f'Experimental Mode -> {"AN" if not cur else "AUS"}'}

  if action_id == 'toggle_always_on_lateral' and params_obj is not None:
    cur = bool(params_obj.get_bool('AlwaysOnLateral'))
    params_obj.put_bool('AlwaysOnLateral', not cur)
    return {'action': action_id, 'newValue': not cur, 'summary': f'Always-On Lateral -> {"AN" if not cur else "AUS"}'}

  if action_id == 'toggle_speed_limit_control' and params_obj is not None:
    cur = bool(params_obj.get_bool('SpeedLimitController'))
    params_obj.put_bool('SpeedLimitController', not cur)
    return {'action': action_id, 'newValue': not cur, 'summary': f'Tempolimit-Assistent -> {"AN" if not cur else "AUS"}'}

  if action_id == 'cycle_personality' and params_obj is not None:
    try:
      cur = int(params_obj.get('LongitudinalPersonality') or 1)
    except (ValueError, TypeError):
      cur = 1
    nxt = (cur + 1) % 3
    params_obj.put('LongitudinalPersonality', str(nxt))
    names = ('Aggressiv', 'Standard', 'Entspannt')
    return {'action': action_id, 'newValue': nxt, 'summary': f'Fahrprofil -> {names[nxt]}'}

  if action_id == 'trigger_event_recording':
    if on_record is not None:
      on_record('Manueller Event-Knopf')
    return {'action': action_id, 'summary': 'Diagnose-/Event-Aufzeichnung ausgelöst'}

  if action_id == 'mark_refuel':
    if on_refuel_marker is not None:
      on_refuel_marker()
    return {'action': action_id, 'summary': 'Tankstopp-Marker gesetzt'}

  if action_id == 'schedule_dtc_scan':
    if on_schedule_scan is not None:
      on_schedule_scan()
    return {'action': action_id, 'summary': 'Steuergeräte-Scan für nächsten Start vorgemerkt'}

  return {'action': action_id, 'summary': f'Aktion {action_id} ausgeführt (ohne Params)'}
