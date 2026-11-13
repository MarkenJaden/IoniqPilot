"""Unit tests for Ioniq CAN decoding, ISO-TP UDS/OBD DTC scan/clear, passive monitoring, and recorder."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
import unittest

from openpilot.starpilot.ioniq.can_signals import (
  ADDR_BAT11,
  ADDR_CLU12,
  ADDR_CLU13,
  ADDR_EMS11,
  ADDR_EMS12,
  ADDR_EMS14,
  ADDR_EMS20,
  ADDR_EMS_H12,
  ADDR_TCU14,
  ADDR_WHL_SPD11,
  IoniqCanState,
  extract_intel,
)
from openpilot.starpilot.ioniq.dtc_catalog import analyze_dtcs, decode_dtc_bytes, decode_uds_status
from openpilot.starpilot.ioniq.recorder import EventFlightRecorder
from openpilot.starpilot.ioniq.uds_dtc import PassiveEcuMonitor, clear_ioniq_dtcs, scan_ioniq_ecus


def encode_intel(bits: list[tuple[int, int, int]]) -> bytes:
  packed = 0
  for start_bit, length, raw in bits:
    mask = (1 << length) - 1
    packed |= (raw & mask) << start_bit
  return packed.to_bytes(8, 'little')


@dataclass
class MockFrame:
  address: int
  dat: bytes
  src: int


class MockIsoTpBus:
  """Simulates multi-ECU ISO-TP single-frame, multi-frame, and ResponsePending responders."""

  def __init__(self) -> None:
    self.inbox: list[list[MockFrame]] = []
    self.sent_frames: list[MockFrame] = []
    self._multiframe_queues: dict[int, list[bytes]] = {}
    self.dtc_store: dict[int, list[tuple[int, int, int, int]]] = {
      0x7E0: [(0x03, 0x01, 0x00, 0x89)],       # ECM: P0301 active + MIL
      0x7E1: [(0x17, 0x44, 0x00, 0x0C)],       # TCU: P1744 pending + confirmed
      0x7E4: [(0x0A, 0x7F, 0x00, 0x28),        # BMS: P0A7F
              (0xC1, 0x00, 0x00, 0x20)],       # U0100 multi-frame (>7 bytes -> FirstFrame + CF!)
    }

  def can_send(self, msgs: list[MockFrame]) -> None:
    for msg in msgs:
      self.sent_frames.append(msg)
      addr = int(msg.address)
      dat = bytes(msg.dat)
      src = int(msg.src)
      pci_type = (dat[0] & 0xF0) >> 4
      if pci_type == 3:  # Flow control from tester -> emit queued Consecutive Frames
        rx = addr + 8
        cfs = self._multiframe_queues.pop(rx, [])
        if cfs:
          self.inbox.append([MockFrame(rx, cf, src) for cf in cfs])
        continue
      length = dat[0] & 0x0F
      req = dat[1:1 + length]
      if addr not in (0x7E0, 0x7E1, 0x7E2, 0x7E4, 0x7D1, 0x7D4):
        continue
      rx = addr + 8
      if req[:2] == b'\x19\x02':
        # Test 0x78 ResponsePending on BMS first
        dtcs = self.dtc_store.get(addr, [])
        payload = bytearray([0x59, 0x02, 0xFF])
        for b0, b1, ftb, st in dtcs:
          payload.extend((b0, b1, ftb, st))
        if addr == 0x7E4:
          self.inbox.append([MockFrame(rx, bytes([0x03, 0x7F, 0x19, 0x78]).ljust(8, b'\xAA'), src)])
        if len(payload) <= 7:
          self.inbox.append([MockFrame(rx, (bytes([len(payload)]) + bytes(payload)).ljust(8, b'\xAA'), src)])
        else:
          total = len(payload)
          ff = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + bytes(payload[:6])
          rem = bytes(payload[6:])
          cfs = []
          seq = 1
          for off in range(0, len(rem), 7):
            cfs.append((bytes([0x20 | (seq & 0x0F)]) + rem[off:off + 7]).ljust(8, b'\xAA'))
            seq += 1
          self._multiframe_queues[rx] = cfs
          self.inbox.append([MockFrame(rx, ff, src)])
      elif req[:2] == b'\x22\xF1':
        did = req[2]
        if did == 0x90:
          p = b'\x62\xF1\x90KMHC851CGHU000123'
        elif did == 0x87:
          p = b'\x62\xF1\x8739110-2B00'
        else:
          p = b'\x62\xF1\x95AEHEV16'
        if len(p) <= 7:
          self.inbox.append([MockFrame(rx, (bytes([len(p)]) + p).ljust(8, b'\xAA'), src)])
        else:
          total = len(p)
          ff = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + p[:6]
          rem = p[6:]
          cfs = []
          seq = 1
          for off in range(0, len(rem), 7):
            cfs.append((bytes([0x20 | (seq & 0x0F)]) + rem[off:off + 7]).ljust(8, b'\xAA'))
            seq += 1
          self._multiframe_queues[rx] = cfs
          self.inbox.append([MockFrame(rx, ff, src)])
      elif req[:1] in (b'\x03', b'\x07', b'\x0A'):
        sid = req[0] + 0x40
        p = bytes([sid, 0x00])
        self.inbox.append([MockFrame(rx, (bytes([len(p)]) + p).ljust(8, b'\xAA'), src)])
      elif req[:1] == b'\x14':
        self.dtc_store[addr] = []
        self.inbox.append([MockFrame(rx, b'\x01\x54'.ljust(8, b'\xAA'), src)])
      elif req[:1] == b'\x04':
        self.dtc_store[addr] = []
        self.inbox.append([MockFrame(rx, b'\x01\x44'.ljust(8, b'\xAA'), src)])

  def can_recv(self, wait_for_one: bool = False) -> list[list[MockFrame]]:
    out = self.inbox[:]
    self.inbox.clear()
    return out


class TestCanAndDtc(unittest.TestCase):
  def test_can_signal_decoding_and_fco(self) -> None:
    st = IoniqCanState()
    # EMS11: SWI_IGK=1, F_N_ENG=1, N=2000 rpm (raw=8000), VS=72
    st.apply_frame(1.0, ADDR_EMS11, encode_intel([(0, 1, 1), (1, 1, 1), (16, 16, 8000), (48, 8, 72)]))
    # EMS_H12: TQI_B=50% (raw=128), CF_Ems_EngRunNorm=1
    st.apply_frame(1.0, ADDR_EMS_H12, encode_intel([(16, 8, 128), (61, 1, 1)]))
    # EMS12: TEMP_ENG = 90 C -> (90 + 48) / 0.75 = 184
    st.apply_frame(1.0, ADDR_EMS12, encode_intel([(8, 8, 184)]))
    # CLU12: Odometer = 12345.6 km (raw=123456)
    st.apply_frame(1.0, ADDR_CLU12, encode_intel([(0, 24, 123456)]))
    # CLU13: AvgFCI = 4.3 L/100km (raw=43), FuelDispLvl = 24, DTE = 610 km
    st.apply_frame(1.0, ADDR_CLU13, encode_intel([(0, 2, 0), (3, 2, 1), (6, 10, 43), (18, 5, 24), (24, 10, 610)]))
    # BAT11: SOC=82%, Voltage=14.1V (raw=8100), SOH=91%
    st.apply_frame(1.0, ADDR_BAT11, encode_intel([(16, 8, 82), (24, 14, 8100), (48, 7, 91)]))
    # WHL_SPD11: 4 wheels @ 64.0 km/h (raw=2048)
    st.apply_frame(1.0, ADDR_WHL_SPD11, encode_intel([(0, 14, 2048), (16, 14, 2048), (32, 14, 2048), (48, 14, 2048)]))
    # EMS20: raw uint16 = 5000
    st.apply_frame(1.0, ADDR_EMS20, encode_intel([(0, 16, 5000)]))

    self.assertTrue(st.ignition_on)
    self.assertAlmostEqual(st.engine_rpm, 2000.0)
    self.assertTrue(st.engine_is_firing())
    self.assertAlmostEqual(st.coolant_temp_c or 0.0, 90.0)
    self.assertAlmostEqual(st.odometer_km or 0.0, 12345.6)
    self.assertEqual(st.fuel_disp_level, 24)
    self.assertEqual(st.dte_km, 610)
    self.assertAlmostEqual(st.cluster_avg_fc_value or 0.0, 4.3)
    self.assertAlmostEqual(st.bat_12v_voltage_v or 0.0, 14.1)
    self.assertAlmostEqual(st.wheel_speed_kph, 64.0)
    self.assertEqual(st.fco_raw, 5000)

  def test_dtc_decoder_and_analysis(self) -> None:
    self.assertEqual(decode_dtc_bytes(0x03, 0x01), 'P0301')
    self.assertEqual(decode_dtc_bytes(0xC1, 0x00, 0x00), 'U0100')
    self.assertEqual(decode_dtc_bytes(0x52, 0x60, 0x12), 'C1260-12')
    flags = decode_uds_status(0x89)
    self.assertTrue(flags['testFailed'] and flags['confirmed'] and flags['milRequested'])

  def test_isotp_scan_and_clear_multi_ecu(self) -> None:
    bus = MockIsoTpBus()
    res = scan_ioniq_ecus(bus.can_send, bus.can_recv, bus=1, timeout_s=0.15)
    self.assertEqual(res['vin'], 'KMHC851CGHU000123')
    self.assertGreaterEqual(res['ecusResponded'], 6)
    codes = {d['code'] for d in res['dtcs']}
    self.assertEqual(codes, {'P0301', 'P1744', 'P0A7F', 'U0100'})
    ecm_info = next(e for e in res['ecus'] if e['ecuId'] == 'ECM')
    self.assertEqual(ecm_info['partNumber'], '39110-2B00')
    self.assertTrue(any('Hybrid' in i['title'] or 'Hochvolt' in i['title'] for i in res['analysis']['insights']))

    clr = clear_ioniq_dtcs(bus.can_send, bus.can_recv, target_ecu='ECM', bus=1)
    self.assertEqual(clr['clearedCount'], 1)
    res2 = scan_ioniq_ecus(bus.can_send, bus.can_recv, bus=1, read_identities=False, timeout_s=0.15)
    codes2 = {d['code'] for d in res2['dtcs']}
    self.assertNotIn('P0301', codes2)
    self.assertIn('P1744', codes2)

    clr_all = clear_ioniq_dtcs(bus.can_send, bus.can_recv, target_ecu='ALL', bus=1)
    self.assertGreaterEqual(clr_all['clearedCount'], 3)
    res3 = scan_ioniq_ecus(bus.can_send, bus.can_recv, bus=1, read_identities=False, timeout_s=0.15)
    self.assertEqual(res3['dtcs'], [])
    self.assertTrue(analyze_dtcs([])['insights'][0]['severity'] == 'ok')

  def test_passive_monitor_and_flight_recorder(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      mon = PassiveEcuMonitor()
      rec = EventFlightRecorder(tmp, pre_seconds=10.0, post_seconds=2.0, sample_hz=5.0)
      st = IoniqCanState()
      for step in range(5):
        st.apply_frame(step * 0.3, ADDR_EMS11, encode_intel([(0, 1, 1), (16, 16, 6000), (48, 8, 50)]))
        rec.record_sample(st, step * 0.3, wall_time=1000.0 + step * 0.3)

      # Trigger MIL & TCU warning
      st.apply_frame(2.0, ADDR_EMS14, encode_intel([(1, 1, 1)]))
      st.apply_frame(2.0, ADDR_TCU14, encode_intel([(0, 3, 2)]))
      faults = mon.update(st, wall_time=1002.0)
      self.assertEqual({f['code'] for f in faults}, {'EMS_MIL', 'TCU_WARN'})
      rec.trigger_event('FAULT', faults[0]['message'], st, mono_s=2.0, wall_time=1002.0, details={'faults': faults})

      # Feed post-trigger samples until deadline (2.0 + 2.0 = 4.0)
      saved = None
      for step in range(8):
        mono = 2.4 + step * 0.4
        res = rec.record_sample(st, mono, wall_time=1000.0 + mono)
        if res is not None:
          saved = res
          break
      self.assertIsNotNone(saved)
      assert saved is not None
      self.assertGreaterEqual(saved['sampleCount'], 5)
      csv_out = rec.export_csv(saved['id'])
      self.assertIsNotNone(csv_out)
      assert csv_out is not None
      self.assertIn('speedKph,rpm', csv_out)

      # Continuous recording test
      rec.start_continuous('Testlauf', mono_s=6.0, wall_time=1006.0)
      rec.record_sample(st, 7.0, wall_time=1007.0)
      cont = rec.stop_continuous(wall_time=1008.0)
      self.assertIsNotNone(cont)
      self.assertEqual(len(rec.list_recordings()), 2)


if __name__ == '__main__':
  unittest.main()
