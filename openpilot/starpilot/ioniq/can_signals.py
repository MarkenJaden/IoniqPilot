"""Pure-Python CAN signal decoder for 2016 Hyundai Ioniq Hybrid (AE HEV, classic CAN)."""

from __future__ import annotations

from dataclasses import dataclass, field

# Decimal CAN IDs -> hex:
ADDR_DATC11 = 0x044     # 68
ADDR_EMS_H12 = 0x18F    # 399
ADDR_EMS20 = 0x200      # 512
ADDR_EMS11 = 0x316      # 790
ADDR_EMS12 = 0x329      # 809
ADDR_WHL_SPD11 = 0x386  # 902
ADDR_ABS11 = 0x38A      # 906
ADDR_FCA11 = 0x38D      # 909
ADDR_TCU14 = 0x502      # 1282
ADDR_TCS15 = 0x507      # 1287
ADDR_CLU13 = 0x50C      # 1292
ADDR_CLU15 = 0x52A      # 1322
ADDR_EMS14 = 0x545      # 1349
ADDR_EMS15 = 0x547      # 1351
ADDR_BAT11 = 0x549      # 1353
ADDR_TCU_DCT14 = 0x54C  # 1356
ADDR_ENG_FRZ_11 = 0x556  # 1366
ADDR_CLU12 = 0x5B0      # 1456


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

  # Powertrain / Engine / Hybrid (EMS11, EMS12, EMS14, EMS_H12, EMS20)
  ignition_on: bool | None = None
  engine_running_flag: bool = False
  engine_running_normal: bool = False
  engine_rpm: float = 0.0
  vehicle_speed_ems_kph: float = 0.0
  engine_torque_pct: float = 0.0
  tqi_b_pct: float = 0.0
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
  freeze_frame_rpm: float | None = None
  freeze_frame_load_pct: float | None = None

  # Optional OBD-II Mode 01 fallback telemetry (populated only during ELM327 diagnostic scan)
  obd_fuel_rate_lph: float | None = None
  obd_maf_gps: float | None = None
  obd_fuel_level_pct: float | None = None
  obd_coolant_c: float | None = None
  obd_control_module_v: float | None = None

  def best_speed_kph(self) -> float:
    if ADDR_WHL_SPD11 in self.seen_addresses:
      return self.wheel_speed_kph
    if ADDR_CLU15 in self.seen_addresses:
      return self.cluster_speed_kph
    return self.vehicle_speed_ems_kph

  def cluster_avg_l_per_100km(self) -> float | None:
    val = self.cluster_avg_fc_value
    if val is None or val <= 0.0:
      return None
    if self.cluster_avg_fc_unit == 1:
      return round(val, 2)
    if self.cluster_avg_fc_unit == 0:
      return round(100.0 / val, 2)
    if self.cluster_avg_fc_unit == 2:
      return round(235.215 / val, 2)
    return round(val, 2) if val <= 15.0 else round(100.0 / val, 2)

  def engine_is_firing(self) -> bool:
    if self.fuel_cut_idle_stop or self.fuel_cut_open:
      return False
    if self.engine_rpm > 450.0:
      return True
    return self.engine_rpm > 250.0 and (
      self.engine_running_flag or self.engine_running_normal or self.engine_torque_pct > 1.0 or self.tqi_b_pct > 1.0
    )

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
    if bus != 0 and address not in self.seen_addresses:
      # Main powertrain/cluster frames live on bus 0 (C-CAN); ignore non-bus-0 duplicates unless unseen elsewhere.
      if bus > 2:
        return
    if len(data) == 0:
      return
    self.last_update_s = ts
    self.seen_addresses[address] = ts

    if address == ADDR_EMS11 and len(data) >= 7:
      self.ignition_on = bool(extract_intel(data, 0, 1))
      self.engine_running_flag = bool(extract_intel(data, 1, 1))
      self.ac_relay_on = bool(extract_intel(data, 6, 1))
      self.engine_rpm = float(extract_intel(data, 16, 16, scale=0.25))
      self.engine_torque_pct = float(extract_intel(data, 32, 8, scale=0.390625))
      self.vehicle_speed_ems_kph = float(extract_intel(data, 48, 8))
    elif address == ADDR_EMS12 and len(data) >= 2:
      raw_coolant = int(extract_intel(data, 8, 8))
      if raw_coolant not in (0xFE, 0xFF):
        self.coolant_temp_c = float(raw_coolant * 0.75 - 48.0)
    elif address == ADDR_EMS_H12 and len(data) >= 8:
      self.tqi_b_pct = float(extract_intel(data, 16, 8, scale=0.390625))
      raw_isg = int(extract_intel(data, 35, 3))
      self.isg_stat = 0 if raw_isg == 0x7 else raw_isg
      self.limp_home_mode = bool(extract_intel(data, 39, 1))
      self.fuel_cut_idle_stop = bool(extract_intel(data, 58, 1))
      self.fuel_cut_open = bool(extract_intel(data, 59, 1))
      self.eco_active = bool(extract_intel(data, 60, 1))
      self.engine_running_normal = bool(extract_intel(data, 61, 1))
    elif address == ADDR_EMS20 and len(data) >= 2:
      raw_fco = int(extract_intel(data, 0, 16))
      if raw_fco != 0xFFFF:
        self.fco_raw = raw_fco
    elif address == ADDR_EMS14 and len(data) >= 8:
      self.mil_lamp = bool(extract_intel(data, 1, 1))
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
    elif address == ADDR_CLU15 and len(data) >= 1:
      self.cluster_speed_kph = float(extract_intel(data, 0, 8))
    elif address == ADDR_WHL_SPD11 and len(data) >= 8:
      fl = float(extract_intel(data, 0, 14, scale=0.03125))
      fr = float(extract_intel(data, 16, 14, scale=0.03125))
      rl = float(extract_intel(data, 32, 14, scale=0.03125))
      rr = float(extract_intel(data, 48, 14, scale=0.03125))
      self.wheel_speeds_kph = (fl, fr, rl, rr)
      self.wheel_speed_kph = (fl + fr + rl + rr) * 0.25
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

  def summary(self) -> dict:
    bat_health = self.battery_12v_health()
    thermal = self.thermal_status()
    return {
      'lastUpdateS': round(self.last_update_s, 3),
      'ignitionOn': self.ignition_on,
      'speedKph': round(self.best_speed_kph(), 1),
      'engineRpm': round(self.engine_rpm, 0),
      'engineFiring': self.engine_is_firing(),
      'torquePct': round(max(self.engine_torque_pct, self.tqi_b_pct), 1),
      'coolantTempC': thermal['coolantTempC'],
      'intakeAirTempC': thermal['intakeAirTempC'],
      'fuelTempC': thermal['fuelTempC'],
      'ambientTempC': thermal['ambientTempC'],
      'cabinTempC': thermal['cabinTempC'],
      'odometerKm': self.odometer_km,
      'fuelDispLevel': self.fuel_disp_level,
      'fuelDispPct': None if self.fuel_disp_level is None else round(min(100.0, max(0.0, self.fuel_disp_level / 31.0 * 100.0)), 1),
      'lowFuelWarn': self.low_fuel_warn,
      'dteKm': self.dte_km,
      'clusterAvgFcValue': self.cluster_avg_fc_value,
      'clusterAvgFcUnit': self.cluster_avg_fc_unit,
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
