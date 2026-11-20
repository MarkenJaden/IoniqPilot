from openpilot.starpilot.speed_limits import preferences as slc_prefs
from openpilot.starpilot.speed_limits.runtime_settings import (
  ADVANCE_BYTES, ADVANCE_KEYS, ADVANCE_SECONDS, CONFIRMATION_BYTES, CONFIRMATION_CHOICES,
  PEDAL_OVERRIDE_BYTES, PEDAL_OVERRIDE_CHOICES, PEDAL_OVERRIDE_KEY, PedalOverride,
)
from openpilot.starpilot.ui.feature_settings_state import FeatureRow

SLC_ADVANCE_CHOICES = ('Off', *(f'{seconds} s' for seconds in ADVANCE_SECONDS[1:]))
SLC_PREF_PREFIX = 'slc_pref:'

_UNLIM_LABELS = {
  slc_prefs.UNLIMITED_OFF: 'Off',
  slc_prefs.UNLIMITED_MOTORWAY: 'Autobahn (Map)',
  slc_prefs.UNLIMITED_COMBINED: 'Map + Dashboard',
}
_UNLIM_DISPLAY_LABELS = {
  slc_prefs.UNLIMITED_DISPLAY_SIGN: 'Zeichen 282',
  slc_prefs.UNLIMITED_DISPLAY_HIDE: 'Hide sign',
}
_ADVANCE_MODE_LABELS = {
  slc_prefs.ADVANCE_DYNAMIC: 'Dynamic braking curve',
  slc_prefs.ADVANCE_TIME: 'Time ahead',
  slc_prefs.ADVANCE_DISTANCE: 'Fixed distance',
}
_SET_MODE_LABELS = {
  slc_prefs.ENGAGE_SET_LIMIT: 'Speed limit',
  slc_prefs.ENGAGE_SET_CURRENT: 'Current speed',
}
_RESUME_MODE_LABELS = {
  slc_prefs.ENGAGE_RESUME_LAST_OR_CURRENT: 'Last SET or current',
  slc_prefs.ENGAGE_RESUME_CURRENT: 'Current speed',
  slc_prefs.ENGAGE_RESUME_LAST_SET: 'Last SET',
}
_ADV_DISTANCES = (0, 50, 100, 150, 200, 250, 300, 400, 500)
_ADV_DECELS = (0.8, 1.0, 1.2, 1.5, 2.0, 2.5)


def preference_rows(*, allowed: bool, preferences_allowed: bool) -> list[FeatureRow]:
  prefs = slc_prefs.load_preferences()
  dist_choices = tuple('Off' if d == 0 else f'{d} m' for d in _ADV_DISTANCES)
  dist_val = 'Off' if prefs.advance_distance_m == 0 else f'{prefs.advance_distance_m} m'
  if dist_val not in dist_choices:
    dist_choices = (dist_val, *dist_choices)
  decel_choices = tuple(f'{d:.1f} m/s²' for d in _ADV_DECELS)
  decel_val = f'{prefs.advance_decel_mps2:.1f} m/s²'
  if decel_val not in decel_choices:
    decel_choices = (decel_val, *decel_choices)
  return [
    FeatureRow(
      f'{SLC_PREF_PREFIX}cluster_effective_speed', 'Cluster shows active limit',
      'On' if prefs.cluster_effective_speed else 'Off', b'1' if prefs.cluster_effective_speed else b'0',
      ('Off', 'On'), available=allowed, default_value='On',
      reason='Show the active SLC-limited or overridden speed (e.g. 50 / 100 / 120) on the car dashboard while keeping your max SET speed',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}step_override_enabled', '+/- steps from active limit',
      'On' if prefs.step_override_enabled else 'Off', b'1' if prefs.step_override_enabled else b'0',
      ('Off', 'On'), available=allowed, default_value='On',
      reason='When SLC caps below SET, + and − step directly from the active speed limit until the next limit or Cancel + SET',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}unlimited_recognition', 'Unlimited recognition',
      _UNLIM_LABELS[prefs.unlimited_recognition], prefs.unlimited_recognition.encode(),
      tuple(_UNLIM_LABELS.values()), available=preferences_allowed, default_value='Map + Dashboard',
      reason='Clear stale speed limits and return to SET when entering unlimited Autobahn sections',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}unlimited_display', 'Unlimited sign display',
      _UNLIM_DISPLAY_LABELS[prefs.unlimited_display], prefs.unlimited_display.encode(),
      tuple(_UNLIM_DISPLAY_LABELS.values()), available=preferences_allowed, default_value='Zeichen 282',
      reason='Show the German Zeichen 282 sign or hide the sign on unlimited Autobahn sections',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}stvo_fallback', 'German StVO fallback',
      'On' if prefs.stvo_fallback else 'Off', b'1' if prefs.stvo_fallback else b'0',
      ('Off', 'On'), available=preferences_allowed, default_value='Off',
      reason='Assume 50 km/h urban, 100 km/h rural, and unlimited dual-carriageway trunk when OSM has no speed tag',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}advance_mode', 'Early adoption mode',
      _ADVANCE_MODE_LABELS[prefs.advance_mode], prefs.advance_mode.encode(),
      tuple(_ADVANCE_MODE_LABELS.values()), available=allowed, default_value='Dynamic braking curve',
      reason='Dynamic braking curve starts slowing down so target speed is reached at the sign',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}advance_distance_m', 'Early adoption distance',
      dist_val, str(prefs.advance_distance_m).encode(),
      dist_choices, available=allowed, default_value='Off',
      reason='Fixed distance threshold or extra buffer added before upcoming speed limits',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}advance_decel_mps2', 'Braking curve comfort',
      decel_val, str(prefs.advance_decel_mps2).encode(),
      decel_choices, available=allowed, default_value='1.2 m/s²',
      reason='Target deceleration used by the dynamic braking curve before lower speed limits',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}engage_set_mode', 'SET button target',
      _SET_MODE_LABELS[prefs.engage_set_mode], prefs.engage_set_mode.encode(),
      tuple(_SET_MODE_LABELS.values()), available=allowed, default_value='Speed limit',
      reason='Speed used when engaging with SET (or after Cancel + SET): active speed limit or current driving speed',
    ),
    FeatureRow(
      f'{SLC_PREF_PREFIX}engage_resume_mode', 'RESUME button target',
      _RESUME_MODE_LABELS[prefs.engage_resume_mode], prefs.engage_resume_mode.encode(),
      tuple(_RESUME_MODE_LABELS.values()), available=allowed, default_value='Last SET or current',
      reason='Speed used when engaging with RESUME: last SET if available, else current speed',
    ),
  ]


