"""Personal Calendar CLI. Mutations require explicit confirmation."""
from datetime import datetime, timedelta, timezone
import re
import hashlib
import uuid
import argparse
import time
import json
import os
from pathlib import Path
import tempfile
import sys
from googleapiclient.errors import HttpError

def runtime_home():
    """Resolve on each call, respecting an isolated home or active profile."""
    value = os.environ.get('CALENDAR_REMINDERS_HOME') or os.environ.get('HERMES_HOME')
    return Path(value).expanduser() if value else Path.home()/'.local/share/hermes-calendar-reminders'


def save_token(path, text):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.calendar-token-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def connect(token_path=None):
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp, Request
    from googleapiclient.discovery import build
    import httplib2
    path = Path(token_path).expanduser() if token_path is not None else runtime_home()/'google_token.json'
    with path.open() as stream:
        info = json.load(stream)
    scopes = info.get('scopes')
    if not isinstance(scopes, list) or not scopes or not all(isinstance(x, str) for x in scopes):
        raise ValueError('Saved token must contain granted scopes; reauthorize without exposing credentials')
    credentials = Credentials.from_authorized_user_info(info, scopes=scopes)
    transport = httplib2.Http(timeout=30)
    if not credentials.valid:
        if not credentials.refresh_token:
            raise ValueError('Saved authorization cannot refresh; reauthorize')
        credentials.refresh(Request(transport))
        saved = json.loads(credentials.to_json())
        saved['scopes'] = list(credentials.granted_scopes or scopes)
        saved['type'] = 'authorized_user'
        save_token(path, json.dumps(saved))
    else:
        os.chmod(path, 0o600)
    # Refresh before requests, never transparently replay a mutation after 401.
    http = AuthorizedHttp(credentials, http=transport, max_refresh_attempts=0)
    return build('calendar', 'v3', http=http, cache_discovery=False, static_discovery=True)


def main(argv=None):
    args = parse_args(argv)
    try:
        token = Path(args.token).expanduser() if args.token else (Path(args.home).expanduser() if args.home else runtime_home())/'google_token.json'
        service = connect(token)
    except Exception as exc:
        print(json.dumps({'error': 'Calendar authorization/connection failed', 'type': type(exc).__name__}), file=sys.stderr)
        return 1
    try:
        result = run(args, service)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HttpError as exc:
        print(json.dumps({'error': 'Calendar API request failed', 'httpStatus': exc.resp.status,
                          'hint': 'Mutation not retried automatically; use get before retrying. Reuse create request ID.'}), file=sys.stderr)
    except ValueError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
    except Exception as exc:
        # OAuth/transport exception strings may contain credentials. Do not echo them.
        print(json.dumps({'error': 'Calendar operation failed', 'type': type(exc).__name__,
                          'hint': 'Check authorization/network. Mutation may have succeeded; read target before retrying.'}), file=sys.stderr)
    return 1


def verify_event(actual, expected):
    if actual.get('status') == 'cancelled':
        raise ValueError('Target is cancelled')
    for field in ['summary', 'start', 'end', 'attendees', 'description', 'location']:
        if field not in expected:
            continue
        left, right = actual.get(field), expected[field]
        if field in ['start', 'end']:
            if right.get('dateTime'):
                same = bool(left and left.get('dateTime')) and parse_datetime(left['dateTime']) == parse_datetime(right['dateTime'])
            else:
                same = left == right
        elif field == 'attendees':
            same = {x['email'].lower() for x in left or []} == {x['email'].lower() for x in right or []}
        else:
            same = (left or '') == (right or '')
        if not same:
            raise ValueError('Readback mismatch for ' + field + '; no mutation retry performed')


def verified_result(service, calendar, identifier, expected, status, poll_delay):
    actual = get_event(service, calendar, identifier)
    verify_event(actual, expected)
    for _ in range(4):
        code = actual.get('conferenceData', {}).get('createRequest', {}).get('status', {}).get('statusCode')
        if code != 'pending':
            break
        time.sleep(poll_delay)
        actual = get_event(service, calendar, identifier)
        verify_event(actual, expected)
    data = actual.get('conferenceData', {})
    code = data.get('createRequest', {}).get('status', {}).get('statusCode')
    link = next((p.get('uri') for p in data.get('entryPoints', []) if p.get('entryPointType') == 'video'), actual.get('hangoutLink'))
    return {'status': status, 'verified': True, 'event': actual,
            'meet': {'status': code or ('success' if link else 'missing' if expected.get('conferenceData') else 'not_requested'), 'joinLink': link}}


