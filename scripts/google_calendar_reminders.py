"""Calendar-to-WhatsApp reminder watcher, without an LLM."""
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone


def runtime_home():
    """Resolve on each call, respecting an isolated home or active profile."""
    value = os.environ.get('CALENDAR_REMINDERS_HOME') or os.environ.get('HERMES_HOME')
    return Path(value).expanduser() if value else Path.home()/'.local/share/hermes-calendar-reminders'


def write_private_json(path, payload):
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def send_message(text, target, hermes, runner=subprocess.run):
    result = runner([hermes, 'send', '--to', target, '--json', '--', text],
                    capture_output=True, text=True, timeout=100)
    if result.returncode:
        raise RuntimeError('WhatsApp reminder send command failed; consult gateway logs')
    try:
        receipt = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('WhatsApp reminder send returned no structured receipt') from None
    if (not isinstance(receipt, dict) or receipt.get('success') is not True
                or not isinstance(receipt.get('message_id'), str) or not receipt['message_id'].strip()):
        raise RuntimeError('WhatsApp reminder send returned no successful message receipt')
    return receipt


def event_start(event):
    value = (event.get('start') or {}).get('dateTime')
    if not value:
        return None
    try:
        start = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return start if start.utcoffset() is not None else None


def due_events(events, now, lead_minutes=15):
    due = []
    for event in events:
        start = event_start(event)
        declined = any(a.get('self') and a.get('responseStatus') == 'declined'
                       for a in event.get('attendees', []))
        if (start is not None and now < start <= now + timedelta(minutes=lead_minutes)
                and event.get('status') != 'cancelled' and not declined):
            due.append(event)
    return due


def reminder_key(event):
    return event['id'] + '|' + event_start(event).astimezone(timezone.utc).isoformat()


def reminder_text(event, now, zone='UTC'):
    import math
    from zoneinfo import ZoneInfo
    start = event_start(event)
    minutes = max(1, math.ceil((start-now).total_seconds()/60))
    local = start.astimezone(ZoneInfo(zone))
    clock = local.strftime('%I:%M %p').lstrip('0')
    title = event.get('summary') or 'Untitled event'
    message = f'Reminder: {title} starts in {minutes} minute' + ('s' if minutes != 1 else '')
    message += f' — {local.strftime("%B %d, %Y")} at {clock} ({zone}).'
    link = event.get('hangoutLink')
    if not link:
        link = next((entry.get('uri') for entry in event.get('conferenceData', {}).get('entryPoints', [])
                     if entry.get('entryPointType') == 'video'), None)
    if link:
        message += '\nJoin: ' + link
    if event.get('location'):
        message += '\nLocation: ' + event['location']
    return message


def deliver_due(events, now, state, sender, persist=None, lead_minutes=15, zone='UTC', clock=None):
    sent = 0
    for event in due_events(events, now, lead_minutes):
        key = reminder_key(event)
        if key in state:
            continue
        delivery_now = clock() if clock else now
        if not due_events([event], delivery_now, lead_minutes):
            continue
        receipt = sender(reminder_text(event, delivery_now, zone))
        if (not isinstance(receipt, dict) or receipt.get('success') is not True
                or not isinstance(receipt.get('message_id'), str) or not receipt['message_id'].strip()):
            raise RuntimeError('WhatsApp did not acknowledge reminder delivery; reminder remains pending')
        state[key] = {'delivered_at':delivery_now.isoformat(), 'message_id':receipt['message_id']}
        if persist:
            persist(state)
        sent += 1
    return sent


def list_upcoming(service, calendar_id, now, lead_minutes=15):
    events = []
    token = None
    while True:
        params = {'calendarId':calendar_id, 'timeMin':now.isoformat(),
                  'timeMax':(now+timedelta(minutes=lead_minutes)).isoformat(),
                  'singleEvents':True, 'showDeleted':False, 'orderBy':'startTime', 'maxResults':2500}
        if token:
            params['pageToken'] = token
        page = service.events().list(**params).execute(num_retries=2)
        events.extend(page.get('items', []))
        token = page.get('nextPageToken')
        if not token:
            return events


def refresh_credentials(credentials):
    """Retry only transient refresh failures with bounded, fresh connections."""
    import time
    import httplib2
    from google_auth_httplib2 import Request
    from google.auth.exceptions import TransportError, RefreshError
    for attempt in range(3):
        try:
            credentials.refresh(Request(httplib2.Http(timeout=10)))
            return
        except RefreshError as exc:
            if not getattr(exc, 'retryable', False):
                raise
            if attempt == 2:
                raise RuntimeError('Google token refresh temporarily unavailable; scheduler will retry') from None
        except (TransportError, OSError):
            if attempt == 2:
                raise RuntimeError('Google token refresh connection failed after bounded retries; scheduler will retry') from None
        time.sleep(attempt+1)


