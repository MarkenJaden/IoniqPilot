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
  urgency: str = 'soon'  # 'immediate' | 'soon' | 'monitor'
  causes: tuple[str, ...] = ()
  workshop: str = ''


URGENCY_LABELS: dict[str, str] = {
  'immediate': 'Sofort Werkstatt / Nicht weiterfahren',
  'soon': 'Zeitnah in Werkstatt prüfen lassen',
  'monitor': 'Beobachten / Bei nächstem Service prüfen',
  'ok': 'Keine Maßnahme erforderlich',
}


# Curated high-signal SAE + Hyundai/Kia Kappa 1.6 GDi / 6-DCT / AE HEV DTC catalog.
IONIQ_DTC_CATALOG: dict[str, DtcEntry] = {
  'P0106': DtcEntry(
    'P0106', 'Engine / MAP', 'warning', 'Saugrohrdrucksensor (MAP) Bereichs-/Leistungsproblem',
    'Unterdruckschläuche, MAP-Sensorstecker und Drosselklappe auf Verschmutzung prüfen.',
    urgency='soon',
    causes=('Verschmutzter MAP-Sensor im Saugrohr', 'Undichter Unterdruckschlauch oder Kurbelgehäuseentlüftung (PCV)', 'Verkokte Drosselklappe beim 1.6 GDi'),
    workshop='Weiterfahrt möglich; bei unrundem Leerlauf oder erhöhtem Verbrauch zeitnah Saugrohrdrucksensor und Drosselklappe prüfen lassen.',
  ),
  'P0128': DtcEntry(
    'P0128', 'Cooling / Thermal', 'warning', 'Kühlmitteltemperatur unter Regelbereich',
    'Thermostat öffnet zu früh oder klemmt offen — erhöht Kraftstoffverbrauch im Hybridbetrieb.',
    urgency='soon',
    causes=('Kühlmittelthermostat schließt nicht vollständig', 'Kühlmitteltemperatursensor (ECT) driftet', 'EHR-Abgaswärmetauscher-Klappe klemmt'),
    workshop='Kein Notfall, aber der Verbrenner läuft zu kalt und verbraucht deutlich mehr Benzin (EV-Anteil sinkt). Zeitnah Thermostat prüfen lassen.',
  ),
  'P0130': DtcEntry(
    'P0130', 'Exhaust / O2 Sensor', 'warning', 'Lambdasonde 1 Bank 1 (vor Kat) Stromkreis/Signal',
    'Regelsonde vor Katalysator oder Steckverbindung auf Korrosion/Abgasleck prüfen.',
    urgency='soon',
    causes=('Gealterte Breitband-Lambdasonde vor Kat', 'Marder-/Scheuerspuren am Sondenkabel', 'Undichtigkeit am Abgaskrümmer'),
    workshop='Löst häufig eine kurzzeitige Motorkontrollleuchte (MIL) aus. Gemischregelung läuft im Ersatzwert — zeitnah Lambdasonde prüfen lassen.',
  ),
  'P0135': DtcEntry(
    'P0135', 'Exhaust / O2 Sensor', 'warning', 'Lambdasondenheizung Bank 1 Sonde 1 Fehlfunktion',
    'Heizkreis der Vorkat-Lambdasonde und Sicherung prüfen.',
    urgency='soon',
    causes=('Durchgebrannte Sondenheizung nach Kaltstart', 'Korrodierter Stecker oder defekte Sicherung im Motorraum'),
    workshop='Verursacht Kaltstart-Abgasfehler und MIL-Leuchte; Vorkat-Lambdasonde zeitnah tauschen lassen.',
  ),
  'P0136': DtcEntry(
    'P0136', 'Exhaust / O2 Sensor', 'warning', 'Lambdasonde 2 Bank 1 (nach Kat) Stromkreis/Signal',
    'Diagnosesonde nach Katalysator und Kabelbaum unter dem Fahrzeug prüfen.',
    urgency='monitor',
    causes=('Feuchtigkeit im Stecker der Nachkat-Sonde', 'Gealterte Diagnosesonde nach Kat'),
    workshop='Beeinträchtigt die Motorleistung meist nicht direkt; bei wiederholtem Auftreten Sonde prüfen lassen.',
  ),
  'P0141': DtcEntry(
    'P0141', 'Exhaust / O2 Sensor', 'warning', 'Lambdasondenheizung Bank 1 Sonde 2 Fehlfunktion',
    'Heizelement der Nachkat-Lambdasonde prüfen.',
    urgency='soon',
    causes=('Heizwiderstand der Nachkat-Lambdasonde hochohmig', 'Steckerkorrosion am Unterboden'),
    workshop='Typischer Auslöser für eine gelbe Motorkontrollleuchte ohne spürbaren Leistungsverlust. Zeitnah Sonde ersetzen.',
  ),
  'P0171': DtcEntry(
    'P0171', 'Fuel / Air Mixture', 'warning', 'Gemisch zu mager (Bank 1)',
    'Falschluft im Ansaugtrakt, verkokte GDi-Injektoren oder Kraftstoffdruck prüfen.',
    urgency='soon',
    causes=(
      'Falschluft über Ansaugkrümmerdichtung oder PCV-Schlauch',
      'Verkokte 1.6 GDi Hochdruck-Einspritzdüsen',
      'Schwache Kraftstoffpumpe oder Abgasleck vor Lambdasonde',
    ),
    workshop=(
      'Häufiger Grund für kurzes Aufleuchten der Motorkontrollleuchte unter Last. '
      + 'Zeitnah prüfen lassen, da Magerlauf zu erhöhter Verbrennungstemperatur führt.'
    ),
  ),
  'P0172': DtcEntry(
    'P0172', 'Fuel / Air Mixture', 'warning', 'Gemisch zu fett (Bank 1)',
    'Ölverdünnung durch Kurzstrecke, nachtropfender GDi-Injektor oder EVAP-Spülventil prüfen.',
    urgency='soon',
    causes=(
      'Offen klemmendes EVAP-Tankentlüftungsventil',
      'Nachtropfender GDi-Hochdruckinjektor oder Hochdruckpumpe',
      'Benzineintrag ins Motoröl bei vielen Kaltstarts im HEV-Betrieb',
    ),
    workshop='Kann den Katalysator belasten. Ölstand/Ölgeruch und EVAP-Ventil zeitnah prüfen lassen.',
  ),
  'P0300': DtcEntry(
    'P0300', 'Ignition / Fuel', 'critical', 'Zufällige/mehrere Zündaussetzer erkannt',
    'Zündkerzen, Zündspulen, GDi-Injektoren oder Kraftstoffqualität prüfen; Katalysatorschutz beachten.',
    urgency='immediate',
    causes=(
      'Verschlissene Iridium-Zündkerzen oder Zündspulen',
      'Verkokte Einlassventile / GDi-Injektoren beim 1.6 GDi (häufig bei Hybrid-Kaltstarts)',
      'Kondenswasser oder gealterter Kraftstoff',
    ),
    workshop=(
      'Sehr häufiger Auslöser für eine kurz aufleuchtende oder blinkende Motorkontrollleuchte beim Ioniq Hybrid! '
      + 'Wenn die MIL blinkt oder der Motor ruckelt: Last sofort reduzieren und Werkstatt aufsuchen (Gefahr von Katalysatorschäden).'
    ),
  ),
  'P0301': DtcEntry(
    'P0301', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 1',
    'Zündspule/Kerze Zyl. 1 quer tauschen und Hochdruck-Injektor prüfen.',
    urgency='immediate',
    causes=('Defekte Einzelfunken-Zündspule Zylinder 1', 'Verrußte Zündkerze Zylinder 1', 'Verkokter GDi-Injektor Zylinder 1'),
    workshop='Wenn nur kurzzeitig beim Kaltstart aufgetreten: beobachten; bei Ruckeln oder blinkender MIL sofort Zündspule/Kerze Zyl. 1 prüfen lassen.',
  ),
  'P0302': DtcEntry(
    'P0302', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 2',
    'Zündspule/Kerze Zyl. 2 quer tauschen und Hochdruck-Injektor prüfen.',
    urgency='immediate',
    causes=('Defekte Zündspule Zylinder 2', 'Zündkerze Zylinder 2 verschlissen', 'GDi-Injektor Zylinder 2 verkokt'),
    workshop='Bei aktivem Ruckeln oder blinkender MIL Last meiden und sofort Zündspule/Kerze Zyl. 2 prüfen lassen.',
  ),
  'P0303': DtcEntry(
    'P0303', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 3',
    'Zündspule/Kerze Zyl. 3 quer tauschen und Hochdruck-Injektor prüfen.',
    urgency='immediate',
    causes=('Defekte Zündspule Zylinder 3', 'Zündkerze Zylinder 3 verschlissen', 'GDi-Injektor Zylinder 3 verkokt'),
    workshop='Bei aktivem Ruckeln oder blinkender MIL Last meiden und sofort Zündspule/Kerze Zyl. 3 prüfen lassen.',
  ),
  'P0304': DtcEntry(
    'P0304', 'Ignition / Fuel', 'critical', 'Zündaussetzer Zylinder 4',
    'Zündspule/Kerze Zyl. 4 quer tauschen und Hochdruck-Injektor prüfen.',
    urgency='immediate',
    causes=('Defekte Zündspule Zylinder 4', 'Zündkerze Zylinder 4 verschlissen', 'GDi-Injektor Zylinder 4 verkokt'),
    workshop='Bei aktivem Ruckeln oder blinkender MIL Last meiden und sofort Zündspule/Kerze Zyl. 4 prüfen lassen.',
  ),
  'P0401': DtcEntry(
    'P0401', 'EGR / Emissions', 'warning', 'Abgasrückführung (AGR/EGR) Durchfluss zu gering',
    'AGR-Ventil, AGR-Kühler und Kanäle am 1.6 GDi auf Rußablagerungen prüfen.',
    urgency='soon',
    causes=(
      'Verkoktes elektrisches AGR-Ventil oder zugesetzter AGR-Kühler beim 1.6 GDi',
      'Verstopfte AGR-Bohrung im Ansaugkrümmer',
      'Differenzdruck-/MAP-Signalabweichung',
    ),
    workshop=(
      'Klassischer Grund beim Ioniq 1.6 GDi für eine gelbe Motorkontrollleuchte, die nach einigen Fahrten wieder ausgeht. '
      + 'AGR-Ventil und Kanäle bei Gelegenheit reinigen/prüfen lassen.'
    ),
  ),
  'P0420': DtcEntry(
    'P0420', 'Exhaust / Catalyst', 'warning', 'Katalysator-Wirkungsgrad unter Schwellenwert (Bank 1)',
    'Nachkat-Lambdasonde, Abgasleck vor Kat oder Katalysatoralterung prüfen.',
    urgency='soon',
    causes=(
      'Kurzstrecken-/Hybridbetrieb mit häufigem Auskühlen des Katalysators',
      'Kleines Abgasleck am Flexrohr oder Flansch',
      'Gealterte Nachkat-Lambdasonde oder verringerte Sauerstoffspeicherfähigkeit des Kats',
    ),
    workshop=(
      'Der häufigste Auslöser überhaupt für eine temporäre Motorkontrollleuchte ohne spürbare Fahrprobleme! '
      + 'Weiterfahrt ist problemlos möglich; vor der nächsten Hauptuntersuchung (AU) Abgasanlage und Lambdasonden prüfen lassen.'
    ),
  ),
  'P0441': DtcEntry(
    'P0441', 'EVAP', 'info', 'Tankentlüftungssystem (EVAP) falscher Spülstrom',
    'EVAP-Regenerierventil (PCSV) am Saugrohr prüfen.',
    urgency='monitor',
    causes=('Klebendes EVAP-Spülventil (Purge Control Solenoid Valve)', 'Tank bis über das Abschalten hinaus überfüllt (Aktivkohlefilter gesättigt)'),
    workshop='Unkritisch für die Fahrsicherheit, schaltet aber gern kurz die MIL ein. Bei wiederholtem Auftreten EVAP-Spülventil tauschen.',
  ),
  'P0455': DtcEntry(
    'P0455', 'EVAP', 'warning', 'Tankentlüftungssystem (EVAP) großes Leck',
    'Tankdeckeldichtung und EVAP-Spülventil prüfen.',
    urgency='monitor',
    causes=('Tankdeckel nach dem Tanken nicht bis zum Klicken zugedreht', 'Poröse Tankdeckeldichtung oder offenes EVAP-Ventil'),
    workshop='Tankdeckel öffnen und fest bis zum Klicken schließen. Die Motorkontrollleuchte erlischt danach meist nach einigen Fahrzyklen von selbst.',
  ),
  'P0456': DtcEntry(
    'P0456', 'EVAP', 'info', 'Tankentlüftungssystem (EVAP) kleines Leck',
    'Tankdeckel fest schließen, EVAP-Leitungen bei Inspektion prüfen.',
    urgency='monitor',
    causes=('Leicht undichter Tankdeckel', 'Mikroriss im Entlüftungsschlauch'),
    workshop='Unkritisch — Tankdeckeldichtung prüfen und beobachten.',
  ),
  'P0562': DtcEntry(
    'P0562', '12V Power', 'warning', 'Bordnetzspannung zu niedrig',
    '12V-Batterie-/IBS-Status prüfen; beim 2016 Ioniq HEV 12V-Hilfsbatterie im Kofferraum & LDC-Wandler prüfen.',
    urgency='soon',
    causes=(
      'Gealterte 12V-Hilfsbatterie im Kofferraum (beim 2016er Modell noch separate 12V-Bleibatterie)',
      'Ruhestromverbrauch im Stand',
      'Kurzer Spannungseinbruch beim Starten',
    ),
    workshop=(
      'Schwache 12V-Spannung erzeugt beim Ioniq oft Folgefehler (U-Codes, Getriebe-/ABS-Warnungen beim Einschalten). '
      + '12V-Batterie im Monitor beobachten und ggf. nachladen/ersetzen.'
    ),
  ),
  'P0563': DtcEntry(
    'P0563', '12V Power', 'warning', 'Bordnetzspannung zu hoch',
    'LDC (DC/DC-Wandler) Ausgangsspannung prüfen.',
    urgency='soon',
    causes=('Regelabweichung des LDC (DC/DC-Wandler in der HPCU)', 'Fremdstart-/Ladegerät mit überhöhter Spannung'),
    workshop='Wenn im Fahrbetrieb dauerhaft über 15.2 V anliegen, LDC-Regelung in der Werkstatt prüfen lassen.',
  ),
  'P0605': DtcEntry(
    'P0605', 'ECU Internal', 'critical', 'Steuergerät ROM/Flash-Prüfsummenfehler',
    'Softwarestand und Spannungsversorgung des Steuergeräts prüfen.',
    urgency='immediate',
    causes=('Spannungseinbruch während Steuergeräte-Nachlauf', 'Interner Speicherfehler im Steuergerät'),
    workshop='Werkstatt / Hyundai-Service auf ECU-Software-Update und Spannungsversorgung prüfen lassen.',
  ),
  'P0700': DtcEntry(
    'P0700', 'Transmission / DCT', 'warning', 'Getriebesteuerung (TCU) MIL-Anforderung',
    'Detailcodes direkt aus Steuergerät 0x7E1 (TCU / 6-Gang DCT) auslesen.',
    urgency='soon',
    causes=(
      'Folgecode im Motorsteuergerät, weil die 6-DCT Getriebesteuerung (0x7E1) einen Fehler gemeldet hat',
      'Kurzzeitige Kupplungsaktuator- oder Gangwahl-Abweichung',
    ),
    workshop=(
      'Begleitcodes der TCU (0x7E1) beachten. Wenn das Getriebe sauber schaltet, '
      + 'war es meist ein einmaliger Aktuator-Hänger; bei Schaltproblemen Werkstatt aufsuchen.'
    ),
  ),
  'P0731': DtcEntry(
    'P0731', 'Transmission / DCT', 'warning', 'Gang 1 falsches Übersetzungsverhältnis',
    'DCT-Kupplungsaktuator und Kalibrierwerte der Doppelkupplung prüfen.',
    urgency='soon',
    causes=('Kupplungsschlupf auf Welle 1 (ungerade Gänge)', 'Luft oder zu wenig DOT4-Flüssigkeit im hydraulischen Kupplungsaktuator (HCA)'),
    workshop='Zeitnah HCA-Behälter im Motorraum und DCT-Kupplungsadaption in der Werkstatt prüfen lassen.',
  ),
  'P0732': DtcEntry(
    'P0732', 'Transmission / DCT', 'warning', 'Gang 2 falsches Übersetzungsverhältnis',
    'DCT-Kupplungsaktuator (ungerade/gerade Welle) und Getriebeöl prüfen.',
    urgency='soon',
    causes=('Kupplungsschlupf auf Welle 2 (gerade Gänge)', 'Verschlissene Doppelkupplung oder HCA-Druckverlust'),
    workshop='Zeitnah DCT-Kupplungsaktuator und Kupplungsspiel in der Werkstatt prüfen lassen.',
  ),
  'P0741': DtcEntry(
    'P0741', 'Transmission / DCT', 'warning', 'Kupplung/Überbrückung Leistungsproblem',
    'Elektrischen/hydraulischen Kupplungsaktuator des DCT prüfen.',
    urgency='soon',
    causes=('Verzögerte Druckregelung im hydraulischen Kupplungsaktuator (HCA)'),
    workshop='Bei spürbarem Ruckeln beim Anfahren oder Schalten Werkstatt aufsuchen.',
  ),
  'P0805': DtcEntry(
    'P0805', 'Transmission / DCT', 'critical', 'Kupplungspositionssensor Schaltkreis',
    'Stecker und Kabelbaum am DCT-Kupplungsaktuator prüfen.',
    urgency='immediate',
    causes=('Kontaktproblem am DCT-Kupplungsaktuator', 'Defekter Wegsensor im Aktuator'),
    workshop='Gefahr von Gangverlust (nur gerade oder ungerade Gänge verfügbar). Direkt in der Werkstatt prüfen lassen.',
  ),
  'P0A0F': DtcEntry(
    'P0A0F', 'Hybrid Powertrain', 'critical', 'Verbrennungsmotor startet im Hybridbetrieb nicht',
    'Kraftstoffstand, HSG (Hybrid Starter Generator), Kurbelwellensensor und HV-Freigabe prüfen.',
    urgency='immediate',
    causes=(
      'Kraftstoffmangel oder Luft im Kraftstoffsystem',
      'HSG-Riemen (Startergenerator-Keilrippenriemen) gerissen oder durchrutschend',
      'Kurbelwellensensor oder Hochdruckpumpe ohne Freigabe',
    ),
    workshop='Sofort prüfen! Wenn der Verbrenner im Hybridbetrieb nicht anspringt, wird die HV-Batterie schnell entladen.',
  ),
  'P0A1F': DtcEntry(
    'P0A1F', 'HV Battery / BMS', 'critical', 'Batteriesteuergerät (BMS) interner Fehler',
    'BMS 0x7E4 Freeze-Frame auslesen und HV-Trennstecker/Kühlung prüfen.',
    urgency='immediate',
    causes=('BMS-Steuergerätefehler oder Zellüberwachungs-Kommunikationsfehler'),
    workshop='Freeze-Frame sichern und umgehend Hyundai-Hochvolt-Fachwerkstatt aufsuchen.',
  ),
  'P0A7F': DtcEntry(
    'P0A7F', 'HV Battery / BMS', 'warning', 'Hybrid-Akku Alterung / Kapazitätsverlust',
    'Zellspannungsdifferenz und Innenwiderstand über BMS prüfen.',
    urgency='soon',
    causes=('Erhöhter Innenwiderstand einzelner Li-Ion-Polymer-Zellblöcke', 'Verschmutzter HV-Batterielüfter unter der Rücksitzbank'),
    workshop='Lüftungsgitter der HV-Batterie an der Rücksitzbank freihalten und Kapazität/Zelldrift bei Hyundai prüfen lassen.',
  ),
  'P0A80': DtcEntry(
    'P0A80', 'HV Battery / BMS', 'critical', 'Hybrid-Batteriepack muss geprüft/ersetzt werden',
    'Starke Zellimbalance im Li-Ion Polymer Pack beim AE HEV.',
    urgency='immediate',
    causes=('Deutliche Zellspannungsabweichung im 1.56 kWh Li-Ion-Polymer-Akku'),
    workshop='Vor dem Löschen Diagnosebericht exportieren und Hyundai-Vertragswerkstatt aufsuchen.',
  ),
  'P0A94': DtcEntry(
    'P0A94', 'Hybrid Inverter / LDC', 'critical', 'DC/DC-Wandler (LDC) Leistung/Überstrom',
    '12V-Ladesystem und Inverter-Kühlmittelpumpe prüfen.',
    urgency='immediate',
    causes=('Übertemperatur oder Ausfall im LDC/HPCU-Inverter', 'Elektrische Kühlmittelpumpe des Inverters ausgefallen'),
    workshop='Nicht auf längere Strecke gehen — wenn der LDC ausfällt, wird die 12V-Batterie während der Fahrt nicht mehr geladen. Sofort Werkstatt aufsuchen.',
  ),
  'P0AC0': DtcEntry(
    'P0AC0', 'HV Battery / BMS', 'warning', 'Hybrid-Batteriestromsensor Bereich/Leistung',
    'BMS-Stromsensor und Masseverbindungen am HV-Pack prüfen.',
    urgency='soon',
    causes=('Offset-Drift am Hall-Stromsensor des HV-Batteriepacks'),
    workshop='Zeitnah in einer Hochvolt-Werkstatt prüfen lassen.',
  ),
  'P1744': DtcEntry(
    'P1744', 'Transmission / DCT', 'warning', 'Hydraulischer Kupplungsaktuator (HCA) / DCT Kupplung',
    'Bremsflüssigkeit des hydraulischen Ausrücklagers im DCT (Reservoir im Motorraum) und Entlüftung prüfen!',
    urgency='soon',
    causes=(
      'Gealterte oder zu niedrige DOT4-Flüssigkeit im kleinen HCA-Behälter im Motorraum',
      'Luftblasen oder Druckverlust im hydraulischen Kupplungsaktuator (bekanntes Ioniq/Niro HEV Thema)',
    ),
    workshop=(
      'Wichtiger Ioniq-Spezialhinweis: Kleinen DOT4-Ausgleichsbehälter des Kupplungsaktuators (HCA) prüfen '
      + 'und in der Werkstatt entlüften/wechseln lassen.'
    ),
  ),
  'P1B70': DtcEntry(
    'P1B70', 'HV Battery / BMS', 'critical', 'Hochvolt-Batteriespannung niedrig',
    'HV-Vorladung (Precharge) und Schütze sowie Zellspannungen prüfen.',
    urgency='immediate',
    causes=('Tiefentladung des HV-Packs oder defektes HV-Hauptrelais (PRA)'),
    workshop='Sofort Hochvolt-Diagnose in der Werkstatt durchführen lassen.',
  ),
  'P1B74': DtcEntry(
    'P1B74', 'HV Battery / BMS', 'critical', 'Hochvolt-Batterie Zellspannungsüberwachung',
    'Zellspannungssensorik / Busbar-Kontakte im Li-Ion Pack prüfen.',
    urgency='immediate',
    causes=('Kontaktproblem an der Zellüberwachungsplatine (CMU) im HV-Akku'),
    workshop='Freeze-Frame sichern und Hyundai-Werkstatt aufsuchen (bekannte Serviceaktion bei frühen AE HEV beachten).',
  ),
  'P1B77': DtcEntry(
    'P1B77', 'Hybrid Inverter', 'critical', 'Inverter-Vorladung (Precharge) fehlgeschlagen',
    'HV-Hauptsicherung, Vorladewiderstand und PRA-Relais prüfen.',
    urgency='immediate',
    causes=('Defektes Vorladerelais oder HV-Sicherung im PRA-Modul', 'Kurzschluss im Zwischenkreis der HPCU'),
    workshop='Fahrzeug lässt sich meist nicht mehr in READY schalten — Hochvolt-Werkstatt erforderlich.',
  ),
  'P1B96': DtcEntry(
    'P1B96', 'Hybrid System', 'critical', 'Hybrid-Steuergerät Leistungsrelais / Isolationsfehler',
    'HV-Isolationswiderstand (HPCU / Klimakompressor / Kabel) messen.',
    urgency='immediate',
    causes=('Feuchtigkeit im orangen HV-Kabelbaum oder HV-Klimakompressor', 'Hängendes HV-Hauptschütz'),
    workshop='Sicherheitsrelevanter Hochvolt-Fehler — umgehend in einer Fachwerkstatt prüfen lassen.',
  ),
  'P2196': DtcEntry(
    'P2196', 'Exhaust / O2 Sensor', 'warning', 'Lambdasonde Bank 1 Sonde 1 Signal hängt auf „fett“',
    'Vorkat-Lambdasonde, Kraftstoffdruck oder EVAP-Spülventil prüfen.',
    urgency='soon',
    causes=('Träge Vorkat-Breitbandsonde', 'Offen hängendes EVAP-Spülventil oder kurzzeitige Gemischanfettung'),
    workshop='Typischer Auslöser für eine kurz aufleuchtende Motorkontrollleuchte. Zeitnah Lambdasonden-Signalverlauf prüfen lassen.',
  ),
  'C1200': DtcEntry(
    'C1200', 'ABS / Wheel Speed', 'warning', 'Raddrehzahlsensor vorne links unterbrochen/Kurzschluss',
    'Sensorleitung und Impulsring VL prüfen (beeinflusst ABS, ESC und Openpilot-Geschwindigkeit).',
    urgency='soon',
    causes=('Verschmutzter oder defekter ABS-Raddrehzahlsensor vorne links', 'Beschädigtes Sensorkabel im Radhaus'),
    workshop='Deaktiviert ABS/ESC und den Tempomaten (SCC). Zeitnah Sensor vorne links prüfen lassen.',
  ),
  'C1203': DtcEntry(
    'C1203', 'ABS / Wheel Speed', 'warning', 'Raddrehzahlsensor vorne rechts unterbrochen/Kurzschluss',
    'Sensorleitung und Impulsring VR prüfen.',
    urgency='soon',
    causes=('Verschmutzter oder defekter ABS-Raddrehzahlsensor vorne rechts', 'Beschädigtes Sensorkabel im Radhaus'),
    workshop='Deaktiviert ABS/ESC und den Tempomaten (SCC). Zeitnah Sensor vorne rechts prüfen lassen.',
  ),
  'C1260': DtcEntry(
    'C1260', 'MDPS / Steering', 'warning', 'Lenkwinkelsensor (SAS) Signal unplausibel / nicht kalibriert',
    'Lenkwinkel-Nullpunkt (SAS11) bei Geradeausfahrt neu anlernen.',
    urgency='soon',
    causes=('Spurverstellung oder Lenkwinkel-Offset nach 12V-Spannungsabfall'),
    workshop='Beeinflusst Spurhalteassistent und Openpilot-Lenkung — Lenkwinkelsensor kalibrieren lassen.',
  ),
  'C1611': DtcEntry(
    'C1611', 'CAN Bus / Communication', 'warning', 'CAN-Timeout zum Motorsteuergerät (EMS)',
    'Steckverbindungen am C-CAN oder Nachrüst-Harness auf Wackelkontakt prüfen.',
    urgency='monitor',
    causes=('Kurzer Zündungs-/Spannungseinbruch beim Start', 'Stecker am Kamera-Harness wurde bei eingeschalteter Zündung bewegt'),
    workshop='Wenn nur historisch gespeichert: unbedenklich und löschbar.',
  ),
  'C1612': DtcEntry(
    'C1612', 'CAN Bus / Communication', 'warning', 'CAN-Timeout zum Getriebesteuergerät (TCU)',
    'Oft Folge von Unterspannung oder Bus-Unterbrechung während Steuergeräte-Reset.',
    urgency='monitor',
    causes=('Kurze Unterspannung der 12V-Batterie beim Systemstart'),
    workshop='Wenn nicht aktiv: historischer Kommunikationscode, kann gelöscht werden.',
  ),
  'C1616': DtcEntry(
    'C1616', 'CAN Bus / Communication', 'warning', 'CAN Bus Off Fehler',
    'Kabelbaum, Abschlusswiderstände (120 Ohm) und OBD/Harness-Stecker prüfen.',
    urgency='soon',
    causes=('Wackelkontakt am Comma-Harness / Adapterstecker', 'Störung auf dem C-CAN-Bus'),
    workshop='Sitz des Comma-Harness-Steckers an der Frontkamera und am OBD-Port prüfen.',
  ),
  'C1620': DtcEntry(
    'C1620', 'SCC / ADAS', 'warning', 'Frontradar Ausrichtung / Kalibrierung fehlgeschlagen',
    'Radarhalter hinter dem Hyundai-Emblem auf Stoß/Schmutz prüfen.',
    urgency='soon',
    causes=('Schnee/Laub/Schmutz vor dem Frontradar im Kühlergrill', 'Leichter Parkrempler am vorderen Stoßfänger'),
    workshop='Frontradar-Abdeckung reinigen; falls dauerhaft aktiv, Radarhalterung und Kalibrierung prüfen lassen.',
  ),
  'C1642': DtcEntry(
    'C1642', 'MDPS / CAN', 'warning', 'CAN-Signal vom ESC unplausibel / fehlend',
    'Tritt teils nach Batterieabklemmen oder CAN-Bus-Unterbrechung auf.',
    urgency='monitor',
    causes=('Folgecode nach kurzem ABS/ESC-Reset oder 12V-Spannungsabfall'),
    workshop='Wenn kein aktiver ABS-Fehler vorliegt, Code löschen und beobachten.',
  ),
  'C2402': DtcEntry(
    'C2402', 'MDPS / Steering', 'critical', 'Servolenkung (MDPS) Motorstrom / Treiberfehler',
    'MDPS-Versorgung und Lenkhilfe bei Neustart prüfen.',
    urgency='immediate',
    causes=('Unterspannung an der 80A-MDPS-Sicherung', 'Interner Fehler im elektrischen Lenkgetriebe (MDPS)'),
    workshop='Wenn die Lenkung schwergängig ist: nicht weiterfahren und sofort Werkstatt aufsuchen.',
  ),
  'U0100': DtcEntry(
    'U0100', 'CAN Communication', 'warning', 'Verbindung zum ECM/PCM verloren',
    'Häufig bei 12V-Unterspannung oder Harness-Abstecken; nach Löschen beobachten.',
    urgency='monitor',
    causes=('Kurzer 12V-Spannungseinbruch beim Einschalten', 'Abziehen/Anstecken des Comma-Harness bei eingeschalteter Zündung'),
    workshop='Typischer historischer Begleitcode — wenn nicht als „Aktiv“ markiert, kein Werkstattbesuch nötig.',
  ),
  'U0101': DtcEntry(
    'U0101', 'CAN Communication', 'warning', 'Verbindung zur Getriebesteuerung (TCU) verloren',
    'CAN-Bus und Klemme 15 Versorgung der TCU prüfen.',
    urgency='monitor',
    causes=('Kurzer Timeout beim Aufwachen der Steuergeräte'),
    workshop='Wenn nur historisch hinterlegt, löschen und beobachten.',
  ),
  'U0121': DtcEntry(
    'U0121', 'CAN Communication', 'warning', 'Verbindung zum ABS/ESC verloren',
    'ABS-Sicherung und C-CAN-Leitung prüfen.',
    urgency='monitor',
    causes=('Kurzer CAN-Timeout oder 12V-Spannungsabfall'),
    workshop='Wenn keine ABS-Lampe im Tacho dauerhaft brennt, historischer Code ohne Handlungsbedarf.',
  ),
  'U0131': DtcEntry(
    'U0131', 'CAN Communication', 'warning', 'Verbindung zur Servolenkung (MDPS) verloren',
    'Typisch wenn Lenkkamera/MDPS getrennt war; nach Zündungswechsel erneut scannen.',
    urgency='monitor',
    causes=('Comma-Harness an der LKAS-Kamera abgesteckt', 'Kurzer Wakeup-Versatz'),
    workshop='Wenn nicht aktiv, unbedenklich.',
  ),
  'U0140': DtcEntry(
    'U0140', 'CAN Communication', 'info', 'Verbindung zum Karosseriesteuergerät (BCM) verloren',
    'B-CAN/C-CAN Gateway und 12V-Ruhespannung prüfen.',
    urgency='monitor',
    causes=('Wakeup-Verzögerung zwischen Karosserie-CAN und Antriebs-CAN'),
    workshop='Rein informativer Kommunikationscode — keine Maßnahme erforderlich.',
  ),
  'U0401': DtcEntry(
    'U0401', 'CAN Communication', 'warning', 'Ungültige Daten vom ECM/PCM empfangen',
    'Steuergerät-Fehler im ECM (0x7E0) zuerst prüfen.',
    urgency='monitor',
    causes=('Folgecode in ABS/TCU, weil das Motorsteuergerät kurzzeitig einen eigenen Fehler gemeldet hat'),
    workshop='Immer zuerst den Hauptfehler im Motorsteuergerät (0x7E0) beachten.',
  ),
  'U0415': DtcEntry(
    'U0415', 'CAN Communication', 'warning', 'Ungültige Daten vom ABS/ESC empfangen',
    'ABS/ESC-Codes zuerst prüfen; Raddrehzahlsensoren verifizieren.',
    urgency='monitor',
    causes=('Folgecode bei kurzzeitigem Radschlupf oder ABS-Regelung'),
    workshop='Solange kein C12xx-Radsensorfehler aktiv ist, unbedenklich.',
  ),
  'U1111': DtcEntry(
    'U1111', '12V IBS / Battery', 'info', 'LIN-Kommunikation Batteriesensor (IBS) unterbrochen',
    'Stecker am Minuspol der 12V-Batterie (IBS-Sensor) prüfen.',
    urgency='monitor',
    causes=('Stecker am intelligenten Batteriesensor (IBS) am Minuspol im Kofferraum war kurz ab'),
    workshop='2-poligen Stecker am Minuspol der 12V-Batterie im Kofferraum auf festen Sitz prüfen.',
  ),
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
    return DtcEntry(
      code,
      entry.system,
      entry.severity,
      entry.title,
      entry.hint,
      urgency=entry.urgency,
      causes=entry.causes,
      workshop=entry.workshop,
    )
  system = _guess_sae_system(base)
  severity = 'warning' if base.startswith(('P', 'C', 'B', 'U')) else 'info'
  urgency = 'monitor' if base.startswith(('B', 'U')) else 'soon'
  return DtcEntry(
    code,
    system,
    severity,
    f'Hersteller-/SAE-Diagnosecode {code}',
    'Code bei nächster Diagnose mit Status (Aktiv / Ausstehend / Historisch) beobachten.',
    urgency=urgency,
    causes=('Sporadische Signalabweichung im betroffenen Teilsystem', 'Historischer Eintrag nach kurzem Spannungseinbruch'),
    workshop='Wenn der Code nur als „Historisch“ oder „Ausstehend“ markiert ist und keine Warnlampe dauerhaft brennt: beobachten.',
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
  urgency = entry.urgency
  if status_flags.get('milRequested'):
    severity = 'critical'
  elif not active and not pending and not confirmed and status_flags.get('failedSinceClear'):
    severity = 'info'
    urgency = 'monitor'
  elif not active and (pending or confirmed):
    # Sporadic / non-active stored code that turned off its lamp
    if urgency == 'immediate':
      urgency = 'soon'

  if active and status_flags.get('milRequested'):
    state_explanation = 'Aktuell aktiv mit angeforderter Motorkontrollleuchte (MIL).'
  elif active:
    state_explanation = 'Aktuell im laufenden Fahrzyklus als aktiv gemeldet.'
  elif pending and confirmed:
    state_explanation = 'Sporadischer Fehler: war kürzlich aktiv (z. B. kurzes Aufleuchten der MIL) und ist zur Nachbeobachtung gespeichert.'
  elif pending:
    state_explanation = 'Ausstehender Fehler (Pending / Mode 07): einmalig erkannt, wartet auf Bestätigung im nächsten Fahrzyklus.'
  elif confirmed or permanent:
    state_explanation = 'Gespeicherter/historischer Fehler: derzeit NICHT aktiv, erklärt aber ein früheres kurzes Aufleuchten der Motorkontrollleuchte.'
  else:
    state_explanation = 'Historischer Hinweis seit dem letzten Fehlerspeicher-Löschen (aktuell fehlerfrei).'

  return {
    'code': code,
    'ecuId': ecu_id,
    'ecuName': ecu_name,
    'system': entry.system,
    'severity': severity,
    'urgency': urgency,
    'urgencyLabel': URGENCY_LABELS.get(urgency, URGENCY_LABELS['soon']),
    'title': entry.title,
    'hint': entry.hint,
    'causes': list(entry.causes),
    'workshopAdvice': entry.workshop or entry.hint,
    'stateExplanation': state_explanation,
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
  historic_count = sum(
    1 for item in records
    if not item.get('active') and (
      item.get('confirmed') or item.get('pending') or item.get('failedSinceClear') or item.get('permanent')
    )
  )
  codes = [str(item.get('code', '')).split('-', 1)[0] for item in records]
  insights: list[dict] = []

  u_codes = [c for c in codes if c.startswith('U')]
  if len(u_codes) >= 2 or 'P0562' in codes:
    insights.append({
      'severity': 'warning',
      'title': '12V-Spannung / CAN-Wakeup Muster',
      'detail': (
        f'{len(u_codes)} Kommunikations-Codes (U-Codes) oder Unterspannung erkannt — '
        + 'beim 2016 Ioniq HEV häufig durch schwache 12V-Batterie im Kofferraum oder kurzes Stecker-Ziehen ausgelöst.'
      ),
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
  mil_suspect_set = {
    'P0300', 'P0301', 'P0302', 'P0303', 'P0304', 'P0420', 'P0401', 'P0171', 'P0172',
    'P0130', 'P0135', 'P0136', 'P0141', 'P0441', 'P0455', 'P0456', 'P2196',
  }
  mil_suspects = [c for c in codes if c in mil_suspect_set]
  if mil_suspects:
    insights.append({
      'severity': 'warning' if active_count == 0 else 'critical',
      'title': 'Ursache für kurzes Aufleuchten der Motorkontrollleuchte (MIL) gefunden',
      'detail': (
        f'Emissions-/Verbrennungscode(s) {", ".join(sorted(set(mil_suspects)))} gespeichert. '
        + 'Selbst wenn die Leuchte im Tacho wieder aus ist, bleibt der Auslöser inkl. Umgebungsdaten (Freeze-Frame) hier zur Auswertung erhalten.'
      ),
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

  urgencies = [str(item.get('urgency', 'monitor')) for item in records]
  if 'immediate' in urgencies:
    overall_urgency = 'immediate'
  elif 'soon' in urgencies or bool(live_warnings):
    overall_urgency = 'soon'
  elif records:
    overall_urgency = 'monitor'
  else:
    overall_urgency = 'ok'

  mil_explanation = (
    'Warum geht die Motorkontrollleuchte (MIL) manchmal kurz an und wieder aus? '
    + 'Beim Ioniq Hybrid startet und stoppt der 1.6 GDi Verbrenner während der Fahrt ständig. '
    + 'Tritt dabei kurzzeitig ein Zündaussetzer (P0300–P0304), eine Lambdasonden-/Katalysatorschwankung (P0420/P0130–P0141), '
    + 'ein verkoktes AGR-Ventil (P0401) oder ein Tankentlüftungsfehler (P0441/P0455) auf, legt das Motorsteuergerät (0x7E0) '
    + 'sofort einen „Ausstehenden“ (Mode 07) oder „Bestätigten“ (Mode 03 / UDS 0x19) Code samt Freeze-Frame-Messwerten ab. '
    + 'Sind die nächsten Fahrzyklen wieder fehlerfrei, schaltet der Tacho die gelbe Lampe automatisch wieder ab — '
    + 'der Code bleibt aber bis zu 40 Aufwärmzyklen im Speicher und kann hier per „Aktiver ECU-Scan“ im Stand ausgelesen werden.'
  )

  return {
    'total': len(records),
    'activeCount': active_count,
    'confirmedCount': confirmed_count,
    'pendingCount': pending_count,
    'historicCount': historic_count,
    'milCount': mil_count,
    'overallUrgency': overall_urgency,
    'overallUrgencyLabel': URGENCY_LABELS.get(overall_urgency, URGENCY_LABELS['ok']),
    'milExplanation': mil_explanation,
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
