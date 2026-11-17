"""Unit tests for 3x button learning, fuel & Volltanken tracking, runtime controller, and Galaxy IoniqTools."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpilot.starpilot.galaxy.ioniq_tools import IoniqTools, IoniqToolsChanged
from openpilot.starpilot.ioniq.button_learner import ButtonLearnEngine, ButtonMatcher, execute_button_action
from openpilot.starpilot.ioniq.can_signals import (
  ADDR_CLU12,
  ADDR_CLU13,
  ADDR_EMS11,
  ADDR_EMS20,
  ADDR_EMS_H12,
  ADDR_WHL_SPD11,
  IoniqCanState,
)
from openpilot.starpilot.ioniq.fuel_tracker import FuelTripTracker
from openpilot.starpilot.ioniq.runtime import IoniqRuntimeController, maybe_run_startup_diagnostic
from openpilot.starpilot.ioniq.tests.test_can_and_dtc import MockIsoTpBus, encode_intel
from openpilot.starpilot.ioniq.uds_dtc import read_diagnostics_state


class MemoryParams:
  def __init__(self) -> None:
    self.data: dict[str, str] = {
      'ExperimentalMode': '0',
      'AlwaysOnLateral': '1',
      'SpeedLimitController': '0',
      'LongitudinalPersonality': '1',
    }

  def get_bool(self, key: str) -> bool:
    return self.data.get(key, '0') == '1'

  def put_bool(self, key: str, val: bool) -> None:
    self.data[key] = '1' if val else '0'

  def get(self, key: str, encoding: str = 'utf-8') -> str | None:
    return self.data.get(key)

  def put(self, key: str, val: str) -> None:
    self.data[key] = str(val)


@dataclass
class _MockCan:
  address: int
  dat: bytes
  src: int


class TestButtonAndFuel(unittest.TestCase):
  def test_3x_button_learn_momentary_filters_alive_counter_noise(self) -> None:
    learner = ButtonLearnEngine(baseline_seconds=1.0, listen_timeout_seconds=20.0)
    learner.start(label='HEV/EV Taste', action_id='toggle_experimental_mode', mono_s=0.0)

    # Baseline 0.0 -> 1.2s with an alive counter/checksum toggling in byte 7 & wheel frame noise
    t = 0.0
    counter = 0
    while t <= 1.3:
      # Frame 0x50C has stable bytes 0..6 and live counter in byte 7
      learner.feed_frame(t, 0, 0x50C, bytes([0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, counter & 0x0F]))
      counter += 1
      t += 0.1

    self.assertEqual(learner.state, 'listening')

    # Perform 3 crisp presses on byte 2 bit 0x04 while byte 7 counter keeps changing constantly
    for _press_i in range(3):
      # Press down
      for _ in range(3):
        learner.feed_frame(t, 0, 0x50C, bytes([0x00, 0x00, 0x14, 0x00, 0x00, 0x00, 0x00, counter & 0x0F]))
        counter += 1
        t += 0.05
      # Release
      for _ in range(4):
        learner.feed_frame(t, 0, 0x50C, bytes([0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, counter & 0x0F]))
        counter += 1
        t += 0.1

    self.assertEqual(learner.state, 'learned')
    binding = learner.learned_binding
    self.assertIsNotNone(binding)
    assert binding is not None
    self.assertEqual(binding['address'], 0x50C)
    self.assertEqual(binding['byteIndex'], 2)
    self.assertEqual(binding['bitMask'], 0x04)
    self.assertEqual(binding['mode'], 'momentary')

    # Match and execute bound action with cooldown
    matcher = ButtonMatcher(cooldown_s=0.5)
    params = MemoryParams()
    hits1 = matcher.process_frame(10.0, 0, 0x50C, bytes([0, 0, 0x14, 0, 0, 0, 0, 9]), [binding])
    self.assertEqual(len(hits1), 1)
    res = execute_button_action(hits1[0]['action'], params)
    self.assertEqual(res['newValue'], True)
    self.assertTrue(params.get_bool('ExperimentalMode'))

    # Release and press again within cooldown -> debounced
    matcher.process_frame(10.1, 0, 0x50C, bytes([0, 0, 0x10, 0, 0, 0, 0, 1]), [binding])
    hits2 = matcher.process_frame(10.2, 0, 0x50C, bytes([0, 0, 0x14, 0, 0, 0, 0, 2]), [binding])
    self.assertEqual(hits2, [])

  def test_3x_button_learn_toggle_switch(self) -> None:
    learner = ButtonLearnEngine(baseline_seconds=0.5, listen_timeout_seconds=20.0)
    learner.start(label='Eco Schalter', action_id='cycle_personality', mono_s=0.0)
    for t in (0.0, 0.2, 0.4, 0.6):
      learner.feed_frame(t, 0, 0x50C, bytes([0] * 8))

    # Latched switch transitions: 0 -> 1, 1 -> 0, 0 -> 1
    learner.feed_frame(1.0, 0, 0x50C, bytes([0, 0, 0, 0, 0x80, 0, 0, 0]))
    learner.feed_frame(2.0, 0, 0x50C, bytes([0, 0, 0, 0, 0x00, 0, 0, 0]))
    learner.feed_frame(3.0, 0, 0x50C, bytes([0, 0, 0, 0, 0x80, 0, 0, 0]))
    # Wait settle period >= 0.85s
    learner.feed_frame(4.0, 0, 0x50C, bytes([0, 0, 0, 0, 0x80, 0, 0, 0]))
    self.assertEqual(learner.state, 'learned')
    assert learner.learned_binding is not None
    self.assertEqual(learner.learned_binding['mode'], 'toggle')
    self.assertEqual(learner.learned_binding['byteIndex'], 4)
    self.assertEqual(learner.learned_binding['bitMask'], 0x80)

  def test_fuel_fco_wraparound_trips_and_volltanken_calibration(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      tracker = FuelTripTracker(tmp, price_per_liter=1.80)
      st = IoniqCanState()

      # Start trip at odo 1000.0 km, FCO near wraparound 65000
      fco = 65000
      odo_tenths = 10000
      for i in range(120):
        mono = float(i)  # 1s steps -> 2 minutes total at 72 km/h = 2.4 km
        st.apply_frame(mono, ADDR_EMS11, encode_intel([(0, 1, 1), (1, 1, 1), (16, 16, 8000), (48, 8, 72)]))
        st.apply_frame(mono, ADDR_EMS_H12, encode_intel([(16, 8, 90), (61, 1, 1)]))
        st.apply_frame(mono, ADDR_WHL_SPD11, encode_intel([(0, 14, 2304), (16, 14, 2304), (32, 14, 2304), (48, 14, 2304)]))
        st.apply_frame(mono, ADDR_CLU12, encode_intel([(0, 24, odo_tenths + int(i * 0.2))]))
        st.apply_frame(mono, ADDR_CLU13, encode_intel([(6, 10, 46), (18, 5, 20), (24, 10, 480)]))
        st.apply_frame(mono, ADDR_EMS20, encode_intel([(0, 16, fco & 0xFFFF)]))
        fco += 4000  # ~0.51 mL/s -> crosses 65535 multiple times cleanly!
        tracker.update(st, mono, wall_time=10000.0 + mono)

      # End trip cleanly on ignition off
      archived = tracker.end_trip(wall_time=10120.0)
      self.assertIsNotNone(archived)
      assert archived is not None
      self.assertEqual(archived['sourceMode'], 'fco_can')
      self.assertGreater(archived['distanceKm'], 2.0)
      self.assertGreater(archived['fuelLiters'], 0.05)

      # Force large tank distance & refuel jump to test Volltanken calibration
      tracker.state['currentTank']['distanceKm'] = 720.0
      tracker.state['currentTank']['estimatedFuelLiters'] = 32.0
      tracker.state['currentTank']['startOdometerKm'] = 1000.0
      tracker.state['currentTank']['lastOdometerKm'] = 1720.0

      # Partial top-up 5L first
      tracker.log_refuel(5.0, full_tank=False, odometer_km=1400.0, wall_time=15000.0)
      # Full tank fill 29.0L at 1720.0 km with total costEur=61.20 -> total 34.0L over 720km = 4.72 L/100km
      res = tracker.log_refuel(29.0, full_tank=True, cost_eur=52.20, odometer_km=1720.0, notes='Shell', wall_time=20000.0)
      closed = res['closedTank']
      self.assertIsNotNone(closed)
      self.assertAlmostEqual(closed['actualLiters'], 34.0)
      self.assertAlmostEqual(closed['distanceKm'], 720.0)
      self.assertAlmostEqual(closed['actualLPer100Km'], 4.72)
      self.assertIsNotNone(closed['costPer100Km'])
      self.assertGreater(res['calibrationFactor'], 1.0)
      self.assertIn('fuelLiters,avgLPer100Km', tracker.export_csv())

      # Test update_refuel, delete_refuel, and reset_calibration
      refuel_id = res['refuel']['id']
      updated_rf = tracker.update_refuel(refuel_id, cost_eur=55.10, notes='Shell V-Power')
      self.assertAlmostEqual(updated_rf['costEur'], 55.10)
      self.assertEqual(updated_rf['notes'], 'Shell V-Power')
      self.assertTrue(tracker.delete_refuel(refuel_id))
      self.assertEqual(tracker.reset_calibration(), 1.0)

  def test_fuel_nonzero_fallback_when_fco_frozen_and_volltanken_stationary_guard(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      tracker = FuelTripTracker(tmp, price_per_liter=1.80)
      st = IoniqCanState()

      # Startup with fuel_disp_level jumping 0 -> 14 on first frames while moving must NOT trigger false refuelAlert
      for i in range(60):
        mono = float(i)
        # Engine RPM=1800, Torque=45%, Speed=60 km/h, but FCO frozen at 0 (simulating EMS20 FCO static)
        # Also F_N_ENG=0 and CF_Ems_EngRunNorm=0 to verify engine_is_firing() still detects RPM=1800!
        st.apply_frame(mono, ADDR_EMS11, encode_intel([(0, 1, 1), (1, 1, 0), (16, 16, 7200), (48, 8, 60)]))
        st.apply_frame(mono, ADDR_EMS_H12, encode_intel([(16, 8, 115), (61, 1, 0)]))
        st.apply_frame(mono, ADDR_WHL_SPD11, encode_intel([(0, 14, 1920), (16, 14, 1920), (32, 14, 1920), (48, 14, 1920)]))
        st.apply_frame(mono, ADDR_CLU12, encode_intel([(0, 24, 50000 + int(i * 0.16))]))
        st.apply_frame(mono, ADDR_CLU13, encode_intel([(3, 2, 0), (6, 10, 44), (18, 5, 8), (24, 10, 210)]))
        st.apply_frame(mono, ADDR_EMS20, encode_intel([(0, 16, 0)]))
        tracker.update(st, mono, wall_time=20000.0 + mono)

      snap = tracker.snapshot()
      self.assertIsNone(snap['refuelAlert'])
      self.assertIsNotNone(snap['activeTrip'])
      self.assertGreater(snap['activeTrip']['fuelLiters'], 0.02)
      self.assertIn(snap['activeTrip']['sourceMode'], ('torque_model', 'cluster_avg_fci'))

      # Now stop the car (speed=0) for 10 seconds, then increase fuel bars from 8 -> 29 (full tank)
      for s in range(12):
        mono = 60.0 + float(s)
        st.apply_frame(mono, ADDR_EMS11, encode_intel([(0, 1, 1), (16, 16, 0), (48, 8, 0)]))
        st.apply_frame(mono, ADDR_WHL_SPD11, encode_intel([(0, 14, 0), (16, 14, 0), (32, 14, 0), (48, 14, 0)]))
        bars = 29 if s >= 10 else 8
        st.apply_frame(mono, ADDR_CLU13, encode_intel([(3, 2, 0), (6, 10, 44), (18, 5, bars), (24, 10, 650)]))
        tracker.update(st, mono, wall_time=20000.0 + mono)

      alert = tracker.snapshot()['refuelAlert']
      self.assertIsNotNone(alert)
      assert alert is not None
      self.assertEqual(alert['deltaBars'], 21)
      self.assertGreater(alert['estimatedAddedLiters'], 25.0)
      self.assertTrue(alert['isLikelyFullTank'])

  def test_runtime_controller_and_galaxy_tools_integration(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      params = MemoryParams()
      ctrl = IoniqRuntimeController(root=tmp, params_obj=params)
      cp = SimpleNamespace(brand='hyundai', carFingerprint='HYUNDAI_IONIQ', notCar=False)
      current = SimpleNamespace(cp=cp, parked=True)
      ctx = SimpleNamespace(sample=lambda: current, close=lambda: None)
      bus = MockIsoTpBus()
      tools = IoniqTools(params, ctx, root=tmp, direct_transport=lambda: (bus.can_send, bus.can_recv))

      page = tools.page('tok1', b'gen1')
      self.assertTrue(page['visible'])
      self.assertTrue(page['vehicle']['isIoniqHev'])

      # Start 3x button learn through Galaxy API, feed runtime controller frames until learned
      tools.action({'view': page['view'], 'action': 'start_learn', 'label': 'Stern', 'buttonAction': 'toggle_experimental_mode'},
                   'tok1', b'gen1')
      # Trigger controller external reload at t=0.5, feed baseline then 3 presses
      t = 0.5
      ctrl.step([(0, 0x50C, bytes([0] * 8))], mono_s=t, wall_time=100.0)
      t = 2.0
      ctrl.step([(0, 0x50C, bytes([0] * 8))], mono_s=t, wall_time=101.5)
      for _ in range(3):
        t += 0.2
        ctrl.step([(0, 0x50C, bytes([0, 0x08, 0, 0, 0, 0, 0, 0]))], mono_s=t, wall_time=100.0 + t)
        t += 0.2
        ctrl.step([(0, 0x50C, bytes([0, 0x00, 0, 0, 0, 0, 0, 0]))], mono_s=t, wall_time=100.0 + t)

      # Next press executes toggle_experimental_mode!
      t += 0.8
      ctrl.step([(0, 0x50C, bytes([0, 0x08, 0, 0, 0, 0, 0, 0]))], mono_s=t, wall_time=100.0 + t)
      self.assertTrue(params.get_bool('ExperimentalMode'))

      # Active ECU scan via Galaxy direct transport
      page2 = tools.page('tok1', b'gen1')
      scan_res = tools.action({'view': page2['view'], 'action': 'scan_dtcs'}, 'tok1', b'gen1')
      self.assertTrue(scan_res['executedNow'])
      self.assertGreaterEqual(len(scan_res['diagnostics']['lastScan']['dtcs']), 3)

      # Parked check rejects destructive clear when driving
      current.parked = False
      with self.assertRaises(IoniqToolsChanged):
        tools.action({'view': page2['view'], 'action': 'clear_dtcs', 'confirmed': True, 'target': 'ALL'}, 'tok1', b'gen1')

      # Default scanOnStart is False so cold-boot does not delay Panda safety transition
      obd_states: list[bool] = []
      self.assertIsNone(maybe_run_startup_diagnostic(
        bus.can_recv, bus.can_send, lambda enabled: obd_states.append(enabled), brand='hyundai', root=tmp
      ))
      # When a scan is queued (e.g. while driving), maybe_run_startup_diagnostic executes it cleanly
      queue_res = tools.action({'view': page2['view'], 'action': 'scan_dtcs'}, 'tok1', b'gen1')
      self.assertTrue(queue_res['queued'])
      startup_out = maybe_run_startup_diagnostic(
        bus.can_recv, bus.can_send, lambda enabled: obd_states.append(enabled), brand='hyundai', root=tmp
      )
      self.assertIsNotNone(startup_out)
      assert startup_out is not None
      self.assertTrue(startup_out['ranScan'])
      self.assertEqual(obd_states, [True, False])
      self.assertIsNotNone(read_diagnostics_state(tmp)['lastScan'])

      # Test clear_passive_warnings action
      current.parked = True
      clr_warn = tools.action({'view': page2['view'], 'action': 'clear_passive_warnings'}, 'tok1', b'gen1')
      self.assertTrue(clr_warn['saved'])
      self.assertEqual(clr_warn['diagnostics']['passive']['activeWarnings'], [])

  def test_galaxy_static_fetch_contract(self) -> None:
    from pathlib import Path
    import re
    web_js = Path(__file__).resolve().parents[2] / 'galaxy/web/js'
    scripts = '\n'.join(p.read_text(encoding='utf-8') for p in web_js.glob('*.js'))
    found = sorted(re.findall(r"\bfetch\s*\(\s*[\"']([^\"']+)", scripts))
    self.assertEqual(found, ['./api/auth/session', './api/drive-state/action', './api/drive-state/status', '/_gateway/devices'])

  def test_ioniqd_health_isolation_and_process_config_ordering(self) -> None:
    import ast
    from pathlib import Path
    from openpilot.system.manager.process_health import ANCILLARY_PROCESSES, driving_process_failures

    self.assertIn('ioniqd', ANCILLARY_PROCESSES)
    self.assertEqual(
      driving_process_failures([
        SimpleNamespace(name='ioniqd', running=False, shouldBeRunning=True),
        SimpleNamespace(name='controlsd', running=True, shouldBeRunning=True),
      ]),
      set(),
    )
    self.assertEqual(
      driving_process_failures([
        SimpleNamespace(name='ioniqd', running=False, shouldBeRunning=True),
        SimpleNamespace(name='controlsd', running=False, shouldBeRunning=True),
      ]),
      {'controlsd'},
    )

    proc_cfg_path = Path(__file__).resolve().parents[3] / 'system/manager/process_config.py'
    tree = ast.parse(proc_cfg_path.read_text(encoding='utf-8'))
    procs_node = next(
      node.value for node in tree.body
      if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'procs' for t in node.targets)
    )
    assert isinstance(procs_node, ast.List)
    proc_names: list[str] = []
    ioniqd_call: ast.Call | None = None
    for elt in procs_node.elts:
      if isinstance(elt, ast.Call) and elt.args and isinstance(elt.args[0], ast.Constant) and isinstance(elt.args[0].value, str):
        proc_names.append(elt.args[0].value)
        if elt.args[0].value == 'ioniqd':
          ioniqd_call = elt

    self.assertIsNotNone(ioniqd_call)
    assert ioniqd_call is not None
    self.assertEqual(ast.literal_eval(ioniqd_call.args[1]), 'openpilot.starpilot.ioniq.runtime')
    restart_kw = next((kw for kw in ioniqd_call.keywords if kw.arg == 'restart_on_exit'), None)
    self.assertIsNotNone(restart_kw)
    assert restart_kw is not None
    self.assertIs(ast.literal_eval(restart_kw.value), True)
    self.assertGreater(proc_names.index('ioniqd'), proc_names.index('uploader'))
    self.assertLess(proc_names.index('ioniqd'), proc_names.index('webrtcd'))

  def test_runtime_imports_and_can_frame_capnp_compatibility(self) -> None:
    import ast
    from pathlib import Path
    from openpilot.starpilot.ioniq import runtime, uds_dtc

    src = Path(runtime.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    for node in ast.walk(tree):
      if isinstance(node, ast.ImportFrom):
        self.assertNotEqual(node.module, 'cereal')
        self.assertNotEqual(node.module, 'openpilot.selfdrive.car.card')
      elif isinstance(node, ast.Import):
        for alias in node.names:
          self.assertNotEqual(alias.name, 'cereal')
          self.assertNotEqual(alias.name, 'openpilot.selfdrive.car.card')

    msg1 = uds_dtc._make_can_msg(0x7E0, b'\x02\x19\x02\x00\x00\x00\x00\x00', 1)
    msg2 = runtime._CanFrame(0x7E0, b'\x11\x22', 1)
    for msg in (msg1, msg2):
      self.assertEqual(msg[0], msg.address)
      self.assertEqual(msg[1], msg.dat)
      self.assertEqual(msg[2], msg.src)

  def test_runtime_main_startup_delay_and_backoff_recovery(self) -> None:
    import threading
    from openpilot.starpilot.ioniq import runtime

    class _RecordingStop:
      def __init__(self, *, cancel_on_delay: bool = False) -> None:
        self._set = False
        self.cancel_on_delay = cancel_on_delay
        self.waits: list[float] = []

      def is_set(self) -> bool:
        return self._set

      def set(self) -> None:
        self._set = True

      def wait(self, timeout: float = 0.0) -> bool:
        self.waits.append(timeout)
        if self.cancel_on_delay:
          self._set = True
        return self._set

    setup_calls = 0

    def _never_called():
      nonlocal setup_calls
      setup_calls += 1
      raise AssertionError('setup_fn must not run when stopped during startup delay')

    canceled_stop = _RecordingStop(cancel_on_delay=True)
    runtime.main(stop_event=canceled_stop, startup_delay_s=2.0, setup_fn=_never_called)  # type: ignore[arg-type]
    self.assertEqual(setup_calls, 0)
    self.assertEqual(canceled_stop.waits, [2.0])

    # Now verify recovery from setup failures (1s -> 2s backoff) and loop step error (1s backoff -> clean shutdown).
    recovery_stop = _RecordingStop()
    steps = {'init': 0, 'tick': 0, 'shutdown': 0}

    class _FakeController:
      def step(self, *args, **kwargs):
        steps['tick'] += 1
        if steps['tick'] == 1:
          raise RuntimeError('transient tick failure')
        recovery_stop.set()

      def shutdown(self):
        steps['shutdown'] += 1

    class _FakeSm:
      alive = {'deviceState': False, 'pandaStates': False}

      def update(self, _timeout: int = 0) -> None:
        pass

      def __getitem__(self, _key: str):
        return None

    def _flaky_setup():
      steps['init'] += 1
      if steps['init'] <= 2:
        raise RuntimeError(f'init failure {steps["init"]}')
      return (
        _FakeController(),
        SimpleNamespace(drain_sock=lambda _sock: []),
        object(),
        _FakeSm(),
        lambda _wait=False: [],
        lambda _msgs: None,
        SimpleNamespace(keep_time=lambda: None),
      )

    logged_errors: list[str] = []
    with patch.object(runtime, '_get_cloudlog', return_value=SimpleNamespace(exception=lambda msg: logged_errors.append(str(msg)))):
      runtime.main(stop_event=recovery_stop, startup_delay_s=2.0, setup_fn=_flaky_setup)  # type: ignore[arg-type]
    self.assertEqual(steps['init'], 3)
    self.assertEqual(steps['tick'], 2)
    self.assertEqual(steps['shutdown'], 1)
    self.assertEqual(len(logged_errors), 3)
    # 2.0 startup wait + 1.0, 2.0 init failure waits + 1.0 step failure wait
    self.assertEqual(recovery_stop.waits, [2.0, 1.0, 2.0, 1.0])

    # Real threading.Event exit path also returns promptly
    real_stop = threading.Event()
    real_stop.set()
    runtime.main(stop_event=real_stop, startup_delay_s=5.0, setup_fn=_never_called)

  def test_runtime_controller_reload_updates_fuel_calibration(self) -> None:
    from openpilot.starpilot.ioniq.storage import read_config, write_config

    with tempfile.TemporaryDirectory() as tmp:
      params = MemoryParams()
      ctrl = IoniqRuntimeController(root=tmp, params_obj=params)
      self.assertAlmostEqual(ctrl.fuel.calibration_factor, 1.0)

      cfg = read_config(tmp)
      cfg['fuelCalibrationFactor'] = 1.08
      write_config(cfg, tmp)
      ctrl.step([], mono_s=1.0, wall_time=200.0)
      self.assertAlmostEqual(ctrl.fuel.calibration_factor, 1.08)

      # Updating fuel.json without changing its calibration keeps config.json calibration intact
      ctrl.fuel.flush(0.0)
      ctrl.step([], mono_s=2.0, wall_time=201.0)
      self.assertAlmostEqual(ctrl.fuel.calibration_factor, 1.08)

  def test_hev_c_can_mixed_drive_without_ems11_or_clu13(self) -> None:
    from openpilot.starpilot.ioniq.can_signals import (
      ADDR_ELECT_GEAR,
      ADDR_ESP12,
      ADDR_E_EMS11,
      ADDR_SCC12,
      ADDR_TCS13,
    )
    with tempfile.TemporaryDirectory() as tmp:
      tracker = FuelTripTracker(tmp, price_per_liter=1.80)
      st = IoniqCanState()
      t = 0.0

      def step_drive(speed_kph: float, gas_raw: int, brake: bool = False, gear: int = 5, acc_mode: int = 0) -> None:
        raw_spd = int(round(speed_kph / 0.03125))
        st.apply_frame(t, ADDR_WHL_SPD11, encode_intel([(0, 14, raw_spd), (16, 14, raw_spd), (32, 14, raw_spd), (48, 14, raw_spd)]), bus=0)
        st.apply_frame(t, ADDR_E_EMS11, encode_intel([(0, 8, 20 if brake else 0), (8, 3, 2), (31, 8, gas_raw), (56, 8, gas_raw)]), bus=0)
        st.apply_frame(t, ADDR_ELECT_GEAR, encode_intel([(16, 4, gear)]), bus=0)
        st.apply_frame(t, ADDR_TCS13, encode_intel([(11, 1, 1 if brake else 0), (55, 1, 1 if brake else 0)]), bus=0)
        st.apply_frame(t, ADDR_ESP12, encode_intel([(13, 11, 1023), (26, 12, 250 if brake else 0)]), bus=0)
        st.apply_frame(t, ADDR_SCC12, encode_intel([(13, 2, acc_mode), (37, 11, 1023)]), bus=0)
        st.apply_frame(t, ADDR_CLU13, encode_intel([(3, 2, 1), (6, 10, 0), (18, 5, 22), (24, 10, 520)]), bus=0)
        tracker.update(st, t, wall_time=50000.0 + t, ignition_hint=True)

      # Phase 1: Accelerate 0 -> 55 km/h over 12s (gas ~35%)
      for i in range(1, 25):
        t = i * 0.5
        step_drive(min(55.0, i * 2.3), gas_raw=90)
      # Phase 2: Steady light city cruise @ 45 km/h for 45s (transitions into EV mode)
      for _ in range(90):
        t += 0.5
        step_drive(45.0, gas_raw=20)
      # Phase 3: Regen braking 45 -> 0 km/h over 8s
      for i in range(16):
        t += 0.5
        step_drive(max(0.0, 45.0 - (i + 1) * 2.8), gas_raw=0, brake=True)
      # Phase 4: Highway ramp & 115 km/h cruise over 60s
      for i in range(20):
        t += 0.5
        step_drive(min(115.0, i * 5.8), gas_raw=120)
      for _ in range(100):
        t += 0.5
        step_drive(115.0, gas_raw=52, acc_mode=1)

      # Ignition off ends the trip cleanly
      for i in range(6):
        t += 0.5
        st.apply_frame(t, ADDR_WHL_SPD11, encode_intel([(0, 14, 0), (16, 14, 0), (32, 14, 0), (48, 14, 0)]), bus=0)
        st.apply_frame(t, ADDR_ELECT_GEAR, encode_intel([(16, 4, 0)]), bus=0)
        tracker.update(st, t, wall_time=50000.0 + t, ignition_hint=False)

      snap = tracker.snapshot(st)
      self.assertIsNone(snap['activeTrip'])
      self.assertEqual(len(snap['trips']), 1)
      archived = snap['trips'][0]
      self.assertEqual(archived['sourceMode'], 'hev_model')
      self.assertGreater(archived['distanceKm'], 2.0)
      self.assertGreater(archived['fuelLiters'], 0.08)
      self.assertGreaterEqual(archived['avgLPer100Km'], 3.0)
      self.assertLessEqual(archived['avgLPer100Km'], 9.0)
      self.assertGreaterEqual(archived['evTimePct'], 10.0)
      self.assertLessEqual(archived['evTimePct'], 85.0)
      self.assertGreater(archived['evDistanceKm'], 0.05)
      self.assertGreater(archived['iceDistanceKm'], 0.5)
      self.assertGreater(snap['currentTank']['estimatedFuelLiters'], 0.08)
      self.assertGreater(snap['currentTank']['iceDistanceKm'], 0.5)

  def test_read_fuel_state_repairs_broken_zero_fuel_records(self) -> None:
    from openpilot.starpilot.ioniq.fuel_tracker import write_fuel_state
    with tempfile.TemporaryDirectory() as tmp:
      broken_state = {
        'activeTrip': None,
        'trips': [
          {
            'id': 'trip_broken_1',
            'startedAt': 2000.0,
            'endedAt': 2900.0,
            'durationS': 900.0,
            'distanceKm': 12.4,
            'evDistanceKm': 12.4,
            'iceDistanceKm': 0.0,
            'evDurationS': 900.0,
            'iceDurationS': 0.0,
            'evTimePct': 100.0,
            'fuelLiters': 0.0,
            'avgLPer100Km': 0.0,
            'avgKmPerL': None,
            'estimatedCostEur': 0.0,
            'sourceMode': 'torque_model',
          }
        ],
        'currentTank': {
          'id': 'tank_1',
          'startedAt': 1000.0,
          'distanceKm': 12.4,
          'estimatedFuelLiters': 0.0,
          'partialRefuelsLiters': 0.0,
          'evDistanceKm': 12.4,
          'iceDistanceKm': 0.0,
          'tripCount': 1,
        },
        'tankHistory': [],
        'refuelLog': [],
        'refuelAlert': None,
        'calibrationFactor': 1.0,
        'pendingFuelJump': None,
      }
      write_fuel_state(broken_state, tmp)

      repaired = FuelTripTracker(tmp)
      trip = repaired.state['trips'][0]
      self.assertEqual(trip['sourceMode'], 'hev_model')
      self.assertGreater(trip['fuelLiters'], 0.4)
      self.assertGreaterEqual(trip['avgLPer100Km'], 4.0)
      self.assertLessEqual(trip['avgLPer100Km'], 6.5)
      self.assertLess(trip['evTimePct'], 90.0)
      self.assertGreater(trip['iceDistanceKm'], 2.0)
      self.assertGreater(repaired.state['currentTank']['estimatedFuelLiters'], 0.4)
      self.assertGreater(repaired.state['currentTank']['iceDistanceKm'], 2.0)

      # Reload verifies auto-repaired data was persisted cleanly to fuel.json
      reloaded = FuelTripTracker(tmp)
      self.assertAlmostEqual(reloaded.state['trips'][0]['fuelLiters'], trip['fuelLiters'])

  def test_runtime_controller_feeds_car_state_and_live_summary_flow(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      params = MemoryParams()
      ctrl = IoniqRuntimeController(root=tmp, params_obj=params)
      cs = SimpleNamespace(
        vEgo=20.0,
        aEgo=0.8,
        gasPressed=True,
        brakePressed=False,
        standstill=False,
        gearShifter='drive',
        cruiseState=SimpleNamespace(enabled=False),
      )
      res = ctrl.step([], mono_s=1.0, wall_time=100.0, ignition_hint=True, car_state_msg=cs)
      res2 = ctrl.step([], mono_s=3.5, wall_time=102.5, ignition_hint=True, car_state_msg=cs)
      self.assertAlmostEqual(res2['live']['speedKph'], 72.0)
      self.assertEqual(res2['live']['gearShifter'], 'D')
      self.assertGreater(res2['fuel']['activeTrip']['fuelLiters'], 0.0)
      self.assertEqual(res2['fuel']['activeTrip']['sourceMode'], 'hev_model')
      diag = read_diagnostics_state(tmp)
      self.assertIn('clusterAvgFc', diag['liveSummary'])
      self.assertIn('clusterAvgFcUnitLabel', diag['liveSummary'])
      self.assertGreater(diag['liveSummary']['instantFuelRateLph'], 0.0)
      self.assertTrue(res is not None)


if __name__ == '__main__':
  unittest.main()
