import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'google_calendar_reminders.py'

def event(event_id, start, **extra):
    return {'id':event_id, 'summary':'Call', 'start':{'dateTime':start.isoformat()}, **extra}

from offline_safety import OfflineTestCase

class ReminderTests(OfflineTestCase, unittest.TestCase):
    def load_module(self):
        self.assertTrue(SCRIPT.exists(), 'Calendar reminder implementation does not exist yet')
        spec = importlib.util.spec_from_file_location('google_calendar_reminders', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_only_upcoming_timed_non_declined_events_are_due(self):
        m = self.load_module()
        now = datetime(2026,10,10,10,0,tzinfo=timezone.utc)
        events = [event('due', now+timedelta(minutes=15)),
                  event('soon', now+timedelta(seconds=15)),
                  event('future', now+timedelta(minutes=16)),
                  event('past', now-timedelta(seconds=1)),
                  event('cancelled', now+timedelta(minutes=5), status='cancelled'),
                  event('declined', now+timedelta(minutes=5), attendees=[{'self':True,'responseStatus':'declined'}]),
                  {'id':'all_day','start':{'date':'2026-10-10'}}]
        self.assertEqual([e['id'] for e in m.due_events(events, now)], ['due','soon'])

    def test_delivery_is_deduplicated_only_after_success(self):
        m = self.load_module()
        self.assertTrue(hasattr(m, 'deliver_due'), 'Durable reminder delivery is missing')
        now = datetime(2026,10,10,10,0,tzinfo=timezone.utc)
        e = event('due', now+timedelta(minutes=15), hangoutLink='https://meet.google.com/test-only')
        state = {}
        sent = []
        def sender(text):
            sent.append(text)
            return {'success':True, 'message_id':'unit-test-receipt'}
        self.assertEqual(m.deliver_due([e], now, state, sender, zone='Asia/Karachi'), 1)
        self.assertEqual(m.deliver_due([e], now+timedelta(minutes=1), state, sender), 0)
        self.assertEqual(len(sent), 1)
        self.assertIn('15 minutes', sent[0])
        self.assertIn('3:15 PM', sent[0])
        self.assertIn('Asia/Karachi', sent[0])
        self.assertIn('https://meet.google.com/test-only', sent[0])
        moved = event('due', now+timedelta(minutes=14))
        self.assertEqual(m.deliver_due([moved], now, state, sender), 1)
        failed_state = {}
        with self.assertRaises(RuntimeError):
            m.deliver_due([e], now, failed_state, lambda _: {'success':False})
        self.assertEqual(failed_state, {})

    def test_calendar_scan_reads_all_pages_with_time_bounds(self):
        from unittest.mock import Mock
        m = self.load_module()
        self.assertTrue(hasattr(m, 'list_upcoming'), 'Live paginated calendar scan is missing')
        now = datetime(2026,10,10,10,0,tzinfo=timezone.utc)
        service = Mock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {'items':[{'id':'one'}], 'nextPageToken':'page2'}, {'items':[{'id':'two'}]}]
        self.assertEqual(m.list_upcoming(service, 'primary', now, 15), [{'id':'one'},{'id':'two'}])
        first = service.events.return_value.list.call_args_list[0].kwargs
        second = service.events.return_value.list.call_args_list[1].kwargs
        self.assertEqual(first['timeMin'], '2026-10-10T10:00:00+00:00')
        self.assertEqual(first['timeMax'], '2026-10-10T10:15:00+00:00')
        self.assertTrue(first['singleEvents'])
        self.assertFalse(first['showDeleted'])
        self.assertEqual(second['pageToken'], 'page2')

    def test_private_state_and_verified_transport_receipt(self):
        import json, subprocess, tempfile
        from unittest.mock import Mock
        m = self.load_module()
        self.assertTrue(hasattr(m, 'write_private_json'), 'Atomic reminder state storage is missing')
        self.assertTrue(hasattr(m, 'send_message'), 'WhatsApp delivery wrapper is missing')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            m.write_private_json(path, {'sent':{}})
            self.assertEqual(json.loads(path.read_text()), {'sent':{}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        runner = Mock(return_value=subprocess.CompletedProcess([],0,'{"success":true,"message_id":"test-receipt"}',''))
        result = m.send_message('Reminder text', 'whatsapp_agent_platform:user:example-recipient', '/path/hermes', runner=runner)
        self.assertEqual(result['message_id'], 'test-receipt')
        self.assertIn('--json', runner.call_args.args[0])
        self.assertIn('whatsapp_agent_platform:user:example-recipient', runner.call_args.args[0])
        runner.return_value = subprocess.CompletedProcess([],0,'{"success":false}','')
        with self.assertRaises(RuntimeError):
            m.send_message('Reminder text', 'target', '/path/hermes', runner=runner)

    def test_dry_run_scans_calendar_without_sending_or_marking_delivered(self):
        import io, json, os, tempfile
        from contextlib import redirect_stdout
        from unittest.mock import Mock, patch
        m = self.load_module()
        self.assertTrue(hasattr(m, 'main'), 'Scheduled reminder entry point is missing')
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home/'scripts').mkdir()
            (home/'scripts'/'calendar_reminder_settings.json').write_text(json.dumps({
                'calendar_id':'primary', 'lead_minutes':15, 'timezone':'Asia/Karachi',
                'target':'whatsapp_agent_platform:user:example-recipient', 'hermes':'/path/hermes'}))
            service = Mock()
            service.events.return_value.list.return_value.execute.return_value = {'items':[]}
            output = io.StringIO()
            with patch.dict(os.environ, {'HERMES_HOME':folder, 'CALENDAR_REMINDERS_HOME':folder}), patch.object(m, 'calendar_service', return_value=service), patch.object(m, 'send_message') as send, redirect_stdout(output):
                m.main(['--dry-run'])
            self.assertFalse(send.called)
            self.assertFalse((home/'calendar_reminder_state.json').exists())
            self.assertTrue(json.loads(output.getvalue())['dry_run'])

    def test_refresh_retries_connection_reset_with_bounded_transport(self):
        import json, tempfile
        from unittest.mock import Mock, patch
        from google.auth.exceptions import TransportError
        m = self.load_module()
        credentials = Mock()
        credentials.expired = True
        credentials.refresh.side_effect = [TransportError('unit-test connection reset'), None]
        credentials.to_json.return_value = json.dumps({'token':'unit-test-token', 'scopes':['https://www.googleapis.com/auth/calendar']})
        with tempfile.TemporaryDirectory() as folder, patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=credentials), patch('google_auth_httplib2.Request') as request, patch('httplib2.Http') as transport, patch('googleapiclient.discovery.build'), patch('time.sleep'):
            home = Path(folder)
            m.calendar_service(home)
            self.assertEqual(credentials.refresh.call_count, 2)
            self.assertTrue((home/'google_token.json').exists())
            self.assertGreaterEqual(request.call_count, 2)
            self.assertTrue(all(0 < c.kwargs['timeout'] <= 30 for c in transport.call_args_list))
            self.assertTrue(any(c.kwargs['timeout'] == 10 for c in transport.call_args_list))

    def test_scheduler_failure_is_concise_and_does_not_expose_tracebacks(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch
        from google.auth.exceptions import TransportError
        m = self.load_module()
        self.assertTrue(hasattr(m, 'entrypoint'), 'Scheduler error sanitization is missing')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(m, 'main', side_effect=TransportError('unit-test-sensitive-detail')), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(m.entrypoint([]), 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertNotIn('unit-test-sensitive-detail', stderr.getvalue())
        self.assertIn('retry', stderr.getvalue().lower())

    def test_permanent_auth_failure_does_not_retry_or_overwrite_credentials(self):
        import tempfile
        from unittest.mock import Mock, patch
        from google.auth.exceptions import RefreshError
        m = self.load_module()
        credentials = Mock(expired=True)
        credentials.refresh.side_effect = RefreshError('unit-test invalid_grant', retryable=False)
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            token = home/'google_token.json'
            token.write_text('unit-test-original')
            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file', return_value=credentials), patch('google_auth_httplib2.Request'), patch('httplib2.Http'), patch('time.sleep') as sleep:
                with self.assertRaises(RefreshError):
                    m.calendar_service(home)
                self.assertEqual(credentials.refresh.call_count, 1)
                self.assertFalse(sleep.called)
                self.assertEqual(token.read_text(), 'unit-test-original')

    def test_transient_refresh_failure_has_finite_retries(self):
        from unittest.mock import Mock, patch
        from google.auth.exceptions import TransportError
        m = self.load_module()
        credentials = Mock()
        credentials.refresh.side_effect = TransportError('unit-test network outage')
        with patch('google_auth_httplib2.Request'), patch('httplib2.Http'), patch('time.sleep') as sleep:
            with self.assertRaises(RuntimeError):
                m.refresh_credentials(credentials)
        self.assertEqual(credentials.refresh.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

if __name__ == '__main__':
    unittest.main()
