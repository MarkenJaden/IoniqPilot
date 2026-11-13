"""SAE J2012 DTC codec, ISO 14229 status decoder, and Hyundai Ioniq AE HEV fault analyzer."""

from __future__ import annotations

from dataclasses import dataclass

_DTC_PREFIX = ('P', 'C', 'B', 'U')


@dataclass(frozen=True)
class DtcEntry:
  code: str
  system: str
  severity: str  # 'critical' | 'warning' | 'info'
  title: str
  hint: str


# Curated high-signal SAE + Hyundai/Kia Kappa 1.6 GDi / 6-DCT / AE HEV DTC catalog.
IONIQ_DTC_CATALOG: dict[str, DtcEntry] = {
  'P0106': DtcEntry('P0106', 'Engine / MAP', 'warning', 'Saugrohrdrucksensor (MAP) Bereichs-/Leistungsproblem',
                    'Unterdruckschläuche, MAP-Sensorstecker und Drosselklappe auf Verschmutzung prüfen.'),
  'P0128': DtcEntry('P0128', 'Cooling / Thermal', 'warning', 'Kühlmitteltemperatur unter Regelbereich',
                    'Thermostat öffnet zu früh oder klemmt offen — erhöht Kraftstoffverbrauch im Hybridbetrieb.'),
  'P0300': DtcEntry('P0300', 'Ignition / Fuel', 'critical', 'Zufällige/mehrere Zündaussetzer erkannt',
                    'Zündkerzen, Zündspulen, GDi-Injektoren oder Kraftstoffqualität prüfen; Katalysatorschutz beachten.'),
  'P0301': DtcEntry('P0301', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 1',
                    'Zündspule/Kerze Zyl. 1 quer tauschen und Hochdruck-Injektor prüfen.'),
  'P0302': DtcEntry('P0302', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 2',
                    'Zündspule/Kerze Zyl. 2 quer tauschen und Hochdruck-Injektor prüfen.'),
  'P0303': DtcEntry('P0303', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 3',
                    'Zündspule/Kerze Zyl. 3 quer tauschen und Hochdruck-Injektor prüfen.'),
  'P0304': DtcEntry('P0304', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 4',
                    'Zündspule/Kerze Zyl. 4 quer tauschen und Hochdruck-Injektor prüfen.'),
  'P0420': DtcEntry('P0420', 'Exhaust / Catalyst', 'warning', 'Katalysator-Wirkungsgrad unter Schwellenwert',
                    'Nachkat-Lambdasonde, Abgasleck vor Kat oder Katalysatoralterung prüfen.'),
  'P0455': DtcEntry('P0455', 'EVAP', 'warning', 'Tankentlüftungssystem (EVAP) großes Leck',
                    'Tankdeckeldichtung und EVAP-Spülventil prüfen.'),
  'P0456': DtcEntry('P0456', 'EVAP', 'info', 'Tankentlüftungssystem (EVAP) kleines Leck',
                    'Tankdeckel fest schließen, EVAP-Leitungen bei Inspektion prüfen.'),
  'P0562': DtcEntry('P0562', '12V Power', 'warning', 'Bordnetzspannung zu niedrig',
                    '12V-Batterie-/IBS-Status prüfen; beim 2016 Ioniq HEV 12V-Hilfsbatterie im Kofferraum & LDC-Wandler prüfen.'),
  'P0563': DtcEntry('P0563', '12V Power', 'warning', 'Bordnetzspannung zu hoch',
                    'LDC (DC/DC-Wandler) Ausgangsspannung prüfen.'),
  'P0605': DtcEntry('P0605', 'ECU Internal', 'critical', 'Steuergerät ROM/Flash-Prüfsummenfehler',
                    'Softwarestand und Spannungsversorgung des Steuergeräts prüfen.'),
  'P0700': DtcEntry('P0700', 'Transmission / DCT', 'warning', 'Getriebesteuerung (TCU) MIL-Anforderung',
                    'Detailcodes direkt aus Steuergerät 0x7E1 (TCU / 6-Gang DCT) auslesen.'),
  'P0731': DtcEntry('P0731', 'Transmission / DCT', 'warning', 'Gang 1 falsches Übersetzungsverhältnis',
                    'DCT-Kupplungsaktuator und Kalibrierwerte der Doppelkupplung prüfen.'),
  'P0732': DtcEntry('P0732', 'Transmission / DCT', 'warning', 'Gang 2 falsches Übersetzungsverhältnis',
                    'DCT-Kupplungsaktuator (ungerade/gerade Welle) und Getriebeöl prüfen.'),
  'P0741': DtcEntry('P0741', 'Transmission / DCT', 'warning', 'Kupplung/Überbrückung Leistungsproblem',
                    'Elektrischen Kupplungsaktuator des DCT prüfen.'),
  'P0805': DtcEntry('P0805', 'Transmission / DCT', 'critical', 'Kupplungspositionssensor Schaltkreis',
                    'Stecker und Kabelbaum am DCT-Kupplungsaktuator prüfen.'),
  'P0A0F': DtcEntry('P0A0F', 'Hybrid Powertrain', 'critical', 'Verbrennungsmotor startet im Hybridbetrieb nicht',
                    'Kraftstoffstand, HSG (Hybrid Starter Generator), Kurbelwellensensor und HV-Freigabe prüfen.'),
  'P0A1F': DtcEntry('P0A1F', 'HV Battery / BMS', 'critical', 'Batteriesteuergerät (BMS) interner Fehler',
                    'BMS 0x7E4 Freeze-Frame auslesen und HV-Trennstecker/Kühlung prüfen.'),
  'P0A7F': DtcEntry('P0A7F', 'HV Battery / BMS', 'warning', 'Hybrid-Akku Alterung / Kapazitätsverlust',
                    'Zellspannungsdifferenz und Innenwiderstand über BMS prüfen.'),
  'P0A80': DtcEntry('P0A80', 'HV Battery / BMS', 'critical', 'Hybrid-Batteriepack muss geprüft/ersetzt werden',
                    'Starke Zellimbalance im Li-Ion Polymer Pack beim AE HEV.'),
  'P0A94': DtcEntry('P0A94', 'Hybrid Inverter / LDC', 'critical', 'DC/DC-Wandler (LDC) Leistung/Überstrom',
                    '12V-Ladesystem und Inverter-Kühlmittelpumpe prüfen.'),
  'P0AC0': DtcEntry('P0AC0', 'HV Battery / BMS', 'warning', 'Hybrid-Batteriestromsensor Bereich/Leistung',
                    'BMS-Stromsensor und Masseverbindungen am HV-Pack prüfen.'),
  'P1744': DtcEntry('P1744', 'Transmission / DCT', 'warning', 'Hydraulischer Kupplungsaktuator (HCA) / DCT Kupplung',
                    'Bremsflüssigkeit des hydraulischen Ausrücklagers im DCT (Reservoir im Motorraum) und Entlüftung prüfen!'),
  'P1B70': DtcEntry('P1B70', 'HV Battery / BMS', 'critical', 'Hochvolt-Batteriespannung niedrig',
                    'HV-Vorladung (Precharge) und Schütze sowie Zellspannungen prüfen.'),
  'P1B74': DtcEntry('P1B74', 'HV Battery / BMS', 'critical', 'Hochvolt-Batterie Zellspannungsüberwachung',
                    'Zellspannungssensorik / Busbar-Kontakte im Li-Ion Pack prüfen.'),
  'P1B77': DtcEntry('P1B77', 'Hybrid Inverter', 'critical', 'Inverter-Vorladung (Precharge) fehlgeschlagen',
                    'HV-Hauptsicherung, Vorladewiderstand und PRA-Relais prüfen.'),
  'P1B96': DtcEntry('P1B96', 'Hybrid System', 'critical', 'Hybrid-Steuergerät Leistungsrelais / Isolationsfehler',
                    'HV-Isolationswiderstand (HPCU / Klimakompressor / Kabel) messen.'),
  'C1200': DtcEntry('C1200', 'ABS / Wheel Speed', 'warning', 'Raddrehzahlsensor vorne links unterbrochen/Kurzschluss',
                    'Sensorleitung und Impulsring VL prüfen (beeinflusst ABS, ESC und Openpilot-Geschwindigkeit).'),
  'C1203': DtcEntry('C1203', 'ABS / Wheel Speed', 'warning', 'Raddrehzahlsensor vorne rechts unterbrochen/Kurzschluss',
                    'Sensorleitung und Impulsring VR prüfen.'),
  'C1260': DtcEntry('C1260', 'MDPS / Steering', 'warning', 'Lenkwinkelsensor (SAS) Signal unplausibel / nicht kalibriert',
                    'Lenkwinkel-Nullpunkt (SAS11) bei Geradeausfahrt neu anlernen.'),
  'C1611': DtcEntry('C1611', 'CAN Bus / Communication', 'warning', 'CAN-Timeout zum Motorsteuergerät (EMS)',
                    'Steckverbindungen am C-CAN oder Nachrüst-Harness auf Wackelkontakt prüfen.'),
  'C1612': DtcEntry('C1612', 'CAN Bus / Communication', 'warning', 'CAN-Timeout zum Getriebesteuergerät (TCU)',
                    'Oft Folge von Unterspannung oder Bus-Unterbrechung während Steuergeräte-Reset.'),
  'C1616': DtcEntry('C1616', 'CAN Bus / Communication', 'warning', 'CAN Bus Off Fehler',
                    'Kabelbaum, Abschlusswiderstände (120 Ohm) und OBD/Harness-Stecker prüfen.'),
  'C1620': DtcEntry('C1620', 'SCC / ADAS', 'warning', 'Frontradar Ausrichtung / Kalibrierung fehlgeschlagen',
                    'Radarhalter hinter dem Hyundai-Emblem auf Stoß/Schmutz prüfen.'),
  'C1642': DtcEntry('C1642', 'MDPS / CAN', 'warning', 'CAN-Signal vom ESC unplausibel / fehlend',
                    'Tritt teils nach Batterieabklemmen oder CAN-Bus-Unterbrechung auf.'),
  'C2402': DtcEntry('C2402', 'MDPS / Steering', 'critical', 'Servolenkung (MDPS) Motorstrom / Treiberfehler',
                    'MDPS-Versorgung und Lenkhilfe bei Neustart prüfen.'),
  'U0100': DtcEntry('U0100', 'CAN Communication', 'warning', 'Verbindung zum ECM/PCM verloren',
                    'Häufig bei 12V-Unterspannung oder Harness-Abstecken; nach Löschen beobachten.'),
  'U0101': DtcEntry('U0101', 'CAN Communication', 'warning', 'Verbindung zur Getriebesteuerung (TCU) verloren',
                    'CAN-Bus und Klemme 15 Versorgung der TCU prüfen.'),
  'U0121': DtcEntry('U0121', 'CAN Communication', 'warning', 'Verbindung zum ABS/ESC verloren',
                    'ABS-Sicherung und C-CAN-Leitung prüfen.'),
  'U0131': DtcEntry('U0131', 'CAN Communication', 'warning', 'Verbindung zur Servolenkung (MDPS) verloren',
                    'Typisch wenn Lenkkamera/MDPS getrennt war; nach Zündungswechsel erneut scannen.'),
  'U0140': DtcEntry('U0140', 'CAN Communication', 'info', 'Verbindung zum Karosseriesteuergerät (BCM) verloren',
                    'B-CAN/C-CAN Gateway und 12V-Ruhespannung prüfen.'),
  'U0401': DtcEntry('U0401', 'CAN Communication', 'warning', 'Ungültige Daten vom ECM/PCM empfangen',
                    'Steuergerät-Fehler im ECM (0x7E0) zuerst prüfen.'),
  'U0415': DtcEntry('U0415', 'CAN Communication', 'warning', 'Ungültige Daten vom ABS/ESC empfangen',
                    'ABS/ESC-Codes zuerst prüfen; Raddrehzahlsensoren verifizieren.'),
  'U1111': DtcEntry('U1111', '12V IBS / Battery', 'info', 'LIN-Kommunikation Batteriesensor (IBS) unterbrochen',
                    'Stecker am Minuspol der 12V-Batterie (IBS-Sensor) prüfen.'),
}


