"""Fuel consumption estimator, ignition-to-ignition trip log, and Tank-to-Tank ("Volltanken zu Volltanken") tracker."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
import io
from pathlib import Path
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
  ts = now if now is not None else datetime.now(UTC).timestamp()
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


def _repair_broken_zero_fuel_records(state: dict[str, Any]) -> bool:
  """Backfill previously recorded trips/tank cycles stuck at 0 L / 100% EV from missing EMS11 on C-CAN."""
  changed = False
  records: list[dict[str, Any]] = []
  if isinstance(state.get('activeTrip'), dict):
    records.append(state['activeTrip'])
  for trip in state.get('trips') or []:
    if isinstance(trip, dict):
      records.append(trip)

  for trip in records:
    dist = float(trip.get('distanceKm', 0.0) or 0.0)
    if dist < 0.05:
      continue
    fuel = float(trip.get('fuelLiters', 0.0) or 0.0)
    ev_pct = float(trip.get('evTimePct', 0.0) or 0.0)
    ice_dist = float(trip.get('iceDistanceKm', 0.0) or 0.0)
    if fuel > 0.0001 and not (ev_pct >= 99.5 and ice_dist <= 0.0):
      continue

    dur = max(1.0, float(trip.get('durationS', 60.0) or 60.0))
    avg_kph = dist / (dur / 3600.0)
    clu = trip.get('clusterAvgLPer100Km')
    est_l100 = float(clu) if isinstance(clu, (int, float)) and 1.5 <= float(clu) <= 18.0 else (
      4.6 if avg_kph < 55.0 else (4.9 if avg_kph < 95.0 else 5.6)
    )
    if fuel <= 0.0001:
      repaired_fuel = round(dist * est_l100 / 100.0, 4)
      trip['fuelLiters'] = repaired_fuel
      trip['fuelLitersModel'] = repaired_fuel
      trip['avgLPer100Km'] = round(est_l100, 2)
      trip['avgKmPerL'] = round(100.0 / est_l100, 2) if est_l100 > 0.0 else None
      trip['estimatedCostEur'] = round(repaired_fuel * 1.75, 2)
      if trip.get('sourceMode') in (None, '', 'torque_model'):
        trip['sourceMode'] = 'cluster_avg_fci' if isinstance(clu, (int, float)) and 1.5 <= float(clu) <= 18.0 else 'hev_model'
      changed = True

    if ev_pct >= 99.5 and ice_dist <= 0.0:
      ev_dist_frac = 0.52 if avg_kph < 45.0 else (0.36 if avg_kph < 85.0 else 0.14)
      ev_time_frac = min(0.88, ev_dist_frac + 0.12)
      trip['evDistanceKm'] = round(dist * ev_dist_frac, 3)
      trip['iceDistanceKm'] = round(dist * (1.0 - ev_dist_frac), 3)
      trip['evDistancePct'] = round(ev_dist_frac * 100.0, 1)
      trip['evDurationS'] = round(dur * ev_time_frac, 2)
      trip['iceDurationS'] = round(dur * (1.0 - ev_time_frac), 2)
      trip['evTimePct'] = round(ev_time_frac * 100.0, 1)
      changed = True

  tank = state.get('currentTank')
  if isinstance(tank, dict):
    tank_dist = float(tank.get('distanceKm', 0.0) or 0.0)
    tank_fuel = float(tank.get('estimatedFuelLiters', 0.0) or 0.0)
    tank_ice = float(tank.get('iceDistanceKm', 0.0) or 0.0)
    if tank_dist >= 0.05 and (tank_fuel <= 0.0001 or tank_ice <= 0.0):
      tank_start = float(tank.get('startedAt', 0.0) or 0.0)
      matching_trips = [
        t for t in records
        if float(t.get('startedAt', 0.0) or 0.0) >= (tank_start - 5.0)
      ]
      sum_fuel = sum(float(t.get('fuelLiters', 0.0) or 0.0) for t in matching_trips)
      sum_ev = sum(float(t.get('evDistanceKm', 0.0) or 0.0) for t in matching_trips)
      sum_ice = sum(float(t.get('iceDistanceKm', 0.0) or 0.0) for t in matching_trips)
      if tank_fuel <= 0.0001:
        tank['estimatedFuelLiters'] = round(sum_fuel if sum_fuel > 0.001 else tank_dist * 4.8 / 100.0, 4)
        changed = True
      if tank_ice <= 0.0:
        if (sum_ev + sum_ice) >= 0.05 and sum_ice > 0.0:
          tank['evDistanceKm'] = round(sum_ev, 3)
          tank['iceDistanceKm'] = round(sum_ice, 3)
        else:
          tank['evDistanceKm'] = round(tank_dist * 0.42, 3)
          tank['iceDistanceKm'] = round(tank_dist * 0.58, 3)
        changed = True

  return changed


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
  path = fuel_path(root)
  data = read_json(path, default)
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
  if _repair_broken_zero_fuel_records(data) and path.exists():
    write_fuel_state(data, root)
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
  """Estimate instantaneous ICE fuel flow in L/h from OBD PID, MAF, or RPM/torque/fuel-cut model."""
  if not can_state.engine_is_firing():
    return 0.0
  if can_state.obd_fuel_rate_lph is not None and can_state.obd_fuel_rate_lph > 0.05:
    return float(can_state.obd_fuel_rate_lph) * calibration_factor
  if can_state.obd_maf_gps is not None and can_state.obd_maf_gps > 0.2:
    # Stoichiometric gasoline AFR 14.7:1, density 745 g/L -> L/h = (MAF_g_s * 3600) / (14.7 * 745)
    return max(0.35, (float(can_state.obd_maf_gps) * 3600.0) / (14.7 * GASOLINE_DENSITY_G_PER_L)) * calibration_factor
  rpm = can_state.engine_rpm
  torque_pct = max(can_state.engine_torque_pct, can_state.tqi_b_pct)
  if torque_pct <= 2.5:
    # Unloaded warm/cold idle or HEV low-load charging (~0.58 - 1.15 L/h depending on RPM & coolant)
    rpm_factor = max(1.0, min(1.8, rpm / 1050.0))
    base_lph = (0.85 if (can_state.coolant_temp_c is None or can_state.coolant_temp_c < 55.0) else 0.58) * rpm_factor
    return base_lph * calibration_factor
  torque_nm = MAX_ENGINE_TORQUE_NM * min(1.0, max(0.0, torque_pct / 100.0))
  power_kw = torque_nm * rpm / 9549.0
  bsfc = estimate_bsfc_g_per_kwh(rpm, torque_pct, can_state.coolant_temp_c)
  liters_per_hour = max(0.45, (power_kw * bsfc) / GASOLINE_DENSITY_G_PER_L)
  return liters_per_hour * calibration_factor


def cluster_avg_l_per_100km(can_state: IoniqCanState) -> float | None:
  """Convert CLU13 CF_Clu_AvgFCI to L/100km regardless of cluster unit setting."""
  return can_state.cluster_avg_l_per_100km()


class FuelTripTracker:
  """Tracks ignition-to-ignition drive trips, EV vs ICE split, and Tank-to-Tank cycles."""

  def __init__(self, root: Path | str | None = None, *, fco_scale_ul: float = 0.128,
               calibration_factor: float | None = None, price_per_liter: float = 1.75,
               tank_capacity_liters: float = 45.0) -> None:
    self.root = root
    self.fco_scale_ul = fco_scale_ul
    self.price_per_liter = price_per_liter
    self.tank_capacity_liters = tank_capacity_liters
    self.state = read_fuel_state(root)
    self.calibration_factor = float(calibration_factor if calibration_factor is not None else self.state.get('calibrationFactor', 1.0))

    self._last_mono_s: float | None = None
    self._last_fco_raw: int | None = None
    self._fco_valid_ticks: int = 0
    self._fco_rejected_ticks: int = 0
    self._integrated_distance_km: float = 0.0
    self._start_odometer_km: float | None = None
    self._min_fuel_disp_seen: int | None = None
    self._valid_fuel_disp_samples: int = 0
    self._stationary_duration_s: float = 0.0
    self._off_since_mono_s: float | None = None
    self._last_instant_lph: float = 0.0
    self._dirty: bool = False
    self._last_flush_mono_s: float = 0.0
    self._hev_ev_debt_wh: float = 60.0
    self._hev_ice_on: bool = False
    self._hev_ice_hold_until_mono: float = 0.0
    self._hev_ice_ever_fired: bool = False

    active = self.state.get('activeTrip')
    if isinstance(active, dict):
      self._integrated_distance_km = float(active.get('integratedDistanceKm', active.get('distanceKm', 0.0)))
      self._start_odometer_km = active.get('odometerStartKm')
      self._hev_ice_ever_fired = float(active.get('fuelLiters', 0.0) or 0.0) > 0.001

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
      'fuelLitersClusterFallback': 0.0,
      'instantFuelRateLph': 0.0,
      'sourceMode': 'torque_model' if can_state.has_direct_engine_rpm() else 'hev_model',
      'sourceDiagnostics': {},
      'avgLPer100Km': None,
      'avgKmPerL': None,
      'evTimePct': 0.0,
      'evDistancePct': 0.0,
      'estimatedCostEur': 0.0,
      'clusterAvgFci': can_state.cluster_avg_fc_value,
      'clusterAvgLPer100Km': cluster_avg_l_per_100km(can_state),
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
    self._fco_rejected_ticks = 0
    self._hev_ev_debt_wh = 60.0
    self._hev_ice_on = False
    self._hev_ice_hold_until_mono = mono_s
    self._hev_ice_ever_fired = False

    tank = self.state['currentTank']
    if tank.get('startOdometerKm') is None and odo is not None:
      tank['startOdometerKm'] = odo
    if tank.get('fuelDispStart') is None and can_state.fuel_disp_level is not None and can_state.fuel_disp_level > 0:
      tank['fuelDispStart'] = can_state.fuel_disp_level
    self._dirty = True
    return trip

  def _build_source_diagnostics(self, can_state: IoniqCanState, trip: dict[str, Any] | None) -> dict[str, Any]:
    seen = can_state.seen_addresses
    has_ems11 = 0x316 in seen
    has_ems_h12 = 0x18F in seen
    has_ems20 = 0x200 in seen
    has_clu12 = 0x5B0 in seen
    has_clu13 = 0x50C in seen
    has_whl_spd = 0x386 in seen
    default_mode = 'torque_model' if can_state.has_direct_engine_rpm() else 'hev_model'
    mode = (trip or {}).get('sourceMode', default_mode)
    mode_labels = {
      'fco_can': 'Direkte Einspritzmenge (EMS20 0x200 FCO)',
      'obd_pid': 'OBD-II Live-Sensorik (MAF / Fuel Rate)',
      'torque_model': 'Atkinson-Kennfeldmodell (EMS11 Drehzahl/Drehmoment)',
      'hev_model': 'HEV Traktions-/Rekuperationsmodell (C-CAN)',
      'cluster_avg_fci': 'Kombiinstrument-Referenz (CLU13 Durchschnittsverbrauch)',
    }
    return {
      'activeSource': mode,
      'activeSourceLabel': mode_labels.get(mode, mode),
      'instantFuelRateLph': round(self._last_instant_lph, 2),
      'fcoSeen': has_ems20,
      'fcoRaw': can_state.fco_raw,
      'fcoValidTicks': self._fco_valid_ticks,
      'fcoRejectedTicks': self._fco_rejected_ticks,
      'ems11Seen': has_ems11,
      'emsH12Seen': has_ems_h12,
      'eEms11Seen': 0x371 in seen,
      'electGearSeen': 0x372 in seen,
      'tcs13Seen': 0x394 in seen,
      'clu12OdoSeen': has_clu12,
      'clu13FuelSeen': has_clu13,
      'wheelSpeedSeen': has_whl_spd,
      'hasDirectRpm': can_state.has_direct_engine_rpm(),
      'engineFiring': can_state.engine_is_firing(),
      'engineRpm': round(can_state.engine_rpm if can_state.has_direct_engine_rpm() else can_state.synthetic_engine_rpm, 0),
      'torquePct': round(max(can_state.engine_torque_pct, can_state.tqi_b_pct), 1),
      'gasPedalPct': round(can_state.gas_pedal_pct, 1),
      'tractivePowerKw': round(can_state.tractive_power_kw(), 1),
      'gearShifter': can_state.gear_shifter_label(),
      'hevEvDebtWh': round(self._hev_ev_debt_wh, 1),
      'clusterAvgFci': can_state.cluster_avg_fc_value,
      'clusterAvgLPer100Km': cluster_avg_l_per_100km(can_state),
      'fcoValid': self._fco_valid_ticks >= 2,
      'fcoLiters': None if trip is None else round(float(trip.get('fuelLitersFco', 0.0)), 4),
      'modelLiters': None if trip is None else round(float(trip.get('fuelLitersModel', 0.0)), 4),
      'clusterFallbackLiters': None if trip is None else round(float(trip.get('fuelLitersClusterFallback', 0.0)), 4),
      'fuelLitersFco': None if trip is None else round(float(trip.get('fuelLitersFco', 0.0)), 4),
      'fuelLitersModel': None if trip is None else round(float(trip.get('fuelLitersModel', 0.0)), 4),
      'fuelLitersClusterFallback': None if trip is None else round(float(trip.get('fuelLitersClusterFallback', 0.0)), 4),
      'calibrationFactor': round(self.calibration_factor, 4),
    }

  def _step_hev_model(self, can_state: IoniqCanState, trip: dict[str, Any], dt: float,
                      mono_s: float, speed_kph: float) -> float:
    """Power-split & HV battery energy-balance estimator when C-CAN omits direct ICE RPM."""
    p_wheel = can_state.tractive_power_kw()
    gas_pct = can_state.gas_pedal_pct
    if can_state.cs_gas_pressed and gas_pct <= 1.0:
      gas_pct = 18.0
    gear = can_state.gear_shifter_label()
    braking = can_state.is_braking_or_regen()
    p_aux_kw = 0.65 + (0.85 if can_state.ac_relay_on else 0.0)
    cold_coolant = can_state.coolant_temp_c is not None and can_state.coolant_temp_c < 48.0
    initial_warmup = can_state.coolant_temp_c is None and float(trip.get('durationS', 0.0)) < 25.0 and speed_kph >= 10.0 and gas_pct >= 8.0
    need_initial_fire = float(trip.get('distanceKm', 0.0)) >= 0.15 and not self._hev_ice_ever_fired and p_wheel >= 1.5 and gear != 'P'

    trigger_ice = (
      (gear == 'S' and (speed_kph > 5.0 or gas_pct > 4.0))
      or p_wheel >= 11.5
      or gas_pct >= 26.0
      or (speed_kph >= 92.0 and p_wheel > 3.0)
      or (self._hev_ev_debt_wh >= 210.0 and (speed_kph >= 10.0 or p_wheel > 1.5 or gas_pct > 4.0))
      or ((cold_coolant or initial_warmup) and (speed_kph >= 5.0 or gas_pct > 3.0))
      or need_initial_fire
    )

    if trigger_ice:
      if not self._hev_ice_on:
        self._hev_ice_on = True
        self._hev_ice_hold_until_mono = mono_s + 7.0
      self._hev_ice_ever_fired = True
    elif self._hev_ice_on:
      allow_release = mono_s >= self._hev_ice_hold_until_mono or (braking and p_wheel < -2.0 and speed_kph < 45.0)
      if allow_release and not (gear == 'S' and speed_kph > 15.0) and not cold_coolant:
        stopped_warm = speed_kph < 1.5 and self._hev_ev_debt_wh < 230.0
        light_ev_cruise = p_wheel < 7.5 and speed_kph < 82.0 and gas_pct < 18.0 and self._hev_ev_debt_wh < 110.0
        if stopped_warm or (braking and p_wheel < -0.8) or light_ev_cruise:
          self._hev_ice_on = False

    dfco = self._hev_ice_on and (
      (braking and p_wheel < -0.8) or (p_wheel < -2.5 and gas_pct < 1.5 and speed_kph > 25.0)
    ) and not cold_coolant

    if not self._hev_ice_on or dfco:
      if p_wheel < -0.5:
        regen_kw = min(30.0, (-p_wheel) * 0.62)
        self._hev_ev_debt_wh = max(-120.0, min(380.0, self._hev_ev_debt_wh - (regen_kw - p_aux_kw) * (dt / 3.6)))
      else:
        ev_batt_kw = (max(0.0, p_wheel) / 0.88) + p_aux_kw
        self._hev_ev_debt_wh = min(380.0, self._hev_ev_debt_wh + ev_batt_kw * (dt / 3.6))
      can_state.synthetic_engine_rpm = round(max(1100.0, 900.0 + speed_kph * 16.0), 0) if dfco else 0.0
      can_state.synthetic_engine_firing = False
      return 0.0

    p_charge_kw = min(14.0, max(3.0, self._hev_ev_debt_wh * 0.055)) if self._hev_ev_debt_wh > 25.0 else 0.0
    if p_wheel < -0.5:
      regen_kw = min(25.0, (-p_wheel) * 0.58)
      self._hev_ev_debt_wh = max(-120.0, self._hev_ev_debt_wh - (regen_kw + p_charge_kw * 0.88 - p_aux_kw) * (dt / 3.6))
    else:
      self._hev_ev_debt_wh = max(-80.0, self._hev_ev_debt_wh - (p_charge_kw * 0.88 - p_aux_kw) * (dt / 3.6))

    p_ice_mech_kw = min(77.0, max(3.2, (max(0.0, p_wheel) / 0.90) + p_charge_kw + 0.6))
    syn_rpm = max(1250.0, min(5200.0, 1250.0 + speed_kph * 18.0 + p_ice_mech_kw * 28.0))
    eq_torque_pct = min(98.0, max(12.0, (p_ice_mech_kw * 9549.0 / syn_rpm) / MAX_ENGINE_TORQUE_NM * 100.0))
    bsfc = estimate_bsfc_g_per_kwh(syn_rpm, eq_torque_pct, can_state.coolant_temp_c)
    can_state.synthetic_engine_rpm = round(syn_rpm, 0)
    can_state.synthetic_engine_firing = True
    return max(0.55, (p_ice_mech_kw * bsfc) / GASOLINE_DENSITY_G_PER_L) * self.calibration_factor

  def update(self, can_state: IoniqCanState, mono_s: float, *, wall_time: float | None = None,
             ignition_hint: bool | None = None, force_flush: bool = False) -> dict[str, Any]:
    """Advance trip & tank accumulators with the latest CAN state."""
    now = wall_time if wall_time is not None else datetime.now(UTC).timestamp()
    speed_kph = can_state.best_speed_kph()
    ign = ignition_hint if ignition_hint is not None else can_state.ignition_on
    gear = can_state.gear_shifter_label()
    driving_or_ready = bool(
      ign is True
      or speed_kph > 0.5
      or can_state.engine_rpm > 200.0
      or (ign is not False and gear in ('D', 'S', 'R'))
    )

    if driving_or_ready:
      self._off_since_mono_s = None
      trip = self._ensure_active_trip(can_state, mono_s, now)
    else:
      self._last_instant_lph = 0.0
      can_state.instant_fuel_rate_lph = 0.0
      can_state.synthetic_engine_firing = False
      can_state.synthetic_engine_rpm = 0.0
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
      return self.snapshot(can_state)

    dt = 0.0 if self._last_mono_s is None else max(0.0, min(5.0, mono_s - self._last_mono_s))
    self._last_mono_s = mono_s
    if dt <= 0.0:
      if force_flush and self._dirty:
        self.flush(mono_s)
      return self.snapshot(can_state)

    trip = self.state['activeTrip']
    tank = self.state['currentTank']
    prev_fuel_total = float(trip['fuelLiters'])

    # Stationary timer for false-positive-free Volltanken detection
    if speed_kph < 3.0:
      self._stationary_duration_s += dt
    else:
      self._stationary_duration_s = 0.0

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

    # Evaluate HEV energy-balance model BEFORE computing firing / EV split when direct RPM is absent on C-CAN
    has_direct_rpm = can_state.has_direct_engine_rpm()
    has_obd_fuel = bool(
      (can_state.obd_fuel_rate_lph is not None and can_state.obd_fuel_rate_lph > 0.05)
      or (can_state.obd_maf_gps is not None and can_state.obd_maf_gps > 0.2)
    )
    if not has_direct_rpm and not has_obd_fuel:
      instant_lph = self._step_hev_model(can_state, trip, dt, mono_s, speed_kph)
    else:
      if not has_direct_rpm and has_obd_fuel:
        can_state.synthetic_engine_firing = True
      instant_lph = estimate_fuel_rate_lph(can_state, calibration_factor=self.calibration_factor)
    can_state.instant_fuel_rate_lph = instant_lph
    self._last_instant_lph = instant_lph
    trip['instantFuelRateLph'] = round(instant_lph, 2)

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

    if firing:
      trip['iceDistanceKm'] = round(trip['iceDistanceKm'] + dist_inc, 3)
      tank['iceDistanceKm'] = round(float(tank.get('iceDistanceKm', 0.0)) + dist_inc, 3)
    else:
      trip['evDistanceKm'] = round(trip['evDistanceKm'] + dist_inc, 3)
      tank['evDistanceKm'] = round(float(tank.get('evDistanceKm', 0.0)) + dist_inc, 3)

    tank['distanceKm'] = round(float(tank.get('distanceKm', 0.0)) + dist_inc, 3)
    if odo is not None:
      tank['lastOdometerKm'] = odo

    model_inc = instant_lph * (dt / 3600.0)
    trip['fuelLitersModel'] = round(float(trip.get('fuelLitersModel', 0.0)) + model_inc, 5)

    clu_l100 = cluster_avg_l_per_100km(can_state)
    cluster_inc = (dist_inc * clu_l100 / 100.0) if (clu_l100 is not None and dist_inc > 0.0) else 0.0
    trip['fuelLitersClusterFallback'] = round(float(trip.get('fuelLitersClusterFallback', 0.0)) + cluster_inc, 5)

    fco_inc = 0.0
    fco_step_valid = False
    if can_state.fco_raw is not None:
      if self._last_fco_raw is not None:
        raw_delta = (can_state.fco_raw - self._last_fco_raw) & 0xFFFF
        # Sanity check per step (never permanently poison the trip on a single spike)
        if 0 < raw_delta <= 8000 and (can_state.engine_rpm > 200.0 or raw_delta <= 150):
          fco_inc = raw_delta * self.fco_scale_ul * 1e-6 * self.calibration_factor
          self._fco_valid_ticks += 1
          fco_step_valid = True
        elif raw_delta > 8000 or (raw_delta > 200 and can_state.engine_rpm <= 50.0):
          self._fco_rejected_ticks += 1
      self._last_fco_raw = can_state.fco_raw

    if fco_step_valid:
      trip['fuelLitersFco'] = round(float(trip.get('fuelLitersFco', 0.0)) + fco_inc, 5)

    fco_total = float(trip.get('fuelLitersFco', 0.0))
    model_total = float(trip.get('fuelLitersModel', 0.0))
    cluster_total = float(trip.get('fuelLitersClusterFallback', 0.0))

    # Choose the most authoritative non-zero source without allowing a dead FCO counter to zero out consumption
    if self._fco_valid_ticks >= 2 and fco_total >= max(0.0005, model_total * 0.25):
      step_inc = fco_inc if fco_step_valid else (model_inc if firing else 0.0)
      trip['fuelLiters'] = round(max(fco_total, prev_fuel_total + step_inc), 5)
      trip['sourceMode'] = 'fco_can'
    elif has_obd_fuel and (model_total > 0.0001 or firing):
      trip['fuelLiters'] = round(max(model_total, prev_fuel_total + model_inc), 5)
      trip['sourceMode'] = 'obd_pid'
    elif has_direct_rpm and (model_total > 0.0001 or firing):
      trip['fuelLiters'] = round(max(model_total, prev_fuel_total + model_inc), 5)
      trip['sourceMode'] = 'torque_model'
    elif not has_direct_rpm and (model_total > 0.0001 or firing):
      trip['fuelLiters'] = round(max(model_total, prev_fuel_total + model_inc), 5)
      trip['sourceMode'] = 'hev_model'
    elif cluster_total > 0.0001:
      trip['fuelLiters'] = round(max(cluster_total, prev_fuel_total + cluster_inc), 5)
      trip['sourceMode'] = 'cluster_avg_fci'
    else:
      trip['fuelLiters'] = round(prev_fuel_total, 5)
      trip['sourceMode'] = 'torque_model' if has_direct_rpm else 'hev_model'

    fuel_inc = max(0.0, float(trip['fuelLiters']) - prev_fuel_total)
    tank['estimatedFuelLiters'] = round(float(tank.get('estimatedFuelLiters', 0.0)) + fuel_inc, 4)

    # Derived trip KPIs
    total_dur = max(0.001, float(trip['durationS']))
    trip['evTimePct'] = round(float(trip['evDurationS']) / total_dur * 100.0, 1)
    if float(trip['distanceKm']) >= 0.05:
      trip['evDistancePct'] = round(min(100.0, max(0.0, float(trip['evDistanceKm']) / float(trip['distanceKm']) * 100.0)), 1)
      l100 = float(trip['fuelLiters']) / float(trip['distanceKm']) * 100.0
      trip['avgLPer100Km'] = round(l100, 2)
      trip['avgKmPerL'] = round(float(trip['distanceKm']) / float(trip['fuelLiters']), 2) if float(trip['fuelLiters']) > 0 else None
    elif clu_l100 is not None and float(trip['distanceKm']) > 0.0:
      trip['evDistancePct'] = 0.0
      trip['avgLPer100Km'] = clu_l100
      trip['avgKmPerL'] = round(100.0 / clu_l100, 2) if clu_l100 > 0 else None
    trip['estimatedCostEur'] = round(float(trip['fuelLiters']) * self.price_per_liter, 2)
    trip['maxSpeedKph'] = round(max(float(trip['maxSpeedKph']), speed_kph), 1)
    if can_state.coolant_temp_c is not None:
      prev_cool = trip.get('maxCoolantC')
      trip['maxCoolantC'] = round(can_state.coolant_temp_c if prev_cool is None else max(prev_cool, can_state.coolant_temp_c), 1)
    if can_state.ambient_temp_c is not None:
      trip['ambientTempC'] = round(can_state.ambient_temp_c, 1)
    if clu_l100 is not None and can_state.cluster_avg_fc_value is not None:
      trip['clusterAvgFci'] = can_state.cluster_avg_fc_value
      trip['clusterAvgLPer100Km'] = clu_l100
    if can_state.bat_12v_voltage_v is not None:
      trip['bat12vEndV'] = can_state.bat_12v_voltage_v
      if trip.get('bat12vStartV') is None:
        trip['bat12vStartV'] = can_state.bat_12v_voltage_v

    # Fuel gauge & automatic refuel ("Volltanken") detection:
    # Ignore 0-bar cold-boot init frames before gauge settles; require >= 5 valid (>0) samples
    # and >= 8.0s continuous stationary (speed < 3 kph) before a >= 3-bar upward jump triggers refuelAlert.
    lvl = can_state.fuel_disp_level
    if lvl is not None and 0 < lvl <= 31:
      self._valid_fuel_disp_samples += 1
      trip['fuelDispEnd'] = lvl
      if not trip.get('fuelDispStart'):
        trip['fuelDispStart'] = lvl
      tank['fuelDispCurrent'] = lvl
      if tank.get('fuelDispStart') is None:
        tank['fuelDispStart'] = lvl
      if self._valid_fuel_disp_samples >= 5:
        if self._min_fuel_disp_seen is None or lvl < self._min_fuel_disp_seen:
          self._min_fuel_disp_seen = lvl
        elif (lvl - self._min_fuel_disp_seen) >= 3 and self._stationary_duration_s >= 8.0:
          delta_bars = int(lvl - self._min_fuel_disp_seen)
          est_added_liters = round(delta_bars / 31.0 * self.tank_capacity_liters, 1)
          self.state['refuelAlert'] = {
            'detectedAt': now,
            'previousLevel': self._min_fuel_disp_seen,
            'deltaBars': delta_bars,
            'currentLevel': lvl,
            'fromPct': round(self._min_fuel_disp_seen / 31.0 * 100.0, 0),
            'toPct': round(lvl / 31.0 * 100.0, 0),
            'estimatedAddedLiters': est_added_liters,
            'isLikelyFullTank': bool(lvl >= 28),
            'odometerKm': odo,
          }
          self._min_fuel_disp_seen = lvl
    if can_state.dte_km is not None:
      tank['dteKm'] = can_state.dte_km

    trip['sourceDiagnostics'] = self._build_source_diagnostics(can_state, trip)

    self._dirty = True
    if force_flush or (mono_s - self._last_flush_mono_s) >= 10.0:
      self.flush(mono_s)
    return self.snapshot(can_state)

  def start_manual_trip(self, *, label: str = '', wall_time: float | None = None) -> dict[str, Any]:
    now = wall_time if wall_time is not None else datetime.now(UTC).timestamp()
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
    now = wall_time if wall_time is not None else datetime.now(UTC).timestamp()
    trip['endedAt'] = now
    self.state['activeTrip'] = None

    # Save meaningful trips (> 15s or > 0.05 km); discard accidental 2-second boot blips unless labeled
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
    cost_eur: float | None = None,
    odometer_km: float | None = None,
    notes: str = '',
    wall_time: float | None = None,
  ) -> dict[str, Any]:
    """Record a refuel; accepts either price_per_liter or total cost_eur (or both) and calibrates Tank-to-Tank."""
    if liters <= 0.1 or liters > 90.0:
      raise ValueError('Fuel quantity must be between 0.1 and 90 liters')
    now = wall_time if wall_time is not None else datetime.now(UTC).timestamp()
    if cost_eur is not None and cost_eur > 0.0 and (price_per_liter is None or price_per_liter <= 0.0):
      unit_price = float(cost_eur) / float(liters)
      total_cost = round(float(cost_eur), 2)
    elif price_per_liter is not None and price_per_liter > 0.0:
      unit_price = float(price_per_liter)
      total_cost = round(float(cost_eur), 2) if (cost_eur is not None and cost_eur > 0.0) else round(float(liters) * unit_price, 2)
    else:
      unit_price = float(self.price_per_liter)
      total_cost = round(float(cost_eur), 2) if (cost_eur is not None and cost_eur > 0.0) else round(float(liters) * unit_price, 2)
    self.price_per_liter = round(unit_price, 3)
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
      tank_cost_eur = round(actual_liters * unit_price, 2)
      cost_per_100km = round(tank_cost_eur / actual_dist * 100.0, 2) if actual_dist >= 1.0 else None

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
        'refuelId': refuel_entry['id'],
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
        'pricePerLiter': round(unit_price, 3),
        'costEur': tank_cost_eur,
        'costPer100Km': cost_per_100km,
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

  def update_refuel(
    self,
    refuel_id: str,
    *,
    liters: float | None = None,
    price_per_liter: float | None = None,
    cost_eur: float | None = None,
    odometer_km: float | None = None,
    notes: str | None = None,
  ) -> dict[str, Any]:
    """Edit an existing refuel log entry and synchronize any matching closedTank record."""
    target_id = (refuel_id or '').strip()
    if not target_id:
      raise ValueError('refuel_id is required')
    log = list(self.state.get('refuelLog') or [])
    found: dict[str, Any] | None = None
    for entry in log:
      if entry.get('id') == target_id:
        found = entry
        break
    if found is None:
      raise KeyError(f'Refuel entry {target_id} not found')

    new_liters = float(liters) if liters is not None else float(found.get('liters', 0.0))
    if new_liters <= 0.1 or new_liters > 90.0:
      raise ValueError('Fuel quantity must be between 0.1 and 90 liters')
    if cost_eur is not None and cost_eur > 0.0 and (price_per_liter is None or price_per_liter <= 0.0):
      new_price = float(cost_eur) / new_liters
      new_cost = round(float(cost_eur), 2)
    elif price_per_liter is not None and price_per_liter > 0.0:
      new_price = float(price_per_liter)
      new_cost = round(float(cost_eur), 2) if (cost_eur is not None and cost_eur > 0.0) else round(new_liters * new_price, 2)
    else:
      new_price = float(found.get('pricePerLiter', self.price_per_liter))
      new_cost = round(new_liters * new_price, 2)

    found['liters'] = round(new_liters, 2)
    found['pricePerLiter'] = round(new_price, 3)
    found['costEur'] = new_cost
    if odometer_km is not None:
      found['odometerKm'] = round(float(odometer_km), 1)
    if notes is not None:
      found['notes'] = str(notes).strip()[:120]

    for tank in self.state.get('tankHistory', []):
      if tank.get('refuelId') == target_id or tank.get('endedAt') == found.get('loggedAt'):
        dist = float(tank.get('distanceKm') or 0.0)
        tank['actualLiters'] = round(new_liters, 2)
        tank['pricePerLiter'] = round(new_price, 3)
        tank['costEur'] = new_cost
        if dist >= 1.0:
          tank['actualLPer100Km'] = round(new_liters / dist * 100.0, 2)
          tank['actualKmPerL'] = round(dist / new_liters, 2)
          tank['costPer100Km'] = round(new_cost / dist * 100.0, 2)
        if notes is not None:
          tank['notes'] = found['notes']
        break

    self._dirty = True
    self.flush(0.0)
    return found

  def delete_refuel(self, refuel_id: str) -> bool:
    """Delete a refuel entry and any associated closed tank entry."""
    target_id = (refuel_id or '').strip()
    if not target_id:
      return False
    log = list(self.state.get('refuelLog') or [])
    removed_entry = next((e for e in log if e.get('id') == target_id), None)
    if removed_entry is None:
      return False
    self.state['refuelLog'] = [e for e in log if e.get('id') != target_id]
    self.state['tankHistory'] = [
      t for t in (self.state.get('tankHistory') or [])
      if t.get('refuelId') != target_id and t.get('endedAt') != removed_entry.get('loggedAt')
    ]
    self._dirty = True
    self.flush(0.0)
    return True

  def reset_calibration(self, factor: float = 1.0) -> float:
    """Reset the fuel calibration factor to default (1.0) or a user-specified value."""
    clamped = round(max(0.5, min(1.75, float(factor))), 4)
    self.calibration_factor = clamped
    self.state['calibrationFactor'] = clamped
    self._fco_rejected_ticks = 0
    self._dirty = True
    self.flush(0.0)
    return clamped

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

  def snapshot(self, can_state: IoniqCanState | None = None) -> dict[str, Any]:
    tanks = self.state.get('tankHistory', [])
    refuels = self.state.get('refuelLog', [])
    valid_l100 = [t['actualLPer100Km'] for t in tanks if isinstance(t.get('actualLPer100Km'), (int, float))]
    valid_cost100 = [t['costPer100Km'] for t in tanks if isinstance(t.get('costPer100Km'), (int, float))]
    total_refuel_liters = round(sum(float(r.get('liters', 0.0)) for r in refuels), 2)
    total_refuel_cost = round(sum(float(r.get('costEur', 0.0)) for r in refuels), 2)
    cur = self.state.get('currentTank', {})
    cur_est_l100 = None
    if float(cur.get('distanceKm', 0.0)) >= 0.1 and float(cur.get('estimatedFuelLiters', 0.0)) > 0.0:
      cur_est_l100 = round(float(cur.get('estimatedFuelLiters', 0.0)) / float(cur.get('distanceKm', 1.0)) * 100.0, 2)
    active_trip = self.state.get('activeTrip')
    diag = (
      self._build_source_diagnostics(can_state, active_trip)
      if can_state is not None
      else (active_trip.get('sourceDiagnostics') if isinstance(active_trip, dict) else {
        'activeSource': 'hev_model',
        'activeSourceLabel': 'HEV C-CAN Modell (Bereit)',
        'instantFuelRateLph': round(self._last_instant_lph, 2),
        'calibrationFactor': round(self.calibration_factor, 4),
      })
    )
    return {
      'activeTrip': active_trip,
      'trips': self.state.get('trips', []),
      'currentTank': dict(cur, estimatedLPer100Km=cur_est_l100),
      'tankHistory': tanks,
      'refuelLog': refuels,
      'refuelAlert': self.state.get('refuelAlert'),
      'calibrationFactor': round(self.calibration_factor, 4),
      'lifetimeTankAvgLPer100Km': round(sum(valid_l100) / len(valid_l100), 2) if valid_l100 else None,
      'lifetimeCostPer100Km': round(sum(valid_cost100) / len(valid_cost100), 2) if valid_cost100 else None,
      'totalLoggedLiters': total_refuel_liters,
      'totalLoggedCostEur': total_refuel_cost,
      'sourceDiagnostics': diag,
    }
