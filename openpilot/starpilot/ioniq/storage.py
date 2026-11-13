"""Atomic JSON file persistence under StarPilot storage (no prebuilt libparams_c.so key additions needed)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
  'scanOnStart': True,
  'passiveMonitorEnabled': True,
  'recorderEnabled': True,
  'recorderAutoOnFault': True,
  'recorderPreSeconds': 30.0,
  'recorderPostSeconds': 10.0,
  'tankCapacityLiters': 45.0,
  'fuelPriceEurPerLiter': 1.75,
  'fuelCalibrationFactor': 1.0,
  'fcoScaleUlPerTick': 0.128,
}


def ioniq_storage_root(root: Path | str | None = None) -> Path:
  """Return the writable Ioniq custom state directory."""
  if root is not None:
    target = Path(root)
  else:
    try:
      from openpilot.starpilot.storage import starpilot_storage_root
      target = starpilot_storage_root() / 'ioniq'
    except (ImportError, OSError, RuntimeError, ValueError):
      env_root = os.environ.get('IONIQ_STORAGE_ROOT')
      target = Path(env_root) if env_root else (Path.home() / '.starpilot' / 'ioniq')
  target.mkdir(parents=True, exist_ok=True)
  return target


def read_json(path: Path, default: Any = None) -> Any:
  """Read JSON file if valid, otherwise return a fresh copy of default."""
  if not path.exists():
    return default() if callable(default) else _copy_default(default)
  try:
    return json.loads(path.read_text(encoding='utf-8'))
  except (OSError, ValueError, TypeError, UnicodeDecodeError):
    return default() if callable(default) else _copy_default(default)


def write_json_atomic(path: Path, payload: Any) -> Path:
  """Write JSON atomically via same-directory temporary file and os.replace."""
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
  try:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)
  finally:
    try:
      tmp.unlink(missing_ok=True)
    except OSError:
      pass
  return path


def clamp_config(raw: dict[str, Any] | None) -> dict[str, Any]:
  """Normalize and bound persisted Ioniq configuration."""
  cfg = dict(DEFAULT_CONFIG)
  if isinstance(raw, dict):
    for key in ('scanOnStart', 'passiveMonitorEnabled', 'recorderEnabled', 'recorderAutoOnFault'):
      if isinstance(raw.get(key), bool):
        cfg[key] = raw[key]
    cfg['recorderPreSeconds'] = _clamp_float(raw.get('recorderPreSeconds'), cfg['recorderPreSeconds'], 5.0, 120.0)
    cfg['recorderPostSeconds'] = _clamp_float(raw.get('recorderPostSeconds'), cfg['recorderPostSeconds'], 2.0, 60.0)
    cfg['tankCapacityLiters'] = _clamp_float(raw.get('tankCapacityLiters'), cfg['tankCapacityLiters'], 20.0, 90.0)
    cfg['fuelPriceEurPerLiter'] = _clamp_float(raw.get('fuelPriceEurPerLiter'), cfg['fuelPriceEurPerLiter'], 0.5, 5.0)
    cfg['fuelCalibrationFactor'] = _clamp_float(raw.get('fuelCalibrationFactor'), cfg['fuelCalibrationFactor'], 0.65, 1.45)
    cfg['fcoScaleUlPerTick'] = _clamp_float(raw.get('fcoScaleUlPerTick'), cfg['fcoScaleUlPerTick'], 0.05, 0.5)
  return cfg


def read_config(root: Path | str | None = None) -> dict[str, Any]:
  path = ioniq_storage_root(root) / 'config.json'
  return clamp_config(read_json(path, {}))


def write_config(raw: dict[str, Any], root: Path | str | None = None) -> dict[str, Any]:
  cfg = clamp_config(raw)
  write_json_atomic(ioniq_storage_root(root) / 'config.json', cfg)
  return cfg


def _clamp_float(value: Any, fallback: float, lo: float, hi: float) -> float:
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    if value == value:  # reject NaN
      return float(max(lo, min(hi, value)))
  return float(fallback)


def _copy_default(default: Any) -> Any:
  if isinstance(default, dict):
    return dict(default)
  if isinstance(default, list):
    return list(default)
  return default
