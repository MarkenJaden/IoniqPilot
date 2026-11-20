"""Tests for IoniqPilot -> MarkenJaden Drive telemetry & route metadata sync."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from openpilot.starpilot.ioniq.drive_sync import (
  DriveSyncClient,
  SegmentTelemetryCollector,
  find_active_segment,
)
from openpilot.starpilot.ioniq.recorder import EventFlightRecorder
from openpilot.starpilot.ioniq.can_signals import IoniqCanState
from openpilot.starpilot.ioniq.storage import write_config


class _FakeResponse:
  def __init__(self, status_code: int = 200):
    self.status_code = status_code


class _FakeSession:
  def __init__(self, statuses: list[int] | None = None, *, raise_exc: Exception | None = None):
    self.calls: list[dict] = []
    self._statuses = list(statuses or [200])
    self._raise_exc = raise_exc

  def post(self, url: str, json=None, headers=None, timeout=None):
    self.calls.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
    if self._raise_exc is not None:
      raise self._raise_exc
    code = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
    return _FakeResponse(code)


class TestDriveSync(unittest.TestCase):
  def test_skips_comma_konik_and_offline_providers(self):
    with tempfile.TemporaryDirectory() as tmp:
      session = _FakeSession()
      for name in ('comma', 'konik', 'offline'):
        provider = SimpleNamespace(name=name, api='https://api.example.test')
        client = DriveSyncClient(
          root=tmp,
          session=session,
          provider_getter=lambda p=provider: p,
          dongle_id_getter=lambda: '0123456789abcdef',
          token_provider=lambda _d: 'jwt-token',
          uplink_check=lambda: False,
          build_metadata_getter=lambda: {'version': '0.11.3'},
        )
        self.assertFalse(client.sync_ioniq_state(force=True))
      self.assertEqual(session.calls, [])

  def test_syncs_ioniq_state_and_deduplicates_unchanged_snapshots(self):
    with tempfile.TemporaryDirectory() as tmp:
      write_config({'fuelPriceEurPerLiter': 1.82}, tmp)
      rec = EventFlightRecorder(tmp)
      rec.trigger_event('MANUAL', 'Test Snapshot', IoniqCanState(), mono_s=10.0, wall_time=1700000000.0, immediate=True)

      session = _FakeSession([200, 200])
      provider = SimpleNamespace(name='drive', api='https://drive.markenjaden.de')
      client = DriveSyncClient(
        root=tmp,
        session=session,
        provider_getter=lambda: provider,
        dongle_id_getter=lambda: '6b405d9d8b5399ed',
        token_provider=lambda _d: 'signed-jwt',
        uplink_check=lambda: False,
        build_metadata_getter=lambda: {'version': '0.11.3', 'branch': 'testing'},
      )

      loc = {'lat': 50.875, 'lng': 8.016, 'speed': 12.5, 'bearing': 90.0, 'time': 1700000000}
      self.assertTrue(client.sync_ioniq_state(location=loc))
      self.assertEqual(len(session.calls), 1)
      call = session.calls[0]
      self.assertEqual(call['url'], 'https://drive.markenjaden.de/v1/devices/6b405d9d8b5399ed/ioniq/sync')
      self.assertEqual(call['headers']['Authorization'], 'JWT signed-jwt')
      self.assertEqual(call['json']['location'], loc)
      self.assertEqual(len(call['json']['newRecordingBundles']), 1)

      # Unchanged state without force skips redundant POST
      self.assertFalse(client.sync_ioniq_state(location=loc))
      self.assertEqual(len(session.calls), 1)

      # Second forced sync does not re-upload already synced bundles
      self.assertTrue(client.sync_ioniq_state(location=loc, force=True))
      self.assertEqual(len(session.calls), 2)
      self.assertNotIn('newRecordingBundles', session.calls[1]['json'])

  def test_route_meta_queue_persists_offline_and_drains_on_recovery(self):
    with tempfile.TemporaryDirectory() as tmp:
      failing_session = _FakeSession(raise_exc=ConnectionError('offline'))
      provider = SimpleNamespace(name='custom', api='https://drive.markenjaden.de/')
      client = DriveSyncClient(
        root=tmp,
        session=failing_session,
        provider_getter=lambda: provider,
        dongle_id_getter=lambda: '6b405d9d8b5399ed',
        token_provider=lambda _d: 'signed-jwt',
        uplink_check=lambda: False,
      )
      client.enqueue_route_meta({
        'route_id': '00000001--abcdef0123',
        'segment_num': 0,
        'start_time_utc_millis': 1700000000000,
        'end_time_utc_millis': 1700000060000,
        'distance_meters': 850.0,
        'coords': [{'t': 0, 'lat': 50.875, 'lng': 8.016, 'speed': 10.0, 'dist': 0.0}],
        'events': [],
      })
      self.assertEqual(client.flush_route_meta_queue(), 0)
      self.assertTrue((Path(tmp) / 'drive_sync_queue.json').exists())

      ok_session = _FakeSession([200])
      client._session = ok_session
      self.assertEqual(client.flush_route_meta_queue(), 1)
      self.assertEqual(ok_session.calls[0]['url'], 'https://drive.markenjaden.de/v1/devices/6b405d9d8b5399ed/route_meta')
      self.assertEqual(client.flush_route_meta_queue(), 0)

  def test_segment_telemetry_collector_and_active_segment_lookup(self):
    with tempfile.TemporaryDirectory() as tmp:
      log_root = Path(tmp) / 'logs'
      seg0 = log_root / '00000002--0123456789--0'
      seg1 = log_root / '00000002--0123456789--1'
      seg0.mkdir(parents=True)
      seg1.mkdir(parents=True)
      (seg0 / 'rlog.lock').write_text('')

      self.assertEqual(find_active_segment(log_root), ('00000002--0123456789', 0))

      collector = SegmentTelemetryCollector(log_root=log_root)
      gps1 = SimpleNamespace(latitude=50.8750, longitude=8.0160, speed=15.0, bearingDeg=45.0, unixTimestampMillis=1700000000000)
      gps2 = SimpleNamespace(latitude=50.8755, longitude=8.0165, speed=15.0, bearingDeg=45.0, unixTimestampMillis=1700000002000)
      sd1 = SimpleNamespace(enabled=False, state='disabled', alertStatus='normal')
      sd2 = SimpleNamespace(enabled=True, state='enabled', alertStatus='normal')

      self.assertEqual(
        collector.observe(mono_s=100.0, wall_time=1700000000.0, gps_msg=gps1, selfdrive_msg=sd1, active_segment=('00000002--0123456789', 0)),
        [],
      )
      self.assertEqual(
        collector.observe(mono_s=102.0, wall_time=1700000002.0, gps_msg=gps2, selfdrive_msg=sd2, active_segment=('00000002--0123456789', 0)),
        [],
      )

      done = collector.observe(
        mono_s=160.0,
        wall_time=1700000060.0,
        gps_msg=gps2,
        selfdrive_msg=sd2,
        active_segment=('00000002--0123456789', 1),
        active_trip_summary={'distanceKm': 1.2, 'avgLPer100Km': 4.8},
      )
      self.assertEqual(len(done), 1)
      seg0_payload = done[0]
      self.assertEqual(seg0_payload['segment_num'], 0)
      self.assertGreater(seg0_payload['distance_meters'], 40.0)
      self.assertEqual([c['t'] for c in seg0_payload['coords']], [0, 2])
      self.assertEqual(len(seg0_payload['events']), 2)
      self.assertEqual(seg0_payload['events'][1]['data']['enabled'], True)
      self.assertEqual(seg0_payload['ioniq_trip_summary']['avgLPer100Km'], 4.8)


if __name__ == '__main__':
  unittest.main()
