import importlib.util
import tempfile
import unittest
from pathlib import Path

from openpilot.starpilot.speed_limits import acceptance as acc
from openpilot.starpilot.speed_limits import preferences as prefs
from openpilot.starpilot.speed_limits import selection as sel
from openpilot.starpilot.speed_limits.vision.eu_training import classes as eu_classes
from openpilot.starpilot.speed_limits.vision.eu_training.prepare_dataset import yolo_box_line


def _load_dashboard_speed_limit():
  path = Path(__file__).resolve().parents[4] / 'opendbc_repo' / 'opendbc' / 'car' / 'dashboard_speed_limit.py'
  spec = importlib.util.spec_from_file_location('dashboard_speed_limit', path)
  assert spec is not None and spec.loader is not None
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


_dash = _load_dashboard_speed_limit()
DashStatus = _dash.Status
hyundai_canfd_sign = _dash.hyundai_canfd_sign


class TestSlcPreferences(unittest.TestCase):
  def test_save_load_cache_and_clamp(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      loaded = prefs.load_preferences(root)
      self.assertEqual(loaded, prefs.SlcPreferences())
      self.assertTrue(loaded.cluster_effective_speed)
      self.assertTrue(loaded.step_override_enabled)
      self.assertEqual(loaded.engage_set_mode, 'limit')
      self.assertEqual(loaded.unlimited_display, 'zeichen282')

      updated = prefs.update_preferences(
        root,
        unlimited_recognition='motorway',
        unlimited_display='hide',
        stvo_fallback=True,
        cluster_effective_speed=False,
        step_override_enabled=False,
        advance_mode='dynamic',
        advance_distance_m=650,
        advance_decel_mps2=0.1,
        engage_set_mode='current',
        engage_resume_mode='current',
      )
      self.assertEqual(updated.unlimited_recognition, 'motorway')
      self.assertEqual(updated.unlimited_display, 'hide')
      self.assertTrue(updated.stvo_fallback)
      self.assertFalse(updated.cluster_effective_speed)
      self.assertFalse(updated.step_override_enabled)
      self.assertEqual(updated.advance_distance_m, prefs.ADVANCE_DISTANCE_M_MAX)
      self.assertEqual(updated.advance_decel_mps2, prefs.ADVANCE_DECEL_MIN)
      self.assertEqual(updated.engage_set_mode, 'current')
      self.assertEqual(updated.engage_resume_mode, 'current')
      self.assertEqual(prefs.load_preferences_cached(root, min_check_interval_s=10.0), updated)


class TestUnlimitedBehavior(unittest.TestCase):
  def test_hyundai_canfd_253_is_unlimited(self):
    status, speed = hyundai_canfd_sign(253, 0, True)
    self.assertIs(status, DashStatus.UNLIMITED)
    self.assertEqual(speed, 0.0)
    status_absent, _ = hyundai_canfd_sign(0, 0, True)
    self.assertIs(status_absent, DashStatus.ABSENT)

  def test_acceptance_clears_previous_limit_and_pending_on_unlimited(self):
    authority = acc.Authority(acc.Mode.LONGITUDINAL_ONLY, acc.LongitudinalOwner.SYSTEM, False, True, False, False)
    policy = acc.Policy(confirm_lower=False, confirm_higher=False)
    cand = acc.Candidate('map', acc.ObservationIdentity(acc.IdentityKind.GEOGRAPHIC, value='way-1'), 120 / 3.6)
    s0 = acc.new_session('drive')
    d1 = acc.step(s0, acc.Observation(acc.ObservationKind.VALID, cand), authority, policy, now_ns=1_000_000_000)
    self.assertAlmostEqual(d1.control_target_mps, 120 / 3.6)

    d2 = acc.step(d1.state, acc.Observation(acc.ObservationKind.UNLIMITED), authority, policy, now_ns=2_000_000_000)
    self.assertEqual(d2.basis, 'unlimited')
    self.assertIsNone(d2.control_target_mps)
    self.assertIsNone(d2.state.accepted)
    self.assertIsNone(d2.state.pending)
    self.assertIsNone(d2.state.received_speed_mps)

  def test_selection_unlimited_vs_valid(self):
    policy = sel.SelectionPolicy(sel.SelectionMode.ORDERED, (sel.Source.VISION, sel.Source.MAP, None), False)
    obs = {
      sel.Source.DASHBOARD: acc.Observation(acc.ObservationKind.UNKNOWN),
      sel.Source.MAP: acc.Observation(acc.ObservationKind.UNLIMITED),
      sel.Source.VISION: acc.Observation(acc.ObservationKind.UNKNOWN),
      sel.Source.ONLINE: acc.Observation(acc.ObservationKind.UNKNOWN),
    }
    chosen = sel.select_limit(obs, policy)
    self.assertIs(chosen.observation.kind, acc.ObservationKind.UNLIMITED)

    # A posted sign on Dashboard/Vision overrides Unlimited
    obs[sel.Source.VISION] = acc.Observation(
      acc.ObservationKind.VALID,
      acc.Candidate('vision', acc.ObservationIdentity(acc.IdentityKind.GEOGRAPHIC, value='sign-80'), 80 / 3.6),
    )
    chosen_sign = sel.select_limit(obs, policy)
    self.assertIs(chosen_sign.observation.kind, acc.ObservationKind.VALID)
    self.assertAlmostEqual(chosen_sign.observation.candidate.speed_mps, 80 / 3.6)


class TestEuVisionTrainingScaffold(unittest.TestCase):
  def test_german_sign_taxonomy_and_dataset_mappings(self):
    self.assertEqual(len(eu_classes.EU_SIGN_CLASSES), 18)
    c130 = eu_classes.resolve_mtsd_label('regulatory--maximum-speed-limit-130--g1')
    self.assertIsNotNone(c130)
    self.assertEqual(c130.speed_kph, 130)
    self.assertFalse(c130.unlimited)

    c282 = eu_classes.resolve_gtsrb_class(32)
    self.assertIsNotNone(c282)
    self.assertTrue(c282.unlimited)
    self.assertIsNone(c282.speed_kph)

    box = yolo_box_line(0, 100, 50, 200, 150, 1000, 500)
    self.assertEqual(box, '0 0.150000 0.200000 0.100000 0.200000')
