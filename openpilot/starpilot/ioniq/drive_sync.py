"""Background sync of Ioniq consumption/diagnostics, GPS coords, and route metadata to MarkenJaden Drive."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from openpilot.starpilot.ioniq.can_discovery import CanFuelDiscoveryRecorder
from openpilot.starpilot.ioniq.fuel_tracker import FuelTripTracker
from openpilot.starpilot.ioniq.recorder import EventFlightRecorder
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_config, read_json, write_json_atomic
from openpilot.starpilot.ioniq.uds_dtc import read_diagnostics_state

_json_dumps = json.dumps


class _UrllibResponse:
  def __init__(self, status_code: int) -> None:
    self.status_code = status_code


class _UrllibJsonSession:
  """Minimal stdlib JSON HTTP client fallback when requests is unavailable."""

  def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None, timeout: float = 10.0) -> _UrllibResponse:
    body = _json_dumps(json, ensure_ascii=False).encode('utf-8') if json is not None else b'{}'
    req_headers = {'Content-Type': 'application/json'}
    if headers:
      req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method='POST')
    try:
      with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _UrllibResponse(int(getattr(resp, 'status', 200)))
    except urllib.error.HTTPError as err:
      return _UrllibResponse(int(err.code))


def _default_session() -> Any:
  try:
    import requests
    return requests.Session()
  except ImportError:
    return _UrllibJsonSession()

DRIVE_PROVIDER_NAMES = ('drive', 'custom')
MAX_QUEUE_ITEMS = 200
MAX_RECORDING_BUNDLE_BYTES = 512 * 1024
MAX_BUNDLES_PER_SYNC = 3
MAX_SYNCED_RECORDING_IDS = 300
_DONGLE_RE = re.compile(r'^[a-f0-9]{16}$')
_SEGMENT_DIR_RE = re.compile(r'^(?P<route>[a-f0-9]{8}--[a-f0-9]{10}|[0-9]{4}-[0-9]{2}-[0-9]{2}--[0-9]{2}-[0-9]{2}-[0-9]{2})--(?P<seg>[0-9]+)$')


def _default_provider() -> Any:
  from openpilot.starpilot.connect.provider import active_provider
  return active_provider()


def _default_dongle_id() -> str | None:
  from openpilot.common.params import Params
  return Params().get('DongleId')


def _default_log_root() -> Path:
  from openpilot.common.hardware.hw import Paths
  return Path(Paths.log_root())


def _default_uplink_active() -> bool:
  from openpilot.starpilot.galaxy.companion import phone_uplink_active
  return phone_uplink_active()


def _default_token(dongle_id: str) -> str:
  from openpilot.common.api import Api
  return Api(dongle_id).get_token()


def _default_build_metadata() -> dict[str, str]:
  try:
    from openpilot.common.version import get_build_metadata
    meta = get_build_metadata()
    return {
      'version': str(meta.openpilot.version or ''),
      'branch': str(meta.channel or ''),
      'commit': str(meta.openpilot.git_commit or ''),
    }
  except Exception:
    return {}


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
  r = 6_371_000.0
  phi1, phi2 = math.radians(lat1), math.radians(lat2)
  dphi = math.radians(lat2 - lat1)
  dlambda = math.radians(lng2 - lng1)
  a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
  return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def find_active_segment(log_root: Path) -> tuple[str, int] | None:
  """Locate the currently locked loggerd segment `(route_id, segment_num)`."""
  try:
    if not log_root.is_dir():
      return None
    best: tuple[float, str, int] | None = None
    with os.scandir(log_root) as entries:
      for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
          continue
        matched = _SEGMENT_DIR_RE.fullmatch(entry.name)
        if matched is None:
          continue
        lock_path = Path(entry.path) / 'rlog.lock'
        if not lock_path.exists():
          continue
        try:
          mtime = lock_path.stat().st_mtime
        except OSError:
          mtime = 0.0
        route_id = matched.group('route')
        seg_num = int(matched.group('seg'))
        if best is None or (mtime, seg_num) > (best[0], best[2]):
          best = (mtime, route_id, seg_num)
    return (best[1], best[2]) if best is not None else None
  except OSError:
    return None


class SegmentTelemetryCollector:
  """Accumulates 1Hz GPS coordinates and selfdrive state events per active route segment."""

  def __init__(self, *, log_root: Path | None = None) -> None:
    self._log_root = log_root
    self._lock = threading.Lock()
    self._active_route: str | None = None
    self._active_seg: int | None = None
    self._route_start_mono_s: float = 0.0
    self._route_start_wall_ms: int = 0
    self._seg_start_wall_ms: int = 0
    self._last_wall_ms: int = 0
    self._last_coord_sec: int = -1
    self._last_gps_pos: tuple[float, float] | None = None
    self._latest_location: dict[str, Any] | None = None
    self._coords: list[dict[str, Any]] = []
    self._events: list[dict[str, Any]] = []
    self._distance_m: float = 0.0
    self._last_state_sig: tuple[bool, str, str] | None = None
    self._last_seg_scan_mono: float = -1e9
    self._cached_seg: tuple[str, int] | None = None

  def _scan_active_segment(self, mono_s: float) -> tuple[str, int] | None:
    if (mono_s - self._last_seg_scan_mono) >= 2.0:
      self._last_seg_scan_mono = mono_s
      root = self._log_root if self._log_root is not None else _default_log_root()
      self._cached_seg = find_active_segment(root)
    return self._cached_seg

  def observe(
    self,
    *,
    mono_s: float,
    wall_time: float,
    gps_msg: Any | None = None,
    selfdrive_msg: Any | None = None,
    active_segment: tuple[str, int] | None = None,
    active_trip_summary: dict[str, Any] | None = None,
  ) -> list[dict[str, Any]]:
    """Record live telemetry and return any finalized segment metadata ready for upload."""
    wall_ms = int(wall_time * 1000)
    completed: list[dict[str, Any]] = []
    detected = active_segment if active_segment is not None else self._scan_active_segment(mono_s)

    with self._lock:
      if gps_msg is not None:
        loc = self._extract_location(gps_msg, wall_time)
        if loc is not None:
          self._latest_location = loc

      if detected != (self._active_route, self._active_seg):
        if self._active_route is not None and self._active_seg is not None:
          completed.append(self._build_payload_locked(active_trip_summary, wall_ms))
        if detected is not None:
          route_id, seg_num = detected
          if route_id != self._active_route:
            self._route_start_mono_s = mono_s - (seg_num * 60.0)
            self._route_start_wall_ms = wall_ms - int(seg_num * 60_000)
          self._active_route = route_id
          self._active_seg = seg_num
          self._seg_start_wall_ms = wall_ms
          self._last_wall_ms = wall_ms
          self._last_coord_sec = -1
          self._last_gps_pos = None
          self._coords = []
          self._events = []
          self._distance_m = 0.0
          self._last_state_sig = None
        else:
          self._reset_active_locked()

      if self._active_route is not None and self._active_seg is not None:
        self._last_wall_ms = max(self._last_wall_ms, wall_ms)
        route_offset_s = max(0, int(round(mono_s - self._route_start_mono_s)))
        route_offset_ms = max(0, int(round((mono_s - self._route_start_mono_s) * 1000.0)))

        if gps_msg is not None:
          loc = self._extract_location(gps_msg, wall_time)
          if loc is not None and route_offset_s > self._last_coord_sec:
            if self._last_gps_pos is not None:
              step_m = _haversine_meters(self._last_gps_pos[0], self._last_gps_pos[1], loc['lat'], loc['lng'])
              if 0.0 <= step_m <= 150.0:
                self._distance_m += step_m
            self._last_gps_pos = (loc['lat'], loc['lng'])
            self._last_coord_sec = route_offset_s
            self._coords.append({
              't': route_offset_s,
              'lat': round(loc['lat'], 7),
              'lng': round(loc['lng'], 7),
              'speed': round(loc['speed'], 2),
              'dist': round(self._distance_m, 1),
            })

        if selfdrive_msg is not None:
          enabled = bool(getattr(selfdrive_msg, 'enabled', False))
          state_name = str(getattr(selfdrive_msg, 'state', 'disabled'))
          alert_status = str(getattr(selfdrive_msg, 'alertStatus', 'normal'))
          sig = (enabled, state_name, alert_status)
          if sig != self._last_state_sig:
            self._last_state_sig = sig
            self._events.append({
              'type': 'state',
              'route_offset_millis': route_offset_ms,
              'route_offset_nanos': 0,
              'data': {
                'enabled': enabled,
                'state': state_name,
                'alertStatus': alert_status,
              },
            })

    return completed

  def flush_active(self, active_trip_summary: dict[str, Any] | None = None, *, wall_time: float | None = None) -> dict[str, Any] | None:
    now_ms = int((wall_time if wall_time is not None else datetime.now(UTC).timestamp()) * 1000)
    with self._lock:
      if self._active_route is None or self._active_seg is None:
        return None
      payload = self._build_payload_locked(active_trip_summary, now_ms)
      self._reset_active_locked()
      return payload

  def latest_location(self) -> dict[str, Any] | None:
    with self._lock:
      return dict(self._latest_location) if self._latest_location is not None else None

  def _reset_active_locked(self) -> None:
    self._active_route = None
    self._active_seg = None
    self._coords = []
    self._events = []
    self._distance_m = 0.0
    self._last_gps_pos = None
    self._last_state_sig = None

  def _build_payload_locked(self, active_trip_summary: dict[str, Any] | None, wall_ms: int) -> dict[str, Any]:
    end_ms = max(self._seg_start_wall_ms + 1000, self._last_wall_ms, wall_ms)
    first_coord = self._coords[0] if self._coords else None
    last_coord = self._coords[-1] if self._coords else None
    payload: dict[str, Any] = {
      'route_id': self._active_route,
      'segment_num': self._active_seg,
      'start_time_utc_millis': self._seg_start_wall_ms,
      'end_time_utc_millis': end_ms,
      'distance_meters': round(self._distance_m, 1),
      'coords': list(self._coords),
      'events': list(self._events),
    }
    if first_coord is not None and last_coord is not None:
      payload['start_lat'] = first_coord['lat']
      payload['start_lng'] = first_coord['lng']
      payload['end_lat'] = last_coord['lat']
      payload['end_lng'] = last_coord['lng']
    if active_trip_summary:
      payload['ioniq_trip_summary'] = dict(active_trip_summary)
    return payload

  @staticmethod
  def _extract_location(gps_msg: Any, wall_time: float) -> dict[str, Any] | None:
    try:
      lat = float(gps_msg.latitude)
      lng = float(gps_msg.longitude)
      if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
      if lat == 0.0 and lng == 0.0:
        return None
      if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
      speed = float(getattr(gps_msg, 'speed', 0.0) or 0.0)
      bearing = float(getattr(gps_msg, 'bearingDeg', 0.0) or 0.0)
      ts_ms = int(getattr(gps_msg, 'unixTimestampMillis', 0) or 0)
      ts_s = int(ts_ms / 1000) if ts_ms > 1_000_000_000_000 else int(wall_time)
      return {
        'lat': round(lat, 6),
        'lng': round(lng, 6),
        'speed': round(max(0.0, speed), 2) if math.isfinite(speed) else 0.0,
        'bearing': round(bearing % 360.0, 1) if math.isfinite(bearing) else 0.0,
        'time': ts_s,
      }
    except Exception:
      return None


def summarize_trip(trip: dict[str, Any] | None) -> dict[str, Any] | None:
  if not isinstance(trip, dict):
    return None
  return {
    'id': trip.get('id'),
    'distanceKm': trip.get('distanceKm'),
    'fuelLiters': trip.get('fuelLiters'),
    'avgLPer100Km': trip.get('avgLPer100Km'),
    'evDistancePct': trip.get('evDistancePct'),
    'evTimePct': trip.get('evTimePct'),
    'estimatedCostEur': trip.get('estimatedCostEur'),
    'sourceMode': trip.get('sourceMode'),
  }


class DriveSyncClient:
  """Synchronizes Ioniq telemetry snapshots and segment route metadata with MarkenJaden Drive."""

  def __init__(
    self,
    *,
    root: Path | str | None = None,
    session: Any | None = None,
    provider_getter: Callable[[], Any] = _default_provider,
    dongle_id_getter: Callable[[], str | None] = _default_dongle_id,
    token_provider: Callable[[str], str] = _default_token,
    uplink_check: Callable[[], bool] = _default_uplink_active,
    build_metadata_getter: Callable[[], dict[str, str]] = _default_build_metadata,
  ) -> None:
    self.root = ioniq_storage_root(root)
    self._session = session if session is not None else _default_session()
    self._provider_getter = provider_getter
    self._dongle_id_getter = dongle_id_getter
    self._token_provider = token_provider
    self._uplink_check = uplink_check
    self._build_metadata_getter = build_metadata_getter
    self._state_path = self.root / 'drive_sync_state.json'
    self._queue_path = self.root / 'drive_sync_queue.json'
    self._lock = threading.Lock()
    self._last_snapshot_hash: str | None = None

  def _resolve_target(self) -> tuple[str, str] | None:
    try:
      provider = self._provider_getter()
    except Exception:
      return None
    if getattr(provider, 'name', None) not in DRIVE_PROVIDER_NAMES:
      return None
    api_url = str(getattr(provider, 'api', '') or '').strip().rstrip('/')
    if not api_url.startswith('https://'):
      return None
    dongle_id = self._dongle_id_getter()
    if not isinstance(dongle_id, str) or not _DONGLE_RE.fullmatch(dongle_id):
      return None
    if self._uplink_check():
      return None
    return api_url, dongle_id

  def _auth_headers(self, dongle_id: str) -> dict[str, str]:
    token = self._token_provider(dongle_id)
    return {
      'Authorization': f'JWT {token}',
      'Content-Type': 'application/json',
    }

  def _read_sync_state(self) -> dict[str, Any]:
    state = read_json(self._state_path, {'syncedRecordingIds': []})
    if not isinstance(state, dict):
      state = {'syncedRecordingIds': []}
    if not isinstance(state.get('syncedRecordingIds'), list):
      state['syncedRecordingIds'] = []
    return state

  def enqueue_route_meta(self, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not payload.get('route_id') or payload.get('segment_num') is None:
      return
    with self._lock:
      queue = read_json(self._queue_path, [])
      if not isinstance(queue, list):
        queue = []
      key = (payload.get('route_id'), payload.get('segment_num'))
      queue = [item for item in queue if (item.get('route_id'), item.get('segment_num')) != key]
      queue.append(payload)
      write_json_atomic(self._queue_path, queue[-MAX_QUEUE_ITEMS:])

  def flush_route_meta_queue(self, *, timeout_s: float = 10.0) -> int:
    target = self._resolve_target()
    if target is None:
      return 0
    api_url, dongle_id = target

    with self._lock:
      queue = read_json(self._queue_path, [])
      if not isinstance(queue, list) or not queue:
        return 0
      pending = list(queue)

    sent = 0
    remaining: list[dict[str, Any]] = []
    try:
      headers = self._auth_headers(dongle_id)
    except Exception:
      return 0

    url = f'{api_url}/v1/devices/{dongle_id}/route_meta'
    for idx, item in enumerate(pending):
      try:
        resp = self._session.post(url, json=item, headers=headers, timeout=timeout_s)
        if 200 <= resp.status_code < 300:
          sent += 1
        elif 400 <= resp.status_code < 500:
          # Drop invalid/malformed item rather than wedging the queue forever
          continue
        else:
          remaining = pending[idx:]
          break
      except Exception:
        remaining = pending[idx:]
        break

    with self._lock:
      latest_queue = read_json(self._queue_path, [])
      if not isinstance(latest_queue, list):
        latest_queue = []
      sent_keys = {(item.get('route_id'), item.get('segment_num')) for item in pending[:sent]}
      merged = [item for item in remaining]
      seen = {(item.get('route_id'), item.get('segment_num')) for item in merged}
      for item in latest_queue:
        key = (item.get('route_id'), item.get('segment_num'))
        if key not in sent_keys and key not in seen:
          merged.append(item)
          seen.add(key)
      write_json_atomic(self._queue_path, merged[-MAX_QUEUE_ITEMS:])
    return sent

  def build_ioniq_payload(self, *, location: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str], str]:
    cfg = read_config(self.root)
    diag = read_diagnostics_state(self.root)
    if (self.root / 'fuel.json').exists():
      fuel = FuelTripTracker(
        self.root,
        fco_scale_ul=float(cfg.get('fcoScaleUlPerTick', 0.128)),
        price_per_liter=float(cfg.get('fuelPriceEurPerLiter', 1.75)),
        tank_capacity_liters=float(cfg.get('tankCapacityLiters', 45.0)),
      ).snapshot()
    else:
      fuel = {
        'activeTrip': None,
        'trips': [],
        'currentTank': None,
        'tankHistory': [],
        'refuelLog': [],
        'refuelAlert': None,
        'calibrationFactor': float(cfg.get('fuelCalibrationFactor', 1.0)),
        'lifetimeTankAvgLPer100Km': None,
        'lifetimeCostPer100Km': None,
        'totalLoggedLiters': 0.0,
        'totalLoggedCostEur': 0.0,
        'sourceDiagnostics': {'activeSource': 'hev_model', 'calibrationFactor': float(cfg.get('fuelCalibrationFactor', 1.0))},
      }
    recorder = EventFlightRecorder(self.root)
    rec_status = recorder.status()
    discovery = CanFuelDiscoveryRecorder(self.root).status(live_override=diag.get('discoveryStatus'))

    sync_state = self._read_sync_state()
    synced_ids = set(str(x) for x in sync_state.get('syncedRecordingIds', []))
    new_bundles: list[dict[str, Any]] = []
    bundle_ids: list[str] = []
    for entry in rec_status.get('recordings', []):
      rec_id = str(entry.get('id', ''))
      if not rec_id or rec_id in synced_ids:
        continue
      bundle = recorder.read_recording(rec_id)
      if bundle is None:
        continue
      encoded_len = len(json.dumps(bundle, ensure_ascii=False).encode('utf-8'))
      if encoded_len <= MAX_RECORDING_BUNDLE_BYTES:
        new_bundles.append(bundle)
        bundle_ids.append(rec_id)
      if len(new_bundles) >= MAX_BUNDLES_PER_SYNC:
        break

    payload: dict[str, Any] = {
      'schemaVersion': 1,
      'syncedAt': datetime.now(UTC).timestamp(),
      'config': cfg,
      'fuel': fuel,
      'diagnostics': diag,
      'recordings': rec_status.get('recordings', []),
      'discovery': {
        'active': discovery.get('active', False),
        'captures': discovery.get('captures', [])[:20],
      },
      'openpilot': self._build_metadata_getter(),
    }
    if location is not None:
      payload['location'] = location
    if new_bundles:
      payload['newRecordingBundles'] = new_bundles

    hash_basis = dict(payload)
    hash_basis.pop('syncedAt', None)
    hash_basis.pop('newRecordingBundles', None)
    digest = hashlib.sha256(json.dumps(hash_basis, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return payload, bundle_ids, digest

  def sync_ioniq_state(self, *, location: dict[str, Any] | None = None, force: bool = False, timeout_s: float = 10.0) -> bool:
    target = self._resolve_target()
    if target is None:
      return False
    api_url, dongle_id = target

    payload, bundle_ids, digest = self.build_ioniq_payload(location=location)
    if not force and digest == self._last_snapshot_hash and not bundle_ids:
      return False

    try:
      headers = self._auth_headers(dongle_id)
      resp = self._session.post(f'{api_url}/v1/devices/{dongle_id}/ioniq/sync', json=payload, headers=headers, timeout=timeout_s)
      if not (200 <= resp.status_code < 300):
        return False
    except Exception:
      return False

    self._last_snapshot_hash = digest
    if bundle_ids:
      with self._lock:
        sync_state = self._read_sync_state()
        existing = [str(x) for x in sync_state.get('syncedRecordingIds', [])]
        for rid in bundle_ids:
          if rid not in existing:
            existing.append(rid)
        sync_state['syncedRecordingIds'] = existing[-MAX_SYNCED_RECORDING_IDS:]
        write_json_atomic(self._state_path, sync_state)
    return True


class DriveSyncWorker:
  """Non-blocking background worker coordinating route metadata and Ioniq telemetry sync."""

  def __init__(
    self,
    *,
    client: DriveSyncClient | None = None,
    collector: SegmentTelemetryCollector | None = None,
    onroad_interval_s: float = 30.0,
    offroad_interval_s: float = 120.0,
  ) -> None:
    self.client = client or DriveSyncClient()
    self.collector = collector or SegmentTelemetryCollector()
    self.onroad_interval_s = onroad_interval_s
    self.offroad_interval_s = offroad_interval_s
    self._stop = threading.Event()
    self._wake = threading.Event()
    self._state_lock = threading.Lock()
    self._force_ioniq_sync = False
    self._is_onroad = False
    self._last_trip_summary: dict[str, Any] | None = None
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    if self._thread is not None and self._thread.is_alive():
      return
    self._thread = threading.Thread(target=self._run, name='ioniq_drive_sync', daemon=True)
    self._thread.start()

  def stop(self, timeout_s: float = 3.0) -> None:
    flushed = self.collector.flush_active(self._last_trip_summary)
    if flushed is not None:
      self.client.enqueue_route_meta(flushed)
    self._stop.set()
    self._wake.set()
    if self._thread is not None and self._thread.is_alive():
      self._thread.join(timeout=timeout_s)
    # Best-effort synchronous final flush when shutting down
    self.client.flush_route_meta_queue(timeout_s=2.5)
    self.client.sync_ioniq_state(location=self.collector.latest_location(), force=True, timeout_s=2.5)

  def request_sync(self, *, force: bool = False) -> None:
    if force:
      with self._state_lock:
        self._force_ioniq_sync = True
    self._wake.set()

  def observe_step(
    self,
    *,
    mono_s: float,
    wall_time: float,
    onroad: bool,
    fuel_snap: dict[str, Any] | None = None,
    gps_msg: Any | None = None,
    selfdrive_msg: Any | None = None,
  ) -> None:
    active_trip = (fuel_snap or {}).get('activeTrip')
    trip_summary = summarize_trip(active_trip)
    with self._state_lock:
      was_onroad = self._is_onroad
      self._is_onroad = onroad
      if trip_summary is not None:
        self._last_trip_summary = trip_summary
      if was_onroad and not onroad:
        self._force_ioniq_sync = True

    completed = self.collector.observe(
      mono_s=mono_s,
      wall_time=wall_time,
      gps_msg=gps_msg,
      selfdrive_msg=selfdrive_msg,
      active_trip_summary=trip_summary or self._last_trip_summary,
    )
    if completed:
      for item in completed:
        self.client.enqueue_route_meta(item)
      self._wake.set()
    elif was_onroad and not onroad:
      flushed = self.collector.flush_active(self._last_trip_summary, wall_time=wall_time)
      if flushed is not None:
        self.client.enqueue_route_meta(flushed)
      self._wake.set()

  def _run(self) -> None:
    last_ioniq_mono = -1e9
    backoff_s = 5.0
    while not self._stop.is_set():
      now = time.monotonic()
      with self._state_lock:
        onroad = self._is_onroad
        force = self._force_ioniq_sync
        self._force_ioniq_sync = False

      sent_meta = self.client.flush_route_meta_queue()
      interval = self.onroad_interval_s if onroad else self.offroad_interval_s
      did_sync = False
      if force or (now - last_ioniq_mono) >= interval:
        did_sync = self.client.sync_ioniq_state(location=self.collector.latest_location(), force=force)
        last_ioniq_mono = now

      if sent_meta > 0 or did_sync:
        backoff_s = min(interval, 15.0)
      else:
        backoff_s = min(interval, backoff_s * 1.5)

      self._wake.wait(timeout=min(interval, backoff_s))
      self._wake.clear()