def decode_dtc_bytes(high_byte: int, low_byte: int, failure_type: int | None = None) -> str:
  """Format 2-byte ISO 15031-6 / SAE J2012 DTC (+ optional UDS failure type byte)."""
  prefix = _DTC_PREFIX[(high_byte >> 6) & 0x03]
  second = (high_byte >> 4) & 0x03
  third = high_byte & 0x0F
  fourth = (low_byte >> 4) & 0x0F
  fifth = low_byte & 0x0F
  base = f'{prefix}{second}{third:X}{fourth:X}{fifth:X}'
  if failure_type is not None and failure_type != 0:
    return f'{base}-{failure_type:02X}'
  return base


def decode_uds_status(status: int) -> dict[str, bool]:
  """Decode ISO 14229 DTC status availability mask bits."""
  return {
    'testFailed': bool(status & 0x01),
    'testFailedThisCycle': bool(status & 0x02),
    'pending': bool(status & 0x04),
    'confirmed': bool(status & 0x08),
    'notCompletedSinceClear': bool(status & 0x10),
    'failedSinceClear': bool(status & 0x20),
    'notCompletedThisCycle': bool(status & 0x40),
    'milRequested': bool(status & 0x80),
  }


def lookup_dtc(code: str) -> DtcEntry:
  """Return catalog entry for exact or base SAE DTC, falling back to structural SAE classification."""
  base = code.split('-', 1)[0].upper()
  if base in IONIQ_DTC_CATALOG:
    entry = IONIQ_DTC_CATALOG[base]
    return DtcEntry(code, entry.system, entry.severity, entry.title, entry.hint)
  system = _guess_sae_system(base)
  severity = 'warning' if base.startswith(('P', 'C', 'B', 'U')) else 'info'
  return DtcEntry(
    code,
    system,
    severity,
    f'Hersteller-/SAE-Diagnosecode {code}',
    'Code bei nächster Diagnose mit Status (Aktiv / Ausstehend / Historisch) beobachten.',
  )


