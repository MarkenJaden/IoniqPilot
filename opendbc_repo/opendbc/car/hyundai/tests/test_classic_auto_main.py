"""Tests for automatic Cruise Main standby activation on classic SCC Hyundai/Kia models."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from opendbc.can import CANParser
from opendbc.car import Bus, gen_empty_fingerprint, structs
from opendbc.car.hyundai.carcontroller import (
  AUTO_MAIN_COOLDOWN_FRAMES,
  AUTO_MAIN_PULSE_FRAMES,
  AUTO_MAIN_STARTUP_FRAMES,
  eligible_auto_cruise_main,
)
from opendbc.car.hyundai.classic_scc_aol import aol_word
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.values import CAR, DBC
from opendbc.safety.tests.common import CANPackerSafety
from opendbc.safety.tests.libsafety import libsafety_py
from openpilot.starpilot.controller_extensions import configure_controller
from openpilot.starpilot.ioniq.storage import write_config


def _ioniq_cp(*, aol: bool = True, longitudinal: bool = False):
  fp = gen_empty_fingerprint()
  fp[0][0x391] = 8
  cp = CarInterface.get_params(CAR.HYUNDAI_IONIQ, fp, [], longitudinal, False, False)
  if aol and not longitudinal:
    cp.safetyConfigs[0].safetyParam = aol_word(cp)
    cp.alternativeExperience = 32
  return cp


def _prepare_cs(ci, *, can_valid=True, available=False, enabled=False, brake=False, gas=False,
                acc_faulted=False, button_events=(), clu11=None):
  ci.update([])
  out = ci.CS.out
  out.canValid = can_valid
  out.canTimeout = False
  out.cruiseState.available = available
  out.cruiseState.enabled = enabled
  out.brakePressed = brake
  out.gasPressed = gas
  out.accFaulted = acc_faulted
  out.buttonEvents = list(button_events)
  ci.CS.out = out.as_reader()
  if clu11 is not None:
    ci.CS.clu11 = dict(clu11)
  return ci.CS


class TestHyundaiClassicAutoMain(unittest.TestCase):
  def test_eligibility_and_config_wiring(self):
    self.assertTrue(eligible_auto_cruise_main(_ioniq_cp()))
    self.assertFalse(eligible_auto_cruise_main(_ioniq_cp(longitudinal=True)))

    fp = gen_empty_fingerprint()
    non_scc_cp = CarInterface.get_params(CAR.KIA_FORTE_2019_NON_SCC, fp, [], False, False, False)
    canfd_cp = CarInterface.get_params(CAR.HYUNDAI_IONIQ_5, fp, [], False, False, False)
    self.assertFalse(eligible_auto_cruise_main(non_scc_cp))
    self.assertFalse(eligible_auto_cruise_main(canfd_cp))

    ci = CarInterface(_ioniq_cp())
    self.assertFalse(ci.CC.auto_cruise_main)  # Default off until controller_extensions enables it.
    with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'IONIQ_STORAGE_ROOT': tmp}):
      write_config({'autoCruiseMain': True}, root=tmp)
      configure_controller(ci, SimpleNamespace())
      self.assertTrue(ci.CC.auto_cruise_main)
      write_config({'autoCruiseMain': False}, root=tmp)
      configure_controller(ci, SimpleNamespace())
      self.assertFalse(ci.CC.auto_cruise_main)

  def test_auto_main_startup_delay_pulse_and_safety_tx_acceptance(self):
    cp = _ioniq_cp()
    ci = CarInterface(cp)
    ci.CC.auto_cruise_main = True
    parser = CANParser(DBC[CAR.HYUNDAI_IONIQ][Bus.pt], [('CLU11', 0)], 0)

    safety = libsafety_py.libsafety
    safety.set_safety_hooks(cp.safetyConfigs[0].safetyModel.raw, cp.safetyConfigs[0].safetyParam)
    safety.set_alternative_experience(cp.alternativeExperience)
    safety.init_tests()
    safety_packer = CANPackerSafety('hyundai_can_generated')
    safety.safety_rx_hook(safety_packer.make_can_msg_safety('SCC11', 0, {'MainMode_ACC': 0}))

    cc = structs.CarControl().as_reader()
    main_frames = []
    for frame in range(AUTO_MAIN_STARTUP_FRAMES + AUTO_MAIN_PULSE_FRAMES + 10):
      _prepare_cs(ci, can_valid=True, available=False)
      _, packets = ci.CC.update(cc, ci.CS, 10_000_000_000 + frame * 10_000_000)
      parser.update([[10_000_000_000 + frame * 10_000_000, packets]])
      for addr, dat, bus in packets:
        self.assertTrue(safety.safety_tx_hook(libsafety_py.make_CANPacket(addr, bus, dat)))
        if addr == 0x4F1:
          main_frames.append((frame, int(parser.vl['CLU11']['CF_Clu_CruiseSwMain']),
                              int(parser.vl['CLU11']['CF_Clu_CruiseSwState'])))

    self.assertEqual([f for f, main, btn in main_frames if main == 1 and btn == 0],
                     list(range(AUTO_MAIN_STARTUP_FRAMES, AUTO_MAIN_STARTUP_FRAMES + AUTO_MAIN_PULSE_FRAMES)))

    # If CS.clu11 is empty, no 0x4F1 frame is emitted even when auto_main is active
    ci_empty = CarInterface(cp)
    ci_empty.CC.auto_cruise_main = True
    for frame in range(AUTO_MAIN_STARTUP_FRAMES + 5):
      _prepare_cs(ci_empty, can_valid=True, available=False, clu11={})
      _, packets = ci_empty.CC.update(cc, ci_empty.CS, 10_000_000_000 + frame * 10_000_000)
      self.assertFalse(any(addr == 0x4F1 for addr, _, _ in packets))

  def test_manual_main_off_and_cancel_inhibit_for_remainder_of_drive(self):
    cp = _ioniq_cp()
    ci = CarInterface(cp)
    ci.CC.auto_cruise_main = True
    parser = CANParser(DBC[CAR.HYUNDAI_IONIQ][Bus.pt], [('CLU11', 0)], 0)
    cc = structs.CarControl().as_reader()

    # Main came on after first pulse frames 150..161
    for frame in range(AUTO_MAIN_STARTUP_FRAMES + AUTO_MAIN_PULSE_FRAMES + 5):
      available = frame >= AUTO_MAIN_STARTUP_FRAMES + 2
      _prepare_cs(ci, can_valid=True, available=available)
      _, packets = ci.CC.update(cc, ci.CS, 10_000_000_000 + frame * 10_000_000)
      parser.update([[10_000_000_000 + frame * 10_000_000, packets]])

    self.assertFalse(ci.CC._auto_main_inhibited)

    # Driver toggles Cruise Main OFF -> must stay inhibited for rest of drive
    sent_after_off = []
    for offset in range(AUTO_MAIN_COOLDOWN_FRAMES + 50):
      frame = AUTO_MAIN_STARTUP_FRAMES + AUTO_MAIN_PULSE_FRAMES + 5 + offset
      _prepare_cs(ci, can_valid=True, available=False)
      _, packets = ci.CC.update(cc, ci.CS, 10_000_000_000 + frame * 10_000_000)
      sent_after_off.extend(addr for addr, _, _ in packets if addr == 0x4F1)

    self.assertTrue(ci.CC._auto_main_inhibited)
    self.assertEqual(sent_after_off, [])

  def test_brake_defers_without_tx_then_fires_when_released_and_caps_retries(self):
    cp = _ioniq_cp()
    ci = CarInterface(cp)
    ci.CC.auto_cruise_main = True
    cc = structs.CarControl().as_reader()

    # Hold brake past startup threshold -> no TX while braked
    for frame in range(AUTO_MAIN_STARTUP_FRAMES + 20):
      _prepare_cs(ci, can_valid=True, available=False, brake=True)
      _, packets = ci.CC.update(cc, ci.CS, 10_000_000_000 + frame * 10_000_000)
      self.assertFalse(any(addr == 0x4F1 for addr, _, _ in packets))

    # Release brake -> attempt 1 fires immediately, then after cooldown attempt 2, never attempt 3
    attempt_starts = []
    was_sending = False
    for offset in range(1000):
      frame = AUTO_MAIN_STARTUP_FRAMES + 20 + offset
      _prepare_cs(ci, can_valid=True, available=False, brake=False)
      _, packets = ci.CC.update(cc, ci.CS, 10_000_000_000 + frame * 10_000_000)
      sending = any(addr == 0x4F1 for addr, _, _ in packets)
      if sending and not was_sending:
        attempt_starts.append(frame)
      was_sending = sending

    self.assertEqual(len(attempt_starts), 2)
    self.assertGreaterEqual(attempt_starts[1] - attempt_starts[0],
                            AUTO_MAIN_PULSE_FRAMES + AUTO_MAIN_COOLDOWN_FRAMES)


if __name__ == '__main__':
  unittest.main()
