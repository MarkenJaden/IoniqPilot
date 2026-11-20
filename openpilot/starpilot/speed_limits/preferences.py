"""Persistent user preferences for Speed Limit Controller (SLC) customization.

Stored as atomic JSON under ``starpilot_storage_root() / 'slc' / 'preferences.json'``
so no new entries are required in ``openpilot/common/params_keys.h`` (which is backed
by the prebuilt ``libparams_c.so``).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

UNLIMITED_OFF = 'off'
UNLIMITED_MOTORWAY = 'motorway'
UNLIMITED_COMBINED = 'combined'
UNLIMITED_MODES = (UNLIMITED_OFF, UNLIMITED_MOTORWAY, UNLIMITED_COMBINED)

UNLIMITED_DISPLAY_SIGN = 'zeichen282'
UNLIMITED_DISPLAY_HIDE = 'hide'
UNLIMITED_DISPLAY_MODES = (UNLIMITED_DISPLAY_SIGN, UNLIMITED_DISPLAY_HIDE)

ADVANCE_TIME = 'time'
ADVANCE_DISTANCE = 'distance'
ADVANCE_DYNAMIC = 'dynamic'
ADVANCE_MODES = (ADVANCE_TIME, ADVANCE_DISTANCE, ADVANCE_DYNAMIC)

ENGAGE_SET_CURRENT = 'current'
ENGAGE_SET_LIMIT = 'limit'
ENGAGE_SET_MODES = (ENGAGE_SET_LIMIT, ENGAGE_SET_CURRENT)

ENGAGE_RESUME_LAST_SET = 'last_set'
ENGAGE_RESUME_CURRENT = 'current'
ENGAGE_RESUME_LAST_OR_CURRENT = 'last_set_or_current'
ENGAGE_RESUME_MODES = (
  ENGAGE_RESUME_LAST_OR_CURRENT,
  ENGAGE_RESUME_CURRENT,
  ENGAGE_RESUME_LAST_SET,
)

ADVANCE_SECONDS_MAX = 25
ADVANCE_DISTANCE_M_MIN = 0
ADVANCE_DISTANCE_M_MAX = 500
ADVANCE_DECEL_MIN = 0.5
ADVANCE_DECEL_MAX = 3.0


@dataclass(frozen=True)
class SlcPreferences:
  unlimited_recognition: str = UNLIMITED_COMBINED
  unlimited_display: str = UNLIMITED_DISPLAY_SIGN
  stvo_fallback: bool = False
  cluster_effective_speed: bool = True
  step_override_enabled: bool = True
  advance_mode: str = ADVANCE_DYNAMIC
  advance_distance_m: int = 0
  advance_decel_mps2: float = 1.2
  engage_set_mode: str = ENGAGE_SET_LIMIT
  engage_resume_mode: str = ENGAGE_RESUME_LAST_OR_CURRENT


_CACHE: dict[str, tuple[float, int, int, SlcPreferences]] = {}


def preferences_path(root: Path | None = None) -> Path:
  if root is not None:
    return Path(root) / 'slc' / 'preferences.json'
  from openpilot.starpilot.storage import starpilot_storage_root
  return starpilot_storage_root() / 'slc' / 'preferences.json'


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
  try:
    parsed = int(round(float(value)))
  except (TypeError, ValueError):
    return default
  return max(low, min(high, parsed))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
  try:
    parsed = float(value)
  except (TypeError, ValueError):
    return default
  if parsed != parsed:  # NaN
    return default
  return round(max(low, min(high, parsed)), 2)


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
  text = str(value).strip().lower() if value is not None else ''
  return text if text in allowed else default


def _flag(value: Any, default: bool) -> bool:
  if isinstance(value, bool):
    return value
  if isinstance(value, (int, float)):
    return bool(value)
  if isinstance(value, str):
    lowered = value.strip().lower()
    if lowered in ('1', 'true', 'yes', 'on'):
      return True
    if lowered in ('0', 'false', 'no', 'off'):
      return False
  return default


def normalize_preferences(raw: dict[str, Any] | None) -> SlcPreferences:
  if not isinstance(raw, dict):
    return SlcPreferences()
  defaults = SlcPreferences()
  return SlcPreferences(
    unlimited_recognition=_choice(
      raw.get('unlimited_recognition'),
      UNLIMITED_MODES,
      defaults.unlimited_recognition,
    ),
    unlimited_display=_choice(
      raw.get('unlimited_display'),
      UNLIMITED_DISPLAY_MODES,
      defaults.unlimited_display,
    ),
    stvo_fallback=_flag(raw.get('stvo_fallback'), defaults.stvo_fallback),
    cluster_effective_speed=_flag(raw.get('cluster_effective_speed'), defaults.cluster_effective_speed),
    step_override_enabled=_flag(raw.get('step_override_enabled'), defaults.step_override_enabled),
    advance_mode=_choice(raw.get('advance_mode'), ADVANCE_MODES, defaults.advance_mode),
    advance_distance_m=_clamp_int(
      raw.get('advance_distance_m'),
      defaults.advance_distance_m,
      ADVANCE_DISTANCE_M_MIN,
      ADVANCE_DISTANCE_M_MAX,
    ),
    advance_decel_mps2=_clamp_float(
      raw.get('advance_decel_mps2'),
      defaults.advance_decel_mps2,
      ADVANCE_DECEL_MIN,
      ADVANCE_DECEL_MAX,
    ),
    engage_set_mode=_choice(raw.get('engage_set_mode'), ENGAGE_SET_MODES, defaults.engage_set_mode),
    engage_resume_mode=_choice(
      raw.get('engage_resume_mode'),
      ENGAGE_RESUME_MODES,
      defaults.engage_resume_mode,
    ),
  )


def load_preferences(root: Path | None = None) -> SlcPreferences:
  path = preferences_path(root)
  try:
    data = json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError, TypeError):
    return SlcPreferences()
  return normalize_preferences(data)


def load_preferences_cached(root: Path | None = None, *, min_check_interval_s: float = 0.5) -> SlcPreferences:
  path = preferences_path(root)
  key = str(path)
  now = time.monotonic()
  cached = _CACHE.get(key)
  if cached is not None and (now - cached[0]) < min_check_interval_s:
    return cached[3]
  try:
    st = path.stat()
    mtime_ns, size = int(st.st_mtime_ns), int(st.st_size)
  except OSError:
    prefs = SlcPreferences()
    _CACHE[key] = (now, -1, -1, prefs)
    return prefs
  if cached is not None and cached[1] == mtime_ns and cached[2] == size:
    _CACHE[key] = (now, mtime_ns, size, cached[3])
    return cached[3]
  prefs = load_preferences(root)
  _CACHE[key] = (now, mtime_ns, size, prefs)
  return prefs


def save_preferences(prefs: SlcPreferences, root: Path | None = None) -> SlcPreferences:
  normalized = normalize_preferences(asdict(prefs))
  path = preferences_path(root)
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = json.dumps(asdict(normalized), sort_keys=True, indent=2) + '\n'
  fd, tmp_name = tempfile.mkstemp(prefix='.preferences-', suffix='.tmp', dir=str(path.parent))
  try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
      handle.write(payload)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp_name, path)
  except BaseException:
    try:
      os.unlink(tmp_name)
    except OSError:
      pass
    raise
  _CACHE.pop(str(path), None)
  return normalized


def update_preferences(root: Path | None = None, **changes: Any) -> SlcPreferences:
  current = asdict(load_preferences(root))
  current.update(changes)
  return save_preferences(normalize_preferences(current), root=root)