def calendar_service(home):
    import httplib2
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    token = home/'google_token.json'
    credentials = Credentials.from_authorized_user_file(str(token))
    if not credentials.has_scopes(['https://www.googleapis.com/auth/calendar']):
        raise RuntimeError('Calendar authorization is missing the required Calendar scope')
    if credentials.expired:
        refresh_credentials(credentials)
        payload = json.loads(credentials.to_json())
        payload['type'] = 'authorized_user'
        write_private_json(token, payload)
    return build('calendar', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def validate_settings(settings):
    """Reject misrouting and invalid configuration before opening credentials."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    import re
    if not isinstance(settings, dict):
        raise ValueError('Settings must be an object')
    lead = settings.get('lead_minutes')
    if type(lead) is not int or not 1 <= lead <= 1440:
        raise ValueError('Reminder lead time must be an integer between 1 and 1440 minutes')
    for key in ('calendar_id', 'hermes', 'timezone'):
        value = settings.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Required nonempty setting: ' + key)
    target = settings.get('target')
    if not isinstance(target, str) or not re.fullmatch(r'(?:whatsapp|whatsapp_agent_platform):[^\s:]+(?::[^\s:]+)*', target):
        raise ValueError('Target must be an explicit Hermes WhatsApp destination')
    try:
        ZoneInfo(settings['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Timezone must be a valid IANA timezone') from None


def main(argv=None):
    import argparse
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Read live Calendar without sending or updating reminder state')
    parser.add_argument('--home', help='Runtime data directory')
    parser.add_argument('--settings', help='Settings JSON path, overriding default/legacy lookup')
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else runtime_home()
    settings_path = Path(args.settings).expanduser() if args.settings else home/'calendar_reminder_settings.json'
    if not args.settings and not settings_path.exists():
        settings_path = home/'scripts'/'calendar_reminder_settings.json'
    settings = json.loads(settings_path.read_text())
    validate_settings(settings)
    lead = settings['lead_minutes']
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home/'calendar_reminder_state.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = datetime.now(timezone.utc)
        state_path = home/'calendar_reminder_state.json'
        document = json.loads(state_path.read_text()) if state_path.exists() else {'version':1, 'sent':{}}
        state = document['sent']
        routing = {'calendar_id':settings['calendar_id'], 'target':settings['target']}
        if state and 'routing' not in document:
            raise ValueError('Legacy reminder state requires an explicitly verified routing binding; see README')
        if document.get('routing', routing) != routing:
            raise ValueError('Reminder state belongs to another calendar or destination; use a separate runtime home')
        document['routing'] = routing
        service = calendar_service(home)
        events = list_upcoming(service, settings['calendar_id'], now, lead)
        now = datetime.now(timezone.utc)
        due = due_events(events, now, lead)
        if args.dry_run:
            print(json.dumps({'dry_run':True, 'scanned':len(events), 'due':len(due),
                              'pending':sum(reminder_key(e) not in state for e in due),
                              'timezone':settings['timezone'], 'target':settings['target']}))
            return
        def persist(updated):
            document['sent'] = updated
            document['last_scan'] = now.isoformat()
            write_private_json(state_path, document)
        count = deliver_due(events, now, state,
            lambda text:send_message(text, settings['target'], settings['hermes']),
            persist=persist, lead_minutes=lead, zone=settings['timezone'],
            clock=lambda:datetime.now(timezone.utc))
        cutoff = now-timedelta(days=30)
        document['sent'] = {key:value for key,value in state.items()
                            if datetime.fromisoformat(value['delivered_at']) > cutoff}
        document['last_scan'] = now.isoformat()
        write_private_json(state_path, document)
        print(json.dumps({'status':'ok', 'scanned':len(events), 'reminders_sent':count, 'last_scan':now.isoformat()}))


def entrypoint(argv=None):
    """Keep failures honest, but never deliver a raw traceback to WhatsApp."""
    import sys
    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError
    try:
        main(argv)
        return 0
    except RefreshError:
        message = 'Google Calendar authorization could not be renewed. Ask your agent to check or reconnect Calendar.'
    except (TransportError, OSError):
        message = 'Google Calendar connection temporarily failed. Reminder scan did not complete; the next scheduled run will retry.'
    except HttpError as exc:
        if exc.resp.status in (401, 403):
            message = 'Google Calendar access was refused. Ask your agent to check Calendar authorization and permissions.'
        else:
            message = 'Google Calendar API is temporarily unavailable. Reminder scan did not complete; the next scheduled run will retry.'
    except RuntimeError:
        message = 'Google Calendar reminder check or delivery failed. No successful delivery was assumed; the next scheduled run will retry.'
    except Exception:
        message = 'Calendar reminders could not run. Ask your agent to inspect the local reminder configuration.'
    print(message, file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(entrypoint())

