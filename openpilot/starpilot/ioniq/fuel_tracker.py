"""Fuel consumption estimator, ignition-to-ignition trip log, and Tank-to-Tank ("Volltanken zu Volltanken") tracker."""

from __future__ import annotations

import csv
import io
from pathlib import Path
import time
from typing import Any

from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_json, write_json_atomic

MAX_TRIP_HISTORY = 150
MAX_TANK_HISTORY = 60

# Kappa 1.6 GDi Atkinson-cycle HEV (G4LE) reference constants
MAX_ENGINE_TORQUE_NM = 147.0
GASOLINE_DENSITY_G_PER_L = 745.0


def fuel_path(root: Path | str | None = None) -> Path:
  return ioniq_storage_root(root) / 'fuel.json'


def default_tank(now: float | None = None, odometer_km: float | None = None,
                 fuel_disp_lvl: int | None = None, dte_km: int | None = None) -> dict[str, Any]:
  ts = now if now is not None else time.time()
  return {
    'id': f'tank_{int(ts * 1000)}',
    'startedAt': ts,
    'startOdometerKm': odometer_km,
    'lastOdometerKm': odometer_km,
    'distanceKm': 0.0,
    'estimatedFuelLiters': 0.0,
    'partialRefuelsLiters': 0.0,
    'evDistanceKm': 0.0,
    'iceDistanceKm': 0.0,
    'tripCount': 0,
    'fuelDispStart': fuel_disp_lvl,
    'fuelDispCurrent': fuel_disp_lvl,
    'dteKm': dte_km,
  }


def read_fuel_state(root: Path | str | None = None) -> dict[str, Any]:
  default: dict[str, Any] = {
    'activeTrip': None,
    'trips': [],
    'currentTank': default_tank(),
    'tankHistory': [],
    'refuelLog': [],
    'refuelAlert': None,
    'calibrationFactor': 1.0,
    'pendingFuelJump': None,
  }
  data = read_json(fuel_path(root), default)
  if not isinstance(data, dict):
    return default
  for k, v in default.items():
    data.setdefault(k, v)
  if not isinstance(data.get('currentTank'), dict):
    data['currentTank'] = default_tank()
  if not isinstance(data.get('trips'), list):
    data['trips'] = []
  if not isinstance(data.get('tankHistory'), list):
    data['tankHistory'] = []
  if not isinstance(data.get('refuelLog'), list):
    data['refuelLog'] = []
  return data


def write_fuel_state(state: dict[str, Any], root: Path | str | None = None) -> dict[str, Any]:
  state['trips'] = list(state.get('trips') or [])[:MAX_TRIP_HISTORY]
  state['tankHistory'] = list(state.get('tankHistory') or [])[:MAX_TANK_HISTORY]
  state['refuelLog'] = list(state.get('refuelLog') or [])[:MAX_TANK_HISTORY * 3]
  write_json_atomic(fuel_path(root), state)
  return state


def estimate_bsfc_g_per_kwh(rpm: float, torque_pct: float, coolant_c: float | None) -> float:
  """Piecewise efficiency model for the Kappa 1.6 GDi Atkinson hybrid engine."""
  if torque_pct >= 55.0 and 1600.0 <= rpm <= 3600.0:
    bsfc = 225.0  # Atkinson sweet spot (~40% thermal efficiency)
  elif torque_pct >= 30.0 and 1300.0 <= rpm <= 4200.0:
    bsfc = 242.0
  else:
    bsfc = 270.0
  if coolant_c is not None and coolant_c < 60.0:
    warmup_penalty = 1.0 + min(0.28, (60.0 - max(-20.0, coolant_c)) * 0.004)
    bsfc *= warmup_penalty
  return bsfc