def apply_preference_request(key: str, value: str) -> bool:
  field_name = key.removeprefix(SLC_PREF_PREFIX)
  if field_name in ('cluster_effective_speed', 'step_override_enabled', 'stvo_fallback'):
    if value not in ('On', 'Off'):
      return False
    mapped = value == 'On'
  elif field_name == 'unlimited_recognition':
    mapped = {v: k for k, v in _UNLIM_LABELS.items()}.get(value)
  elif field_name == 'unlimited_display':
    mapped = {v: k for k, v in _UNLIM_DISPLAY_LABELS.items()}.get(value)
  elif field_name == 'advance_mode':
    mapped = {v: k for k, v in _ADVANCE_MODE_LABELS.items()}.get(value)
  elif field_name == 'advance_distance_m':
    if value == 'Off':
      mapped = 0
    else:
      try:
        mapped = int(value.split()[0])
      except (ValueError, IndexError):
        return False
  elif field_name == 'advance_decel_mps2':
    try:
      mapped = float(value.split()[0])
    except (ValueError, IndexError):
      return False
  elif field_name == 'engage_set_mode':
    mapped = {v: k for k, v in _SET_MODE_LABELS.items()}.get(value)
  elif field_name == 'engage_resume_mode':
    mapped = {v: k for k, v in _RESUME_MODE_LABELS.items()}.get(value)
  else:
    return False
  if mapped is None:
    return False
  slc_prefs.update_preferences(**{field_name: mapped})
  return True


def policy_rows(read_value, readable, *, allowed):
  rows = []
  _, raw_confirmation, readable_confirmation = read_value('SLCConfirmation')
  valid_confirmation = readable_confirmation and raw_confirmation in (None, *CONFIRMATION_BYTES)
  confirmation = CONFIRMATION_CHOICES[int(raw_confirmation or b'0')] if valid_confirmation else 'Invalid'
  rows.append(FeatureRow('SLCConfirmation', 'Confirm limits', confirmation, raw_confirmation,
    tuple(CONFIRMATION_CHOICES.values()), available=allowed and readable('SLCConfirmation'), default_value='Off',
    repair_value='' if valid_confirmation else 'Both',
    reason='Off adopts limits automatically. For prompts, + accepts and − declines within 30 seconds'
      if valid_confirmation else 'Invalid saved confirmation mode; choose a confirmation policy'))
  for key, direction in zip(ADVANCE_KEYS, ('higher', 'lower'), strict=True):
    advance, raw_advance, readable_advance = read_value(key)
    valid_advance = readable_advance and (raw_advance is None or raw_advance in ADVANCE_BYTES)
    rows.append(FeatureRow(key, f'Adopt {direction} limits early',
      SLC_ADVANCE_CHOICES[int(advance)] if valid_advance else f'Saved {advance}', raw_advance,
      SLC_ADVANCE_CHOICES if valid_advance else ('Off',), available=allowed, default_value='Off',
      reason=f'Offer or adopt {direction} mapped limits this many seconds early at the current speed'
        if valid_advance else 'Invalid saved advance time; restore Off'))
  _, raw_override, readable_override = read_value(PEDAL_OVERRIDE_KEY)
  resume_limit = PEDAL_OVERRIDE_CHOICES[PedalOverride.OFF]
  default_override = PEDAL_OVERRIDE_CHOICES[PedalOverride.SPEED_REACHED]
  valid_override = readable_override and (raw_override is None or raw_override in PEDAL_OVERRIDE_BYTES)
  rows.append(FeatureRow(PEDAL_OVERRIDE_KEY, 'After an accelerator override',
    default_override if raw_override is None else PEDAL_OVERRIDE_CHOICES[int(raw_override)] if valid_override else 'Invalid',
    raw_override, PEDAL_OVERRIDE_CHOICES if valid_override else (resume_limit,),
    available=allowed and readable(PEDAL_OVERRIDE_KEY), default_value=default_override,
    repair_value='' if valid_override else resume_limit,
    reason='Cruise uses this target when you lift off the accelerator. Held speed stays at or below SET.'))
  return rows
