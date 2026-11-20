"""Raw CAN fuel-discovery logger and candidate counter ranker for 2016 Hyundai Ioniq Hybrid."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any

from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_json, write_json_atomic

_SAFE_ID = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
MAX_DISCOVERY_CAPTURES = 10
MAX_TIMELINE_SAMPLES = 600
MAX_TRACKED_IDS = 256

WATCHED_DISCOVERY_ADDRS: frozenset[int] = frozenset((
  0x18F, 0x200, 0x260, 0x316, 0x329, 0x366, 0x371, 0x372,
  0x50C, 0x52A, 0x545, 0x547, 0x549, 0x553, 0x559, 0x595, 0x596, 0x5B0,
))


def _utc_timestamp() -> float:
  return datetime.now(UTC).timestamp()


class CanFuelDiscoveryRecorder:
  """Records compact raw CAN frame timelines and ranks monotonic 16-bit fuel counter candidates."""

  def __init__(self, root: Path | str | None = None, *, sample_hz: float = 1.0) -> None:
    self.root = ioniq_storage_root(root) / 'discovery'
    self.root.mkdir(parents=True, exist_ok=True)
    self.min_sample_dt = 1.0 / max(0.2, sample_hz)
    self._session: dict[str, Any] | None = None

  @property
  def active(self) -> bool:
    return self._session is not None

  def _index_path(self) -> Path:
    return self.root / 'index.json'

  def list_captures(self) -> list[dict[str, Any]]:
    items = read_json(self._index_path(), [])
    return items if isinstance(items, list) else []

  def _save_index(self, items: list[dict[str, Any]]) -> None:
    write_json_atomic(self._index_path(), items)

  def live_status(self) -> dict[str, Any]:
    sess = self._session
    if sess is None:
      return {'active': False, 'id': None, 'label': None, 'startedAt': None, 'sampleCount': 0, 'uniqueCanIds': 0}
    return {
      'active': True,
      'id': sess['id'],
      'label': sess['label'],
      'startedAt': sess['startedAt'],
      'sampleCount': len(sess['timeline']),
      'uniqueCanIds': len(sess['trackers']),
    }

  def status(self, *, live_override: dict[str, Any] | None = None) -> dict[str, Any]:
    live = live_override if isinstance(live_override, dict) else self.live_status()
    return {
      'active': bool(live.get('active', False)),
      'id': live.get('id'),
      'label': live.get('label'),
      'startedAt': live.get('startedAt'),
      'sampleCount': int(live.get('sampleCount', 0)),
      'uniqueCanIds': int(live.get('uniqueCanIds', 0)),
      'captures': self.list_captures(),
    }

  def start(self, label: str = 'Verbrauchsanalyse', *, mono_s: float = 0.0,
            wall_time: float | None = None) -> dict[str, Any]:
    now = wall_time if wall_time is not None else _utc_timestamp()
    if self._session is not None:
      self.stop(wall_time=now)
    clean_label = (label or '').strip()[:64] or 'Verbrauchsanalyse'
    cap_id = f'disc_{int(now * 1000)}'
    self._session = {
      'id': cap_id,
      'label': clean_label,
      'startedAt': now,
      'startMonoS': mono_s,
      'lastTickMonoS': -1e9,
      'startDistanceKm': None,
      'lastDistanceKm': 0.0,
      'startModelLiters': None,
      'lastModelLiters': 0.0,
      'trackers': {},
      'latestByAddr': {},
      'timeline': deque(maxlen=MAX_TIMELINE_SAMPLES),
    }
    return self.live_status()

  def observe_frame(self, bus: int, address: int, data: bytes | bytearray | memoryview, *,
                    engine_firing: bool) -> None:
    sess = self._session
    if sess is None or bus not in (0, 1, 2) or not data:
      return
    raw = bytes(data)
    dlc = len(raw)
    key = (bus, address)
    trackers: dict[tuple[int, int], dict[str, Any]] = sess['trackers']
    entry = trackers.get(key)
    if entry is None:
      if len(trackers) >= MAX_TRACKED_IDS:
        return
      windows: dict[tuple[int, str], list[int]] = {}
      for off in range(max(0, dlc - 1)):
        windows[(off, 'le')] = [0, 0, 0, 0]  # [fwd, rej, ice_sum, ev_sum]
        windows[(off, 'be')] = [0, 0, 0, 0]
      hex_str = raw.hex()
      entry = {
        'bus': bus,
        'address': address,
        'dlc': dlc,
        'count': 1,
        'firstHex': hex_str,
        'lastHex': hex_str,
        'prevBytes': raw,
        'changedMask': [0] * dlc,
        'iceChangedMask': [0] * dlc,
        'evChangedMask': [0] * dlc,
        'windows': windows,
      }
      trackers[key] = entry
      prev_bus_entry = sess['latestByAddr'].get(address)
      if prev_bus_entry is None or bus == 0 or prev_bus_entry[0] != 0:
        sess['latestByAddr'][address] = (bus, hex_str)
      return

    entry['count'] += 1
    prev: bytes = entry['prevBytes']
    if raw == prev:
      return

    hex_str = raw.hex()
    entry['lastHex'] = hex_str
    prev_bus_entry = sess['latestByAddr'].get(address)
    if prev_bus_entry is None or bus == 0 or prev_bus_entry[0] != 0:
      sess['latestByAddr'][address] = (bus, hex_str)

    changed = entry['changedMask']
    ice_changed = entry['iceChangedMask']
    ev_changed = entry['evChangedMask']
    min_len = min(len(prev), dlc)
    if dlc > len(changed):
      extra = dlc - len(changed)
      changed.extend([0] * extra)
      ice_changed.extend([0] * extra)
      ev_changed.extend([0] * extra)
      entry['dlc'] = max(entry['dlc'], dlc)

    for idx in range(min_len):
      diff = prev[idx] ^ raw[idx]
      if diff:
        changed[idx] |= diff
        if engine_firing:
          ice_changed[idx] |= diff
        else:
          ev_changed[idx] |= diff

    windows = entry['windows']
    for off in range(max(0, min_len - 1)):
      for endian, byteorder in (('le', 'little'), ('be', 'big')):
        w_key = (off, endian)
        stats = windows.get(w_key)
        if stats is None:
          stats = [0, 0, 0, 0]
          windows[w_key] = stats
        prev_val = int.from_bytes(prev[off:off + 2], byteorder)  # type: ignore[arg-type]
        cur_val = int.from_bytes(raw[off:off + 2], byteorder)  # type: ignore[arg-type]
        delta = (cur_val - prev_val) & 0xFFFF
        if delta == 0:
          continue
        if delta <= 8000:
          stats[0] += 1
          if engine_firing:
            stats[2] += delta
          else:
            stats[3] += delta
        else:
          stats[1] += 1

    entry['prevBytes'] = raw

  def record_tick(self, can_state: IoniqCanState, fuel_snap: dict[str, Any] | None,
                  mono_s: float, *, wall_time: float | None = None) -> dict[str, Any] | None:
    sess = self._session
    if sess is None or (mono_s - sess['lastTickMonoS']) < self.min_sample_dt:
      return None
    sess['lastTickMonoS'] = mono_s
    now = wall_time if wall_time is not None else _utc_timestamp()

    active_trip = (fuel_snap or {}).get('activeTrip') if isinstance(fuel_snap, dict) else None
    src_diag = (fuel_snap or {}).get('sourceDiagnostics') if isinstance(fuel_snap, dict) else None
    trip_dist = float(active_trip.get('distanceKm', 0.0)) if isinstance(active_trip, dict) else 0.0
    model_liters = float(
      src_diag.get('modelLiters')
      if isinstance(src_diag, dict) and src_diag.get('modelLiters') is not None
      else (active_trip.get('fuelLitersModel', 0.0) if isinstance(active_trip, dict) else 0.0)
    )
    active_liters = float(active_trip.get('fuelLiters', 0.0)) if isinstance(active_trip, dict) else 0.0

    if sess['startDistanceKm'] is None:
      sess['startDistanceKm'] = trip_dist
    sess['lastDistanceKm'] = trip_dist
    if sess['startModelLiters'] is None:
      sess['startModelLiters'] = model_liters
    sess['lastModelLiters'] = model_liters

    # Prioritize watched fuel/cluster/HEV IDs + top 20 dynamically changing IDs (by ICE bit activity)
    dynamic_addrs = sorted(
      (
        (sum(m.bit_count() for m in entry['iceChangedMask']), entry['address'])
        for entry in sess['trackers'].values()
        if entry['address'] not in WATCHED_DISCOVERY_ADDRS and any(entry['changedMask'])
      ),
      reverse=True,
    )
    selected_addrs: list[int] = sorted(WATCHED_DISCOVERY_ADDRS)
    seen_sel = set(selected_addrs)
    added_dynamic = 0
    for _score, addr in dynamic_addrs:
      if addr in seen_sel:
        continue
      selected_addrs.append(addr)
      seen_sel.add(addr)
      added_dynamic += 1
      if added_dynamic >= 20:
        break

    frames_out: dict[str, str] = {}
    latest_by_addr: dict[int, tuple[int, str]] = sess['latestByAddr']
    for addr in selected_addrs:
      pair = latest_by_addr.get(addr)
      if pair is not None:
        frames_out[f'0x{addr:03X}'] = pair[1]

    row = {
      't': round(now, 3),
      'monoS': round(mono_s, 3),
      'speedKph': round(can_state.best_speed_kph(), 2),
      'rpm': round(can_state.engine_rpm, 1),
      'torquePct': round(max(can_state.engine_torque_pct, can_state.tqi_b_pct), 1),
      'engineFiring': can_state.engine_is_firing(),
      'coolantC': None if can_state.coolant_temp_c is None else round(can_state.coolant_temp_c, 1),
      'odometerKm': can_state.odometer_km,
      'fuelDispLvl': can_state.fuel_disp_level,
      'dteKm': can_state.dte_km,
      'modelFuelLiters': round(model_liters, 5),
      'activeTripLiters': round(active_liters, 5),
      'frames': frames_out,
    }
    sess['timeline'].append(row)
    return row

  def stop(self, *, dashboard_l_per_100km: float | None = None,
           dashboard_distance_km: float | None = None, notes: str = '',
           wall_time: float | None = None) -> dict[str, Any] | None:
    sess = self._session
    if sess is None:
      return None
    self._session = None
    now = wall_time if wall_time is not None else _utc_timestamp()

    captured_dist = max(0.0, float(sess['lastDistanceKm']) - float(sess['startDistanceKm'] or 0.0))
    captured_model_liters = max(0.0, float(sess['lastModelLiters']) - float(sess['startModelLiters'] or 0.0))

    ref_dist = float(dashboard_distance_km) if (dashboard_distance_km is not None and dashboard_distance_km > 0.0) else captured_dist
    ref_liters: float | None = None
    if dashboard_l_per_100km is not None and dashboard_l_per_100km > 0.0 and ref_dist > 0.05:
      ref_liters = round(dashboard_l_per_100km * ref_dist / 100.0, 5)
    elif captured_model_liters > 0.0:
      ref_liters = round(captured_model_liters, 5)

    candidates: list[dict[str, Any]] = []
    id_summaries: list[dict[str, Any]] = []
    for (_bus, _addr), entry in sorted(sess['trackers'].items(), key=lambda kv: (kv[0][0], kv[0][1])):
      id_summaries.append({
        'bus': entry['bus'],
        'addressHex': f'0x{entry["address"]:03X}',
        'dlc': entry['dlc'],
        'count': entry['count'],
        'firstHex': entry['firstHex'],
        'lastHex': entry['lastHex'],
        'changedMaskHex': bytes(entry['changedMask']).hex(),
        'iceChangedMaskHex': bytes(entry['iceChangedMask']).hex(),
        'evChangedMaskHex': bytes(entry['evChangedMask']).hex(),
      })

      ice_mask = entry['iceChangedMask']
      for (off, endian), (fwd, rej, ice_sum, ev_sum) in entry['windows'].items():
        if fwd < 5 or rej > fwd * 0.05:
          continue
        if ice_sum <= ev_sum * 3:
          continue
        lsb_idx = off if endian == 'le' else off + 1
        if lsb_idx >= len(ice_mask) or ice_mask[lsb_idx] == 0:
          continue
        est_ul = (ref_liters * 1e6 / ice_sum) if (ref_liters is not None and ref_liters > 0.0 and ice_sum > 0) else None
        plausible_bonus = 1000.0 if (est_ul is not None and 0.005 <= est_ul <= 5.0) else 0.0
        ratio_score = ice_sum / max(1.0, float(ev_sum))
        cleanliness = fwd / max(1.0, float(fwd + rej * 10))
        rank_score = plausible_bonus + ratio_score * cleanliness
        candidates.append({
          'bus': entry['bus'],
          'addressHex': f'0x{entry["address"]:03X}',
          'byteOffset': off,
          'bitStart': off * 8,
          'length': 16,
          'endian': endian,
          'forwardSteps': fwd,
          'rejectSteps': rej,
          'iceDeltaSum': ice_sum,
          'evDeltaSum': ev_sum,
          'estimatedUlPerTick': None if est_ul is None else round(est_ul, 6),
          '_rank': rank_score,
        })

    candidates.sort(key=lambda item: (-item['_rank'], item['rejectSteps'], -item['iceDeltaSum']))
    top_candidates = [{k: v for k, v in cand.items() if k != '_rank'} for cand in candidates[:15]]

    timeline = list(sess['timeline'])
    duration_s = round(max(0.0, now - float(sess['startedAt'])), 1)
    summary = {
      'id': sess['id'],
      'label': sess['label'],
      'startedAt': sess['startedAt'],
      'endedAt': now,
      'durationS': duration_s,
      'sampleCount': len(timeline),
      'uniqueCanIds': len(id_summaries),
      'topCandidate': top_candidates[0] if top_candidates else None,
      'dashboardReference': {
        'dashboardLPer100Km': dashboard_l_per_100km,
        'dashboardDistanceKm': dashboard_distance_km,
        'capturedDistanceKm': round(captured_dist, 3),
        'capturedModelLiters': round(captured_model_liters, 5),
        'referenceLiters': ref_liters,
        'notes': (notes or '').strip()[:160],
      },
    }
    bundle = dict(summary)
    bundle['topFuelCandidates'] = top_candidates
    bundle['idSummaries'] = id_summaries
    bundle['timeline'] = timeline

    write_json_atomic(self.root / f'{sess["id"]}.json', bundle)

    index = [item for item in self.list_captures() if item.get('id') != sess['id']]
    index.insert(0, summary)
    while len(index) > MAX_DISCOVERY_CAPTURES:
      victim = index.pop()
      victim_id = str(victim.get('id', ''))
      if _SAFE_ID.fullmatch(victim_id):
        try:
          (self.root / f'{victim_id}.json').unlink(missing_ok=True)
        except OSError:
          pass
    self._save_index(index)
    return summary

  def flush(self) -> dict[str, Any] | None:
    if self._session is not None:
      return self.stop()
    return None

  def read_capture(self, cap_id: str) -> dict[str, Any] | None:
    if not _SAFE_ID.fullmatch(cap_id):
      return None
    data = read_json(self.root / f'{cap_id}.json', None)
    return data if isinstance(data, dict) else None

  def delete_capture(self, cap_id: str) -> bool:
    if not _SAFE_ID.fullmatch(cap_id):
      return False
    index = self.list_captures()
    new_index = [item for item in index if item.get('id') != cap_id]
    if len(new_index) == len(index) and not (self.root / f'{cap_id}.json').exists():
      return False
    try:
      (self.root / f'{cap_id}.json').unlink(missing_ok=True)
    except OSError:
      pass
    self._save_index(new_index)
    return True
