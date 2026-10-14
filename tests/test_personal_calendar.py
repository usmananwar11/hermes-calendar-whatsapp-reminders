import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from googleapiclient.errors import HttpError
import httplib2


def http_error(status):
    return HttpError(httplib2.Response({'status':str(status)}), b'{}')

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'personal_calendar.py'

def load():
    assert SCRIPT.exists(), 'Calendar CLI implementation is missing'
    spec = importlib.util.spec_from_file_location('personal_calendar', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class CalendarTests(unittest.TestCase):
    def test_create_body_and_idempotency(self):
        m = load()
        self.assertTrue(hasattr(m, 'build_create'), 'create builder missing')
        args = dict(summary='Call', start='2026-10-10T12:00:00+05:00', end='2026-10-10T12:30:00+05:00', request_id='job-1', attendees='a@example.com, b@example.com', meet=True, description='Agenda', location='Remote')
        body = m.build_create(**args)
        self.assertRegex(body['id'], r'^[0-9a-v]{5,1024}$')
        self.assertEqual(m.build_create(**args)['id'], body['id'])
        self.assertNotEqual(m.build_create(**dict(args, request_id='job-2'))['id'], body['id'])
        self.assertNotEqual(m.build_create(**args)['conferenceData']['createRequest']['requestId'], body['conferenceData']['createRequest']['requestId'])
        self.assertEqual(body['attendees'], [{'email':'a@example.com'}, {'email':'b@example.com'}])
        self.assertEqual(body['reminders'], {'useDefault':False, 'overrides':[{'method':'popup','minutes':15}]})
        self.assertEqual(body['description'], 'Agenda')
        self.assertEqual(m.build_create(**dict(args, event_id='abcde'))['id'], 'abcde')
        for change in [dict(request_id=''), dict(event_id='BAD-ID'), dict(end=args['start']), dict(attendees='bad'), dict(summary='')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                m.build_create(**dict(args, **change))

    def test_argument_safeguards(self):
        m = load()
        self.assertTrue(hasattr(m, 'parse_args'), 'CLI parser missing')
        create = ['create', '--summary', 'Call', '--start', '2026-10-10T12:00:00+05:00', '--end', '2026-10-10T12:30:00+05:00']
        for argv in [create, create + ['--request-id','x'], ['update','abcde','--summary','New'], ['delete','abcde'], ['list','--start','2026-10-10T12:00:00Z'], ['list','--start','bad','--end','bad'], ['update','abcde','--confirm']]:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                m.parse_args(argv)
        args = m.parse_args(create + ['--request-id','x','--confirm'])
        self.assertEqual(args.calendar, 'primary')
        self.assertEqual(m.parse_args(['get','abcde','--calendar','other']).calendar, 'other')
        self.assertEqual(m.parse_args(['status']).command, 'status')
        for argv in [create + ['--request-id','x','--confirm','--end','2026-10-10T11:00:00+05:00'], ['update','abcde','--confirm','--start','no-offset']]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                m.parse_args(argv)

    def test_read_commands_all_pages(self):
        m = load()
        self.assertTrue(hasattr(m, 'run'), 'command dispatcher missing')
        service = Mock()
        events = service.events.return_value
        events.list.return_value.execute.side_effect = [{'items':[{'id':'a'}], 'nextPageToken':'next'}, {'items':[{'id':'b'}]}]
        result = m.run(m.parse_args(['list']), service)
        self.assertEqual([x['id'] for x in result], ['a','b'])
        first = events.list.call_args_list[0].kwargs
        self.assertTrue(first['singleEvents'])
        self.assertEqual(first['orderBy'], 'startTime')
        self.assertEqual((m.parse_datetime(first['timeMax']) - m.parse_datetime(first['timeMin'])).days, 7)
        self.assertEqual(events.list.call_args_list[1].kwargs['pageToken'], 'next')
        self.assertTrue(all(c.kwargs['num_retries'] == 2 for c in events.list.return_value.execute.call_args_list))
        events.get.return_value.execute.return_value = {'id':'abcde'}
        self.assertEqual(m.run(m.parse_args(['get','abcde']), service)['id'], 'abcde')
        service.calendars.return_value.get.return_value.execute.return_value = {'id':'owner','timeZone':'Asia/Karachi'}
        self.assertEqual(m.run(m.parse_args(['status']), service)['calendar']['id'], 'owner')
        service.calendarList.return_value.list.return_value.execute.side_effect = [{'items':[{'id':'c'}], 'nextPageToken':'p'}, {'items':[{'id':'d'}]}]
        self.assertEqual(len(m.run(m.parse_args(['calendars']), service)), 2)
        events.list.return_value.execute.side_effect = None
        events.list.return_value.execute.return_value = {'items':[]}
        m.run(m.parse_args(['list','--start','2026-10-10T00:00:00Z','--end','2026-10-11T00:00:00Z']), service)
        self.assertEqual(events.list.call_args.kwargs['timeMin'], '2026-10-10T00:00:00Z')

    def test_confirmed_create_readback_retry_and_pending_meet(self):
        m = load()
        service = Mock()
        args = m.parse_args(['create','--summary','Call','--start','2026-10-10T12:00:00+05:00','--end','2026-10-10T12:30:00+05:00','--attendees','a@example.com','--meet','--request-id','job','--confirm'])
        expected = m.build_create(**{k:getattr(args,k) for k in ['summary','start','end','request_id','event_id','description','location','attendees','meet']})
        expected['conferenceData']['createRequest']['status'] = {'statusCode':'pending'}
        service.events.return_value.get.return_value.execute.side_effect = [http_error(404)] + [copy.deepcopy(expected)] * 6
        service.events.return_value.insert.return_value.execute.return_value = expected
        try:
            result = m.run(args, service, poll_delay=0)
        except (TypeError, ValueError) as exc:
            self.fail('Create dispatcher missing: ' + str(exc))
        self.assertTrue(result['verified'])
        self.assertEqual(result['meet']['status'], 'pending')
        self.assertEqual(service.events.return_value.insert.call_args.kwargs['sendUpdates'], 'all')
        self.assertEqual(service.events.return_value.insert.call_args.kwargs['conferenceDataVersion'], 1)
        service.events.return_value.insert.return_value.execute.assert_called_once_with(num_retries=0)
        service.events.return_value.insert.reset_mock()
        expected['conferenceData'] = {'entryPoints':[{'entryPointType':'video','uri':'https://meet.google.com/test'}], 'createRequest':{'status':{'statusCode':'success'}}}
        service.events.return_value.get.return_value.execute.side_effect = None
        service.events.return_value.get.return_value.execute.return_value = expected
        result = m.run(args, service, poll_delay=0)
        self.assertEqual(result['status'], 'already_exists')
        self.assertEqual(result['meet']['joinLink'], 'https://meet.google.com/test')
        service.events.return_value.insert.assert_not_called()
        expected['summary'] = 'Different'
        with self.assertRaises(ValueError):
            m.run(args, service, poll_delay=0)
        service.events.return_value.insert.assert_not_called()

    def test_update_preserves_unspecified_and_verifies(self):
        m = load()
        service = Mock()
        old = {'id':'abcde','summary':'Old','start':{'dateTime':'2026-10-10T12:00:00+05:00'}, 'end':{'dateTime':'2026-10-10T12:30:00+05:00'}, 'attendees':[{'email':'a@example.com','responseStatus':'accepted'}], 'location':'Room','description':'Keep','etag':'"v1"'}
        new = dict(old, summary='New')
        service.events.return_value.patch.return_value.headers = {}
        service.events.return_value.get.return_value.execute.side_effect = [copy.deepcopy(old), copy.deepcopy(new)]
        try:
            result = m.run(m.parse_args(['update','abcde','--summary','New','--confirm']), service)
        except ValueError as exc:
            self.fail('Update missing: ' + str(exc))
        self.assertTrue(result['verified'])
        kwargs = service.events.return_value.patch.call_args.kwargs
        self.assertEqual(kwargs['body'], {'summary':'New'})
        self.assertEqual(kwargs['sendUpdates'], 'all')
        self.assertEqual(kwargs['conferenceDataVersion'], 1)
        self.assertEqual(service.events.return_value.patch.return_value.headers['If-Match'], '"v1"')
        service.events.return_value.patch.return_value.execute.assert_called_once_with(num_retries=0)
        service.events.return_value.get.return_value.execute.side_effect = [old, dict(new, summary='Wrong')]
        with self.assertRaises(ValueError):
            m.run(m.parse_args(['update','abcde','--summary','New','--confirm']), service)
        service.events.return_value.patch.reset_mock()
        service.events.return_value.get.return_value.execute.side_effect = [old]
        with self.assertRaises(ValueError):
            m.run(m.parse_args(['update','abcde','--end','2026-10-10T11:00:00+05:00','--confirm']), service)
        service.events.return_value.patch.assert_not_called()

    def test_delete_verified_cancelled_or_missing(self):
        m = load()
        args = m.parse_args(['delete','abcde','--confirm'])
        for after in [{'id':'abcde','status':'cancelled'}, http_error(404), http_error(410)]:
            service = Mock()
            service.events.return_value.get.return_value.execute.side_effect = [{'id':'abcde','attendees':[{'email':'a@example.com'}]}, after]
            try:
                result = m.run(args, service)
            except ValueError as exc:
                self.fail('Delete missing: ' + str(exc))
            self.assertTrue(result['verified'])
            self.assertEqual(service.events.return_value.delete.call_args.kwargs['sendUpdates'], 'all')
            service.events.return_value.delete.return_value.execute.assert_called_once_with(num_retries=0)
        service = Mock()
        service.events.return_value.get.return_value.execute.return_value = {'id':'abcde'}
        with self.assertRaises(ValueError):
            m.run(args, service)
        service.events.return_value.get.return_value.execute.side_effect = [http_error(404)]
        service.events.return_value.delete.reset_mock()
        self.assertEqual(m.run(args, service)['status'], 'already_deleted')
        service.events.return_value.delete.assert_not_called()

    def test_auth_stored_scopes_timeout_secure_save(self):
        m = load()
        self.assertTrue(hasattr(m, 'connect'), 'SDK credential connection missing')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token.json'
            token = {'token':'test-only-access','refresh_token':'test-only-refresh','client_id':'test-only-client','client_secret':'test-only-secret','token_uri':'https://oauth2.googleapis.com/token','scopes':['https://www.googleapis.com/auth/calendar'], 'expiry':'2099-01-01T00:00:00Z'}
            path.write_text(json.dumps(token))
            with patch('googleapiclient.discovery.build') as build:
                m.connect(path)
                http = build.call_args.kwargs['http']
                self.assertEqual(http.http.timeout, 30)
                self.assertEqual(http.credentials.scopes, token['scopes'])
                self.assertEqual(http._max_refresh_attempts, 0)
            m.save_token(path, json.dumps(token))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), token)
            path.write_text(json.dumps(dict(token, scopes=[])))
            with self.assertRaises(ValueError):
                m.connect(path)

    def test_main_runpy_and_safe_error_output(self):
        m = load()
        self.assertTrue(hasattr(m, 'main'), 'main entrypoint missing')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('googleapiclient.discovery.build') as build:
            # Invalid args must exit before any credential/network access.
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                m.main(['delete','abcde'])
            build.assert_not_called()
        self.assertEqual(output.getvalue(), '')

    def test_runpy_success_does_not_exit_host(self):
        m = load()
        import runpy
        with tempfile.TemporaryDirectory() as directory:
            token = {'token':'test-access', 'refresh_token':'test-refresh', 'client_id':'test-client',
                     'client_secret':'test-secret', 'token_uri':'https://oauth2.googleapis.com/token',
                     'scopes':['https://www.googleapis.com/auth/calendar'], 'expiry':'2099-01-01T00:00:00Z'}
            (Path(directory)/'google_token.json').write_text(json.dumps(token))
            with patch.dict(os.environ, {'CALENDAR_REMINDERS_HOME':directory}), patch('sys.argv', ['personal_calendar.py', 'status']), patch('googleapiclient.discovery.build') as build, patch('httplib2.Http.request', side_effect=AssertionError('Network forbidden')), contextlib.redirect_stdout(io.StringIO()):
                build.return_value.calendars.return_value.get.return_value.execute.return_value = {'id':'test-calendar'}
                try:
                    runpy.run_path(str(SCRIPT), run_name='__main__')
                except SystemExit:
                    self.fail('Successful runpy should return without terminating managed timeit host')

    def test_refresh_uses_real_credentials_and_persists(self):
        m = load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token.json'
            token = {'token':'expired-test-access','refresh_token':'test-refresh','client_id':'test-client','client_secret':'test-secret','token_uri':'https://oauth2.googleapis.com/token','scopes':['https://www.googleapis.com/auth/calendar'], 'expiry':'2000-01-01T00:00:00Z'}
            path.write_text(json.dumps(token))
            response = httplib2.Response({'status':'200','content-type':'application/json'})
            payload = json.dumps({'access_token':'new-test-access','expires_in':3600,'token_type':'Bearer','scope':token['scopes'][0]}).encode()
            with patch('httplib2.Http.request', return_value=(response,payload)) as request, patch('googleapiclient.discovery.build'):
                m.connect(path)
            request.assert_called_once()
            saved = json.loads(path.read_text())
            self.assertEqual(saved['token'], 'new-test-access')
            self.assertEqual(saved['scopes'], token['scopes'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_network_failures_do_not_repeat_mutations(self):
        m = load()
        args = m.parse_args(['create','--summary','Call','--start','2026-10-10T12:00:00Z','--end','2026-10-10T13:00:00Z','--request-id','job','--confirm'])
        service = Mock()
        service.events.return_value.get.return_value.execute.side_effect = http_error(404)
        service.events.return_value.insert.return_value.execute.side_effect = TimeoutError('test')
        with self.assertRaises(TimeoutError):
            m.run(args, service)
        self.assertEqual(service.events.return_value.insert.call_args.kwargs['sendUpdates'], 'none')
        service.events.return_value.insert.return_value.execute.assert_called_once_with(num_retries=0)
        service.events.return_value.insert.reset_mock()
        service.events.return_value.get.return_value.execute.side_effect = http_error(403)
        with self.assertRaises(HttpError):
            m.run(args, service)
        service.events.return_value.insert.assert_not_called()

    def test_verification_compares_instants_and_attendee_sets(self):
        m = load()
        expected = {'summary':'Call','start':{'dateTime':'2026-10-10T12:00:00+05:00'},'end':{'dateTime':'2026-10-10T13:00:00+05:00'},'attendees':[{'email':'A@example.com'}]}
        actual = dict(expected, start={'dateTime':'2026-10-10T07:00:00Z'}, end={'dateTime':'2026-10-10T08:00:00Z'}, attendees=[{'email':'a@example.com','responseStatus':'accepted'}])
        m.verify_event(actual, expected)
        for changes in [dict(start={'dateTime':'2026-10-10T08:00:00Z'}), dict(end={'dateTime':'2026-10-10T09:00:00Z'}),dict(attendees=[]),dict(status='cancelled')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                m.verify_event(dict(actual, **changes), expected)

    def test_update_notifications_for_existing_new_removed_guests(self):
        m = load()
        for existing, supplied, send in [([],None,'none'),([], 'a@example.com','all'),([{'email':'a@example.com'}], '', 'all')]:
            old = {'id':'abcde','summary':'Old','start':{'dateTime':'2026-10-10T12:00:00Z'},'end':{'dateTime':'2026-10-10T13:00:00Z'},'attendees':existing}
            argv = ['update','abcde','--summary','New','--confirm']
            new = dict(old, summary='New')
            if supplied is not None:
                argv += ['--attendees', supplied]
                new['attendees'] = m.attendees_body(supplied)
            service = Mock()
            service.events.return_value.get.return_value.execute.side_effect = [old,new]
            m.run(m.parse_args(argv), service)
            self.assertEqual(service.events.return_value.patch.call_args.kwargs['sendUpdates'],send)

    def test_credential_error_is_not_printed(self):
        m = load()
        stderr = io.StringIO()
        with patch.object(m, 'connect', side_effect=ValueError('secret-test-token')), contextlib.redirect_stderr(stderr):
            self.assertEqual(m.main(['status']), 1)
        self.assertNotIn('secret-test-token', stderr.getvalue())

    def test_requested_meet_absent_is_reported_missing(self):
        m = load()
        service = Mock()
        event = {'id':'abcde','summary':'Call','start':{'dateTime':'2026-10-10T12:00:00Z'},'end':{'dateTime':'2026-10-10T13:00:00Z'}}
        expected = dict(event, conferenceData=m.conference_request())
        service.events.return_value.get.return_value.execute.return_value = event
        result = m.verified_result(service, 'primary', 'abcde', expected, 'created', 0)
        self.assertEqual(result['meet']['status'], 'missing')
        self.assertIsNone(result['meet']['joinLink'])

    def test_offset_datetime_validation(self):
        m = load()
        self.assertTrue(hasattr(m, 'parse_datetime'), 'datetime validator missing')
        self.assertEqual(m.parse_datetime('2026-10-10T12:00:00+05:00').hour, 12)
        self.assertEqual(m.parse_datetime('2026-10-10T07:00:00Z').utcoffset().total_seconds(), 0)
        for bad in ['2026-10-10', '2026-10-10T12:00:00', '2026-10-10 12:00:00+05:00', 'bad']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.parse_datetime(bad)