def enrich_dtc_record(ecu_id: str, ecu_name: str, code: str, status_byte: int | None = None,
                      source: str = 'uds') -> dict:
  entry = lookup_dtc(code)
  status_flags = decode_uds_status(status_byte) if status_byte is not None else {}
  active = bool(status_flags.get('testFailed') or status_flags.get('milRequested'))
  confirmed = bool(status_flags.get('confirmed', source == 'obd_03'))
  pending = bool(status_flags.get('pending', source == 'obd_07'))
  permanent = source == 'obd_0A'
  severity = entry.severity
  if status_flags.get('milRequested'):
    severity = 'critical'
  elif not active and not pending and not confirmed and status_flags.get('failedSinceClear'):
    severity = 'info'
  return {
    'code': code,
    'ecuId': ecu_id,
    'ecuName': ecu_name,
    'system': entry.system,
    'severity': severity,
    'title': entry.title,
    'hint': entry.hint,
    'statusByte': status_byte,
    'statusHex': None if status_byte is None else f'0x{status_byte:02X}',
    'active': active,
    'pending': pending,
    'confirmed': confirmed,
    'permanent': permanent,
    'milRequested': bool(status_flags.get('milRequested')),
    'failedSinceClear': bool(status_flags.get('failedSinceClear')),
    'source': source,
  }