def estimate_fuel_rate_lph(can_state: IoniqCanState, *, calibration_factor: float = 1.0) -> float:
  """Estimate instantaneous ICE fuel flow in L/h from RPM, torque %, and fuel-cut flags."""
  if not can_state.engine_is_firing():
    return 0.0
  rpm = can_state.engine_rpm
  torque_pct = max(can_state.engine_torque_pct, can_state.tqi_b_pct)
  if torque_pct <= 2.5:
    # Unloaded warm/cold idle (~0.55 - 0.90 L/h)
    base_lph = 0.85 if (can_state.coolant_temp_c is None or can_state.coolant_temp_c < 55.0) else 0.58
    return base_lph * calibration_factor
  torque_nm = MAX_ENGINE_TORQUE_NM * min(1.0, max(0.0, torque_pct / 100.0))
  power_kw = torque_nm * rpm / 9549.0
  bsfc = estimate_bsfc_g_per_kwh(rpm, torque_pct, can_state.coolant_temp_c)
  liters_per_hour = max(0.45, (power_kw * bsfc) / GASOLINE_DENSITY_G_PER_L)
  return liters_per_hour * calibration_factor


class FuelTripTracker:
  """Tracks ignition-to-ignition drive trips, EV vs ICE split, and Tank-to-Tank cycles."""

  def __init__(self, root: Path | str | None = None, *, fco_scale_ul: float = 0.128,
               calibration_factor: float | None = None, price_per_liter: float = 1.75) -> None:
    self.root = root
    self.fco_scale_ul = fco_scale_ul
    self.price_per_liter = price_per_liter
    self.state = read_fuel_state(root)
    self.calibration_factor = float(calibration_factor if calibration_factor is not None else self.state.get('calibrationFactor', 1.0))

    self._last_mono_s: float | None = None
    self._last_fco_raw: int | None = None
    self._fco_valid_ticks: int = 0
    self._fco_invalid: bool = False
    self._integrated_distance_km: float = 0.0
    self._start_odometer_km: float | None = None
    self._min_fuel_disp_seen: int | None = None
    self._off_since_mono_s: float | None = None
    self._dirty: bool = False
    self._last_flush_mono_s: float = 0.0

    active = self.state.get('activeTrip')
    if isinstance(active, dict):
      self._integrated_distance_km = float(active.get('integratedDistanceKm', active.get('distanceKm', 0.0)))
      self._start_odometer_km = active.get('odometerStartKm')

  def _ensure_active_trip(self, can_state: IoniqCanState, mono_s: float, wall_time: float) -> dict[str, Any]:
    trip = self.state.get('activeTrip')
    if isinstance(trip, dict):
      return trip
    odo = can_state.odometer_km
    trip = {
      'id': f'trip_{int(wall_time * 1000)}',
      'label': '',
      'startedAt': wall_time,
      'endedAt': None,
      'durationS': 0.0,
      'movingDurationS': 0.0,
      'idleDurationS': 0.0,
      'evDurationS': 0.0,
      'iceDurationS': 0.0,
      'distanceKm': 0.0,
      'integratedDistanceKm': 0.0,
      'evDistanceKm': 0.0,
      'iceDistanceKm': 0.0,
      'odometerStartKm': odo,
      'odometerEndKm': odo,
      'fuelLiters': 0.0,
      'fuelLitersFco': 0.0,
      'fuelLitersModel': 0.0,
      'sourceMode': 'torque_model',
      'avgLPer100Km': None,
      'avgKmPerL': None,
      'evTimePct': 0.0,
      'estimatedCostEur': 0.0,
      'clusterAvgFci': can_state.cluster_avg_fc_value,
      'fuelDispStart': can_state.fuel_disp_level,
      'fuelDispEnd': can_state.fuel_disp_level,
      'bat12vStartV': can_state.bat_12v_voltage_v,
      'bat12vEndV': can_state.bat_12v_voltage_v,
      'maxSpeedKph': 0.0,
      'maxCoolantC': can_state.coolant_temp_c,
      'ambientTempC': can_state.ambient_temp_c,
    }
    self.state['activeTrip'] = trip
    self._integrated_distance_km = 0.0
    self._start_odometer_km = odo
    self._last_fco_raw = can_state.fco_raw
    self._fco_valid_ticks = 0
    self._fco_invalid = False

    tank = self.state['currentTank']
    if tank.get('startOdometerKm') is None and odo is not None:
      tank['startOdometerKm'] = odo
    if tank.get('fuelDispStart') is None and can_state.fuel_disp_level is not None:
      tank['fuelDispStart'] = can_state.fuel_disp_level
    self._dirty = True
    return trip

  def update(self, can_state: IoniqCanState, mono_s: float, *, wall_time: float | None = None,
             ignition_hint: bool | None = None, force_flush: bool = False) -> dict[str, Any]:
    """Advance trip & tank accumulators with the latest CAN state."""
    now = wall_time if wall_time is not None else time.time()
    speed_kph = can_state.best_speed_kph()
    ign = ignition_hint if ignition_hint is not None else can_state.ignition_on
    driving_or_ready = bool(ign is True or speed_kph > 0.5 or can_state.engine_rpm > 200.0)

    if driving_or_ready:
      self._off_since_mono_s = None
      trip = self._ensure_active_trip(can_state, mono_s, now)
    else:
      if self.state.get('activeTrip') is not None:
        if ign is False:
          if self._off_since_mono_s is None:
            self._off_since_mono_s = mono_s
          elif (mono_s - self._off_since_mono_s) >= 2.0:
            self.end_trip(wall_time=now)
        else:
          self._off_since_mono_s = None
      self._last_mono_s = mono_s
      if force_flush and self._dirty:
        self.flush(mono_s)
      return self.snapshot()

    dt = 0.0 if self._last_mono_s is None else max(0.0, min(5.0, mono_s - self._last_mono_s))
    self._last_mono_s = mono_s
    if dt <= 0.0:
      if force_flush and self._dirty:
        self.flush(mono_s)
      return self.snapshot()

    trip = self.state['activeTrip']
    tank = self.state['currentTank']
    prev_fuel_total = float(trip['fuelLiters'])

    # Time & hybrid split
    trip['durationS'] = round(trip['durationS'] + dt, 2)
    is_moving = speed_kph >= 0.8
    firing = can_state.engine_is_firing()
    if is_moving:
      trip['movingDurationS'] = round(trip['movingDurationS'] + dt, 2)
    else:
      trip['idleDurationS'] = round(trip['idleDurationS'] + dt, 2)
    if firing:
      trip['iceDurationS'] = round(trip['iceDurationS'] + dt, 2)
    else:
      trip['evDurationS'] = round(trip['evDurationS'] + dt, 2)

    # Distance: integrate continuous speed + anchor to CLU12 odometer when delta >= 0.2 km
    ddist = max(0.0, speed_kph) * (dt / 3600.0)
    self._integrated_distance_km += ddist
    trip['integratedDistanceKm'] = round(self._integrated_distance_km, 4)

    odo = can_state.odometer_km
    if odo is not None:
      if self._start_odometer_km is None:
        self._start_odometer_km = odo
        trip['odometerStartKm'] = odo
      trip['odometerEndKm'] = odo
      odo_delta = max(0.0, odo - self._start_odometer_km)
      if odo_delta >= 0.2 and abs(odo_delta - self._integrated_distance_km) < max(0.5, odo_delta * 0.25):
        best_dist = odo_delta
      else:
        best_dist = self._integrated_distance_km
      prev_tank_dist = float(trip['distanceKm'])
      trip['distanceKm'] = round(best_dist, 3)
      dist_inc = max(0.0, trip['distanceKm'] - prev_tank_dist)
    else:
      prev_trip_dist = float(trip['distanceKm'])
      trip['distanceKm'] = round(self._integrated_distance_km, 3)
      dist_inc = max(0.0, trip['distanceKm'] - prev_trip_dist)

    if firing:
      trip['iceDistanceKm'] = round(trip['iceDistanceKm'] + dist_inc, 3)
      tank['iceDistanceKm'] = round(float(tank.get('iceDistanceKm', 0.0)) + dist_inc, 3)
    else:
      trip['evDistanceKm'] = round(trip['evDistanceKm'] + dist_inc, 3)
      tank['evDistanceKm'] = round(float(tank.get('evDistanceKm', 0.0)) + dist_inc, 3)

    tank['distanceKm'] = round(float(tank.get('distanceKm', 0.0)) + dist_inc, 3)
    if odo is not None:
      tank['lastOdometerKm'] = odo

    # Fuel: EMS20 0x200 FCO + physics model
    model_inc = estimate_fuel_rate_lph(can_state, calibration_factor=self.calibration_factor) * (dt / 3600.0)
    trip['fuelLitersModel'] = round(trip['fuelLitersModel'] + model_inc, 5)

    fco_inc = 0.0
    if can_state.fco_raw is not None and not self._fco_invalid:
      if self._last_fco_raw is not None:
        raw_delta = (can_state.fco_raw - self._last_fco_raw) & 0xFFFF
        # Sanity checks: ignore massive bogus jumps (e.g. non-FCO bytes)
        if 0 < raw_delta <= 8000:
          if can_state.engine_rpm > 200.0 or raw_delta <= 120:
            fco_inc = raw_delta * self.fco_scale_ul * 1e-6 * self.calibration_factor
            self._fco_valid_ticks += 1
          else:
            # Non-zero activity while RPM == 0 repeatedly -> likely non-FCO frame on trim
            if raw_delta > 200:
              self._fco_invalid = True
        elif raw_delta > 25000:
          self._fco_invalid = True
      self._last_fco_raw = can_state.fco_raw

    if self._fco_valid_ticks >= 2 and not self._fco_invalid:
      trip['fuelLitersFco'] = round(trip['fuelLitersFco'] + fco_inc, 5)
      trip['fuelLiters'] = trip['fuelLitersFco']
      trip['sourceMode'] = 'fco_can'
    else:
      trip['fuelLiters'] = trip['fuelLitersModel']
      trip['sourceMode'] = 'torque_model'

    fuel_inc = max(0.0, float(trip['fuelLiters']) - prev_fuel_total)
    tank['estimatedFuelLiters'] = round(float(tank.get('estimatedFuelLiters', 0.0)) + fuel_inc, 4)

    # Derived trip KPIs
    total_dur = max(0.001, float(trip['durationS']))
    trip['evTimePct'] = round(float(trip['evDurationS']) / total_dur * 100.0, 1)
    if float(trip['distanceKm']) >= 0.05:
      l100 = float(trip['fuelLiters']) / float(trip['distanceKm']) * 100.0
      trip['avgLPer100Km'] = round(l100, 2)
      trip['avgKmPerL'] = round(float(trip['distanceKm']) / float(trip['fuelLiters']), 2) if float(trip['fuelLiters']) > 0 else None
    trip['estimatedCostEur'] = round(float(trip['fuelLiters']) * self.price_per_liter, 2)
    trip['maxSpeedKph'] = round(max(float(trip['maxSpeedKph']), speed_kph), 1)
    if can_state.coolant_temp_c is not None:
      prev_cool = trip.get('maxCoolantC')
      trip['maxCoolantC'] = round(can_state.coolant_temp_c if prev_cool is None else max(prev_cool, can_state.coolant_temp_c), 1)
    if can_state.ambient_temp_c is not None:
      trip['ambientTempC'] = round(can_state.ambient_temp_c, 1)
    if can_state.cluster_avg_fc_value is not None:
      trip['clusterAvgFci'] = can_state.cluster_avg_fc_value
    if can_state.bat_12v_voltage_v is not None:
      trip['bat12vEndV'] = can_state.bat_12v_voltage_v
      if trip.get('bat12vStartV') is None:
        trip['bat12vStartV'] = can_state.bat_12v_voltage_v

    # Fuel gauge & automatic refuel jump detection
    if can_state.fuel_disp_level is not None:
      trip['fuelDispEnd'] = can_state.fuel_disp_level
      if trip.get('fuelDispStart') is None:
        trip['fuelDispStart'] = can_state.fuel_disp_level
      tank['fuelDispCurrent'] = can_state.fuel_disp_level
      if self._min_fuel_disp_seen is None or can_state.fuel_disp_level < self._min_fuel_disp_seen:
        self._min_fuel_disp_seen = can_state.fuel_disp_level
      elif (can_state.fuel_disp_level - self._min_fuel_disp_seen) >= 3 and speed_kph < 5.0:
        self.state['refuelAlert'] = {
          'detectedAt': now,
          'deltaBars': can_state.fuel_disp_level - self._min_fuel_disp_seen,
          'currentLevel': can_state.fuel_disp_level,
          'odometerKm': odo,
        }
        self._min_fuel_disp_seen = can_state.fuel_disp_level
    if can_state.dte_km is not None:
      tank['dteKm'] = can_state.dte_km

    self._dirty = True
    if force_flush or (mono_s - self._last_flush_mono_s) >= 10.0:
      self.flush(mono_s)
    return self.snapshot()

  def start_manual_trip(self, *, label: str = '', wall_time: float | None = None) -> dict[str, Any]:
    now = wall_time if wall_time is not None else time.time()
    if self.state.get('activeTrip') is not None:
      self.end_trip(wall_time=now)
    trip = self._ensure_active_trip(IoniqCanState(), 0.0, now)
    trip['label'] = label.strip()[:64]
    self.flush(0.0)
    return trip

  def end_trip(self, *, wall_time: float | None = None) -> dict[str, Any] | None:
    trip = self.state.get('activeTrip')
    if not isinstance(trip, dict):
      return None
    now = wall_time if wall_time is not None else time.time()
    trip['endedAt'] = now
    self.state['activeTrip'] = None

    # Save meaningful trips (> 20s or > 0.05 km); discard accidental 2-second boot blips unless labeled
    archived: dict[str, Any] | None = None
    if float(trip.get('durationS', 0.0)) >= 15.0 or float(trip.get('distanceKm', 0.0)) >= 0.05 or trip.get('label'):
      trips = list(self.state.get('trips') or [])
      trips.insert(0, trip)
      self.state['trips'] = trips[:MAX_TRIP_HISTORY]
      self.state['currentTank']['tripCount'] = int(self.state['currentTank'].get('tripCount', 0)) + 1
      archived = trip

    self._dirty = True
    self.flush(0.0)
    return archived

  def log_refuel(
    self,
    liters: float,
    *,
    full_tank: bool = True,
    price_per_liter: float | None = None,
    odometer_km: float | None = None,
    notes: str = '',
    wall_time: float | None = None,
  ) -> dict[str, Any]:
    """Record a refuel; closes and calibrates the Tank-to-Tank cycle when full_tank=True."""
    if liters <= 0.1 or liters > 90.0:
      raise ValueError('Fuel quantity must be between 0.1 and 90 liters')
    now = wall_time if wall_time is not None else time.time()
    unit_price = float(price_per_liter if price_per_liter is not None else self.price_per_liter)
    total_cost = round(liters * unit_price, 2)
    tank = self.state['currentTank']

    refuel_entry = {
      'id': f'refuel_{int(now * 1000)}',
      'loggedAt': now,
      'liters': round(float(liters), 2),
      'fullTank': bool(full_tank),
      'pricePerLiter': round(unit_price, 3),
      'costEur': total_cost,
      'odometerKm': odometer_km if odometer_km is not None else tank.get('lastOdometerKm'),
      'notes': notes.strip()[:120],
    }
    log = list(self.state.get('refuelLog') or [])
    log.insert(0, refuel_entry)
    self.state['refuelLog'] = log
    self.state['refuelAlert'] = None

    closed_tank: dict[str, Any] | None = None
    if not full_tank:
      tank['partialRefuelsLiters'] = round(float(tank.get('partialRefuelsLiters', 0.0)) + float(liters), 2)
    else:
      start_odo = tank.get('startOdometerKm')
      end_odo = odometer_km if odometer_km is not None else tank.get('lastOdometerKm')
      if start_odo is not None and end_odo is not None and end_odo > start_odo:
        actual_dist = round(float(end_odo - start_odo), 1)
      else:
        actual_dist = round(float(tank.get('distanceKm', 0.0)), 1)

      actual_liters = round(float(liters) + float(tank.get('partialRefuelsLiters', 0.0)), 2)
      est_liters = round(float(tank.get('estimatedFuelLiters', 0.0)), 2)
      actual_l100 = round(actual_liters / actual_dist * 100.0, 2) if actual_dist >= 1.0 else None
      actual_kml = round(actual_dist / actual_liters, 2) if actual_dist >= 1.0 else None

      error_pct = None
      if est_liters >= 1.0:
        error_pct = round((est_liters - actual_liters) / actual_liters * 100.0, 1)

      # Smooth auto-calibration when real distance and volume are trustworthy
      if actual_dist >= 50.0 and actual_liters >= 3.0 and est_liters >= 2.0:
        ratio = max(0.65, min(1.45, self.calibration_factor * (actual_liters / est_liters)))
        self.calibration_factor = round(self.calibration_factor * 0.5 + ratio * 0.5, 4)
        self.state['calibrationFactor'] = self.calibration_factor

      closed_tank = {
        'id': tank.get('id'),
        'startedAt': tank.get('startedAt'),
        'endedAt': now,
        'startOdometerKm': start_odo,
        'endOdometerKm': end_odo,
        'distanceKm': actual_dist,
        'actualLiters': actual_liters,
        'estimatedLiters': est_liters,
        'actualLPer100Km': actual_l100,
        'actualKmPerL': actual_kml,
        'estimateErrorPct': error_pct,
        'evDistanceKm': round(float(tank.get('evDistanceKm', 0.0)), 1),
        'iceDistanceKm': round(float(tank.get('iceDistanceKm', 0.0)), 1),
        'tripCount': int(tank.get('tripCount', 0)),
        'costEur': round(actual_liters * unit_price, 2),
        'notes': notes.strip()[:120],
      }
      history = list(self.state.get('tankHistory') or [])
      history.insert(0, closed_tank)
      self.state['tankHistory'] = history[:MAX_TANK_HISTORY]
      self.state['currentTank'] = default_tank(
        now=now,
        odometer_km=end_odo,
        fuel_disp_lvl=tank.get('fuelDispCurrent'),
        dte_km=tank.get('dteKm'),
      )

    self._dirty = True
    self.flush(0.0)
    return {'refuel': refuel_entry, 'closedTank': closed_tank, 'calibrationFactor': self.calibration_factor}

  def export_csv(self) -> str:
    buf = io.StringIO()
    fieldnames = [
      'id', 'label', 'startedAt', 'endedAt', 'durationS', 'distanceKm', 'evDistanceKm', 'iceDistanceKm',
      'evTimePct', 'fuelLiters', 'avgLPer100Km', 'avgKmPerL', 'clusterAvgFci', 'estimatedCostEur',
      'odometerStartKm', 'odometerEndKm', 'sourceMode', 'maxSpeedKph', 'maxCoolantC',
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    for trip in self.state.get('trips', []):
      writer.writerow(trip)
    return buf.getvalue()

  def flush(self, mono_s: float = 0.0) -> None:
    if self._dirty:
      self.state['calibrationFactor'] = round(self.calibration_factor, 4)
      write_fuel_state(self.state, self.root)
      self._dirty = False
      self._last_flush_mono_s = mono_s

  def snapshot(self) -> dict[str, Any]:
    tanks = self.state.get('tankHistory', [])
    valid_l100 = [t['actualLPer100Km'] for t in tanks if isinstance(t.get('actualLPer100Km'), (int, float))]
    cur = self.state.get('currentTank', {})
    cur_est_l100 = None
    if float(cur.get('distanceKm', 0.0)) >= 1.0:
      cur_est_l100 = round(float(cur.get('estimatedFuelLiters', 0.0)) / float(cur.get('distanceKm', 1.0)) * 100.0, 2)
    return {
      'activeTrip': self.state.get('activeTrip'),
      'trips': self.state.get('trips', []),
      'currentTank': dict(cur, estimatedLPer100Km=cur_est_l100),
      'tankHistory': tanks,
      'refuelLog': self.state.get('refuelLog', []),
      'refuelAlert': self.state.get('refuelAlert'),
      'calibrationFactor': round(self.calibration_factor, 4),
      'lifetimeTankAvgLPer100Km': round(sum(valid_l100) / len(valid_l100), 2) if valid_l100 else None,
    }
