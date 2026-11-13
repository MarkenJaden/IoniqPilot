"""Parked & local Galaxy backend for 2016 Hyundai Ioniq Hybrid diagnostics, recorder, 3x buttons, and fuel tracking."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable

from openpilot.starpilot.ioniq.button_learner import (
  BUTTON_ACTIONS,
  VALID_ACTION_IDS,
  read_buttons_state,
  write_buttons_state,
)
from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.dtc_catalog import analyze_dtcs
from openpilot.starpilot.ioniq.fuel_tracker import FuelTripTracker, write_fuel_state
from openpilot.starpilot.ioniq.recorder import EventFlightRecorder
from openpilot.starpilot.ioniq.runtime import push_runtime_command
from openpilot.starpilot.ioniq.storage import ioniq_storage_root, read_config, write_config
from openpilot.starpilot.ioniq.uds_dtc import (
  IONIQ_ECUS,
  apply_clear_result,
  apply_scan_result,
  clear_ioniq_dtcs,
  queue_diagnostic_action,
  read_diagnostics_state,
  scan_ioniq_ecus,
  write_diagnostics_state,
)


class IoniqToolsChanged(Exception):
  pass


def vehicle_info(cp: Any) -> dict[str, Any]:
  """Describe connected vehicle and whether it is a Hyundai / 2016 Ioniq Hybrid."""
  if cp is None:
    return {
      'known': False,
      'brand': None,
      'fingerprint': None,
      'isHyundai': True,
      'isIoniqHev': False,
      'label': 'Warte auf Fahrzeugverbindung (Ioniq AE HEV Profil aktiv)',
    }
  brand = str(getattr(cp, 'brand', '') or '')
  fp = str(getattr(cp, 'carFingerprint', '') or '')
  is_hyundai = brand == 'hyundai' and not bool(getattr(cp, 'notCar', False))
  is_ioniq = fp == 'HYUNDAI_IONIQ'
  label = '2016 Hyundai Ioniq Hybrid (AE HEV)' if is_ioniq else (fp or brand or 'Unbekanntes Fahrzeug')
  return {
    'known': True,
    'brand': brand,
    'fingerprint': fp,
    'isHyundai': is_hyundai,
    'isIoniqHev': is_ioniq,
    'label': label,
  }


@dataclass(frozen=True)
class View:
  identity: tuple
  expires: float


class IoniqTools:
  """Stateful Galaxy owner for Ioniq ECU diagnostics, recordings, 3x button learning, and Tank-to-Tank fuel."""

  def __init__(
    self,
    params: Any,
    context: Any,
    *,
    root: Path | str | None = None,
    clock: Callable[[], float] = time.monotonic,
    direct_transport: Callable[[], tuple[Callable[..., Any], Callable[..., Any]] | None] | None = None,
  ) -> None:
    self.params = params
    self.context = context
    self.root = ioniq_storage_root(root)
    self.clock = clock
    self.direct_transport = direct_transport
    self.lock = threading.Lock()
    self.views: dict[str, View] = {}

  def close(self) -> None:
    close = getattr(self.context, 'close', None)
    if callable(close):
      close()

  @staticmethod
  def identity(token: str, generation: bytes) -> tuple:
    return hashlib.sha256(token.encode('ascii')).digest(), generation

  def page(self, token: str, generation: bytes) -> dict[str, Any]:
    ctx = self.context.sample()
    vinfo = vehicle_info(getattr(ctx, 'cp', None))
    visible = bool(vinfo['isHyundai'])
    identity = self.identity(token, generation)
    now_mono = self.clock()
    with self.lock:
      self.views = {k: v for k, v in self.views.items() if v.expires > now_mono}
      view_token = secrets.token_urlsafe(24)
      self.views[view_token] = View(identity=identity, expires=now_mono + 300.0)

    cfg = read_config(self.root)
    diag = read_diagnostics_state(self.root)
    buttons = read_buttons_state(self.root)
    fuel = FuelTripTracker(
      self.root,
      fco_scale_ul=float(cfg.get('fcoScaleUlPerTick', 0.128)),
      price_per_liter=float(cfg.get('fuelPriceEurPerLiter', 1.75)),
    ).snapshot()
    recorder_status = EventFlightRecorder(self.root).status()

    # Ensure analysis stays fresh against latest passive warnings
    if isinstance(diag.get('lastScan'), dict):
      warnings = (diag.get('passive') or {}).get('activeWarnings', [])
      diag['lastScan']['analysis'] = analyze_dtcs(diag['lastScan'].get('dtcs', []), warnings)

    return {
      'version': 1,
      'visible': visible,
      'parked': bool(getattr(ctx, 'parked', True)),
      'readable': True,
      'view': view_token,
      'vehicle': vinfo,
      'config': cfg,
      'ecuCatalog': [{'ecuId': e.ecu_id, 'name': e.name, 'txHex': f'0x{e.tx_addr:03X}', 'rxHex': f'0x{e.rx_addr:03X}'} for e in IONIQ_ECUS],
      'diagnostics': diag,
      'buttons': buttons,
      'buttonActions': list(BUTTON_ACTIONS),
      'fuel': fuel,
      'recordings': recorder_status,
    }

  def action(self, payload: Any, token: str, generation: bytes, *, session_valid: Callable[[], bool] | None = None) -> dict[str, Any]:
    if type(payload) is not dict:
      raise ValueError('Invalid Ioniq payload')
    view_token = payload.get('view')
    act = payload.get('action')
    if type(view_token) is not str or type(act) is not str or not view_token:
      raise ValueError('Missing view token or action')

    identity = self.identity(token, generation)
    now_mono = self.clock()
    with self.lock:
      saved_view = self.views.get(view_token)
      if saved_view is None or saved_view.identity != identity or saved_view.expires <= now_mono:
        raise IoniqToolsChanged('View expired or session changed')
      # Refresh expiry so multiple UI clicks within the active page work without extra round-trips
      self.views[view_token] = View(identity=identity, expires=now_mono + 300.0)

    if session_valid is not None and not session_valid():
      raise IoniqToolsChanged('Session invalidated')

    ctx = self.context.sample()
    parked = bool(getattr(ctx, 'parked', True))

    parked_only_actions = frozenset((
      'clear_dtcs', 'save_config', 'start_learn', 'cancel_learn',
      'delete_binding', 'update_binding', 'delete_recording', 'delete_trip',
    ))
    if act in parked_only_actions and not parked:
      raise IoniqToolsChanged('Car must be parked for this operation')

    if act == 'save_config':
      if payload.get('confirmed') is not True:
        raise ValueError('Confirmation required')
      updates = payload.get('config')
      if type(updates) is not dict:
        raise ValueError('Invalid config object')
      current = read_config(self.root)
      current.update(updates)
      saved = write_config(current, self.root)
      return {'saved': True, 'config': saved}

    if act == 'scan_dtcs':
      transport = self.direct_transport() if (callable(self.direct_transport) and parked) else None
      if transport is not None:
        can_send, can_recv = transport
        scan_res = scan_ioniq_ecus(can_send, can_recv, bus=1)
        state = apply_scan_result(scan_res, root=self.root)
        return {'saved': True, 'executedNow': True, 'diagnostics': state}
      state = queue_diagnostic_action('scan', 'ALL', root=self.root)
      return {'saved': True, 'queued': True, 'diagnostics': state}

    if act == 'clear_dtcs':
      if payload.get('confirmed') is not True:
        raise ValueError('Confirmation required')
      target = str(payload.get('target', 'ALL')).upper()
      transport = self.direct_transport() if callable(self.direct_transport) else None
      if transport is not None:
        can_send, can_recv = transport
        clear_res = clear_ioniq_dtcs(can_send, can_recv, target_ecu=target, bus=1)
        state = apply_clear_result(clear_res, root=self.root)
        return {'saved': True, 'executedNow': True, 'diagnostics': state}
      state = queue_diagnostic_action('clear', target, root=self.root)
      return {'saved': True, 'queued': True, 'diagnostics': state}

    if act == 'cancel_pending_diag':
      state = read_diagnostics_state(self.root)
      state['pendingAction'] = None
      write_diagnostics_state(state, self.root)
      return {'saved': True, 'diagnostics': state}

    if act == 'record_snapshot':
      title = str(payload.get('title', 'Manuelle Diagnose-Aufzeichnung')).strip()[:80] or 'Manuelle Diagnose-Aufzeichnung'
      immediate = bool(payload.get('immediate', True))
      push_runtime_command('record_snapshot', {'title': title, 'immediate': immediate}, root=self.root)
      if immediate:
        rec = EventFlightRecorder(self.root)
        can_snap = IoniqCanState()
        summary = rec.trigger_event('MANUAL', title, can_snap, mono_s=now_mono, details=read_diagnostics_state(self.root).get('liveSummary'), immediate=True)
        return {'saved': True, 'recording': summary}
      return {'saved': True, 'queued': True}

    if act == 'start_continuous_recording':
      label = str(payload.get('label', 'Vollaufzeichnung')).strip()[:80] or 'Vollaufzeichnung'
      push_runtime_command('start_continuous_recording', {'label': label}, root=self.root)
      return {'saved': True, 'queued': True}

    if act == 'stop_continuous_recording':
      push_runtime_command('stop_continuous_recording', {}, root=self.root)
      return {'saved': True, 'queued': True}

    if act == 'inspect_recording':
      rec_id = str(payload.get('id', ''))
      bundle = EventFlightRecorder(self.root).read_recording(rec_id)
      if bundle is None:
        raise ValueError('Recording not found')
      return {'ok': True, 'recording': bundle}

    if act == 'export_recording_csv':
      rec_id = str(payload.get('id', ''))
      csv_text = EventFlightRecorder(self.root).export_csv(rec_id)
      if csv_text is None:
        raise ValueError('Recording not found')
      return {'ok': True, 'filename': f'{rec_id}.csv', 'csv': csv_text}

    if act == 'delete_recording':
      if payload.get('confirmed') is not True:
        raise ValueError('Confirmation required')
      rec_id = str(payload.get('id', ''))
      deleted = EventFlightRecorder(self.root).delete_recording(rec_id)
      if not deleted:
        raise ValueError('Recording not found')
      return {'saved': True, 'deleted': rec_id}

    if act == 'start_learn':
      label = str(payload.get('label', '')).strip()[:48]
      action_id = str(payload.get('buttonAction', ''))
      if not label or action_id not in VALID_ACTION_IDS:
        raise ValueError('Button label and valid action are required')
      buttons = read_buttons_state(self.root)
      buttons['learnStatus'] = {
        'state': 'sampling_baseline',
        'label': label,
        'action': action_id,
        'detectedPresses': 0,
        'targetPresses': 3,
        'remainingS': 31.2,
        'message': f'Starte Anlernmodus für "{label}"...',
        'learnedBinding': None,
        'alternatives': [],
      }
      write_buttons_state(buttons, self.root)
      push_runtime_command('start_learn', {'label': label, 'action': action_id}, root=self.root)
      return {'saved': True, 'buttons': buttons}

    if act == 'cancel_learn':
      buttons = read_buttons_state(self.root)
      buttons['learnStatus'] = {'state': 'idle', 'detectedPresses': 0, 'targetPresses': 3, 'message': 'Anlernen abgebrochen.'}
      write_buttons_state(buttons, self.root)
      push_runtime_command('cancel_learn', {}, root=self.root)
      return {'saved': True, 'buttons': buttons}

    if act == 'update_binding':
      bid = str(payload.get('id', ''))
      buttons = read_buttons_state(self.root)
      found = False
      for row in buttons['bindings']:
        if row.get('id') == bid:
          if 'enabled' in payload:
            row['enabled'] = bool(payload['enabled'])
          if 'name' in payload and isinstance(payload['name'], str) and payload['name'].strip():
            row['name'] = payload['name'].strip()[:48]
          if 'buttonAction' in payload:
            if payload['buttonAction'] not in VALID_ACTION_IDS:
              raise ValueError('Invalid button action')
            row['action'] = payload['buttonAction']
          found = True
          break
      if not found:
        raise ValueError('Button binding not found')
      write_buttons_state(buttons, self.root)
      return {'saved': True, 'buttons': buttons}

    if act == 'delete_binding':
      if payload.get('confirmed') is not True:
        raise ValueError('Confirmation required')
      bid = str(payload.get('id', ''))
      buttons = read_buttons_state(self.root)
      before = len(buttons['bindings'])
      buttons['bindings'] = [b for b in buttons['bindings'] if b.get('id') != bid]
      if len(buttons['bindings']) == before:
        raise ValueError('Button binding not found')
      write_buttons_state(buttons, self.root)
      return {'saved': True, 'buttons': buttons}

    if act == 'log_refuel':
      liters = _parse_float(payload.get('liters'))
      full_tank = bool(payload.get('fullTank', True))
      price = _parse_optional_float(payload.get('pricePerLiter'))
      odometer = _parse_optional_float(payload.get('odometerKm'))
      notes = str(payload.get('notes', '')).strip()[:120]
      cfg = read_config(self.root)
      tracker = FuelTripTracker(self.root, fco_scale_ul=float(cfg.get('fcoScaleUlPerTick', 0.128)),
                              price_per_liter=float(cfg.get('fuelPriceEurPerLiter', 1.75)))
      res = tracker.log_refuel(liters, full_tank=full_tank, price_per_liter=price, odometer_km=odometer, notes=notes)
      # Sync updated calibration back to config.json cleanly
      cfg['fuelCalibrationFactor'] = res['calibrationFactor']
      if price is not None:
        cfg['fuelPriceEurPerLiter'] = price
      write_config(cfg, self.root)
      return {'saved': True, 'refuelResult': res, 'fuel': tracker.snapshot()}

    if act == 'dismiss_refuel_alert':
      tracker = FuelTripTracker(self.root)
      tracker.state['refuelAlert'] = None
      write_fuel_state(tracker.state, self.root)
      return {'saved': True, 'fuel': tracker.snapshot()}

    if act == 'start_manual_trip':
      label = str(payload.get('label', '')).strip()[:64]
      tracker = FuelTripTracker(self.root)
      tracker.start_manual_trip(label=label)
      return {'saved': True, 'fuel': tracker.snapshot()}

    if act == 'end_manual_trip':
      tracker = FuelTripTracker(self.root)
      archived = tracker.end_trip()
      return {'saved': True, 'archivedTrip': archived, 'fuel': tracker.snapshot()}

    if act == 'rename_trip':
      trip_id = str(payload.get('id', ''))
      label = str(payload.get('label', '')).strip()[:64]
      tracker = FuelTripTracker(self.root)
      found = False
      for row in tracker.state.get('trips', []):
        if row.get('id') == trip_id:
          row['label'] = label
          found = True
          break
      if not found:
        raise ValueError('Trip not found')
      write_fuel_state(tracker.state, self.root)
      return {'saved': True, 'fuel': tracker.snapshot()}

    if act == 'delete_trip':
      if payload.get('confirmed') is not True:
        raise ValueError('Confirmation required')
      trip_id = str(payload.get('id', ''))
      tracker = FuelTripTracker(self.root)
      trips = tracker.state.get('trips', [])
      new_trips = [t for t in trips if t.get('id') != trip_id]
      if len(new_trips) == len(trips):
        raise ValueError('Trip not found')
      tracker.state['trips'] = new_trips
      write_fuel_state(tracker.state, self.root)
      return {'saved': True, 'fuel': tracker.snapshot()}

    if act == 'export_trips_csv':
      csv_text = FuelTripTracker(self.root).export_csv()
      return {'ok': True, 'filename': 'ioniq-trips.csv', 'csv': csv_text}

    raise ValueError(f'Unknown action: {act}')


def _parse_float(value: Any) -> float:
  if isinstance(value, bool) or not isinstance(value, (int, float, str)):
    raise ValueError('Numeric value required')
  num = float(value)
  if num != num:
    raise ValueError('NaN not allowed')
  return num


def _parse_optional_float(value: Any) -> float | None:
  if value is None or value == '':
    return None
  return _parse_float(value)
