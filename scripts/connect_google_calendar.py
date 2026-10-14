"""Authorize only Google Calendar via a local browser callback; no codes in chat."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

SCOPES = ['https://www.googleapis.com/auth/calendar']


def runtime_home():
    value = os.environ.get('CALENDAR_REMINDERS_HOME') or os.environ.get('HERMES_HOME')
    return Path(value).expanduser() if value else Path.home()/'.local/share/hermes-calendar-reminders'


def save_token(path, payload):
    """Atomically replace the token, with owner-only permissions from creation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.calendar-token-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authorize(args):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    home = Path(args.home).expanduser() if args.home else runtime_home()
    client = Path(args.client_secret).expanduser() if args.client_secret else home/'google_client_secret.json'
    target = Path(args.token).expanduser() if args.token else home/'google_token.json'
    flow = InstalledAppFlow.from_client_secrets_file(str(client), scopes=SCOPES)
    print('Opening Google authorization in your browser. Approve Calendar access.', flush=True)
    credentials = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True,
        timeout_seconds=600, access_type='offline', prompt='consent',
        authorization_prompt_message='Authorization page opened. Waiting for approval.',
        success_message='Google Calendar authorization received. You can close this tab and return to Hermes.')
    if credentials is None or not credentials.refresh_token or not credentials.has_scopes(SCOPES):
        raise RuntimeError('Durable Calendar authorization was not granted; no token saved')
    service = build('calendar', 'v3', credentials=credentials, cache_discovery=False)
    calendar = service.calendars().get(calendarId=args.calendar).execute(num_retries=2)
    if not calendar.get('id'):
        raise RuntimeError('Calendar readback has no identifier; no token saved')
    payload = json.loads(credentials.to_json())
    payload['type'] = 'authorized_user'
    payload['scopes'] = list(credentials.granted_scopes or SCOPES)
    # Verify serialized credentials against the exact returned Calendar ID before saving.
    saved = Credentials.from_authorized_user_info(payload, scopes=payload['scopes'])
    verified = build('calendar', 'v3', credentials=saved, cache_discovery=False).calendars().get(calendarId=calendar['id']).execute(num_retries=2)
    if verified.get('id') != calendar['id']:
        raise RuntimeError('Calendar readback mismatch; no token saved')
    save_token(target, payload)
    print(json.dumps({'status':'CALENDAR_CONNECTED', 'calendar_id':verified['id'],
                      'timezone':verified.get('timeZone'), 'token_path':str(target)}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='Runtime data directory')
    parser.add_argument('--client-secret', help='Desktop OAuth client JSON path')
    parser.add_argument('--token', help='Output token path')
    parser.add_argument('--calendar', default='primary', help='Calendar to verify')
    args = parser.parse_args(argv)
    if not args.calendar.strip():
        parser.error('Calendar must not be empty')
    try:
        authorize(args)
        return 0
    except Exception as exc:
        print(json.dumps({'error':'Calendar authorization or verification failed',
                          'type':type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