def all_pages(method, **kwargs):
    items = []
    while True:
        page = method(**kwargs).execute(num_retries=2)
        items.extend(page.get('items', []))
        token = page.get('nextPageToken')
        if not token:
            return items
        kwargs['pageToken'] = token


def get_event(service, calendar, identifier):
    return service.events().get(calendarId=calendar, eventId=identifier).execute(num_retries=2)


def run(args, service, poll_delay=1):
    if args.command in ['create', 'update', 'delete'] and not args.confirm:
        raise ValueError('Mutations require --confirm')
    if args.command == 'create':
        body = build_create(**{key: getattr(args, key) for key in ['summary','start','end','request_id','event_id','description','location','attendees','meet']})
        existing = None
        try:
            existing = get_event(service, args.calendar, body['id'])
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
        if existing is not None:
            if existing.get('extendedProperties', {}).get('private', {}).get('hermesRequestId') != args.request_id:
                raise ValueError('Event ID conflict: request ID differs')
            verify_event(existing, body)
            status = 'already_exists'
        else:
            service.events().insert(calendarId=args.calendar, body=body,
                                    sendUpdates='all' if body['attendees'] else 'none',
                                    conferenceDataVersion=1).execute(num_retries=0)
            status = 'created'
        return verified_result(service, args.calendar, body['id'], body, status, poll_delay)
    if args.command == 'update':
        old = get_event(service, args.calendar, args.event_id)
        verify_event(old, {})
        patch = {}
        for field in ['summary', 'description', 'location']:
            value = getattr(args, field)
            if value is not None:
                patch[field] = value
        for field in ['start', 'end']:
            value = getattr(args, field)
            if value is not None:
                parse_datetime(value)
                patch[field] = {'dateTime': value}
                if old.get(field, {}).get('date'):
                    patch[field]['date'] = None
        if args.attendees is not None:
            patch['attendees'] = attendees_body(args.attendees)
        if args.meet:
            patch['conferenceData'] = conference_request()
        expected = dict(old, **patch)
        if args.start is not None or args.end is not None:
            if not expected['start'].get('dateTime') or not expected['end'].get('dateTime'):
                raise ValueError('Converting all-day events requires both --start and --end')
            if parse_datetime(expected['end']['dateTime']) <= parse_datetime(expected['start']['dateTime']):
                raise ValueError('End must be after start')
        request = service.events().patch(calendarId=args.calendar, eventId=args.event_id,
                                        body=patch, conferenceDataVersion=1,
                                        sendUpdates='all' if old.get('attendees') or patch.get('attendees') else 'none')
        if old.get('etag'):
            request.headers['If-Match'] = old['etag']
        request.execute(num_retries=0)
        return verified_result(service, args.calendar, args.event_id, expected, 'updated', poll_delay)
    if args.command == 'delete':
        try:
            old = get_event(service, args.calendar, args.event_id)
        except HttpError as exc:
            if exc.resp.status in [404, 410]:
                return {'status': 'already_deleted', 'id': args.event_id, 'verified': True}
            raise
        if old.get('status') == 'cancelled':
            return {'status': 'already_deleted', 'id': args.event_id, 'verified': True}
        request = service.events().delete(calendarId=args.calendar, eventId=args.event_id,
                                          sendUpdates='all' if old.get('attendees') else 'none')
        if old.get('etag'):
            request.headers['If-Match'] = old['etag']
        request.execute(num_retries=0)
        try:
            after = get_event(service, args.calendar, args.event_id)
            if after.get('status') != 'cancelled':
                raise ValueError('Deletion readback still active; no mutation retry performed')
        except HttpError as exc:
            if exc.resp.status not in [404, 410]:
                raise
        return {'status': 'deleted', 'id': args.event_id, 'verified': True}
    if args.command == 'status':
        return {'status': 'connected', 'calendar': service.calendars().get(calendarId=args.calendar).execute(num_retries=2)}
    if args.command == 'calendars':
        return all_pages(service.calendarList().list)
    if args.command == 'get':
        return get_event(service, args.calendar, args.event_id)
    if args.command == 'list':
        start = args.start or datetime.now(timezone.utc).isoformat()
        end = args.end or (parse_datetime(start) + timedelta(days=7)).isoformat()
        return all_pages(service.events().list, calendarId=args.calendar,
                         timeMin=start, timeMax=end, singleEvents=True,
                         orderBy='startTime', maxResults=2500)
    raise ValueError('Unsupported command')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='Runtime data directory (before subcommand)')
    parser.add_argument('--token', help='Explicit token path (before subcommand)')
    subs = parser.add_subparsers(dest='command', required=True)
    for command in ['list', 'get', 'create', 'update', 'delete', 'calendars', 'status']:
        sub = subs.add_parser(command)
        sub.add_argument('--calendar', default='primary')
        if command in ['get', 'update', 'delete']:
            sub.add_argument('event_id')
        if command in ['create', 'update', 'delete']:
            sub.add_argument('--confirm', action='store_true', help='Explicitly authorize this mutation and guest notifications')
        if command in ['list', 'create', 'update']:
            sub.add_argument('--start', required=command == 'create')
            sub.add_argument('--end', required=command == 'create')
        if command in ['create', 'update']:
            for field in ['summary', 'description', 'location', 'attendees']:
                sub.add_argument('--' + field, required=command == 'create' and field == 'summary')
            sub.add_argument('--meet', action='store_true')
        if command == 'create':
            sub.add_argument('--request-id', required=True)
            sub.add_argument('--event-id')
    args = parser.parse_args(argv)
    try:
        if args.command in ['create', 'update', 'delete'] and not args.confirm:
            raise ValueError('Mutations require --confirm after user approval')
        if args.command == 'create':
            build_create(**{key: getattr(args, key) for key in ['summary','start','end','request_id','event_id','description','location','attendees','meet']})
        if args.command in ['list', 'update']:
            for field in ['start', 'end']:
                if getattr(args, field):
                    parse_datetime(getattr(args, field))
            if args.start and args.end and parse_datetime(args.end) <= parse_datetime(args.start):
                raise ValueError('End must be after start')
        if args.command == 'list' and bool(args.start) != bool(args.end):
            raise ValueError('Explicit list range requires both --start and --end')
        if args.command == 'update':
            if not any(getattr(args, f) is not None for f in ['summary','start','end','description','location','attendees']) and not args.meet:
                raise ValueError('No update fields supplied')
            if args.summary is not None and not args.summary.strip():
                raise ValueError('Summary must not be empty')
            if args.attendees is not None:
                attendees_body(args.attendees)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def attendees_body(value):
    result = []
    for email in value.split(',') if value else []:
        email = email.strip()
        if not re.fullmatch(r'[^\s@,]+@[^\s@,]+\.[^\s@,]+', email):
            raise ValueError('Attendees must be comma-separated email addresses')
        if email.lower() not in {x['email'].lower() for x in result}:
            result.append({'email': email})
    return result


