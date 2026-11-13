"""Unit tests for 3x button learning, fuel & Volltanken tracking, runtime controller, and Galaxy IoniqTools."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
from types import SimpleNamespace
import unittest

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
    for press_i in range(3):
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
      # Full tank fill 29.0L at 1720.0 km -> total 34.0L over 720km = 4.72 L/100km
      res = tracker.log_refuel(29.0, full_tank=True, price_per_liter=1.80, odometer_km=1720.0, notes='Shell', wall_time=20000.0)
      closed = res['closedTank']
      self.assertIsNotNone(closed)
      self.assertAlmostEqual(closed['actualLiters'], 34.0)
      self.assertAlmostEqual(closed['distanceKm'], 720.0)
      self.assertAlmostEqual(closed['actualLPer100Km'], 4.72)
      self.assertGreater(res['calibrationFactor'], 1.0)
      self.assertIn('fuelLiters,avgLPer100Km', tracker.export_csv())

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

      # Startup hook executes queued OBD window scan on Hyundai
      obd_states: list[bool] = []
      startup_out = maybe_run_startup_diagnostic(
        bus.can_recv, bus.can_send, lambda enabled: obd_states.append(enabled), brand='hyundai', root=tmp
      )
      self.assertIsNotNone(startup_out)
      assert startup_out is not None
      self.assertTrue(startup_out['ranScan'])
      self.assertEqual(obd_states, [True, False])
      self.assertIsNotNone(read_diagnostics_state(tmp)['lastScan'])

  def test_galaxy_static_fetch_contract(self) -> None:
    from pathlib import Path
    import re
    web_js = Path(__file__).resolve().parents[2] / 'galaxy/web/js'
    scripts = '\n'.join(p.read_text(encoding='utf-8') for p in web_js.glob('*.js'))
    found = sorted(re.findall(r"\bfetch\s*\(\s*[\"']([^\"']+)", scripts))
    self.assertEqual(found, ['./api/auth/session', './api/drive-state/action', './api/drive-state/status', '/_gateway/devices'])


if __name__ == '__main__':
  unittest.main()
