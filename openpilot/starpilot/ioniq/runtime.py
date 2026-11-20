"""Background runtime & startup diagnostic hook for 2016 Hyundai Ioniq Hybrid (ioniqd)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import logging
import signal
import threading
import time
from typing import Any, NamedTuple

from openpilot.starpilot.ioniq.button_learner import (
  ButtonLearnEngine,
  ButtonMatcher,
  execute_button_action,
  read_buttons_state,
  write_buttons_state,
)
from openpilot.starpilot.ioniq.can_discovery import CanFuelDiscoveryRecorder
from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.fuel_tracker import FuelTripTracker
from openpilot.starpilot.ioniq.recorder import EventFlightRecorder
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_config, read_json, write_json_atomic
from openpilot.starpilot.ioniq.uds_dtc import (
  PassiveEcuMonitor,
  apply_clear_result,
  apply_scan_result,
  clear_ioniq_dtcs,
  clear_passive_warnings_state,
  queue_diagnostic_action,
  read_diagnostics_state,
  scan_ioniq_ecus,
  write_diagnostics_state,
)


def _utc_timestamp() -> float:
  return datetime.now(UTC).timestamp()


class _CanFrame(NamedTuple):
  address: int
  dat: bytes
  src: int


def can_comm_callbacks(logcan: Any, sendcan: Any) -> tuple[Callable[..., list[list[_CanFrame]]], Callable[[list[Any]], None]]:
  """Build CAN send/recv callbacks without importing openpilot.selfdrive.car.card."""
  from openpilot.cereal import messaging
  from openpilot.selfdrive.pandad import can_list_to_can_capnp

  def can_recv(wait_for_one: bool = False) -> list[list[_CanFrame]]:
    ret: list[list[_CanFrame]] = []
    for can in messaging.drain_sock(logcan, wait_for_one=wait_for_one):
      ret.append([_CanFrame(int(msg.address), bytes(msg.dat), int(msg.src)) for msg in can.can])
    return ret

  def can_send(msgs: list[Any]) -> None:
    sendcan.send(can_list_to_can_capnp(msgs, msgtype='sendcan'))

  return can_recv, can_send


def commands_path(root: Path | str | None = None) -> Path:
  return ioniq_storage_root(root) / 'commands.json'


def push_runtime_command(command: str, args: dict[str, Any] | None = None,
                         root: Path | str | None = None) -> dict[str, Any]:
  """Enqueue a UI/Galaxy command for the live ioniqd daemon."""
  path = commands_path(root)
  queue = read_json(path, [])
  if not isinstance(queue, list):
    queue = []
  now = _utc_timestamp()
  item = {'id': f'cmd_{int(now * 1000)}_{len(queue)}', 'command': command, 'args': args or {}, 'createdAt': now}
  queue.append(item)
  write_json_atomic(path, queue[-16:])
  return item


def maybe_run_startup_diagnostic(
  can_recv: Callable[..., list[Any]],
  can_send: Callable[[list[Any]], Any],
  set_obd_multiplexing: Callable[[bool], Any],
  *,
  brand: str | None = None,
  root: Path | str | None = None,
) -> dict[str, Any] | None:
  """Run queued UDS clear/scan (or scanOnStart) during card's OBD-multiplexed startup window."""
  if brand is not None and brand != 'hyundai':
    return None
  cfg = read_config(root)
  diag = read_diagnostics_state(root)
  pending = diag.get('pendingAction')
  should_scan = bool(cfg.get('scanOnStart')) or (isinstance(pending, dict) and pending.get('type') == 'scan')
  should_clear = isinstance(pending, dict) and pending.get('type') == 'clear'
  if not should_scan and not should_clear:
    return None

  result: dict[str, Any] = {'ranClear': False, 'ranScan': False}
  obd_mux_on = False

  def _set_mux(enabled: bool) -> None:
    nonlocal obd_mux_on
    if enabled != obd_mux_on:
      set_obd_multiplexing(enabled)
      obd_mux_on = enabled

  try:
    if should_clear:
      _set_mux(True)
      target = str(pending.get('target', 'ALL')) if isinstance(pending, dict) else 'ALL'
      clear_res = clear_ioniq_dtcs(
        can_send, can_recv, target_ecu=target, bus=1, fallback_bus=0, set_obd_multiplexing=_set_mux,
      )
      apply_clear_result(clear_res, root=root)
      result['ranClear'] = True
      result['clear'] = clear_res
    if should_scan:
      _set_mux(True)
      scan_res = scan_ioniq_ecus(
        can_send,
        can_recv,
        bus=1,
        fallback_bus=0,
        set_obd_multiplexing=_set_mux,
        read_identities=True,
        timeout_s=0.35,
        total_budget_s=2.2,
      )
      apply_scan_result(scan_res, root=root)
      result['ranScan'] = True
      result['scan'] = scan_res
  finally:
    if obd_mux_on:
      try:
        _set_mux(False)
      except Exception:
        pass
  return result