def conference_request():
    return {'createRequest': {'requestId': uuid.uuid4().hex,
                             'conferenceSolutionKey': {'type': 'hangoutsMeet'}}}


def build_create(*, summary, start, end, request_id, event_id=None,
                 description=None, location=None, attendees=None, meet=False):
    if not request_id or not request_id.strip():
        raise ValueError('create requires a nonempty --request-id; reuse it on retries')
    if not summary or not summary.strip():
        raise ValueError('Summary must not be empty')
    if parse_datetime(end) <= parse_datetime(start):
        raise ValueError('End must be after start')
    identifier = event_id if event_id is not None else hashlib.sha256(request_id.encode()).hexdigest()
    if not re.fullmatch(r'[0-9a-v]{5,1024}', identifier):
        raise ValueError('Event ID must be 5–1024 lowercase base32hex characters (0-9, a-v)')
    body = {'id': identifier, 'summary': summary, 'start': {'dateTime': start},
            'end': {'dateTime': end}, 'attendees': attendees_body(attendees),
            'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 15}]},
            'extendedProperties': {'private': {'hermesRequestId': request_id}}}
    for key, value in [('description', description), ('location', location)]:
        if value is not None:
            body[key] = value
    if meet:
        body['conferenceData'] = conference_request()
    return body


def parse_datetime(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value):
        raise ValueError('Datetime must be ISO/RFC3339 with seconds and explicit offset or Z')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Datetime requires an offset')
    return result


if __name__ == '__main__':
    exit_code = main()
    if exit_code:
        raise SystemExit(exit_code)
