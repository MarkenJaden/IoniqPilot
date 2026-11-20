"""Hyundai Ioniq AE HEV multi-ECU ISO-TP UDS/OBD-II DTC scanner, clearer, and passive CAN monitor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Any, NamedTuple

from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.dtc_catalog import analyze_dtcs, decode_dtc_bytes, enrich_dtc_record
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_json, write_json_atomic


def _utc_timestamp() -> float:
  return datetime.now(UTC).timestamp()


@dataclass(frozen=True)
class EcuTarget:
  tx_addr: int
  rx_addr: int
  ecu_id: str
  name: str
  bus: int = 1
  uds_supported: bool = True
  obd_supported: bool = True


# Known 2016 Hyundai Ioniq Hybrid (AE HEV) diagnostic physical & functional ECU targets.
IONIQ_ECUS: tuple[EcuTarget, ...] = (
  EcuTarget(0x7E0, 0x7E8, 'ECM', 'Motorsteuergerät (1.6 GDi Kappa)', bus=1),
  EcuTarget(0x7E1, 0x7E9, 'TCU', 'Getriebesteuergerät (6-Gang DCT)', bus=1),
  EcuTarget(0x7E2, 0x7EA, 'HCU', 'Hybrid-Steuergerät (HCU / VCU)', bus=1),
  EcuTarget(0x7E3, 0x7EB, 'OPU', 'Inverter / Leistungselektronik', bus=1),
  EcuTarget(0x7E4, 0x7EC, 'BMS', 'Hochvolt-Batteriemanagement (BMS)', bus=1),
  EcuTarget(0x7D0, 0x7D8, 'SCC', 'Frontradar (Smart Cruise Control)', bus=1),
  EcuTarget(0x7D1, 0x7D9, 'ESC', 'ABS / ESC / Bremse', bus=1),
  EcuTarget(0x7D4, 0x7DC, 'MDPS', 'Servolenkung (MDPS / EPS)', bus=1),
  EcuTarget(0x7C4, 0x7CC, 'MFC', 'Frontkamera (LKAS / AEB)', bus=1),
  EcuTarget(0x7C6, 0x7CE, 'CLU', 'Kombiinstrument (Tacho)', bus=1),
  EcuTarget(0x7B1, 0x7B9, 'PAS', 'Einparkhilfe (RPAS)', bus=1),
  EcuTarget(0x7B3, 0x7BB, 'DATC', 'Klimaautomatik (DATC)', bus=1),
  EcuTarget(0x7B7, 0x7BF, 'BSD', 'Totwinkelradar (BSM / BSD)', bus=1),
  EcuTarget(0x7A0, 0x7A8, 'BCM', 'Karosseriesteuergerät (BCM / SMK)', bus=1),
)

ECU_BY_RX: dict[int, EcuTarget] = {ecu.rx_addr: ecu for ecu in IONIQ_ECUS}
ECU_BY_ID: dict[str, EcuTarget] = {ecu.ecu_id: ecu for ecu in IONIQ_ECUS}


class _CanFrame(NamedTuple):
  address: int
  dat: bytes
  src: int


def _make_can_msg(address: int, data: bytes, bus: int) -> Any:
  return _CanFrame(address=address, dat=data, src=bus)


def _iter_can_frames(packets: Any):
  if not packets:
    return
  for item in packets:
    if isinstance(item, list):
      for msg in item:
        yield _extract_frame(msg)
    elif hasattr(item, 'can'):
      for msg in item.can:
        yield _extract_frame(msg)
    elif isinstance(item, tuple) and len(item) >= 3:
      yield int(item[0]), bytes(item[1]), int(item[2])
    else:
      yield _extract_frame(item)


def _extract_frame(msg: Any) -> tuple[int, bytes, int]:
  addr = int(msg.address)
  dat = bytes(msg.dat)
  src = int(msg.src)
  return addr, dat, src


def _isotp_single_frame(payload: bytes, pad: int = 0xAA) -> bytes:
  if len(payload) > 7:
    raise ValueError('ISO-TP request payload exceeds single frame (7 bytes)')
  return (bytes([len(payload)]) + payload).ljust(8, bytes([pad]))


def isotp_exchange_many(
  can_send: Callable[[list[Any]], Any],
  can_recv: Callable[..., list[Any]],
  requests: list[tuple[int, int, bytes]],
  *,
  bus: int = 1,
  timeout_s: float = 0.6,
  pending_timeout_s: float = 2.0,
  clock: Callable[[], float] = time.monotonic,
) -> dict[int, bytes]:
  """Run parallel ISO 15765-2 exchanges across physical/functional CAN IDs."""
  if not requests:
    return {}
  rx_to_tx: dict[int, int] = {rx: tx for tx, rx, _ in requests}
  responses: dict[int, bytes] = {}
  buffers: dict[int, bytearray] = {}
  expected_len: dict[int, int] = {}
  next_seq: dict[int, int] = {}
  pending_seen_at: dict[int, float] = {}

  # Drain stale frames before initiating requests
  can_recv(False)

  tx_frames = [_make_can_msg(tx, _isotp_single_frame(payload), bus) for tx, _, payload in requests]
  can_send(tx_frames)

  deadline = clock() + timeout_s
  while clock() < deadline:
    packets = can_recv(False)
    for addr, dat, src in _iter_can_frames(packets):
      if src != bus or addr not in rx_to_tx or len(dat) < 2:
        continue
      pci_type = (dat[0] & 0xF0) >> 4
      if pci_type == 0:
        length = dat[0] & 0x0F
        if 1 <= length <= 7 and len(dat) >= 1 + length:
          payload = bytes(dat[1:1 + length])
          if len(payload) >= 3 and payload[0] == 0x7F and payload[2] == 0x78:
            pending_seen_at[addr] = clock()
            deadline = max(deadline, clock() + min(pending_timeout_s, 1.0))
            continue
          responses[addr] = payload
      elif pci_type == 1 and len(dat) >= 8:
        total = ((dat[0] & 0x0F) << 8) | dat[1]
        if total > 7:
          buffers[addr] = bytearray(dat[2:8])
          expected_len[addr] = total
          next_seq[addr] = 1
          fc = bytes([0x30, 0x00, 0x00]).ljust(8, b'\xAA')
          can_send([_make_can_msg(rx_to_tx[addr], fc, bus)])
      elif pci_type == 2 and addr in buffers:
        seq = dat[0] & 0x0F
        if seq == next_seq.get(addr, 1):
          buffers[addr].extend(dat[1:8])
          next_seq[addr] = (seq + 1) & 0x0F
          if len(buffers[addr]) >= expected_len[addr]:
            responses[addr] = bytes(buffers.pop(addr)[:expected_len.pop(addr)])
            next_seq.pop(addr, None)
    if all(rx in responses for _, rx, _ in requests):
      break
  return responses


def parse_uds_dtc_response(payload: bytes, ecu: EcuTarget) -> tuple[list[dict], str | None]:
  """Parse UDS 0x19 0x02 response (positive sid 0x59 0x02 mask + 4-byte records)."""
  if len(payload) >= 3 and payload[0] == 0x7F and payload[1] == 0x19:
    return [], f'0x{payload[2]:02X}'
  if len(payload) < 3 or payload[0] != 0x59 or payload[1] != 0x02:
    return [], None
  records: list[dict] = []
  body = payload[3:]
  for offset in range(0, len(body) - (len(body) % 4), 4):
    b0, b1, ftb, status = body[offset:offset + 4]
    if b0 == 0 and b1 == 0 and ftb == 0:
      continue
    code = decode_dtc_bytes(b0, b1, ftb if ftb else None)
    rec = enrich_dtc_record(ecu.ecu_id, ecu.name, code, status, source='uds_19_02')
    rec['_dtcBytes'] = (b0, b1, ftb)
    records.append(rec)
  return records, None


def parse_obd_dtc_response(payload: bytes, ecu: EcuTarget, mode: int) -> list[dict]:
  """Parse OBD-II Mode 03/07/0A response (0x43/0x47/0x4A + N + pairs OR ISO 15031 CAN pairs)."""
  expected_sid = mode + 0x40
  if not payload or payload[0] != expected_sid or len(payload) < 2:
    return []
  raw = payload[1:]
  # ISO 15765-4 CAN responses carry a 1-byte DTC count before pairs; detect cleanly.
  if len(raw) >= 1 and (len(raw) - 1) == raw[0] * 2:
    pairs = raw[1:]
  else:
    pairs = raw[:len(raw) - (len(raw) % 2)]
  source_name = f'obd_{mode:02X}'
  records: list[dict] = []
  for offset in range(0, len(pairs), 2):
    hi, lo = pairs[offset], pairs[offset + 1]
    if hi == 0 and lo == 0:
      continue
    code = decode_dtc_bytes(hi, lo)
    rec = enrich_dtc_record(ecu.ecu_id, ecu.name, code, None, source=source_name)
    rec['_dtcBytes'] = (hi, lo, 0)
    records.append(rec)
  return records


def _decode_ascii_did(payload: bytes, did_hi: int, did_lo: int) -> str | None:
  if len(payload) < 4 or payload[0] != 0x62 or payload[1] != did_hi or payload[2] != did_lo:
    return None
  raw = payload[3:].replace(b'\x00', b' ').strip()
  text = ''.join(chr(b) for b in raw if 32 <= b < 127).strip()
  return text or None


def _parse_obd_mode01_readiness(payload: bytes) -> dict | None:
  """Parse OBD-II Mode 01 PID 01 (0x41 0x01 A B C D) MIL + readiness monitor status."""
  if len(payload) < 6 or payload[0] != 0x41 or payload[1] != 0x01:
    return None
  a, b, c, d = payload[2], payload[3], payload[4], payload[5]
  mil_on = bool(a & 0x80)
  dtc_count = a & 0x7F
  monitors = {
    'misfire': {'supported': bool(b & 0x01), 'incomplete': bool(b & 0x10)},
    'fuelSystem': {'supported': bool(b & 0x02), 'incomplete': bool(b & 0x20)},
    'components': {'supported': bool(b & 0x04), 'incomplete': bool(b & 0x40)},
    'catalyst': {'supported': bool(c & 0x01), 'incomplete': bool(d & 0x01)},
    'evap': {'supported': bool(c & 0x04), 'incomplete': bool(d & 0x04)},
    'oxygenSensor': {'supported': bool(c & 0x20), 'incomplete': bool(d & 0x20)},
    'oxygenSensorHeater': {'supported': bool(c & 0x40), 'incomplete': bool(d & 0x40)},
    'egr': {'supported': bool(c & 0x80), 'incomplete': bool(d & 0x80)},
  }
  incomplete_names = [k for k, v in monitors.items() if v['supported'] and v['incomplete']]
  return {
    'milOn': mil_on,
    'emissionDtcCount': dtc_count,
    'incompleteMonitors': incomplete_names,
    'monitors': monitors,
  }


def _decode_supported_pids(base_pid: int, payload: bytes) -> set[int]:
  if len(payload) < 6 or payload[0] != 0x41 or payload[1] != base_pid:
    return set()
  mask = (payload[2] << 24) | (payload[3] << 16) | (payload[4] << 8) | payload[5]
  return {base_pid + offset for offset in range(1, 33) if (mask & (1 << (32 - offset)))}


def _query_obd_live_and_freeze_frame(
  can_send: Callable[[list[Any]], Any],
  can_recv: Callable[..., list[Any]],
  *,
  bus: int = 1,
  deadline: float | None = None,
  query_freeze_frame: bool = True,
  clock: Callable[[], float] = time.monotonic,
) -> tuple[dict | None, dict, dict | None, list[str]]:
  """Query OBD-II Mode 01 supported PIDs, readiness/live PIDs and Mode 02 freeze-frame from ECM (0x7E0/0x7E8)."""
  readiness: dict | None = None
  live: dict[str, Any] = {}
  freeze: dict[str, Any] = {}
  supported_set: set[int] = set()

  def _to(default_s: float) -> float:
    if deadline is None:
      return default_s
    rem = deadline - clock()
    return 0.0 if rem <= 0.02 else min(default_s, rem)

  # Probe Mode 01 supported PID bitmasks (0x00, 0x20, 0x40) with short timeout to skip unsupported PIDs
  for mask_pid in (0x00, 0x20, 0x40):
    if mask_pid > 0x00 and mask_pid not in supported_set:
      break
    to = _to(0.12)
    if to <= 0.0:
      break
    m_resp = isotp_exchange_many(can_send, can_recv, [(0x7E0, 0x7E8, bytes([0x01, mask_pid]))], bus=bus, timeout_s=to, clock=clock)
    mp = m_resp.get(0x7E8)
    if mp:
      supported_set.update(_decode_supported_pids(mask_pid, mp))
    elif mask_pid == 0x00:
      break

  obd_supported_pids = [f'0x{pid:02X}' for pid in sorted(supported_set)]

  # Mode 01 PIDs: 01 (readiness), 05 (coolant), 0C (RPM), 0D (speed), 10 (MAF), 2F (fuel level), 42 (ECU V), 5E (fuel rate)
  candidate_pids = (0x01, 0x05, 0x0C, 0x0D, 0x10, 0x2F, 0x42, 0x5E)
  pids_to_query = tuple(p for p in candidate_pids if not supported_set or p in supported_set)
  for pid in pids_to_query:
    to = _to(0.18)
    if to <= 0.0:
      break
    resp = isotp_exchange_many(can_send, can_recv, [(0x7E0, 0x7E8, bytes([0x01, pid]))], bus=bus, timeout_s=to, clock=clock)
    p = resp.get(0x7E8)
    if not p or len(p) < 3 or p[0] != 0x41 or p[1] != pid:
      continue
    if pid == 0x01:
      readiness = _parse_obd_mode01_readiness(p)
    elif pid == 0x05 and len(p) >= 3:
      live['coolantTempC'] = round(float(p[2]) - 40.0, 1)
    elif pid == 0x0C and len(p) >= 4:
      live['rpm'] = round(((p[2] << 8) | p[3]) / 4.0, 1)
    elif pid == 0x0D and len(p) >= 3:
      live['speedKph'] = float(p[2])
    elif pid == 0x10 and len(p) >= 4:
      maf = ((p[2] << 8) | p[3]) / 100.0
      live['mafGps'] = round(maf, 2)
      live['estimatedFuelRateLphFromMaf'] = round((maf * 3600.0) / (14.7 * 745.0), 3)
    elif pid == 0x2F and len(p) >= 3:
      live['fuelLevelPct'] = round(p[2] * 100.0 / 255.0, 1)
    elif pid == 0x42 and len(p) >= 4:
      live['controlModuleV'] = round(((p[2] << 8) | p[3]) / 1000.0, 2)
    elif pid == 0x5E and len(p) >= 4:
      live['fuelRateLph'] = round(((p[2] << 8) | p[3]) * 0.05, 3)

  # Mode 02 Freeze Frame frame 0x00: only query if readiness reports DTCs or explicitly requested
  has_readiness_dtcs = bool(readiness is not None and readiness.get('emissionDtcCount', 0) > 0)
  if (has_readiness_dtcs or query_freeze_frame) and _to(0.18) > 0.0:
    r02 = isotp_exchange_many(can_send, can_recv, [(0x7E0, 0x7E8, b'\x02\x02\x00')], bus=bus, timeout_s=_to(0.18), clock=clock)
    p02 = r02.get(0x7E8)
    if p02 and len(p02) >= 5 and p02[0] == 0x42 and p02[1] == 0x02 and (p02[3] != 0 or p02[4] != 0):
      freeze['dtcCode'] = decode_dtc_bytes(p02[3], p02[4])
      for pid in (0x04, 0x05, 0x0C, 0x0D):
        to = _to(0.15)
        if to <= 0.0:
          break
        rf = isotp_exchange_many(can_send, can_recv, [(0x7E0, 0x7E8, bytes([0x02, pid, 0x00]))], bus=bus, timeout_s=to, clock=clock)
        pf = rf.get(0x7E8)
        if not pf or len(pf) < 4 or pf[0] != 0x42 or pf[1] != pid:
          continue
        if pid == 0x04:
          freeze['engineLoadPct'] = round(pf[3] * 100.0 / 255.0, 1)
        elif pid == 0x05:
          freeze['coolantTempC'] = round(float(pf[3]) - 40.0, 1)
        elif pid == 0x0C and len(pf) >= 5:
          freeze['rpm'] = round(((pf[3] << 8) | pf[4]) / 4.0, 1)
        elif pid == 0x0D:
          freeze['speedKph'] = float(pf[3])

  return readiness, live, (freeze or None), obd_supported_pids


def scan_ioniq_ecus(
  can_send: Callable[[list[Any]], Any],
  can_recv: Callable[..., list[Any]],
  *,
  bus: int = 1,
  fallback_bus: int | None = None,
  set_obd_multiplexing: Callable[[bool], Any] | None = None,
  total_budget_s: float | None = None,
  ecus: tuple[EcuTarget, ...] = IONIQ_ECUS,
  read_identities: bool = True,
  timeout_s: float = 0.55,
  clock: Callable[[], float] = time.monotonic,
) -> dict:
  """Scan all Ioniq AE HEV ECUs for UDS/OBD-II DTCs, freeze-frames, readiness, and ECU metadata."""
  started_at = _utc_timestamp()
  t0 = clock()
  budget_deadline = (t0 + total_budget_s) if total_budget_s is not None else None

  def _remaining(default_to: float) -> float:
    if budget_deadline is None:
      return default_to
    rem = budget_deadline - clock()
    return 0.0 if rem <= 0.02 else min(default_to, rem)

  ecu_statuses: list[dict] = []
  dtcs: list[dict] = []
  seen_dtc_keys: set[tuple[str, str, str]] = set()
  vin: str | None = None
  uds_resps: dict[int, bytes] = {}
  ecu_bus: dict[int, int] = {}
  ident_resps: dict[int, bytes] = {}
  sw_resps: dict[int, bytes] = {}
  obd_by_rx: dict[int, list[dict]] = {ecu.rx_addr: [] for ecu in ecus if ecu.obd_supported}
  obd_responded_rx: set[int] = set()
  uds_snapshots: dict[tuple[str, str, str], str] = {}
  readiness: dict | None = None
  obd_live: dict[str, Any] = {}
  obd_freeze: dict | None = None
  obd_supported_pids: list[str] = []

  def _scan_bus_phase(target_ecus: list[EcuTarget], target_bus: int, phase_timeout: float) -> None:
    nonlocal vin, readiness, obd_live, obd_freeze, obd_supported_pids
    if not target_ecus:
      return
    to = _remaining(phase_timeout)
    if to <= 0.0:
      return
    uds_requests = [(ecu.tx_addr, ecu.rx_addr, b'\x19\x02\xFF') for ecu in target_ecus if ecu.uds_supported]
    phase_uds = isotp_exchange_many(can_send, can_recv, uds_requests, bus=target_bus, timeout_s=to, clock=clock)
    for rx, payload in phase_uds.items():
      uds_resps[rx] = payload
      ecu_bus[rx] = target_bus

    # OBD-II Modes 03/07/0A on unqueried powertrain ECUs for this bus
    phase_obd_ecus = [
      ecu for ecu in target_ecus
      if ecu.obd_supported and ecu.tx_addr in (0x7E0, 0x7E1, 0x7E2, 0x7E4) and ecu.rx_addr not in obd_responded_rx
    ]
    if phase_obd_ecus:
      for mode in (0x03, 0x07, 0x0A):
        obd_to = _remaining(0.35 if total_budget_s is None else 0.22)
        if obd_to <= 0.0:
          break
        reqs = [(ecu.tx_addr, ecu.rx_addr, bytes([mode])) for ecu in phase_obd_ecus]
        resps = isotp_exchange_many(can_send, can_recv, reqs, bus=target_bus, timeout_s=obd_to, clock=clock)
        for rx, payload in resps.items():
          ecu = ECU_BY_RX.get(rx)
          if ecu is not None:
            obd_responded_rx.add(rx)
            ecu_bus.setdefault(rx, target_bus)
            obd_by_rx[rx].extend(parse_obd_dtc_response(payload, ecu, mode))

    # Query OBD-II Mode 01/02 if ECM (0x7E8) responded on this bus and not yet queried
    if (0x7E8 in phase_uds or 0x7E8 in obd_responded_rx) and readiness is None and not obd_live:
      has_ecm_dtcs = bool(obd_by_rx.get(0x7E8)) or (0x7E8 in phase_uds and len(phase_uds[0x7E8]) > 3)
      readiness, obd_live, obd_freeze, obd_supported_pids = _query_obd_live_and_freeze_frame(
        can_send,
        can_recv,
        bus=target_bus,
        deadline=budget_deadline,
        query_freeze_frame=has_ecm_dtcs or total_budget_s is None,
        clock=clock,
      )

    if read_identities and phase_uds:
      responding_ecus = [ECU_BY_RX[rx] for rx in phase_uds if rx in ECU_BY_RX]
      id_to = _remaining(0.35)
      if id_to > 0.0:
        part_reqs = [(ecu.tx_addr, ecu.rx_addr, b'\x22\xF1\x87') for ecu in responding_ecus]
        ident_resps.update(isotp_exchange_many(can_send, can_recv, part_reqs, bus=target_bus, timeout_s=id_to, clock=clock))
      sw_to = _remaining(0.35)
      if sw_to > 0.0:
        sw_reqs = [(ecu.tx_addr, ecu.rx_addr, b'\x22\xF1\x95') for ecu in responding_ecus]
        sw_resps.update(isotp_exchange_many(can_send, can_recv, sw_reqs, bus=target_bus, timeout_s=sw_to, clock=clock))
      if 0x7E8 in phase_uds and vin is None:
        vin_to = _remaining(0.35)
        if vin_to > 0.0:
          vin_resp = isotp_exchange_many(can_send, can_recv, [(0x7E0, 0x7E8, b'\x22\xF1\x90')], bus=target_bus, timeout_s=vin_to, clock=clock)
          if 0x7E8 in vin_resp:
            vin = _decode_ascii_did(vin_resp[0x7E8], 0xF1, 0x90)

    # Fetch UDS 0x19 0x04 snapshots while this bus is active
    for rx, payload in phase_uds.items():
      ecu = ECU_BY_RX.get(rx)
      if ecu is None:
        continue
      parsed_dtcs, _ = parse_uds_dtc_response(payload, ecu)
      for rec in parsed_dtcs:
        dtc_bytes = rec.get('_dtcBytes')
        if dtc_bytes is None:
          continue
        snap_to = _remaining(0.18)
        if snap_to <= 0.0:
          break
        b0, b1, ftb = dtc_bytes
        snap_req = [(ecu.tx_addr, ecu.rx_addr, bytes([0x19, 0x04, b0, b1, ftb, 0xFF]))]
        snap_resp = isotp_exchange_many(can_send, can_recv, snap_req, bus=target_bus, timeout_s=snap_to, clock=clock)
        sp = snap_resp.get(ecu.rx_addr)
        if sp and len(sp) > 6 and sp[0] == 0x59 and sp[1] == 0x04:
          uds_snapshots[(rec['ecuId'], rec['code'].split('-', 1)[0], rec['source'])] = sp[6:].hex()

  # Phase 1: primary bus (typically bus=1 OBD-II)
  _scan_bus_phase(list(ecus), bus, timeout_s)

  # Phase 2: fallback bus (typically bus=0 C-CAN) for non-responding ECUs (e.g. MFC/SCC/MDPS)
  missing_ecus = [ecu for ecu in ecus if ecu.rx_addr not in uds_resps and ecu.rx_addr not in obd_responded_rx]
  if fallback_bus is not None and fallback_bus != bus and missing_ecus and _remaining(timeout_s) > 0.02:
    if set_obd_multiplexing is not None:
      set_obd_multiplexing(False)
    _scan_bus_phase(missing_ecus, fallback_bus, min(timeout_s, 0.35))

  for ecu in ecus:
    payload = uds_resps.get(ecu.rx_addr)
    ecu_dtcs: list[dict] = []
    nrc = None
    responded = payload is not None or bool(obd_by_rx.get(ecu.rx_addr))
    if payload is not None:
      ecu_dtcs, nrc = parse_uds_dtc_response(payload, ecu)
    for rec in ecu_dtcs + obd_by_rx.get(ecu.rx_addr, []):
      dedup_key = (rec['ecuId'], rec['code'].split('-', 1)[0], rec['source'])
      if dedup_key not in seen_dtc_keys:
        seen_dtc_keys.add(dedup_key)
        rec.pop('_dtcBytes', None)
        if dedup_key in uds_snapshots:
          rec['snapshotHex'] = uds_snapshots[dedup_key]
        if obd_freeze and obd_freeze.get('dtcCode') == rec['code'].split('-', 1)[0]:
          rec['freezeFrame'] = dict(obd_freeze)
        dtcs.append(rec)

    part_no = _decode_ascii_did(ident_resps.get(ecu.rx_addr, b''), 0xF1, 0x87)
    sw_ver = _decode_ascii_did(sw_resps.get(ecu.rx_addr, b''), 0xF1, 0x95)
    ecu_codes = [d for d in dtcs if d['ecuId'] == ecu.ecu_id]
    ecu_statuses.append({
      'ecuId': ecu.ecu_id,
      'name': ecu.name,
      'txHex': f'0x{ecu.tx_addr:03X}',
      'rxHex': f'0x{ecu.rx_addr:03X}',
      'online': responded,
      'busUsed': ecu_bus.get(ecu.rx_addr),
      'dtcCount': len(ecu_codes),
      'activeCount': sum(1 for d in ecu_codes if d['active']),
      'nrc': nrc,
      'partNumber': part_no,
      'softwareVersion': sw_ver,
    })

  dtcs.sort(key=lambda item: (not item['active'], not item['confirmed'], item['ecuId'], item['code']))
  analysis = analyze_dtcs(dtcs)
  return {
    'scannedAt': started_at,
    'transport': 'isotp_elm327_dual_bus' if fallback_bus is not None else 'isotp_elm327',
    'vin': vin,
    'ecusResponded': sum(1 for item in ecu_statuses if item['online']),
    'ecusTotal': len(ecu_statuses),
    'ecus': ecu_statuses,
    'dtcs': dtcs,
    'readiness': readiness,
    'obdLive': obd_live,
    'obdSupportedPids': obd_supported_pids,
    'freezeFrame': obd_freeze,
    'durationMs': round(max(0.0, (clock() - t0) * 1000.0), 1),
    'analysis': analysis,
  }


def clear_ioniq_dtcs(
  can_send: Callable[[list[Any]], Any],
  can_recv: Callable[..., list[Any]],
  *,
  target_ecu: str = 'ALL',
  bus: int = 1,
  fallback_bus: int | None = None,
  set_obd_multiplexing: Callable[[bool], Any] | None = None,
  clock: Callable[[], float] = time.monotonic,
) -> dict:
  """Clear stored DTCs via UDS 0x14 FF FF FF (and OBD Mode 04 for ALL/powertrain)."""
  cleared: list[dict] = []
  errors: list[str] = []
  responded_rx: set[int] = set()

  if target_ecu.upper() == 'ALL':
    targets = list(IONIQ_ECUS)
  else:
    ecu = ECU_BY_ID.get(target_ecu.upper())
    if ecu is None:
      raise ValueError(f'Unknown ECU target: {target_ecu}')
    targets = [ecu]

  def _clear_on_bus(bus_targets: list[EcuTarget], target_bus: int) -> None:
    if not bus_targets:
      return
    reqs = [(ecu.tx_addr, ecu.rx_addr, b'\x14\xFF\xFF\xFF') for ecu in bus_targets]
    resps = isotp_exchange_many(can_send, can_recv, reqs, bus=target_bus, timeout_s=0.7, clock=clock)
    for ecu in bus_targets:
      payload = resps.get(ecu.rx_addr)
      if payload is None:
        continue
      responded_rx.add(ecu.rx_addr)
      if payload[:1] == b'\x54':
        cleared.append({'ecuId': ecu.ecu_id, 'status': 'cleared_uds', 'bus': target_bus})
      elif len(payload) >= 3 and payload[0] == 0x7F and payload[1] == 0x14:
        errors.append(f'{ecu.ecu_id}: UDS NRC 0x{payload[2]:02X}')

    if target_ecu.upper() in ('ALL', 'ECM', 'TCU', 'HCU', 'BMS'):
      obd_targets = [ecu for ecu in bus_targets if ecu.tx_addr in (0x7E0, 0x7E1, 0x7E2, 0x7E4)]
      if obd_targets:
        obd_resps = isotp_exchange_many(
          can_send, can_recv, [(ecu.tx_addr, ecu.rx_addr, b'\x04') for ecu in obd_targets],
          bus=target_bus, timeout_s=0.6, clock=clock,
        )
        for ecu in obd_targets:
          if obd_resps.get(ecu.rx_addr, b'')[:1] == b'\x44':
            responded_rx.add(ecu.rx_addr)
            if not any(item['ecuId'] == ecu.ecu_id for item in cleared):
              cleared.append({'ecuId': ecu.ecu_id, 'status': 'cleared_obd04', 'bus': target_bus})

  _clear_on_bus(targets, bus)
  remaining_targets = [ecu for ecu in targets if ecu.rx_addr not in responded_rx]
  if fallback_bus is not None and fallback_bus != bus and remaining_targets:
    if set_obd_multiplexing is not None:
      set_obd_multiplexing(False)
    _clear_on_bus(remaining_targets, fallback_bus)

  return {
    'clearedAt': _utc_timestamp(),
    'target': target_ecu.upper(),
    'clearedCount': len(cleared),
    'cleared': cleared,
    'errors': errors,
  }


class PassiveEcuMonitor:
  """Always-on passive CAN fault/health monitor (zero TX required) with boot/bulb-check blanking & debounce."""

  def __init__(self, *, boot_blanking_s: float = 8.0, debounce_s: float = 2.5, min_samples: int = 8) -> None:
    self.boot_blanking_s = boot_blanking_s
    self.debounce_s = debounce_s
    self.min_samples = min_samples
    self.active_warnings: dict[str, dict] = {}
    self.warning_history: list[dict] = []
    self._last_low_12v_seen = False
    self._first_mono_s: float | None = None
    self._ign_on_since_mono: float | None = None
    self._candidate_since: dict[str, tuple[float, int]] = {}

  def clear_passive_warnings(self) -> None:
    """Clear active passive warnings and stored passive warning history."""
    self.active_warnings.clear()
    self.warning_history.clear()
    self._candidate_since.clear()

  def update(self, can_state: IoniqCanState, wall_time: float | None = None, *, mono_s: float | None = None) -> list[dict]:
    """Compare current CAN flags with previous state and return newly raised fault events."""
    now = wall_time if wall_time is not None else _utc_timestamp()

    # Track startup and ignition-on bulb-check blanking when running in real time (mono_s provided).
    if mono_s is not None:
      if self._first_mono_s is None:
        self._first_mono_s = mono_s
      if can_state.ignition_on:
        if self._ign_on_since_mono is None:
          self._ign_on_since_mono = mono_s
      else:
        self._ign_on_since_mono = None
        self._candidate_since.clear()
        self.active_warnings.clear()
        return []

      in_boot_blanking = (mono_s - self._first_mono_s) < self.boot_blanking_s
      in_ign_blanking = self._ign_on_since_mono is not None and (mono_s - self._ign_on_since_mono) < self.boot_blanking_s
      if in_boot_blanking or in_ign_blanking:
        self._candidate_since.clear()
        return []

    detected: dict[str, tuple[str, str, str]] = {}

    if can_state.mil_lamp:
      detected['EMS_MIL'] = ('ECM', 'critical', 'Motorkontrollleuchte (MIL) im CAN aktiv (EMS14.L_MIL)')
    if can_state.limp_home_mode:
      detected['EMS_LIMP'] = ('ECM', 'critical', 'Notlaufprogramm aktiv (EMS_H12.CF_Ems_EtcLimpMod)')
    if can_state.ec_fail or can_state.blec_fail:
      detected['EMS_FAIL'] = ('ECM', 'warning', 'Motor-/EMS-Fehlermeldung aktiv (EMS15)')
    if can_state.tcu_warn_msg > 0 or can_state.tcu_warn_img or can_state.tcu_dri_warn1 > 0 or can_state.tcu_dri_warn2 > 0:
      detected['TCU_WARN'] = ('TCU', 'warning', f'6-DCT Getriebewarnung aktiv (msg={can_state.tcu_warn_msg}, drv={can_state.tcu_dri_warn1})')
    if can_state.dct_hill_hold_warn:
      detected['DCT_HILL_HOLD'] = ('TCU', 'warning', 'DCT Überhitzungs-/Berganfahrwarnung (TCU_DCT14)')
    # ABS/EBD warning lamp is on during stationary ignition bulb-check; require either actual defect flag or moving vehicle (> 5 km/h) when mono_s is active
    abs_lamp_valid = (can_state.abs_warn_lamp or can_state.ebd_warn_lamp) and (
      mono_s is None or can_state.best_speed_kph() > 5.0 or can_state.engine_is_firing()
    )
    if can_state.abs_defect or can_state.ebd_defect or abs_lamp_valid:
      detected['ABS_EBD_WARN'] = ('ESC', 'critical', 'ABS/EBD Bremssystem-Warnleuchte aktiv')
    if can_state.fca_fail_info > 0:
      detected['FCA_FAIL'] = ('SCC', 'warning', f'Frontkollisionswarnung / Radar-Störung (FCA_Failinfo={can_state.fca_fail_info})')
    if can_state.bat_12v_error or can_state.bat_12v_invalid:
      detected['BAT_12V_SENSOR'] = ('IBS', 'warning', '12V-Batteriesensor (BAT11) Fehler/Ungültig')
    low_12v = (can_state.bat_12v_voltage_v is not None and can_state.bat_12v_voltage_v < 11.7)
    if low_12v or self._last_low_12v_seen and (can_state.bat_12v_voltage_v is not None and can_state.bat_12v_voltage_v < 11.9):
      detected['BAT_12V_LOW'] = ('IBS', 'warning', f'12V-Batteriespannung niedrig ({can_state.bat_12v_voltage_v:.2f}V)')
    self._last_low_12v_seen = low_12v
    if can_state.mdps_def or can_state.mdps_fail_stat or can_state.mdps_serr:
      detected['MDPS_FAULT'] = ('MDPS', 'critical', 'Servolenkung (MDPS) Störung aktiv (MDPS12)')
    if can_state.tcs13_acc_enable in (1, 2, 3) or can_state.scc_fail_info in (1, 2):
      detected['SCC_FAULT'] = (
        'SCC',
        'warning',
        f'Tempomat/Radar-Störung (ACCEnable={can_state.tcs13_acc_enable}, Fail={can_state.scc_fail_info})',
      )
    if can_state.aeb_fail_info in (1, 2):
      detected['AEB_FAULT'] = ('SCC', 'warning', f'Notbremsassistent (AEB) Störung (AEB_Failinfo={can_state.aeb_fail_info})')
    if can_state.lfa_sys_warning in (3, 4):
      detected['LKAS_FAULT'] = ('MFC', 'warning', f'Spurhalteassistent (LKAS/LFA) Warnung (Warn={can_state.lfa_sys_warning})')
    if can_state.epb_f_lamp in (1, 2) or can_state.epb_fail > 0:
      detected['EPB_FAULT'] = (
        'ESC',
        'warning',
        f'Elektrische Parkbremse (EPB) Fehler/Warnung (Lamp={can_state.epb_f_lamp}, Fail={can_state.epb_fail})',
      )
    if can_state.tpms_w_lamp in (1, 2) or can_state.tread_w_lamp in (1, 2):
      wheels = ', '.join(lbl for lbl, flag in zip(('VL', 'VR', 'HL', 'HR'), can_state.tpms_pos_warn, strict=True) if flag)
      suffix = f': {wheels}' if wheels else ''
      detected['TPMS_WARN'] = ('BCM', 'warning', f'Reifendruck-Warnung (TPMS{suffix})')
    if can_state.acu_flt_stat in (1, 2) or can_state.acu_num_flt > 0:
      dtc_part = f', DTC=0x{can_state.acu_dtc_raw:04X}' if can_state.acu_dtc_raw > 0 else ''
      detected['ACU_AIRBAG'] = ('ACU', 'critical', f'Airbag-System (ACU) Störung (Fehler={can_state.acu_num_flt}{dtc_part})')
    if can_state.ops_fail and (mono_s is None or can_state.engine_is_firing()):
      detected['EMS_OIL_PRESSURE'] = ('ECM', 'critical', 'Öldruck-Störung bei laufendem Motor (EMS19.CF_Ems_OPSFail)')
    if (can_state.bat_lamp_stat or can_state.alt_lamp_status) and (
      mono_s is None or can_state.engine_is_firing() or can_state.best_speed_kph() > 5.0
    ):
      detected['EMS_ALT_LAMP'] = ('ECM', 'warning', 'Ladekontrollleuchte / 12V-Generatormeldung aktiv (EMS19/CLU13)')
    if can_state.coolant_temp_c is not None and can_state.coolant_temp_c > 105.0:
      detected['COOLANT_OVERHEAT'] = ('ECM', 'critical', f'Kühlmittel überhitzt ({can_state.coolant_temp_c:.1f} °C > 105 °C)')

    # Clear resolved warnings and candidate counters
    for code in list(self._candidate_since):
      if code not in detected:
        self._candidate_since.pop(code, None)
    for code in list(self.active_warnings):
      if code not in detected:
        self.active_warnings.pop(code, None)

    new_events: list[dict] = []
    for code, (ecu_id, severity, message) in detected.items():
      if mono_s is not None:
        start_mono, count = self._candidate_since.get(code, (mono_s, 0))
        count += 1
        self._candidate_since[code] = (start_mono, count)
        if (mono_s - start_mono) < self.debounce_s or count < self.min_samples:
          continue

      if code not in self.active_warnings:
        event = {
          'code': code,
          'ecuId': ecu_id,
          'severity': severity,
          'message': message,
          'startedAt': now,
          'lastSeenAt': now,
        }
        self.active_warnings[code] = event
        self.warning_history.insert(0, dict(event))
        del self.warning_history[50:]
        new_events.append(event)
      else:
        self.active_warnings[code]['lastSeenAt'] = now
        self.active_warnings[code]['message'] = message
    return new_events

  def snapshot(self) -> dict:
    return {
      'activeWarnings': list(self.active_warnings.values()),
      'warningHistory': list(self.warning_history),
    }


def diagnostics_path(root: Path | str | None = None) -> Path:
  return ioniq_storage_root(root) / 'diagnostics.json'


def read_diagnostics_state(root: Path | str | None = None) -> dict:
  default = {
    'lastScan': None,
    'lastClear': None,
    'pendingAction': None,   # {'type': 'scan'|'clear', 'target': 'ALL', 'requestedAt': ...}
    'passive': {'activeWarnings': [], 'warningHistory': []},
    'liveSummary': {},
    'liveEcus': [],
    'history': [],
  }
  data = read_json(diagnostics_path(root), default)
  if not isinstance(data, dict):
    return default
  for key, val in default.items():
    data.setdefault(key, val)
  return data


def write_diagnostics_state(state: dict, root: Path | str | None = None) -> dict:
  history = list(state.get('history') or [])[:20]
  state['history'] = history
  write_json_atomic(diagnostics_path(root), state)
  return state


def clear_passive_warnings_state(root: Path | str | None = None) -> dict:
  """Clear passive warning history in diagnostics.json."""
  state = read_diagnostics_state(root)
  state['passive'] = {'activeWarnings': [], 'warningHistory': []}
  return write_diagnostics_state(state, root)


def queue_diagnostic_action(action_type: str, target: str = 'ALL', root: Path | str | None = None) -> dict:
  if action_type not in ('scan', 'clear'):
    raise ValueError('Unsupported diagnostic action')
  state = read_diagnostics_state(root)
  state['pendingAction'] = {
    'type': action_type,
    'target': target.upper(),
    'requestedAt': _utc_timestamp(),
  }
  return write_diagnostics_state(state, root)


def apply_scan_result(scan_result: dict, passive_snapshot: dict | None = None, root: Path | str | None = None) -> dict:
  state = read_diagnostics_state(root)
  warnings = (passive_snapshot or state.get('passive') or {}).get('activeWarnings', [])
  scan_result['analysis'] = analyze_dtcs(scan_result.get('dtcs', []), warnings)
  state['lastScan'] = scan_result
  state['pendingAction'] = None
  if passive_snapshot is not None:
    state['passive'] = passive_snapshot
  summary_entry = {
    'scannedAt': scan_result.get('scannedAt'),
    'ecusResponded': scan_result.get('ecusResponded', 0),
    'dtcCount': len(scan_result.get('dtcs', [])),
    'codes': [d.get('code') for d in scan_result.get('dtcs', [])],
  }
  history = [summary_entry] + [h for h in state.get('history', []) if h.get('scannedAt') != summary_entry['scannedAt']]
  state['history'] = history[:20]
  return write_diagnostics_state(state, root)


def apply_clear_result(clear_result: dict, root: Path | str | None = None) -> dict:
  state = read_diagnostics_state(root)
  state['lastClear'] = clear_result
  state['pendingAction'] = None
  last_scan = state.get('lastScan')
  if isinstance(last_scan, dict) and clear_result.get('clearedCount', 0) > 0:
    cleared_ids = {item['ecuId'] for item in clear_result.get('cleared', [])}
    if clear_result.get('target') == 'ALL':
      remaining = [d for d in last_scan.get('dtcs', []) if d.get('ecuId') not in cleared_ids]
    else:
      remaining = [d for d in last_scan.get('dtcs', []) if d.get('ecuId') != clear_result.get('target')]
    last_scan['dtcs'] = remaining
    last_scan['analysis'] = analyze_dtcs(remaining, (state.get('passive') or {}).get('activeWarnings', []))
    for ecu in last_scan.get('ecus', []):
      ecu['dtcCount'] = sum(1 for d in remaining if d.get('ecuId') == ecu.get('ecuId'))
      ecu['activeCount'] = sum(1 for d in remaining if d.get('ecuId') == ecu.get('ecuId') and d.get('active'))
  return write_diagnostics_state(state, root)
