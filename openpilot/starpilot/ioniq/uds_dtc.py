"""Hyundai Ioniq AE HEV multi-ECU ISO-TP UDS/OBD-II DTC scanner, clearer, and passive CAN monitor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.dtc_catalog import analyze_dtcs, decode_dtc_bytes, enrich_dtc_record
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_json, write_json_atomic


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


def _make_can_msg(address: int, data: bytes, bus: int) -> Any:
  return _CanFrame(address=address, dat=data, src=bus)


@dataclass
class _CanFrame:
  address: int
  dat: bytes
  src: int


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
  addr = int(getattr(msg, 'address'))
  dat = bytes(getattr(msg, 'dat'))
  src = int(getattr(msg, 'src'))
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
    records.append(enrich_dtc_record(ecu.ecu_id, ecu.name, code, status, source='uds_19_02'))
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
    records.append(enrich_dtc_record(ecu.ecu_id, ecu.name, code, None, source=source_name))
  return records


def _decode_ascii_did(payload: bytes, did_hi: int, did_lo: int) -> str | None:
  if len(payload) < 4 or payload[0] != 0x62 or payload[1] != did_hi or payload[2] != did_lo:
    return None
  raw = payload[3:].replace(b'\x00', b' ').strip()
  text = ''.join(chr(b) for b in raw if 32 <= b < 127).strip()
  return text or None


def scan_ioniq_ecus(
  can_send: Callable[[list[Any]], Any],
  can_recv: Callable[..., list[Any]],
  *,
  bus: int = 1,
  ecus: tuple[EcuTarget, ...] = IONIQ_ECUS,
  read_identities: bool = True,
  timeout_s: float = 0.55,
  clock: Callable[[], float] = time.monotonic,
) -> dict:
  """Scan all Ioniq AE HEV ECUs for UDS/OBD-II DTCs and ECU metadata."""
  started_at = time.time()
  ecu_statuses: list[dict] = []
  dtcs: list[dict] = []
  seen_dtc_keys: set[tuple[str, str, str]] = set()
  vin: str | None = None

  uds_requests = [(ecu.tx_addr, ecu.rx_addr, b'\x19\x02\xFF') for ecu in ecus if ecu.uds_supported]
  uds_resps = isotp_exchange_many(can_send, can_recv, uds_requests, bus=bus, timeout_s=timeout_s, clock=clock)

  ident_resps: dict[int, bytes] = {}
  sw_resps: dict[int, bytes] = {}
  if read_identities and uds_resps:
    responding_ecus = [ECU_BY_RX[rx] for rx in uds_resps if rx in ECU_BY_RX]
    part_reqs = [(ecu.tx_addr, ecu.rx_addr, b'\x22\xF1\x87') for ecu in responding_ecus]
    sw_reqs = [(ecu.tx_addr, ecu.rx_addr, b'\x22\xF1\x95') for ecu in responding_ecus]
    ident_resps = isotp_exchange_many(can_send, can_recv, part_reqs, bus=bus, timeout_s=0.35, clock=clock)
    sw_resps = isotp_exchange_many(can_send, can_recv, sw_reqs, bus=bus, timeout_s=0.35, clock=clock)
    if 0x7E8 in uds_resps:
      vin_resp = isotp_exchange_many(can_send, can_recv, [(0x7E0, 0x7E8, b'\x22\xF1\x90')], bus=bus, timeout_s=0.35, clock=clock)
      if 0x7E8 in vin_resp:
        vin = _decode_ascii_did(vin_resp[0x7E8], 0xF1, 0x90)

  # Fallback OBD-II Modes 03 (confirmed), 07 (pending), 0A (permanent) on powertrain ECUs if needed
  obd_ecus = [ecu for ecu in ecus if ecu.obd_supported and ecu.tx_addr in (0x7E0, 0x7E1, 0x7E2, 0x7E4)]
  obd_by_rx: dict[int, list[dict]] = {ecu.rx_addr: [] for ecu in obd_ecus}
  for mode in (0x03, 0x07, 0x0A):
    reqs = [(ecu.tx_addr, ecu.rx_addr, bytes([mode])) for ecu in obd_ecus]
    resps = isotp_exchange_many(can_send, can_recv, reqs, bus=bus, timeout_s=0.35, clock=clock)
    for rx, payload in resps.items():
      ecu = ECU_BY_RX.get(rx)
      if ecu is not None:
        obd_by_rx[rx].extend(parse_obd_dtc_response(payload, ecu, mode))

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
    'transport': 'isotp_elm327',
    'vin': vin,
    'ecusResponded': sum(1 for item in ecu_statuses if item['online']),
    'ecusTotal': len(ecu_statuses),
    'ecus': ecu_statuses,
    'dtcs': dtcs,
    'analysis': analysis,
  }


def clear_ioniq_dtcs(
  can_send: Callable[[list[Any]], Any],
  can_recv: Callable[..., list[Any]],
  *,
  target_ecu: str = 'ALL',
  bus: int = 1,
  clock: Callable[[], float] = time.monotonic,
) -> dict:
  """Clear stored DTCs via UDS 0x14 FF FF FF (and OBD Mode 04 for ALL/powertrain)."""
  cleared: list[dict] = []
  errors: list[str] = []

  if target_ecu.upper() == 'ALL':
    targets = list(IONIQ_ECUS)
  else:
    ecu = ECU_BY_ID.get(target_ecu.upper())
    if ecu is None:
      raise ValueError(f'Unknown ECU target: {target_ecu}')
    targets = [ecu]

  # UDS ClearDiagnosticInformation (0x14 0xFF 0xFF 0xFF)
  reqs = [(ecu.tx_addr, ecu.rx_addr, b'\x14\xFF\xFF\xFF') for ecu in targets]
  resps = isotp_exchange_many(can_send, can_recv, reqs, bus=bus, timeout_s=0.7, clock=clock)
  for ecu in targets:
    payload = resps.get(ecu.rx_addr)
    if payload is None:
      continue
    if payload[:1] == b'\x54':
      cleared.append({'ecuId': ecu.ecu_id, 'status': 'cleared_uds'})
    elif len(payload) >= 3 and payload[0] == 0x7F and payload[1] == 0x14:
      errors.append(f'{ecu.ecu_id}: UDS NRC 0x{payload[2]:02X}')

  # Functional OBD-II Mode 04 if ALL or powertrain requested
  if target_ecu.upper() in ('ALL', 'ECM', 'TCU', 'HCU', 'BMS'):
    obd_targets = [ecu for ecu in targets if ecu.tx_addr in (0x7E0, 0x7E1, 0x7E2, 0x7E4)]
    obd_resps = isotp_exchange_many(
      can_send, can_recv, [(ecu.tx_addr, ecu.rx_addr, b'\x04') for ecu in obd_targets],
      bus=bus, timeout_s=0.6, clock=clock,
    )
    for ecu in obd_targets:
      if obd_resps.get(ecu.rx_addr, b'')[:1] == b'\x44':
        if not any(item['ecuId'] == ecu.ecu_id for item in cleared):
          cleared.append({'ecuId': ecu.ecu_id, 'status': 'cleared_obd04'})

  return {
    'clearedAt': time.time(),
    'target': target_ecu.upper(),
    'clearedCount': len(cleared),
    'cleared': cleared,
    'errors': errors,
  }


class PassiveEcuMonitor:
  """Always-on passive CAN fault/health monitor (zero TX required)."""

  def __init__(self) -> None:
    self.active_warnings: dict[str, dict] = {}
    self.warning_history: list[dict] = []
    self._last_low_12v_seen = False

  def update(self, can_state: IoniqCanState, wall_time: float | None = None) -> list[dict]:
    """Compare current CAN flags with previous state and return newly raised fault events."""
    now = wall_time if wall_time is not None else time.time()
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
    if can_state.abs_defect or can_state.ebd_defect or can_state.abs_warn_lamp or can_state.ebd_warn_lamp:
      detected['ABS_EBD_WARN'] = ('ESC', 'critical', 'ABS/EBD Bremsensystem-Warnleuchte aktiv')
    if can_state.fca_fail_info > 0:
      detected['FCA_FAIL'] = ('SCC', 'warning', f'Frontkollisionswarnung / Radar-Störung (FCA_Failinfo={can_state.fca_fail_info})')
    if can_state.bat_12v_error or can_state.bat_12v_invalid:
      detected['BAT_12V_SENSOR'] = ('IBS', 'warning', '12V-Batteriesensor (BAT11) Fehler/Ungültig')
    low_12v = (can_state.bat_12v_voltage_v is not None and can_state.bat_12v_voltage_v < 11.7)
    if low_12v or self._last_low_12v_seen and (can_state.bat_12v_voltage_v is not None and can_state.bat_12v_voltage_v < 11.9):
      detected['BAT_12V_LOW'] = ('IBS', 'warning', f'12V-Batteriespannung niedrig ({can_state.bat_12v_voltage_v:.2f}V)')
    self._last_low_12v_seen = low_12v

    new_events: list[dict] = []
    # Clear resolved warnings
    for code in list(self.active_warnings):
      if code not in detected:
        self.active_warnings.pop(code, None)

    for code, (ecu_id, severity, message) in detected.items():
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


def queue_diagnostic_action(action_type: str, target: str = 'ALL', root: Path | str | None = None) -> dict:
  if action_type not in ('scan', 'clear'):
    raise ValueError('Unsupported diagnostic action')
  state = read_diagnostics_state(root)
  state['pendingAction'] = {
    'type': action_type,
    'target': target.upper(),
    'requestedAt': time.time(),
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
