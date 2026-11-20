"""Pure-Python CAN signal decoder for 2016 Hyundai Ioniq Hybrid (AE HEV, classic CAN)."""

from __future__ import annotations

from dataclasses import dataclass, field

# Decimal CAN IDs -> hex:
ADDR_DATC11 = 0x044      # 68
ADDR_EMS_DCT11 = 0x080   # 128
ADDR_EMS_DCT12 = 0x081   # 129
ADDR_TCU_DCT13 = 0x162   # 354
ADDR_EMS_H12 = 0x18F     # 399
ADDR_EMS20 = 0x200       # 512
ADDR_ESP12 = 0x220       # 544
ADDR_MDPS12 = 0x251      # 593
ADDR_EMS16 = 0x260       # 608
ADDR_EMS13 = 0x280       # 640
ADDR_EMS11 = 0x316       # 790
ADDR_EMS12 = 0x329       # 809
ADDR_LKAS11 = 0x340      # 832
ADDR_EMS_366 = 0x366     # 870
ADDR_E_EMS11 = 0x371     # 881
ADDR_ELECT_GEAR = 0x372  # 882
ADDR_WHL_SPD11 = 0x386   # 902
ADDR_ABS11 = 0x38A       # 906
ADDR_FCA11 = 0x38D       # 909
ADDR_TCS13 = 0x394       # 916
ADDR_SCC11 = 0x420       # 1056
ADDR_SCC12 = 0x421       # 1057
ADDR_P_STS = 0x470       # 1136
ADDR_LFAHDA_MFC = 0x485  # 1157
ADDR_EPB11 = 0x490       # 1168
ADDR_EMS19 = 0x492       # 1170
ADDR_CLU11 = 0x4F1       # 1265
ADDR_TCU14 = 0x502       # 1282
ADDR_TCS15 = 0x507      # 1287
ADDR_CLU13 = 0x50C      # 1292
ADDR_CLU15 = 0x52A      # 1322
ADDR_EMS14 = 0x545      # 1349
ADDR_EMS15 = 0x547      # 1351
ADDR_BAT11 = 0x549      # 1353
ADDR_TCU_DCT14 = 0x54C  # 1356
ADDR_ENG_FRZ_11 = 0x556  # 1366
ADDR_LCA11 = 0x58B      # 1419
ADDR_TPMS11 = 0x593     # 1427
ADDR_HEV_595 = 0x595    # 1429
ADDR_ACU11 = 0x5A0      # 1440
ADDR_CLU12 = 0x5B0      # 1456

WATCHED_FUEL_ADDRS: frozenset[int] = frozenset((
  ADDR_EMS_H12,
  ADDR_EMS20,
  ADDR_EMS16,
  ADDR_EMS11,
  ADDR_EMS12,
  ADDR_EMS_366,
  ADDR_E_EMS11,
  ADDR_CLU13,
  ADDR_CLU15,
  ADDR_EMS14,
  ADDR_EMS15,
  ADDR_BAT11,
  ADDR_HEV_595,
  ADDR_CLU12,
))

LIVE_ECU_CHANNELS: tuple[tuple[str, str, tuple[int, ...]], ...] = (
  ('ECM', 'Motorsteuergerät (1.6 GDi Kappa)', (ADDR_EMS11, ADDR_EMS12, ADDR_EMS16, ADDR_EMS14, ADDR_EMS15, ADDR_EMS20, ADDR_EMS_366, ADDR_EMS19)),
  ('TCU', 'Getriebesteuergerät (6-Gang DCT)', (ADDR_TCU_DCT13, ADDR_TCU_DCT14, ADDR_TCU14)),
  ('HCU', 'Hybrid-Steuergerät (HCU / VCU)', (ADDR_EMS_H12, ADDR_E_EMS11, ADDR_HEV_595, ADDR_P_STS)),
  ('SCC', 'Frontradar (Smart Cruise Control)', (ADDR_SCC11, ADDR_SCC12, ADDR_FCA11)),
  ('ESC', 'ABS / ESC / Bremse', (ADDR_WHL_SPD11, ADDR_ABS11, ADDR_TCS13, ADDR_TCS15)),
  ('MDPS', 'Servolenkung (MDPS / EPS)', (ADDR_MDPS12,)),
  ('MFC', 'Frontkamera (LKAS / AEB)', (ADDR_LKAS11, ADDR_LFAHDA_MFC)),
  ('CLU', 'Kombiinstrument (Tacho)', (ADDR_CLU11, ADDR_CLU12, ADDR_CLU13, ADDR_CLU15)),
  ('DATC', 'Klimaautomatik (DATC)', (ADDR_DATC11,)),
  ('IBS', '12V-Batteriesensor (BAT11)', (ADDR_BAT11,)),
  ('EPB', 'Elektrische Parkbremse (EPB)', (ADDR_EPB11,)),
  ('TPMS', 'Reifendruckkontrolle (TPMS)', (ADDR_TPMS11,)),
  ('ACU', 'Airbag-Steuergerät (ACU)', (ADDR_ACU11,)),
  ('BSD', 'Totwinkelradar (BSM / BSD)', (ADDR_LCA11,)),
)


