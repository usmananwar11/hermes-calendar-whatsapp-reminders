"""Portable configuration regressions; never use live credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from offline_safety import OfflineTestCase

class PortableTests(OfflineTestCase, unittest.TestCase):
    def test_runtime_home_precedence_and_dynamic_token_path(self):
        for name in ('personal_calendar', 'google_calendar_reminders'):
            module = load(name)
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
                with patch('pathlib.Path.home', return_value=Path(folder)):
                    self.assertEqual(module.runtime_home(), Path(folder)/'.local/share/hermes-calendar-reminders')
                os.environ['HERMES_HOME'] = folder+'/profile'
                self.assertEqual(module.runtime_home(), Path(folder)/'profile')
                os.environ['CALENDAR_REMINDERS_HOME'] = folder+'/dedicated'
                self.assertEqual(module.runtime_home(), Path(folder)/'dedicated')
        module = load('personal_calendar')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CALENDAR_REMINDERS_HOME':folder}), patch('google.oauth2.credentials.Credentials.from_authorized_user_info') as credentials, patch('googleapiclient.discovery.build'):
            (Path(folder)/'google_token.json').write_text(json.dumps({'scopes':['https://www.googleapis.com/auth/calendar']}))
            credentials.return_value.valid = True
            module.connect()
            credentials.assert_called_once()

    def test_calendar_cli_explicit_home_and_token_overrides(self):
        module = load('personal_calendar')
        with patch.object(module, 'connect') as connect, contextlib.redirect_stdout(io.StringIO()):
            connect.return_value.calendars.return_value.get.return_value.execute.return_value = {'id':'example'}
            self.assertEqual(module.main(['--home','/example/home','status']), 0)
            connect.assert_called_with(Path('/example/home/google_token.json'))
            self.assertEqual(module.main(['--home','/example/home','--token','/example/override.json','status']), 0)
            connect.assert_called_with(Path('/example/override.json'))

    def test_watcher_settings_home_override_and_invalid_config_before_auth(self):
        module = load('google_calendar_reminders')
        valid = {'calendar_id':'primary', 'lead_minutes':15, 'timezone':'UTC',
                 'target':'whatsapp_agent_platform:user:example-recipient', 'hermes':'hermes'}
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)/'runtime'
            settings = Path(folder)/'custom.json'
            service = Mock()
            service.events.return_value.list.return_value.execute.return_value = {'items':[]}
            settings.write_text(json.dumps(valid))
            with patch.object(module, 'calendar_service', return_value=service) as auth, patch.object(module, 'send_message') as send, contextlib.redirect_stdout(io.StringIO()):
                module.main(['--home',str(home),'--settings',str(settings),'--dry-run'])
                auth.assert_called_once_with(home)
                send.assert_not_called()
                self.assertFalse((home/'calendar_reminder_state.json').exists())
            for changes in ({'target':''}, {'target':'telegram:user:example'}, {'target':'whatsapp_agent_platform:user:'}, {'timezone':'Invalid/Zone'}, {'lead_minutes':0}, {'lead_minutes':1441}, {'lead_minutes':True}, {'lead_minutes':1.5}, {'calendar_id':''}, {'hermes':''}):
                settings.write_text(json.dumps(dict(valid, **changes)))
                with self.subTest(changes=changes), patch.object(module, 'calendar_service') as auth, patch.object(module, 'send_message') as send:
                    with self.assertRaises(ValueError):
                        module.main(['--home',str(home),'--settings',str(settings)])
                    auth.assert_not_called()
                    send.assert_not_called()

    def test_oauth_import_is_guarded(self):
        with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file', side_effect=AssertionError('OAuth must not run during import')) as flow, patch('googleapiclient.discovery.build') as build:
            module = load('connect_google_calendar')
            flow.assert_not_called()
            build.assert_not_called()
            self.assertTrue(callable(module.main))

    def test_oauth_private_save_and_exact_calendar_readback(self):
        module = load('connect_google_calendar')
        credentials = Mock(refresh_token='fake-refresh', granted_scopes=module.SCOPES)
        credentials.to_json.return_value = json.dumps({'token':'fake-access','refresh_token':'fake-refresh'})
        service, readback = Mock(), Mock()
        service.calendars.return_value.get.return_value.execute.return_value = {'id':'example-calendar'}
        readback.calendars.return_value.get.return_value.execute.return_value = {'id':'example-calendar','timeZone':'UTC'}
        with tempfile.TemporaryDirectory() as folder, patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file') as flow, patch('google.oauth2.credentials.Credentials.from_authorized_user_info'), patch('googleapiclient.discovery.build', side_effect=[service,readback]), contextlib.redirect_stdout(io.StringIO()):
            flow.return_value.run_local_server.return_value = credentials
            self.assertEqual(module.main(['--home',folder,'--calendar','example-calendar']), 0)
            flow.assert_called_once_with(str(Path(folder)/'google_client_secret.json'), scopes=module.SCOPES)
            readback.calendars.return_value.get.assert_called_once_with(calendarId='example-calendar')
            path = Path(folder)/'google_token.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['scopes'], module.SCOPES)
            self.assertEqual(list(Path(folder).glob('.calendar-token-*')), [])

    def test_oauth_failure_does_not_replace_existing_token_or_echo_secret(self):
        module = load('connect_google_calendar')
        for mismatch in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder)/'google_token.json'
                target.write_text('fake-original')
                credentials = Mock(refresh_token='fake-refresh', granted_scopes=module.SCOPES)
                credentials.has_scopes.return_value = True
                credentials.to_json.return_value = json.dumps({'token':'fake-access'})
                before, after = Mock(), Mock()
                before.calendars.return_value.get.return_value.execute.return_value = {'id':'example-calendar'}
                after.calendars.return_value.get.return_value.execute.return_value = {'id':'wrong-calendar'}
                error = io.StringIO()
                with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file') as flow, patch('google.oauth2.credentials.Credentials.from_authorized_user_info'), patch('googleapiclient.discovery.build', side_effect=[before,after]), contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                    if mismatch:
                        flow.return_value.run_local_server.return_value = credentials
                    else:
                        flow.return_value.run_local_server.side_effect = RuntimeError('fake-secret-detail')
                    self.assertEqual(module.main(['--home',folder]), 1)
                self.assertEqual(target.read_text(), 'fake-original')
                self.assertNotIn('fake-secret-detail', error.getvalue())

    def test_unconfigured_reminder_formatting_defaults_to_utc(self):
        from datetime import datetime, timedelta, timezone
        module = load('google_calendar_reminders')
        now = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
        event = {'id':'example', 'start':{'dateTime':(now+timedelta(minutes=15)).isoformat()}}
        self.assertIn('10:15 AM (UTC)', module.reminder_text(event, now))

    def test_receipt_requires_boolean_success_and_nonempty_string_id(self):
        import subprocess
        from datetime import datetime, timedelta, timezone
        module = load('google_calendar_reminders')
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        event = {'id':'example', 'start':{'dateTime':(now+timedelta(minutes=1)).isoformat()}}
        receipts = [{'success':'false','message_id':'fake-id'}, {'success':True,'message_id':42},
                    {'success':True,'message_id':' '}, [], {'success':False,'message_id':'fake-id'}]
        for receipt in receipts:
            state = {}
            runner = Mock(return_value=subprocess.CompletedProcess([],0,json.dumps(receipt),''))
            with self.subTest(receipt=receipt):
                with self.assertRaises(RuntimeError):
                    module.send_message('example', 'whatsapp:example-recipient', 'hermes', runner=runner)
                with self.assertRaises(RuntimeError):
                    module.deliver_due([event], now, state, lambda _:receipt)
                self.assertEqual(state, {})


if __name__ == '__main__':
    unittest.main()
