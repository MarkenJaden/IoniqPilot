"""Continuous telemetry ring buffer & event flight recorder ("was wie wann passiert")."""

from __future__ import annotations

from collections import deque
import csv
import io
from pathlib import Path
import re
import time
from typing import Any

from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_json, write_json_atomic

_SAFE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
MAX_RECORDINGS = 60


def build_sample(can_state: IoniqCanState, *, mono_s: float, wall_time: float | None = None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
  sample: dict[str, Any] = {
    't': round(wall_time if wall_time is not None else time.time(), 3),
    'monoS': round(mono_s, 3),
    'speedKph': round(can_state.best_speed_kph(), 2),
    'rpm': round(can_state.engine_rpm, 1),
    'torquePct': round(max(can_state.engine_torque_pct, can_state.tqi_b_pct), 1),
    'engineFiring': can_state.engine_is_firing(),
    'coolantC': None if can_state.coolant_temp_c is None else round(can_state.coolant_temp_c, 1),
    'ambientC': None if can_state.ambient_temp_c is None else round(can_state.ambient_temp_c, 1),
    'odometerKm': can_state.odometer_km,
    'fuelDispLvl': can_state.fuel_disp_level,
    'dteKm': can_state.dte_km,
    'fcoRaw': can_state.fco_raw,
    'bat12vV': can_state.bat_12v_voltage_v,
    'bat12vSoc': can_state.bat_12v_soc_pct,
    'mil': can_state.mil_lamp,
    'limp': can_state.limp_home_mode,
    'eco': can_state.eco_active or can_state.active_eco_switch,
    'tcuWarn': can_state.tcu_warn_msg,
    'absWarn': can_state.abs_warn_lamp or can_state.abs_defect,
  }
  if extra:
    sample.update(extra)
  return sample


class EventFlightRecorder:
  """Maintains a pre-trigger ring buffer, post-trigger captures, and continuous sessions."""

  def __init__(self, root: Path | str | None = None, *, pre_seconds: float = 30.0,
               post_seconds: float = 10.0, sample_hz: float = 2.0) -> None:
    self.root = ioniq_storage_root(root) / 'recordings'
    self.root.mkdir(parents=True, exist_ok=True)
    self.pre_seconds = max(5.0, pre_seconds)
    self.post_seconds = max(2.0, post_seconds)
    self.min_sample_dt = 1.0 / max(0.5, sample_hz)
    self._last_sample_mono = -1e9
    maxlen = max(20, int(self.pre_seconds * max(1.0, sample_hz) * 1.5))
    self._ring: deque[dict[str, Any]] = deque(maxlen=maxlen)
    self._pending_capture: dict[str, Any] | None = None
    self._continuous_session: dict[str, Any] | None = None

  def _index_path(self) -> Path:
    return self.root / 'index.json'

  def list_recordings(self) -> list[dict]:
    items = read_json(self._index_path(), [])
    return items if isinstance(items, list) else []

  def _save_index(self, items: list[dict]) -> None:
    write_json_atomic(self._index_path(), items)

  def status(self) -> dict:
    return {
      'continuousActive': self._continuous_session is not None,
      'continuousLabel': None if self._continuous_session is None else self._continuous_session['label'],
      'continuousSamples': 0 if self._continuous_session is None else len(self._continuous_session['samples']),
      'pendingCapture': None if self._pending_capture is None else self._pending_capture['reason'],
      'ringBufferSamples': len(self._ring),
      'recordings': self.list_recordings(),
    }

  def record_sample(self, can_state: IoniqCanState, mono_s: float, *, wall_time: float | None = None,
                    extra: dict[str, Any] | None = None, force: bool = False) -> dict | None:
    if not force and (mono_s - self._last_sample_mono) < self.min_sample_dt:
      return None
    self._last_sample_mono = mono_s
    sample = build_sample(can_state, mono_s=mono_s, wall_time=wall_time, extra=extra)
    self._ring.append(sample)

    if self._continuous_session is not None:
      samples = self._continuous_session['samples']
      if len(samples) < 3600:
        samples.append(sample)
      else:
        # Thin oldest points or rotate cleanly
        samples.pop(0)
        samples.append(sample)

    if self._pending_capture is not None:
      self._pending_capture['post_samples'].append(sample)
      if mono_s >= self._pending_capture['deadline_mono']:
        return self._finalize_pending_capture()
    return None

  def trigger_event(self, reason: str, title: str, can_state: IoniqCanState, *, mono_s: float,
                    wall_time: float | None = None, details: dict[str, Any] | None = None,
                    immediate: bool = False) -> dict | None:
    """Snapshot pre-trigger ring buffer and collect post-trigger samples (or save immediately)."""
    now = wall_time if wall_time is not None else time.time()
    self.record_sample(can_state, mono_s, wall_time=now, force=True)
    cutoff_mono = mono_s - self.pre_seconds
    pre_samples = [s for s in self._ring if s.get('monoS', 0.0) >= cutoff_mono]
    if self._pending_capture is not None and immediate:
      self._finalize_pending_capture()

    capture = {
      'id': f'rec_{int(now * 1000)}_{re.sub(r"[^a-z0-9]+", "_", reason.lower()).strip("_")[:24]}',
      'reason': reason,
      'title': title,
      'triggeredAt': now,
      'triggerMonoS': round(mono_s, 3),
      'deadline_mono': mono_s + (0.0 if immediate else self.post_seconds),
      'details': details or {},
      'events': [{'t': now, 'monoS': round(mono_s, 3), 'kind': reason, 'message': title, 'details': details or {}}],
      'pre_samples': pre_samples,
      'post_samples': [],
      'starred': reason in ('MANUAL', 'BUTTON'),
    }
    if immediate:
      self._pending_capture = capture
      return self._finalize_pending_capture()
    if self._pending_capture is not None:
      # Append event marker onto active post window instead of losing it
      self._pending_capture['events'].append(capture['events'][0])
      self._pending_capture['deadline_mono'] = max(self._pending_capture['deadline_mono'], mono_s + self.post_seconds)
      return None
    self._pending_capture = capture
    return None

  def add_event_marker(self, kind: str, message: str, *, mono_s: float, wall_time: float | None = None,
                       details: dict[str, Any] | None = None) -> None:
    now = wall_time if wall_time is not None else time.time()
    entry = {'t': now, 'monoS': round(mono_s, 3), 'kind': kind, 'message': message, 'details': details or {}}
    if self._pending_capture is not None:
      self._pending_capture['events'].append(entry)
    if self._continuous_session is not None:
      self._continuous_session['events'].append(entry)

  def start_continuous(self, label: str = 'Manuelle Vollaufzeichnung', *, mono_s: float = 0.0,
                       wall_time: float | None = None) -> dict:
    now = wall_time if wall_time is not None else time.time()
    if self._continuous_session is not None:
      self.stop_continuous(wall_time=now)
    self._continuous_session = {
      'id': f'cont_{int(now * 1000)}',
      'reason': 'CONTINUOUS',
      'title': label[:80] or 'Vollaufzeichnung',
      'startedAt': now,
      'events': [{'t': now, 'monoS': round(mono_s, 3), 'kind': 'START', 'message': 'Aufzeichnung gestartet'}],
      'samples': list(self._ring)[-10:],
      'starred': True,
    }
    return {'started': True, 'id': self._continuous_session['id'], 'title': self._continuous_session['title']}

  def stop_continuous(self, *, wall_time: float | None = None) -> dict | None:
    if self._continuous_session is None:
      return None
    now = wall_time if wall_time is not None else time.time()
    sess = self._continuous_session
    self._continuous_session = None
    sess['events'].append({'t': now, 'kind': 'STOP', 'message': 'Aufzeichnung beendet'})
    return self._write_bundle(
      rec_id=sess['id'],
      reason='CONTINUOUS',
      title=sess['title'],
      started_at=sess['startedAt'],
      ended_at=now,
      samples=sess['samples'],
      events=sess['events'],
      details={'mode': 'continuous'},
      starred=True,
    )

  def flush(self) -> list[dict]:
    """Finalize any open capture/continuous session safely on shutdown."""
    saved: list[dict] = []
    if self._pending_capture is not None:
      saved.append(self._finalize_pending_capture())
    if self._continuous_session is not None:
      item = self.stop_continuous()
      if item is not None:
        saved.append(item)
    return saved

  def _finalize_pending_capture(self) -> dict:
    cap = self._pending_capture
    assert cap is not None
    self._pending_capture = None
    samples = cap['pre_samples'] + cap['post_samples']
    started_at = samples[0]['t'] if samples else cap['triggeredAt']
    ended_at = samples[-1]['t'] if samples else cap['triggeredAt']
    return self._write_bundle(
      rec_id=cap['id'],
      reason=cap['reason'],
      title=cap['title'],
      started_at=started_at,
      ended_at=ended_at,
      samples=samples,
      events=cap['events'],
      details=cap['details'],
      starred=cap['starred'],
    )

  def _write_bundle(self, *, rec_id: str, reason: str, title: str, started_at: float, ended_at: float,
                    samples: list[dict], events: list[dict], details: dict, starred: bool) -> dict:
    speeds = [s['speedKph'] for s in samples if isinstance(s.get('speedKph'), (int, float))]
    rpms = [s['rpm'] for s in samples if isinstance(s.get('rpm'), (int, float))]
    summary = {
      'id': rec_id,
      'reason': reason,
      'title': title,
      'startedAt': started_at,
      'endedAt': ended_at,
      'durationS': round(max(0.0, ended_at - started_at), 1),
      'sampleCount': len(samples),
      'eventCount': len(events),
      'maxSpeedKph': round(max(speeds), 1) if speeds else 0.0,
      'maxRpm': round(max(rpms), 0) if rpms else 0.0,
      'starred': starred,
    }
    bundle = dict(summary)
    bundle['details'] = details
    bundle['events'] = events
    bundle['samples'] = samples
    write_json_atomic(self.root / f'{rec_id}.json', bundle)

    index = [item for item in self.list_recordings() if item.get('id') != rec_id]
    index.insert(0, summary)
    while len(index) > MAX_RECORDINGS:
      victim_idx = next((i for i in range(len(index) - 1, -1, -1) if not index[i].get('starred')), len(index) - 1)
      victim = index.pop(victim_idx)
      victim_id = str(victim.get('id', ''))
      if _SAFE_ID.fullmatch(victim_id):
        try:
          (self.root / f'{victim_id}.json').unlink(missing_ok=True)
        except OSError:
          pass
    self._save_index(index)
    return summary

  def read_recording(self, rec_id: str) -> dict | None:
    if not _SAFE_ID.fullmatch(rec_id):
      return None
    data = read_json(self.root / f'{rec_id}.json', None)
    return data if isinstance(data, dict) else None

  def delete_recording(self, rec_id: str) -> bool:
    if not _SAFE_ID.fullmatch(rec_id):
      return False
    index = self.list_recordings()
    new_index = [item for item in index if item.get('id') != rec_id]
    if len(new_index) == len(index) and not (self.root / f'{rec_id}.json').exists():
      return False
    try:
      (self.root / f'{rec_id}.json').unlink(missing_ok=True)
    except OSError:
      pass
    self._save_index(new_index)
    return True

  def export_csv(self, rec_id: str) -> str | None:
    bundle = self.read_recording(rec_id)
    if bundle is None:
      return None
    samples = bundle.get('samples') or []
    fieldnames = [
      't', 'monoS', 'speedKph', 'rpm', 'torquePct', 'engineFiring', 'coolantC',
      'ambientC', 'odometerKm', 'fuelDispLvl', 'dteKm', 'fcoRaw', 'bat12vV',
      'bat12vSoc', 'mil', 'limp', 'eco', 'tcuWarn', 'absWarn',
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    for row in samples:
      writer.writerow(row)
    return buf.getvalue()