def extract_intel(data: bytes | bytearray | memoryview, start_bit: int, length: int, *,
                  signed: bool = False, scale: float = 1.0, offset: float = 0.0) -> float | int:
  """Decode an Intel (little-endian) DBC signal without compiled DBC bindings."""
  if length <= 0 or start_bit < 0:
    raise ValueError('Invalid CAN signal bit specification')
  raw_bytes = bytes(data)
  if len(raw_bytes) * 8 < start_bit + length:
    raw_bytes = raw_bytes.ljust((start_bit + length + 7) // 8, b'\x00')
  packed = int.from_bytes(raw_bytes, 'little')
  mask = (1 << length) - 1
  raw = (packed >> start_bit) & mask
  if signed and (raw & (1 << (length - 1))):
    raw -= (1 << length)
  if scale == 1.0 and offset == 0.0:
    return int(raw)
  return raw * scale + offset


@dataclass
class IoniqCanState:
  """Latest decoded 2016 Hyundai Ioniq Hybrid CAN telemetry snapshot."""

  last_update_s: float = 0.0
  seen_addresses: dict[int, float] = field(default_factory=dict)
  seen_bus0_addresses: set[int] = field(default_factory=set)
  seen_address_bus: dict[int, int] = field(default_factory=dict)
  address_counts: dict[int, int] = field(default_factory=dict)
  raw_fuel_frames: dict[str, str] = field(default_factory=dict)

  # Powertrain / Engine / Hybrid (EMS11, EMS12, EMS14, EMS_H12, EMS20)
  ignition_on: bool | None = None
  engine_running_flag: bool = False
  engine_running_normal: bool = False
  engine_rpm: float = 0.0
  vehicle_speed_ems_kph: float = 0.0
  engine_torque_pct: float = 0.0
  tqi_b_pct: float = 0.0
  puc_stat_fuel_cut: bool = False
  pre_fuel_cut_in: bool = False
  ems16_eng_stat: int = 0
  coolant_temp_c: float | None = None
  fuel_temp_c: float | None = None
  ambient_temp_c: float | None = None
  cabin_temp_c: float | None = None
  intake_air_temp_c: float | None = None
  ac_relay_on: bool = False
  isg_stat: int = 0
  eco_active: bool = False
  fuel_cut_idle_stop: bool = False
  fuel_cut_open: bool = False
  fco_raw: int | None = None
  ems14_fco_raw: int | None = None
  eng_oil_temp_c: float | None = None
  bat_lamp_stat: bool = False
  ops_fail: bool = False
  mil_lamp: bool = False
  limp_home_mode: bool = False
  ec_fail: bool = False
  blec_fail: bool = False
  vb_ems_v: float | None = None

  # Cluster / Fuel / Odometer (CLU12, CLU13, CLU15, WHL_SPD11)
  odometer_km: float | None = None
  cluster_speed_kph: float = 0.0
  wheel_speed_kph: float = 0.0
  wheel_speeds_kph: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
  fuel_disp_level: int | None = None       # 0..31 bars on CLU13
  low_fuel_warn: int = 0
  cluster_avg_fc_value: float | None = None
  cluster_avg_fc_unit: int | None = None
  dte_km: int | None = None
  drive_mode_switch: int = 0
  active_eco_switch: bool = False
  alt_lamp_status: bool = False

  # 12V Battery Sensor (BAT11)
  bat_12v_soc_pct: float | None = None
  bat_12v_voltage_v: float | None = None
  bat_12v_current_a: float | None = None
  bat_12v_temp_c: float | None = None
  bat_12v_soh_pct: float | None = None
  bat_12v_error: bool = False
  bat_12v_invalid: bool = False

  # Chassis / DCT / Safety fault lamps
  tcu14_raw_warn_msg: int = 0
  tcu14_raw_dri_warn1: int = 0
  tcu14_raw_dri_warn2: int = 0
  tcu_warn_msg: int = 0
  tcu_warn_img: bool = False
  tcu_dri_warn1: int = 0
  tcu_dri_warn2: int = 0
  dct_hill_hold_warn: bool = False
  abs11_defect: bool = False
  abs11_ebd_defect: bool = False
  abs11_warn_lamp: bool = False
  abs11_ebd_warn_lamp: bool = False
  tcs15_abs_warn_lamp: bool = False
  tcs15_ebd_warn_lamp: bool = False
  abs_defect: bool = False
  ebd_defect: bool = False
  abs_warn_lamp: bool = False
  ebd_warn_lamp: bool = False
  tcs_lamp: int = 0
  tcs_off_lamp: int = 0
  fca11_raw_fail_info: int = 0
  fca_fail_info: int = 0
  mdps_def: bool = False
  mdps_fail_stat: bool = False
  mdps_serr: bool = False
  tcs13_acc_enable: int = 0
  scc_fail_info: int = 0
  aeb_fail_info: int = 0
  lfa_sys_warning: int = 0
  epb_f_lamp: int = 0
  epb_fail: int = 0
  tpms_w_lamp: int = 0
  tread_w_lamp: int = 0
  tpms_pos_warn: tuple[bool, bool, bool, bool] = (False, False, False, False)
  acu_flt_stat: int = 0
  acu_dtc_raw: int = 0
  acu_num_flt: int = 0
  freeze_frame_rpm: float | None = None
  freeze_frame_load_pct: float | None = None

  # Optional OBD-II Mode 01 fallback telemetry (populated only during ELM327 diagnostic scan)
  obd_fuel_rate_lph: float | None = None
  obd_maf_gps: float | None = None
  obd_fuel_level_pct: float | None = None
  obd_coolant_c: float | None = None
  obd_control_module_v: float | None = None

  # Classic Hyundai HEV Bus-0 / C-CAN signals (E_EMS11, ELECT_GEAR, TCS13, ESP12, SCC12)
  accel_pedal_raw: float = 0.0
  vcu_accel_pedal_raw: float = 0.0
  gas_pedal_pct: float = 0.0
  e_ems11_brake_pos: int = 0
  ig_reactive_stat: int = 0
  gear_shifter_raw: int | None = None
  long_accel_mps2: float | None = None
  brake_cyl_press: float = 0.0
  tcs_a_basis_mps2: float | None = None
  tcs_driver_braking: bool = False
  tcs_brake_light: bool = False
  tcs_driver_override: int = 0
  tcs_acc_req: bool = False
  tqi_scc_pct: float = 0.0
  scc_acc_mode: int = 0
  scc_a_req_mps2: float | None = None

  # Derived wheel kinematics & optional Cereal carState mirror
  wheel_accel_mps2: float | None = None
  _prev_wheel_speed_kph: float | None = None
  _prev_wheel_ts: float | None = None
  cs_last_update_s: float = 0.0
  cs_v_ego_mps: float | None = None
  cs_a_ego_mps2: float | None = None
  cs_gas_pressed: bool | None = None
  cs_brake_pressed: bool | None = None
  cs_standstill: bool | None = None
  cs_gear_shifter: str | None = None
  cs_cruise_enabled: bool | None = None

  # Synthetic HEV model outputs (populated when C-CAN lacks direct EMS11/EMS_DCT11 RPM)
  synthetic_engine_rpm: float = 0.0
  synthetic_engine_firing: bool = False
  instant_fuel_rate_lph: float = 0.0
  _tcu_dct13_rpm_valid: bool = False

  def best_speed_kph(self) -> float:
    if ADDR_WHL_SPD11 in self.seen_addresses:
      return self.wheel_speed_kph
    if ADDR_CLU15 in self.seen_addresses:
      return self.cluster_speed_kph
    if self.vehicle_speed_ems_kph > 0.0 or ADDR_EMS11 in self.seen_addresses:
      return self.vehicle_speed_ems_kph
    if self.cs_v_ego_mps is not None:
      return max(0.0, float(self.cs_v_ego_mps) * 3.6)
    return 0.0

  def has_direct_engine_rpm(self) -> bool:
    return bool(
      ADDR_EMS11 in self.seen_addresses
      or ADDR_EMS_366 in self.seen_addresses
      or ADDR_EMS_DCT11 in self.seen_addresses
      or ADDR_EMS13 in self.seen_addresses
      or self._tcu_dct13_rpm_valid
    )

  def gear_shifter_label(self) -> str:
    if self.gear_shifter_raw is not None:
      return {0: 'P', 4: 'S', 5: 'D', 6: 'N', 7: 'R', 8: 'S'}.get(self.gear_shifter_raw, '')
    if self.cs_gear_shifter:
      g = str(self.cs_gear_shifter).lower()
      if 'park' in g or g == 'p':
        return 'P'
      if 'reverse' in g or g == 'r':
        return 'R'
      if 'neutral' in g or g == 'n':
        return 'N'
      if 'sport' in g or g == 'low' or g == 's':
        return 'S'
      if 'drive' in g or g == 'd':
        return 'D'
    return ''

  def is_braking_or_regen(self) -> bool:
    return bool(
      self.tcs_driver_braking
      or self.tcs_brake_light
      or self.brake_cyl_press > 3.0
      or self.e_ems11_brake_pos > 2
      or self.cs_brake_pressed
    )

  def effective_accel_mps2(self) -> float:
    if self.wheel_accel_mps2 is not None:
      return self.wheel_accel_mps2
    if self.cs_a_ego_mps2 is not None:
      return float(self.cs_a_ego_mps2)
    if self.scc_acc_mode in (1, 2) and self.scc_a_req_mps2 is not None:
      return float(self.scc_a_req_mps2)
    return 0.0

  def tractive_power_kw(self) -> float:
    """Longitudinal wheel power demand (kW) for 2016 Hyundai Ioniq Hybrid (1480 kg, Cd=0.24, A=2.24 m^2)."""
    v_mps = max(0.0, self.best_speed_kph() / 3.6)
    gas_pct = self.gas_pedal_pct
    if self.cs_gas_pressed and gas_pct <= 1.0:
      gas_pct = 18.0
    if v_mps < 0.25:
      if gas_pct > 5.0 and self.gear_shifter_label() in ('D', 'S', 'R', ''):
        return min(35.0, (gas_pct - 5.0) * 0.35)
      return 0.0
    mass_kg = 1480.0
    f_roll = 0.0090 * mass_kg * 9.80665
    f_aero = 0.5 * 1.225 * 0.24 * 2.24 * (v_mps * v_mps)
    accel = self.effective_accel_mps2()
    if self.scc_acc_mode in (1, 2) and self.scc_a_req_mps2 is not None:
      accel = 0.65 * accel + 0.35 * float(self.scc_a_req_mps2)
    f_inertia = (mass_kg * 1.04) * accel
    power_kw = (f_roll + f_aero + f_inertia) * v_mps / 1000.0
    if gas_pct > 8.0 and v_mps < 14.0 and accel > -0.15:
      power_kw = max(power_kw, (gas_pct - 5.0) * 0.42)
    return max(-45.0, min(105.0, power_kw))

  def cluster_avg_fc_unit_label(self) -> str:
    return {
      0: 'km/L',
      1: 'L/100km',
      2: 'MPG (US)',
      3: 'MPG (UK)',
    }.get(self.cluster_avg_fc_unit if self.cluster_avg_fc_unit is not None else 1, 'L/100km')

  def cluster_avg_l_per_100km(self) -> float | None:
    val = self.cluster_avg_fc_value
    if val is None or val <= 0.1 or val >= 99.0:
      return None
    # 0: km/L, 1: L/100km (EU), 2: MPG (US), 3: MPG (UK)
    if self.cluster_avg_fc_unit == 1:
      return round(float(val), 2)
    if self.cluster_avg_fc_unit == 0:
      return round(100.0 / val, 2) if val > 0.5 else None
    if self.cluster_avg_fc_unit == 2:
      return round(235.215 / val, 2) if val > 1.0 else None
    if self.cluster_avg_fc_unit == 3:
      return round(282.481 / val, 2) if val > 1.0 else None
    return round(float(val), 2) if val <= 15.0 else round(100.0 / val, 2)

  def engine_is_firing(self) -> bool:
    if not self.has_direct_engine_rpm():
      return bool(self.synthetic_engine_firing)
    if self.fuel_cut_idle_stop or self.fuel_cut_open:
      return False
    if (self.puc_stat_fuel_cut or self.pre_fuel_cut_in) and max(self.engine_torque_pct, self.tqi_b_pct) <= 2.0:
      return False
    if self.engine_rpm > 450.0:
      return True
    return self.engine_rpm > 250.0 and (
      self.engine_running_flag or self.engine_running_normal or self.engine_torque_pct > 1.0 or self.tqi_b_pct > 1.0
    )

  def apply_car_state(self, car_state: object, ts: float) -> None:
    """Ingest optional Cereal carState snapshot when available alongside raw C-CAN frames."""
    if car_state is None:
      return
    self.cs_last_update_s = ts
    v_ego = getattr(car_state, 'vEgo', None)
    if isinstance(v_ego, (int, float)):
      self.cs_v_ego_mps = float(v_ego)
    a_ego = getattr(car_state, 'aEgo', None)
    if isinstance(a_ego, (int, float)):
      self.cs_a_ego_mps2 = float(a_ego)
    gas_pressed = getattr(car_state, 'gasPressed', None)
    if isinstance(gas_pressed, bool):
      self.cs_gas_pressed = gas_pressed
    brake_pressed = getattr(car_state, 'brakePressed', None)
    if isinstance(brake_pressed, bool):
      self.cs_brake_pressed = brake_pressed
    standstill = getattr(car_state, 'standstill', None)
    if isinstance(standstill, bool):
      self.cs_standstill = standstill
    gear_shifter = getattr(car_state, 'gearShifter', None)
    if gear_shifter is not None:
      self.cs_gear_shifter = str(gear_shifter)
    cruise_state = getattr(car_state, 'cruiseState', None)
    if cruise_state is not None:
      enabled = getattr(cruise_state, 'enabled', None)
      if isinstance(enabled, bool):
        self.cs_cruise_enabled = enabled

  def battery_12v_health(self) -> dict:
    voltage = self.bat_12v_voltage_v
    if voltage is None and self.vb_ems_v is not None:
      voltage = round(self.vb_ems_v, 2)
    if voltage is None and self.obd_control_module_v is not None:
      voltage = round(self.obd_control_module_v, 2)
    soc = self.bat_12v_soc_pct
    soh = self.bat_12v_soh_pct
    current = self.bat_12v_current_a
    temp = self.bat_12v_temp_c
    ldc_charging = bool(voltage is not None and voltage >= 13.3)
    status = 'unknown'
    label = 'Keine 12V-Daten'
    advice = 'Warte auf BAT11/EMS14 CAN-Daten vom Bordnetz.'
    if self.bat_12v_error or (voltage is not None and voltage < 11.6) or (soc is not None and soc < 45.0) or (soh is not None and soh < 55.0):
      status = 'critical'
      label = 'Kritisch / Schwach'
      advice = '12V-Hilfsbatterie stark belastet oder gealtert. LDC-Ladung und 12V-Batterie prüfen (Startprobleme möglich).'
    elif (voltage is not None and voltage < 12.1 and not ldc_charging) or (soc is not None and soc < 65.0) or (soh is not None and soh < 72.0):
      status = 'warn'
      label = 'Beobachten'
      advice = '12V-Ladezustand mäßig. Bei Kurzstreckenbetrieb im Blick behalten; das Hochvolt-LDC lädt im READY-Modus nach.'
    elif voltage is not None or soc is not None:
      status = 'ok'
      label = 'Gut (LDC lädt)' if ldc_charging else 'Gut'
      advice = '12V-Hilfsbatterie und LDC-Nachladung arbeiten im normalen Bereich.'
    return {
      'status': status,
      'label': label,
      'voltageV': voltage,
      'socPct': soc,
      'sohPct': soh,
      'currentA': current,
      'tempC': temp,
      'ldcCharging': ldc_charging,
      'sensorError': bool(self.bat_12v_error),
      'sensorInvalid': bool(self.bat_12v_invalid),
      'advice': advice,
    }

  def thermal_status(self) -> dict:
    coolant = self.coolant_temp_c if self.coolant_temp_c is not None else self.obd_coolant_c
    state = 'unknown'
    label = 'Keine Kühlmitteldaten'
    explanation = 'Warte auf EMS12 Kühlmitteltemperatur.'
    if coolant is not None:
      if coolant < 45.0:
        state = 'cold_warmup'
        label = 'Kaltstart / Kat-Heizphase'
        explanation = 'Motor läuft im Kaltstart häufiger zum Aufwärmen von Katalysator und Innenraumheizung (erhöhter Kurzstreckenverbrauch normal).'
      elif coolant < 72.0:
        state = 'warming'
        label = 'Aufwärmphase (HEV-Übergang)'
        explanation = 'Motor nähert sich der Betriebstemperatur; EV-Segeln und Start-Stopp schalten sich zunehmend frei.'
      elif coolant <= 105.0:
        state = 'optimal'
        label = 'Betriebswarm (Optimal)'
        explanation = 'Optimale Betriebstemperatur für vollen Hybrid-/EV-Wirkungsgrad erreicht.'
      else:
        state = 'hot_warn'
        label = 'Erhöhte Kühlmitteltemperatur!'
        explanation = 'Kühlmitteltemperatur über 105 °C — Kühlmittelstand, Wasserpumpe und Kühlerlüfter prüfen!'
    return {
      'state': state,
      'label': label,
      'coolantTempC': None if coolant is None else round(coolant, 1),
      'intakeAirTempC': None if self.intake_air_temp_c is None else round(self.intake_air_temp_c, 1),
      'fuelTempC': None if self.fuel_temp_c is None else round(self.fuel_temp_c, 1),
      'ambientTempC': None if self.ambient_temp_c is None else round(self.ambient_temp_c, 1),
      'cabinTempC': None if self.cabin_temp_c is None else round(self.cabin_temp_c, 1),
      'acRelayOn': self.ac_relay_on,
      'explanation': explanation,
    }

  def apply_frame(self, ts: float, address: int, data: bytes | bytearray | memoryview, bus: int = 0) -> None:
    if bus > 2:
      return
    if bus == 0:
      self.seen_bus0_addresses.add(address)
    elif address in self.seen_bus0_addresses:
      # Main powertrain/cluster frames live on bus 0 (C-CAN); ignore non-bus-0 duplicates.
      return
    if len(data) == 0:
      return
    self.last_update_s = ts
    self.seen_addresses[address] = ts
    self.seen_address_bus[address] = bus
    self.address_counts[address] = self.address_counts.get(address, 0) + 1
    if address in WATCHED_FUEL_ADDRS:
      self.raw_fuel_frames[f'0x{address:03X}'] = bytes(data).hex()

    if address == ADDR_EMS11 and len(data) >= 7:
      self.ignition_on = bool(extract_intel(data, 0, 1))
      self.engine_running_flag = bool(extract_intel(data, 1, 1))
      self.puc_stat_fuel_cut = bool(extract_intel(data, 3, 1))
      self.ac_relay_on = bool(extract_intel(data, 6, 1))
      self.engine_rpm = float(extract_intel(data, 16, 16, scale=0.25))
      self.engine_torque_pct = float(extract_intel(data, 32, 8, scale=0.390625))
      self.vehicle_speed_ems_kph = float(extract_intel(data, 48, 8))
    elif address == ADDR_EMS16 and len(data) >= 4:
      tqi = float(extract_intel(data, 8, 8, scale=0.390625))
      if ADDR_EMS11 not in self.seen_addresses or self.engine_torque_pct <= 0.0:
        self.engine_torque_pct = tqi
      self.pre_fuel_cut_in = bool(extract_intel(data, 27, 1))
      self.ems16_eng_stat = int(extract_intel(data, 28, 3))
    elif address == ADDR_EMS_DCT11 and len(data) >= 7:
      pv_pct = float(extract_intel(data, 0, 8, scale=0.3906))
      if ADDR_E_EMS11 not in self.seen_addresses:
        self.gas_pedal_pct = min(100.0, max(0.0, pv_pct))
      f_n_eng = bool(extract_intel(data, 14, 1))
      rpm = float(extract_intel(data, 16, 16, scale=0.25))
      tqi = float(extract_intel(data, 48, 8, scale=0.390625))
      if ADDR_EMS11 not in self.seen_addresses:
        self.engine_running_flag = f_n_eng
        self.engine_rpm = rpm
        self.engine_torque_pct = tqi
    elif address == ADDR_EMS_DCT12 and len(data) >= 3:
      raw_iat = int(extract_intel(data, 16, 8))
      if raw_iat not in (0xFE, 0xFF) and ADDR_EMS15 not in self.seen_addresses:
        self.intake_air_temp_c = float(raw_iat * 0.75 - 48.0)
    elif address == ADDR_EMS13 and len(data) >= 5:
      raw_tco = int(extract_intel(data, 24, 8))
      if raw_tco not in (0xFE, 0xFF) and ADDR_EMS12 not in self.seen_addresses:
        self.coolant_temp_c = float(raw_tco * 0.75 - 48.0)
      rpm_32 = float(extract_intel(data, 32, 8, scale=32.0))
      if ADDR_EMS11 not in self.seen_addresses and ADDR_EMS_DCT11 not in self.seen_addresses:
        self.engine_rpm = rpm_32
    elif address == ADDR_EMS_366 and len(data) >= 6:
      rpm = float(extract_intel(data, 8, 16, scale=0.25))
      if ADDR_EMS11 not in self.seen_addresses or self.engine_rpm <= 0.0:
        self.engine_rpm = rpm
        self.engine_torque_pct = max(self.engine_torque_pct, float(extract_intel(data, 0, 8, scale=0.390625)))
    elif address == ADDR_TCU_DCT13 and len(data) >= 3:
      rpm_flag = bool(extract_intel(data, 23, 1))
      rpm = float(extract_intel(data, 10, 13, scale=0.9766))
      if rpm_flag and rpm > 50.0:
        self._tcu_dct13_rpm_valid = True
      if (ADDR_EMS11 not in self.seen_addresses and ADDR_EMS_366 not in self.seen_addresses) or (rpm_flag and self.engine_rpm <= 0.0):
        self.engine_rpm = rpm
    elif address == ADDR_ESP12 and len(data) >= 5:
      raw_la = int(extract_intel(data, 13, 11))
      if raw_la != 0x7FF:
        self.long_accel_mps2 = float(raw_la * 0.01 - 10.23)
      self.brake_cyl_press = float(extract_intel(data, 26, 12, scale=0.1))
    elif address == ADDR_E_EMS11 and len(data) >= 8:
      self.e_ems11_brake_pos = int(extract_intel(data, 0, 8))
      self.ig_reactive_stat = int(extract_intel(data, 8, 3))
      raw_acc = int(extract_intel(data, 31, 8))
      raw_vcu = int(extract_intel(data, 56, 8))
      acc_pct = (raw_acc / 254.0 * 100.0) if raw_acc < 0xFF else 0.0
      vcu_pct = (raw_vcu / 254.0 * 100.0) if raw_vcu < 0xFF else 0.0
      self.accel_pedal_raw = float(raw_acc)
      self.vcu_accel_pedal_raw = float(raw_vcu)
      self.gas_pedal_pct = round(min(100.0, max(0.0, acc_pct, vcu_pct)), 1)
    elif address == ADDR_ELECT_GEAR and len(data) >= 3:
      self.gear_shifter_raw = int(extract_intel(data, 16, 4))
    elif address == ADDR_TCS13 and len(data) >= 6:
      self.tcs13_acc_enable = int(extract_intel(data, 43, 2))
      if len(data) >= 7:
        raw_ab = int(extract_intel(data, 0, 11))
        if raw_ab != 0x7FF:
          self.tcs_a_basis_mps2 = float(raw_ab * 0.01 - 10.23)
        self.tcs_brake_light = bool(extract_intel(data, 11, 1))
        self.tqi_scc_pct = float(extract_intel(data, 24, 8, scale=0.390625))
        self.tcs_driver_override = int(extract_intel(data, 45, 2))
        self.tcs_acc_req = bool(extract_intel(data, 54, 1))
        self.tcs_driver_braking = bool(extract_intel(data, 55, 1))
    elif address == ADDR_SCC12 and len(data) >= 2:
      raw_acc_fail = int(extract_intel(data, 11, 2))
      self.scc_fail_info = 0 if raw_acc_fail == 0x3 else raw_acc_fail
      if len(data) >= 7:
        self.scc_acc_mode = int(extract_intel(data, 13, 2))
        raw_req = int(extract_intel(data, 37, 11))
        if raw_req != 0x7FF:
          self.scc_a_req_mps2 = float(raw_req * 0.01 - 10.23)
        raw_aeb_fail = int(extract_intel(data, 50, 2))
        self.aeb_fail_info = 0 if raw_aeb_fail == 0x3 else raw_aeb_fail
    elif address == ADDR_EMS12 and len(data) >= 2:
      raw_coolant = int(extract_intel(data, 8, 8))
      if raw_coolant not in (0xFE, 0xFF):
        self.coolant_temp_c = float(raw_coolant * 0.75 - 48.0)
    elif address == ADDR_EMS_H12 and len(data) >= 8:
      self.tqi_b_pct = float(extract_intel(data, 16, 8, scale=0.390625))
      raw_isg = int(extract_intel(data, 35, 3))
      self.isg_stat = 0 if raw_isg == 0x7 else raw_isg
      self.limp_home_mode = bool(extract_intel(data, 39, 1))
      self.eco_active = bool(extract_intel(data, 60, 1))
      self.engine_running_normal = bool(extract_intel(data, 61, 1))
    elif address == ADDR_EMS20 and len(data) >= 2:
      raw_fco = int(extract_intel(data, 0, 16))
      if raw_fco != 0xFFFF:
        self.fco_raw = raw_fco
    elif address == ADDR_EMS14 and len(data) >= 8:
      self.mil_lamp = bool(extract_intel(data, 1, 1))
      raw_ems14_fco = int(extract_intel(data, 8, 16))
      if raw_ems14_fco != 0xFFFF:
        self.ems14_fco_raw = raw_ems14_fco
      raw_vb = int(extract_intel(data, 24, 8))
      if raw_vb not in (0, 0xFF):
        self.vb_ems_v = float(raw_vb * 0.1015625)
      raw_ft = int(extract_intel(data, 56, 8))
      if raw_ft not in (0xFE, 0xFF):
        self.fuel_temp_c = float(raw_ft * 0.75 - 48.0)
    elif address == ADDR_EMS15 and len(data) >= 3:
      self.ec_fail = bool(extract_intel(data, 2, 1))
      self.blec_fail = bool(extract_intel(data, 4, 1))
      raw_iat = int(extract_intel(data, 16, 8))
      if raw_iat not in (0xFE, 0xFF):
        self.intake_air_temp_c = float(raw_iat * 0.75 - 48.0)
    elif address == ADDR_CLU12 and len(data) >= 3:
      raw_odo = int(extract_intel(data, 0, 24))
      if raw_odo not in (0, 0xFFFFFF):
        self.odometer_km = round(float(raw_odo * 0.1), 1)
    elif address == ADDR_CLU13 and len(data) >= 5:
      raw_lfw = int(extract_intel(data, 0, 2))
      self.low_fuel_warn = 0 if raw_lfw == 0x3 else raw_lfw
      self.cluster_avg_fc_unit = int(extract_intel(data, 3, 2))
      raw_fci = int(extract_intel(data, 6, 10))
      if raw_fci not in (0x3FE, 0x3FF):
        self.cluster_avg_fc_value = round(float(raw_fci * 0.1), 1)
      self.drive_mode_switch = int(extract_intel(data, 16, 2))
      raw_fuel_lvl = int(extract_intel(data, 18, 5))
      if raw_fuel_lvl != 0x1F or self.fuel_disp_level is not None:
        self.fuel_disp_level = raw_fuel_lvl
      raw_dte = int(extract_intel(data, 24, 10))
      if raw_dte not in (0x3FE, 0x3FF):
        self.dte_km = raw_dte
      self.active_eco_switch = bool(extract_intel(data, 39, 1))
      if len(data) >= 8:
        self.alt_lamp_status = bool(extract_intel(data, 59, 1))
    elif address == ADDR_CLU15 and len(data) >= 1:
      self.cluster_speed_kph = float(extract_intel(data, 0, 8))
    elif address == ADDR_WHL_SPD11 and len(data) >= 8:
      fl = float(extract_intel(data, 0, 14, scale=0.03125))
      fr = float(extract_intel(data, 16, 14, scale=0.03125))
      rl = float(extract_intel(data, 32, 14, scale=0.03125))
      rr = float(extract_intel(data, 48, 14, scale=0.03125))
      self.wheel_speeds_kph = (fl, fr, rl, rr)
      avg_kph = (fl + fr + rl + rr) * 0.25
      self.wheel_speed_kph = avg_kph
      if self._prev_wheel_ts is not None and self._prev_wheel_speed_kph is not None:
        dt_w = ts - self._prev_wheel_ts
        if 0.04 <= dt_w <= 1.2:
          inst_a = max(-6.0, min(5.0, ((avg_kph - self._prev_wheel_speed_kph) / 3.6) / dt_w))
          self.wheel_accel_mps2 = inst_a if self.wheel_accel_mps2 is None else (0.35 * inst_a + 0.65 * self.wheel_accel_mps2)
          self._prev_wheel_speed_kph = avg_kph
          self._prev_wheel_ts = ts
        elif dt_w > 1.2:
          self._prev_wheel_speed_kph = avg_kph
          self._prev_wheel_ts = ts
      else:
        self._prev_wheel_speed_kph = avg_kph
        self._prev_wheel_ts = ts
    elif address == ADDR_BAT11 and len(data) >= 8:
      self.bat_12v_invalid = bool(extract_intel(data, 55, 1))
      self.bat_12v_error = bool(extract_intel(data, 63, 1))
      if not self.bat_12v_invalid:
        self.bat_12v_current_a = round(float(extract_intel(data, 0, 16, scale=0.01, offset=-327.0)), 2)
        raw_soc = int(extract_intel(data, 16, 8))
        if raw_soc <= 100:
          self.bat_12v_soc_pct = float(raw_soc)
        raw_volt = int(extract_intel(data, 24, 14))
        if raw_volt not in (0, 0x3FFF):
          self.bat_12v_voltage_v = round(float(raw_volt * 0.001 + 6.0), 3)
        self.bat_12v_temp_c = float(extract_intel(data, 38, 9, signed=True, scale=0.5, offset=-40.0))
        raw_soh = int(extract_intel(data, 48, 7))
        if raw_soh <= 100:
          self.bat_12v_soh_pct = float(raw_soh)
    elif address == ADDR_DATC11 and len(data) >= 6:
      raw_amb = int(extract_intel(data, 24, 8))
      if raw_amb not in (0, 0xFE, 0xFF):
        self.ambient_temp_c = float(raw_amb * 0.5 - 41.0)
      raw_cab = int(extract_intel(data, 40, 8))
      if raw_cab not in (0, 0xFE, 0xFF):
        self.cabin_temp_c = float(raw_cab * 0.5 - 40.0)
    elif address == ADDR_TCU14 and len(data) >= 3:
      raw_warn_msg = int(extract_intel(data, 0, 3))
      raw_dri_warn1 = int(extract_intel(data, 13, 3))
      raw_dri_warn2 = int(extract_intel(data, 16, 2))
      self.tcu14_raw_warn_msg = raw_warn_msg
      self.tcu14_raw_dri_warn1 = raw_dri_warn1
      self.tcu14_raw_dri_warn2 = raw_dri_warn2
      # Mask Hyundai/Kia 3-bit SNA (0x7) and 2-bit SNA (0x3) invalid/unequipped sentinel values
      self.tcu_warn_msg = 0 if raw_warn_msg == 0x7 else raw_warn_msg
      self.tcu_warn_img = bool(extract_intel(data, 3, 1)) if raw_warn_msg != 0x7 else False
      self.tcu_dri_warn1 = 0 if raw_dri_warn1 == 0x7 else raw_dri_warn1
      self.tcu_dri_warn2 = 0 if raw_dri_warn2 == 0x3 else raw_dri_warn2
    elif address == ADDR_TCU_DCT14 and len(data) >= 1:
      self.dct_hill_hold_warn = bool(extract_intel(data, 5, 1))
    elif address == ADDR_ABS11 and len(data) >= 1:
      self.abs11_defect = bool(extract_intel(data, 0, 1))
      self.abs11_ebd_defect = bool(extract_intel(data, 1, 1))
      self.abs11_warn_lamp = bool(extract_intel(data, 3, 1))
      self.abs11_ebd_warn_lamp = bool(extract_intel(data, 4, 1))
      self.abs_defect = self.abs11_defect
      self.ebd_defect = self.abs11_ebd_defect
      self.abs_warn_lamp = self.abs11_warn_lamp or self.tcs15_abs_warn_lamp
      self.ebd_warn_lamp = self.abs11_ebd_warn_lamp or self.tcs15_ebd_warn_lamp
    elif address == ADDR_TCS15 and len(data) >= 4:
      self.tcs15_abs_warn_lamp = bool(extract_intel(data, 0, 1))
      raw_tcs_off = int(extract_intel(data, 1, 2))
      raw_tcs_lamp = int(extract_intel(data, 3, 2))
      self.tcs_off_lamp = 0 if raw_tcs_off == 0x3 else raw_tcs_off
      self.tcs_lamp = 0 if raw_tcs_lamp == 0x3 else raw_tcs_lamp
      self.tcs15_ebd_warn_lamp = bool(extract_intel(data, 26, 1))
      self.abs_warn_lamp = self.abs11_warn_lamp or self.tcs15_abs_warn_lamp
      self.ebd_warn_lamp = self.abs11_ebd_warn_lamp or self.tcs15_ebd_warn_lamp
    elif address == ADDR_FCA11 and len(data) >= 5:
      raw_fca = int(extract_intel(data, 32, 3))
      self.fca11_raw_fail_info = raw_fca
      self.fca_fail_info = 0 if raw_fca == 0x7 else raw_fca
    elif address == ADDR_ENG_FRZ_11 and len(data) >= 5:
      self.freeze_frame_load_pct = float(extract_intel(data, 0, 8, scale=100.0 / 255.0))
      self.freeze_frame_rpm = float(extract_intel(data, 16, 16, scale=0.25))
    elif address == ADDR_EMS19 and len(data) >= 5:
      raw_oil = int(extract_intel(data, 32, 8))
      if raw_oil not in (0xFE, 0xFF):
        self.eng_oil_temp_c = float(raw_oil * 0.75 - 40.0)
      if len(data) >= 6:
        self.bat_lamp_stat = bool(extract_intel(data, 42, 1))
      if len(data) >= 8:
        self.ops_fail = bool(extract_intel(data, 56, 1))
    elif address == ADDR_MDPS12 and len(data) >= 2:
      self.mdps_def = bool(extract_intel(data, 11, 1))
      self.mdps_fail_stat = bool(extract_intel(data, 15, 1))
      if len(data) >= 5:
        self.mdps_serr = bool(extract_intel(data, 37, 1))
    elif address == ADDR_LFAHDA_MFC and len(data) >= 3:
      raw_lfa_warn = int(extract_intel(data, 16, 3))
      self.lfa_sys_warning = 0 if raw_lfa_warn == 0x7 else raw_lfa_warn
    elif address == ADDR_EPB11 and len(data) >= 1:
      raw_epb_lamp = int(extract_intel(data, 4, 2))
      self.epb_f_lamp = 0 if raw_epb_lamp == 0x3 else raw_epb_lamp
      if len(data) >= 4:
        raw_epb_fail = int(extract_intel(data, 29, 3))
        self.epb_fail = 0 if raw_epb_fail == 0x7 else raw_epb_fail
    elif address == ADDR_TPMS11 and len(data) >= 1:
      raw_tpms = int(extract_intel(data, 0, 2))
      raw_tread = int(extract_intel(data, 2, 2))
      self.tpms_w_lamp = 0 if raw_tpms == 0x3 else raw_tpms
      self.tread_w_lamp = 0 if raw_tread == 0x3 else raw_tread
      self.tpms_pos_warn = (
        bool(extract_intel(data, 4, 1)),
        bool(extract_intel(data, 5, 1)),
        bool(extract_intel(data, 6, 1)),
        bool(extract_intel(data, 7, 1)),
      )
    elif address == ADDR_ACU11 and len(data) >= 5:
      raw_acu_flt = int(extract_intel(data, 34, 2))
      self.acu_flt_stat = 0 if raw_acu_flt == 0x3 else raw_acu_flt
      if len(data) >= 7:
        raw_acu_dtc = int(extract_intel(data, 40, 16))
        self.acu_dtc_raw = 0 if raw_acu_dtc == 0xFFFF else raw_acu_dtc
      if len(data) >= 8:
        raw_acu_num = int(extract_intel(data, 56, 8))
        self.acu_num_flt = 0 if raw_acu_num == 0xFF else raw_acu_num

  def live_ecu_matrix(self, now_mono_s: float, *, stale_s: float = 5.0) -> list[dict]:
    """Return live C-CAN presence matrix for all known Ioniq ECUs (zero TX required)."""
    matrix: list[dict] = []
    for ecu_id, name, addrs in LIVE_ECU_CHANNELS:
      latest_ts: float | None = None
      latest_bus: int | None = None
      frames_seen = 0
      for addr in addrs:
        frames_seen += self.address_counts.get(addr, 0)
        ts = self.seen_addresses.get(addr)
        if ts is not None and (latest_ts is None or ts > latest_ts):
          latest_ts = ts
          latest_bus = self.seen_address_bus.get(addr)
      ago = None if latest_ts is None else round(max(0.0, now_mono_s - latest_ts), 2)
      online = bool(latest_ts is not None and (now_mono_s - latest_ts) <= stale_s)
      matrix.append({
        'ecuId': ecu_id,
        'name': name,
        'online': online,
        'bus': latest_bus,
        'lastSeenAgoS': ago,
        'framesSeen': frames_seen,
      })
    return matrix

  def summary(self) -> dict:
    bat_health = self.battery_12v_health()
    thermal = self.thermal_status()
    has_direct_rpm = self.has_direct_engine_rpm()
    effective_rpm = self.engine_rpm if has_direct_rpm else self.synthetic_engine_rpm
    unit_label = self.cluster_avg_fc_unit_label()
    return {
      'lastUpdateS': round(self.last_update_s, 3),
      'ignitionOn': self.ignition_on,
      'speedKph': round(self.best_speed_kph(), 1),
      'engineRpm': round(effective_rpm, 0),
      'hasDirectRpm': has_direct_rpm,
      'engineFiring': self.engine_is_firing(),
      'torquePct': round(max(self.engine_torque_pct, self.tqi_b_pct), 1),
      'gasPedalPct': round(self.gas_pedal_pct, 1),
      'tractivePowerKw': round(self.tractive_power_kw(), 1),
      'gearShifter': self.gear_shifter_label(),
      'isBraking': self.is_braking_or_regen(),
      'wheelAccelMps2': None if self.wheel_accel_mps2 is None else round(self.wheel_accel_mps2, 2),
      'instantFuelRateLph': round(self.instant_fuel_rate_lph, 2),
      'coolantTempC': thermal['coolantTempC'],
      'engOilTempC': None if self.eng_oil_temp_c is None else round(self.eng_oil_temp_c, 1),
      'intakeAirTempC': thermal['intakeAirTempC'],
      'fuelTempC': thermal['fuelTempC'],
      'ambientTempC': thermal['ambientTempC'],
      'cabinTempC': thermal['cabinTempC'],
      'odometerKm': self.odometer_km,
      'fuelDispLevel': self.fuel_disp_level,
      'fuelDispPct': None if self.fuel_disp_level is None else round(min(100.0, max(0.0, self.fuel_disp_level / 31.0 * 100.0)), 1),
      'lowFuelWarn': self.low_fuel_warn,
      'dteKm': self.dte_km,
      'clusterAvgFc': self.cluster_avg_fc_value,
      'clusterAvgFcValue': self.cluster_avg_fc_value,
      'clusterAvgFcUnit': self.cluster_avg_fc_unit,
      'clusterAvgFcL100': self.cluster_avg_l_per_100km(),
      'clusterAvgFcUnitLabel': unit_label,
      'ems14FcoRaw': self.ems14_fco_raw,
      'ems20FcoRaw': self.fco_raw,
      'rawFuelFrames': dict(self.raw_fuel_frames),
      'bat12vVoltageV': bat_health['voltageV'],
      'bat12vSocPct': self.bat_12v_soc_pct,
      'bat12vSohPct': self.bat_12v_soh_pct,
      'bat12vCurrentA': self.bat_12v_current_a,
      'bat12vTempC': self.bat_12v_temp_c,
      'battery12vHealth': bat_health,
      'thermalStatus': thermal,
      'milLamp': self.mil_lamp,
      'limpHomeMode': self.limp_home_mode,
      'ecoActive': self.eco_active or self.active_eco_switch,
    }