class IoniqRuntimeController:
  """Orchestrates CAN signal decoding, DTC passive/active health, 3x button learn, recorder, and fuel tracker."""

  def __init__(self, *, root: Path | str | None = None, params_obj: Any = None,
               clock: Callable[[], float] = time.monotonic) -> None:
    self.root = ioniq_storage_root(root)
    self.params = params_obj
    self.clock = clock
    self.cfg = read_config(self.root)
    self.can_state = IoniqCanState()
    self.monitor = PassiveEcuMonitor()
    self.recorder = EventFlightRecorder(
      self.root,
      pre_seconds=float(self.cfg.get('recorderPreSeconds', 30.0)),
      post_seconds=float(self.cfg.get('recorderPostSeconds', 10.0)),
    )
    self.discovery = CanFuelDiscoveryRecorder(self.root)
    self.learner = ButtonLearnEngine()
    self.matcher = ButtonMatcher()
    self.fuel = FuelTripTracker(
      self.root,
      fco_scale_ul=float(self.cfg.get('fcoScaleUlPerTick', 0.128)),
      calibration_factor=float(self.cfg.get('fuelCalibrationFactor', 1.0)),
      price_per_liter=float(self.cfg.get('fuelPriceEurPerLiter', 1.75)),
      tank_capacity_liters=float(self.cfg.get('tankCapacityLiters', 45.0)),
    )

    self._buttons_state = read_buttons_state(self.root)
    self._mtimes: dict[str, int] = {}
    self._refresh_mtimes()
    self._last_sync_mono = -1e9
    self._last_learn_sync_mono = -1e9
    self._last_learn_state = 'idle'
    self._last_diag_sync_mono = -1e9
    self._diag_dirty = False

  def _file_mtime(self, path: Path) -> int:
    try:
      return path.stat().st_mtime_ns
    except OSError:
      return 0

  def _refresh_mtimes(self) -> None:
    self._mtimes['config'] = self._file_mtime(self.root / 'config.json')
    self._mtimes['buttons'] = self._file_mtime(self.root / 'buttons.json')
    self._mtimes['fuel'] = self._file_mtime(self.root / 'fuel.json')
    self._mtimes['commands'] = self._file_mtime(commands_path(self.root))

  def _reload_external_changes(self, mono_s: float, *, wall_time: float | None = None) -> None:
    cfg_changed = False
    cfg_mtime = self._file_mtime(self.root / 'config.json')
    if cfg_mtime != self._mtimes.get('config'):
      cfg_changed = True
      self._mtimes['config'] = cfg_mtime
      self.cfg = read_config(self.root)
      self.recorder.pre_seconds = float(self.cfg.get('recorderPreSeconds', 30.0))
      self.recorder.post_seconds = float(self.cfg.get('recorderPostSeconds', 10.0))
      self.fuel.fco_scale_ul = float(self.cfg.get('fcoScaleUlPerTick', 0.128))
      self.fuel.price_per_liter = float(self.cfg.get('fuelPriceEurPerLiter', 1.75))
      self.fuel.calibration_factor = float(self.cfg.get('fuelCalibrationFactor', 1.0))
      self.fuel.tank_capacity_liters = float(self.cfg.get('tankCapacityLiters', 45.0))

    btn_mtime = self._file_mtime(self.root / 'buttons.json')
    if btn_mtime != self._mtimes.get('buttons'):
      self._mtimes['buttons'] = btn_mtime
      self._buttons_state = read_buttons_state(self.root)

    fuel_mtime = self._file_mtime(self.root / 'fuel.json')
    if fuel_mtime != self._mtimes.get('fuel'):
      self._mtimes['fuel'] = fuel_mtime
      prev_cal = self.fuel.calibration_factor
      old_file_cal = self.fuel.state.get('calibrationFactor')
      reloaded = FuelTripTracker(
        self.root,
        fco_scale_ul=float(self.cfg.get('fcoScaleUlPerTick', 0.128)),
        price_per_liter=float(self.cfg.get('fuelPriceEurPerLiter', 1.75)),
        tank_capacity_liters=float(self.cfg.get('tankCapacityLiters', 45.0)),
      )
      if cfg_changed or reloaded.calibration_factor == old_file_cal:
        reloaded.calibration_factor = prev_cal
      self.fuel = reloaded

    cmd_path = commands_path(self.root)
    cmd_mtime = self._file_mtime(cmd_path)
    if cmd_mtime != self._mtimes.get('commands') and cmd_mtime > 0:
      queue = read_json(cmd_path, [])
      if isinstance(queue, list) and queue:
        write_json_atomic(cmd_path, [])
        self._mtimes['commands'] = self._file_mtime(cmd_path)
        for item in queue:
          if isinstance(item, dict):
            self._handle_command(item, mono_s=mono_s, wall_time=wall_time)
      else:
        self._mtimes['commands'] = cmd_mtime

  def _handle_command(self, item: dict[str, Any], *, mono_s: float, wall_time: float | None = None) -> None:
    cmd = item.get('command')
    args = item.get('args') if isinstance(item.get('args'), dict) else {}
    if cmd == 'start_learn':
      label = str(args.get('label', 'Custom Knopf'))
      action_id = str(args.get('action', 'trigger_event_recording'))
      self.learner.start(label=label, action_id=action_id, mono_s=mono_s)
      self._persist_learn_status()
    elif cmd == 'cancel_learn':
      self.learner.cancel()
      self._persist_learn_status()
    elif cmd == 'record_snapshot':
      title = str(args.get('title', 'Manuelle Diagnose-Aufzeichnung'))
      self.recorder.trigger_event('MANUAL', title, self.can_state, mono_s=mono_s, immediate=bool(args.get('immediate', False)))
    elif cmd == 'start_continuous_recording':
      label = str(args.get('label', 'Vollaufzeichnung'))
      self.recorder.start_continuous(label, mono_s=mono_s)
    elif cmd == 'stop_continuous_recording':
      self.recorder.stop_continuous()
    elif cmd == 'start_can_discovery':
      label = str(args.get('label', 'Verbrauchsanalyse'))
      self.discovery.start(label=label, mono_s=mono_s, wall_time=wall_time)
      self._diag_dirty = True
    elif cmd == 'stop_can_discovery':
      raw_l100 = args.get('dashboardLPer100Km')
      raw_km = args.get('dashboardDistanceKm')
      dash_l100 = float(raw_l100) if isinstance(raw_l100, (int, float)) else None
      dash_km = float(raw_km) if isinstance(raw_km, (int, float)) else None
      self.discovery.stop(
        dashboard_l_per_100km=dash_l100,
        dashboard_distance_km=dash_km,
        notes=str(args.get('notes', '')),
        wall_time=wall_time,
      )
      self._diag_dirty = True
    elif cmd == 'start_manual_trip':
      self.fuel.start_manual_trip(label=str(args.get('label', '')))
      self._mtimes['fuel'] = self._file_mtime(self.root / 'fuel.json')
    elif cmd == 'end_manual_trip':
      self.fuel.end_trip()
      self._mtimes['fuel'] = self._file_mtime(self.root / 'fuel.json')
    elif cmd == 'clear_passive_warnings':
      self.monitor.clear_passive_warnings()
      clear_passive_warnings_state(self.root)
    elif cmd == 'reset_fuel_calibration':
      self.fuel.reset_calibration()
      self._mtimes['fuel'] = self._file_mtime(self.root / 'fuel.json')

  def _persist_learn_status(self) -> None:
    self._buttons_state['learnStatus'] = self.learner.status(self.clock())
    write_buttons_state(self._buttons_state, self.root)
    self._mtimes['buttons'] = self._file_mtime(self.root / 'buttons.json')

  def _execute_binding(self, binding: dict[str, Any], mono_s: float, wall_time: float) -> None:
    action_id = str(binding.get('action', ''))
    res = execute_button_action(
      action_id,
      self.params,
      on_record=lambda title: self.recorder.trigger_event('BUTTON', f'{binding.get("name")}: {title}',
                                                          self.can_state, mono_s=mono_s, wall_time=wall_time),
      on_refuel_marker=lambda: self.recorder.trigger_event('REFUEL', 'Tankstopp-Marker per Taste',
                                                           self.can_state, mono_s=mono_s, wall_time=wall_time, immediate=True),
      on_schedule_scan=lambda: queue_diagnostic_action('scan', 'ALL', root=self.root),
    )
    binding['triggerCount'] = int(binding.get('triggerCount', 0)) + 1
    binding['lastTriggeredAt'] = wall_time
    self._buttons_state['lastTrigger'] = {
      'bindingId': binding.get('id'),
      'name': binding.get('name'),
      'action': action_id,
      'summary': res.get('summary'),
      'triggeredAt': wall_time,
    }
    write_buttons_state(self._buttons_state, self.root)
    self._mtimes['buttons'] = self._file_mtime(self.root / 'buttons.json')

  def step(
    self,
    frames: list[tuple[int, int, bytes]],
    *,
    mono_s: float,
    wall_time: float | None = None,
    ignition_hint: bool | None = None,
    elm327_active: bool = False,
    can_send: Callable[[list[Any]], Any] | None = None,
    can_recv: Callable[..., list[Any]] | None = None,
    car_state_msg: Any | None = None,
  ) -> dict[str, Any]:
    """Process a batch of CAN frames `(bus, address, data)` plus peripheral state."""
    now = wall_time if wall_time is not None else _utc_timestamp()
    if (mono_s - self._last_sync_mono) >= 0.4:
      self._last_sync_mono = mono_s
      self._reload_external_changes(mono_s, wall_time=now)

    if car_state_msg is not None:
      self.can_state.apply_car_state(car_state_msg, mono_s)

    learn_just_completed = False
    bindings = list(self._buttons_state.get('bindings') or [])

    for bus, address, data in frames:
      self.can_state.apply_frame(mono_s, address, data, bus=bus)
      if self.discovery.active:
        self.discovery.observe_frame(bus, address, data, engine_firing=self.can_state.engine_is_firing())

      if self.learner.state in ('sampling_baseline', 'listening'):
        learned = self.learner.feed_frame(mono_s, bus, address, data)
        if learned is not None:
          bindings.append(learned)
          self._buttons_state['bindings'] = bindings
          learn_just_completed = True
      elif bindings:
        triggered = self.matcher.process_frame(mono_s, bus, address, data, bindings)
        for binding in triggered:
          self._execute_binding(binding, mono_s, now)

    # Even if no frames arrived for the learned ID on a tick, advance timeout/baseline timers
    if self.learner.state in ('sampling_baseline', 'listening') and not frames:
      self.learner.feed_frame(mono_s, 0, 0x7FF, b'')

    if learn_just_completed or self.learner.state != self._last_learn_state or (
      self.learner.state in ('sampling_baseline', 'listening') and (mono_s - self._last_learn_sync_mono) >= 0.25
    ):
      self._last_learn_state = self.learner.state
      self._last_learn_sync_mono = mono_s
      self._persist_learn_status()

    # Passive ECU health & event recorder triggers (with boot blanking & debounce via mono_s)
    if self.cfg.get('passiveMonitorEnabled', True):
      new_faults = self.monitor.update(self.can_state, wall_time=now, mono_s=mono_s)
      if new_faults and self.cfg.get('recorderAutoOnFault', True):
        self.recorder.trigger_event(
          'FAULT',
          new_faults[0]['message'],
          self.can_state,
          mono_s=mono_s,
          wall_time=now,
          details={'faults': new_faults},
        )
    else:
      new_faults = []

    if self.cfg.get('recorderEnabled', True):
      self.recorder.record_sample(self.can_state, mono_s, wall_time=now)

    # Fuel & Trip update
    fuel_snap = self.fuel.update(self.can_state, mono_s, wall_time=now, ignition_hint=ignition_hint)
    if self.discovery.active:
      self.discovery.record_tick(self.can_state, fuel_snap, mono_s, wall_time=now)
    if self._file_mtime(self.root / 'fuel.json') != self._mtimes.get('fuel'):
      self._mtimes['fuel'] = self._file_mtime(self.root / 'fuel.json')

    # Persist passive diagnostic warnings & live snapshot every 2s or on new fault / command
    if new_faults or self._diag_dirty or (mono_s - self._last_diag_sync_mono) >= 2.0:
      self._last_diag_sync_mono = mono_s
      self._diag_dirty = False
      diag = read_diagnostics_state(self.root)
      diag['passive'] = self.monitor.snapshot()
      live_summary = self.can_state.summary()
      live_summary['instantFuelRateLph'] = round(float(fuel_snap.get('sourceDiagnostics', {}).get('instantFuelRateLph', 0.0)), 2)
      diag['liveSummary'] = live_summary
      diag['liveEcus'] = self.can_state.live_ecu_matrix(mono_s)
      diag['discoveryStatus'] = self.discovery.live_status()
      diag['recorderStatus'] = {
        'continuousActive': self.recorder._continuous_session is not None,
        'ringBufferSamples': len(self.recorder._ring),
        'count': len(self.recorder.list_recordings()),
      }
      write_diagnostics_state(diag, self.root)

    # Active ELM327 scan/clear only when Panda is genuinely in ELM327 mode
    if elm327_active and can_send is not None and can_recv is not None:
      diag = read_diagnostics_state(self.root)
      pending = diag.get('pendingAction')
      if isinstance(pending, dict):
        if pending.get('type') == 'clear':
          clear_res = clear_ioniq_dtcs(can_send, can_recv, target_ecu=str(pending.get('target', 'ALL')), bus=1)
          apply_clear_result(clear_res, root=self.root)
        elif pending.get('type') == 'scan':
          scan_res = scan_ioniq_ecus(can_send, can_recv, bus=1)
          obd_live = scan_res.get('obdLive') or {}
          if isinstance(obd_live, dict):
            if obd_live.get('fuelRateLph') is not None:
              self.can_state.obd_fuel_rate_lph = float(obd_live['fuelRateLph'])
            if obd_live.get('mafGps') is not None:
              self.can_state.obd_maf_gps = float(obd_live['mafGps'])
            if obd_live.get('fuelLevelPct') is not None:
              self.can_state.obd_fuel_level_pct = float(obd_live['fuelLevelPct'])
            if obd_live.get('coolantTempC') is not None:
              self.can_state.obd_coolant_c = float(obd_live['coolantTempC'])
            if obd_live.get('controlModuleV') is not None:
              self.can_state.obd_control_module_v = float(obd_live['controlModuleV'])
          apply_scan_result(scan_res, passive_snapshot=self.monitor.snapshot(), root=self.root)

    return {
      'live': self.can_state.summary(),
      'learn': self.learner.status(mono_s),
      'fuel': fuel_snap,
      'passive': self.monitor.snapshot(),
    }

  def shutdown(self) -> None:
    self.discovery.flush()
    self.recorder.flush()
    self.fuel.end_trip()
    self.fuel.flush(0.0)