def analyze_dtcs(records: list[dict], live_warnings: list[dict] | None = None) -> dict:
  """Group DTCs + live CAN warnings into actionable root-cause insights."""
  active_count = sum(1 for item in records if item.get('active'))
  confirmed_count = sum(1 for item in records if item.get('confirmed'))
  pending_count = sum(1 for item in records if item.get('pending'))
  mil_count = sum(1 for item in records if item.get('milRequested'))
  codes = [str(item.get('code', '')).split('-', 1)[0] for item in records]
  insights: list[dict] = []

  u_codes = [c for c in codes if c.startswith('U')]
  if len(u_codes) >= 2 or 'P0562' in codes:
    insights.append({
      'severity': 'warning',
      'title': '12V-Spannung / CAN-Wakeup Muster',
      'detail': f'{len(u_codes)} Kommunikations-Codes (U-Codes) oder Unterspannung erkannt — beim 2016 Ioniq HEV häufig durch schwache 12V-Batterie im Kofferraum oder kurzes Stecker-Ziehen ausgelöst.',
    })
  hev_codes = [c for c in codes if c.startswith(('P0A', 'P1B'))]
  if hev_codes:
    insights.append({
      'severity': 'critical',
      'title': 'Hochvolt-/Hybrid-Subsystem betroffen',
      'detail': f'Folgende Hybrid-/BMS-Codes liegen vor: {", ".join(sorted(set(hev_codes)))}. Vor dem Löschen Freeze-Frame sichern.',
    })
  dct_codes = [c for c in codes if c in ('P0700', 'P0731', 'P0732', 'P0741', 'P0805', 'P1744')]
  if dct_codes:
    insights.append({
      'severity': 'warning',
      'title': '6-Gang DCT Doppelkupplung / Aktuator',
      'detail': f'Getriebe-/Kupplungscodes ({", ".join(sorted(set(dct_codes)))}) erkannt — HCA-Bremsflüssigkeitsstand im Motorraum prüfen.',
    })
  if live_warnings:
    insights.append({
      'severity': 'warning',
      'title': f'{len(live_warnings)} aktive CAN-Warnflag(s)',
      'detail': 'Aktive Signalfehler im laufenden CAN-Verkehr festgestellt.',
    })
  if not records and not live_warnings:
    insights.append({
      'severity': 'ok',
      'title': 'Keine Fehlercodes oder aktiven Warnungen gespeichert',
      'detail': 'Alle erfassten Steuergeräte und CAN-Signale sind unauffällig.',
    })

  return {
    'total': len(records),
    'activeCount': active_count,
    'confirmedCount': confirmed_count,
    'pendingCount': pending_count,
    'milCount': mil_count,
    'insights': insights,
  }


def _guess_sae_system(code: str) -> str:
  if len(code) < 3:
    return 'Unknown'
  kind, sub = code[0], code[1:3]
  if kind == 'U':
    return 'Network / CAN Bus'
  if kind == 'C':
    return 'Chassis (ABS/ESC/MDPS/SCC)'
  if kind == 'B':
    return 'Body / Comfort (BCM/DATC/CLU)'
  if kind == 'P':
    if sub in ('0A', '1B'):
      return 'Hybrid / HV Battery / Inverter'
    if sub in ('07', '08', '17'):
      return 'Transmission / 6-DCT'
    if sub in ('01', '02', '03', '04', '05'):
      return 'Powertrain / Fuel / Emissions'
    return 'Powertrain / Manufacturer'
  return 'Unknown'