def _default_runtime_setup() -> tuple[Any, ...]:
  from openpilot.cereal import messaging
  from openpilot.common.params import Params
  from openpilot.common.realtime import Ratekeeper

  params = Params()
  controller = IoniqRuntimeController(params_obj=params)
  can_sock = messaging.sub_sock('can', timeout=20)
  sendcan = messaging.pub_sock('sendcan')
  can_recv, can_send = can_comm_callbacks(can_sock, sendcan)
  sm = messaging.SubMaster(['deviceState', 'carState', 'pandaStates'])
  rk = Ratekeeper(20, print_delay_threshold=None)
  return controller, messaging, can_sock, sm, can_recv, can_send, rk


def _get_cloudlog() -> Any:
  try:
    from openpilot.common.swaglog import cloudlog
    return cloudlog
  except Exception:
    return logging.getLogger('ioniqd')


def main(
  stop_event: threading.Event | None = None,
  *,
  startup_delay_s: float = 2.0,
  setup_fn: Callable[[], tuple[Any, ...]] | None = None,
) -> None:
  cloudlog = _get_cloudlog()

  stop = stop_event or threading.Event()
  prev_handlers: dict[signal.Signals, Any] = {}
  if threading.current_thread() is threading.main_thread():
    def _handle_stop(_signum: int, _frame: Any) -> None:
      stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
      prev_handlers[sig] = signal.getsignal(sig)
      signal.signal(sig, _handle_stop)

  init_runtime = setup_fn or _default_runtime_setup
  controller: Any = None
  backoff_s = 1.0

  try:
    # Let core Openpilot daemons initialize first before bringing up optional Ioniq telemetry.
    if startup_delay_s > 0.0 and stop.wait(startup_delay_s):
      return

    while not stop.is_set():
      try:
        controller, messaging_mod, can_sock, sm, can_recv, can_send, rk = init_runtime()
        backoff_s = 1.0
        break
      except Exception:
        cloudlog.exception(f'ioniqd initialization failed; retrying in {backoff_s:.1f}s')
        if stop.wait(backoff_s):
          return
        backoff_s = min(backoff_s * 2.0, 30.0)

    while not stop.is_set():
      try:
        sm.update(0)
        can_pkts = messaging_mod.drain_sock(can_sock)
        frames: list[tuple[int, int, bytes]] = []
        for pkt in can_pkts:
          for msg in pkt.can:
            frames.append((int(msg.src), int(msg.address), bytes(msg.dat)))

        ign_hint = bool(sm['deviceState'].started) if sm.alive.get('deviceState') else None
        elm_active = False
        if sm.alive.get('pandaStates') and len(sm['pandaStates']) > 0:
          try:
            elm_active = str(sm['pandaStates'][0].safetyModel) == 'elm327'
          except Exception:
            elm_active = False
        car_state_msg = sm['carState'] if sm.alive.get('carState') else None

        controller.step(
          frames,
          mono_s=time.monotonic(),
          wall_time=_utc_timestamp(),
          ignition_hint=ign_hint,
          elm327_active=elm_active,
          can_send=can_send if elm_active else None,
          can_recv=can_recv if elm_active else None,
          car_state_msg=car_state_msg,
        )
        backoff_s = 1.0
        rk.keep_time()
      except KeyboardInterrupt:
        stop.set()
      except Exception:
        cloudlog.exception(f'ioniqd step failed; retrying in {backoff_s:.1f}s')
        if stop.wait(backoff_s):
          break
        backoff_s = min(backoff_s * 2.0, 30.0)
  except KeyboardInterrupt:
    pass
  finally:
    for sig, prev in prev_handlers.items():
      signal.signal(sig, prev)
    if controller is not None:
      try:
        controller.shutdown()
      except Exception:
        cloudlog.exception('ioniqd shutdown failed')


if __name__ == '__main__':
  main()
