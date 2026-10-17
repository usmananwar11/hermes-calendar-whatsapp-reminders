"""Operator CLI for the native Calendar WhatsApp reminders plugin.

Import and registration are inert. Account access, writes and scheduling only
happen in explicitly invoked commands. Existing standalone scripts remain usable.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from types import SimpleNamespace

from .scripts import connect_google_calendar as oauth_helper
from .scripts import google_calendar_reminders as watcher

PLUGIN_NAME = 'calendar-whatsapp-reminders'
SETTINGS = 'calendar_reminder_settings.json'
STATE = 'calendar_reminder_state.json'
MIGRATION_FILES = (SETTINGS, 'google_client_secret.json', 'google_token.json', STATE)


class _CalendarCLI:
    @staticmethod
    def main(argv):
        # Keep registration and local status usable without importing Google SDKs.
        from .scripts import personal_calendar
        return personal_calendar.main(argv)


calendar_cli = _CalendarCLI()


def profile_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).expanduser().resolve()


def resolve_home(value=None):
    home = Path(value).expanduser().resolve() if value else profile_home()/'plugin-data'/PLUGIN_NAME
    install = Path(__file__).resolve().parent
    if home == install or install in home.parents:
        raise ValueError('Runtime storage must be outside the plugin installation directory')
    return home


def register_cli(parser):
    parser.add_argument('--home', help='Explicit runtime directory (before the subcommand)')
    subs = parser.add_subparsers(dest='calendar_reminders_action', required=True)
    setup = subs.add_parser('setup', help='Configure the approved route; does not connect or send')
    setup.add_argument('--calendar')
    setup.add_argument('--target')
    setup.add_argument('--timezone')
    setup.add_argument('--lead-minutes', type=int)
    setup.add_argument('--hermes', help='Hermes executable name or absolute path')
    setup.add_argument('--replace', action='store_true', help='Explicitly replace existing settings')
    connect = subs.add_parser('connect', help='Authorize Google Calendar in a local browser')
    connect.add_argument('--client-secret', help='Path to a downloaded Desktop OAuth client JSON')
    status = subs.add_parser('status', help='Show local setup state without contacting Google')
    status.add_argument('--live', action='store_true', help='Also verify live Google Calendar access')
    scan = subs.add_parser('scan', help='Run one reminder scan')
    scan.add_argument('--dry-run', action='store_true')
    scan.add_argument('--confirm', action='store_true', help='Approve real sends for all due events')
    for name, text in [('schedule','Enable a scan every minute'), ('unschedule','Remove this runtime home\'s schedule')]:
        child = subs.add_parser(name, help=text)
        child.add_argument('--confirm', action='store_true', help='Explicitly approve this change')
    migrate = subs.add_parser('migrate', help='Copy an explicitly selected legacy runtime home')
    migrate.add_argument('--from-home', required=True)
    migrate.add_argument('--confirm', action='store_true')
    migrate.add_argument('--bind-legacy-route', action='store_true',
                         help='Attest that legacy history belongs to the source settings/account')
    calendar = subs.add_parser('calendar', help='Pass through to the confirmed-mutation Calendar CLI')
    calendar.add_argument('calendar_args', nargs=argparse.REMAINDER)


def _print(payload):
    print(json.dumps(payload, indent=2))


def _settings(home):
    settings = json.loads((home/SETTINGS).read_text())
    watcher.validate_settings(settings)
    return settings


def _routing(settings):
    return {key:settings[key] for key in ('calendar_id', 'target')}


def _check_state(document, settings, bind_legacy=False):
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('sent'), dict):
        raise ValueError('Invalid delivery state; do not discard or overwrite history')
    routing = _routing(settings)
    if document['sent'] and 'routing' not in document and not bind_legacy:
        raise ValueError('Legacy history requires --bind-legacy-route after verifying its original route and Google account')
    if document.get('routing', routing) != routing:
        raise ValueError('Delivery history belongs to a different calendar or target; use a separate runtime home')
    for receipt in document['sent'].values():
        if not isinstance(receipt, dict) or not isinstance(receipt.get('message_id'), str):
            raise ValueError('Invalid delivery receipt in history')
        instant = watcher.datetime.fromisoformat(receipt['delivered_at'])
        if instant.utcoffset() is None:
            raise ValueError('Delivery history timestamps require an offset')
    return dict(document, routing=routing)


@contextmanager
def _locked_home(home):
    import fcntl
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    fd = os.open(home/'calendar_reminder_state.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A reminder operation is running; stop it before changing setup') from None
        yield


def _answer(value, label, default=None):
    if value is not None:
        return value
    if sys.stdin.isatty():
        suffix = ' ['+str(default)+']' if default is not None else ''
        result = input(label+suffix+': ').strip()
        if result:
            return result
    if default is not None:
        return default
    raise ValueError('Supply an explicit '+label+' using setup flags or an interactive terminal')


def setup(args, home):
    path = home/SETTINGS
    if path.exists() and not args.replace:
        raise ValueError('Settings already exist; review them and use --replace deliberately')
    old = _settings(home) if path.exists() else {}
    settings = {
        'calendar_id':_answer(args.calendar, 'calendar', old.get('calendar_id', 'primary')),
        'target':_answer(args.target, 'target', old.get('target')),
        'timezone':_answer(args.timezone, 'timezone', old.get('timezone', 'UTC')),
        'lead_minutes':int(_answer(args.lead_minutes, 'lead-minutes', old.get('lead_minutes', 15))),
        'hermes':_answer(args.hermes, 'hermes', old.get('hermes', shutil.which('hermes') or 'hermes')),
    }
    watcher.validate_settings(settings)
    with _locked_home(home):
        if path.exists() and not args.replace:
            raise ValueError('Settings already exist; use --replace deliberately')
        state_path = home/STATE
        if state_path.exists():
            _check_state(json.loads(state_path.read_text()), settings)
        watcher.write_private_json(path, settings)
        if json.loads(path.read_text()) != settings:
            raise RuntimeError('Setup readback mismatch')
    _print({'status':'configured', 'home':str(home), 'next':'Run hermes calendar-reminders connect. No accounts connected or messages sent.'})


def migrate(args, home):
    if not args.confirm:
        raise ValueError('Migration requires --confirm; stop the old watcher first')
    source = Path(args.from_home).expanduser().resolve()
    if source == home:
        raise ValueError('Migration source and destination must differ')
    settings = _settings(source)
    payloads = {}
    for name in MIGRATION_FILES:
        path = source/name
        if path.exists():
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict):
                raise ValueError('Migration files must contain JSON objects')
            payloads[name] = payload
    if STATE in payloads:
        payloads[STATE] = _check_state(payloads[STATE], settings, args.bind_legacy_route)
    with _locked_home(home):
        if any((home/name).exists() for name in MIGRATION_FILES):
            raise ValueError('Migration destination already contains runtime data; nothing overwritten')
        created = []
        try:
            for name, payload in payloads.items():
                watcher.write_private_json(home/name, payload)
                created.append(home/name)
                if json.loads((home/name).read_text()) != payload:
                    raise RuntimeError('Migration readback mismatch')
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
    _print({'status':'migrated', 'home':str(home), 'files':list(payloads),
            'source_unchanged':True, 'scheduled':False})


def cron_api():
    from cron.jobs import get_job, list_jobs, remove_job
    from cron.scheduler import create_job_with_scheduler_registration
    return SimpleNamespace(get_job=get_job, list_jobs=list_jobs, remove_job=remove_job,
                           create_job_with_scheduler_registration=create_job_with_scheduler_registration)


def _schedule_identity(home):
    suffix = hashlib.sha256(str(home).encode()).hexdigest()[:16]
    return 'Calendar WhatsApp reminders '+suffix, 'calendar-whatsapp-reminders-'+suffix+'.sh'


def _verify_job(job, name, script):
    if (not isinstance(job, dict) or job.get('name') != name or job.get('script') != script
            or job.get('no_agent') is not True or job.get('deliver') != 'local'
            or job.get('failure_deliver') != 'local'):
        raise ValueError('Schedule readback mismatch; inspect the saved job before retrying')


def _matching_jobs(api, name, script):
    matches = [job for job in api.list_jobs(include_disabled=True)
               if job.get('name') == name or job.get('script') == script]
    for job in matches:
        _verify_job(job, name, script)
    if len(matches) > 1:
        raise ValueError('Multiple reminder jobs exist; resolve them before retrying')
    return matches


def _executable(settings):
    value = settings['hermes']
    executable = shutil.which(value)
    if not executable:
        raise ValueError('Configured Hermes executable is unavailable; fix setup before scheduling')
    return str(Path(executable).absolute())


def _write_wrapper(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() != text:
            raise ValueError('Schedule wrapper differs; review it before replacing')
        return
    fd, temporary = tempfile.mkstemp(prefix='.calendar-wrapper-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o700)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if path.read_text() != text:
        raise RuntimeError('Schedule wrapper readback mismatch')


def schedule(args, home, remove=False):
    if not args.confirm:
        raise ValueError('Scheduling changes require --confirm after approval for real reminder delivery')
    with _locked_home(home):
        name, script = _schedule_identity(home)
        api = cron_api()
        matches = _matching_jobs(api, name, script)
        if remove:
            for job in matches:
                api.remove_job(job['id'])
                if api.get_job(job['id']) is not None:
                    raise RuntimeError('Schedule removal readback failed')
            _print({'status':'unscheduled', 'removed':len(matches)})
            return
        if matches:
            _print({'status':'already_scheduled', 'job_id':matches[0]['id'], 'enabled':matches[0].get('enabled')})
            return
        settings = _settings(home)
        if not (home/'google_token.json').is_file():
            raise ValueError('Connect Google Calendar before scheduling')
        if (home/STATE).exists():
            _check_state(json.loads((home/STATE).read_text()), settings)
        executable = _executable(settings)
        profile = profile_home()
        text = ('#!/bin/sh\nset -eu\nexport HERMES_HOME='+shlex.quote(str(profile))+'\nexec '+
                shlex.join([executable, 'calendar-reminders', '--home', str(home), 'scan', '--confirm'])+'\n')
        _write_wrapper(profile/'scripts'/script, text)
        try:
            created = api.create_job_with_scheduler_registration(
                prompt=None, schedule='every 1m', name=name, repeat=None,
                script=script, no_agent=True, deliver='local', failure_deliver='local',
                origin=None, workdir=str(profile), paused=False)
        except Exception as exc:
            # Core may have saved a job before its provider registration failed.
            # Discover it on the next invocation; never blindly repeat create.
            saved = getattr(exc, 'job', None)
            if isinstance(saved, dict) and isinstance(saved.get('id'), str):
                _print({'status':'scheduler_registration_failed', 'job_id':saved['id'],
                        'job_saved':True, 'retry_create':False})
                return 1
            raise
        actual = api.get_job(created['id'])
        _verify_job(actual, name, script)
        if not actual.get('enabled'):
            raise RuntimeError('Schedule was not enabled; inspect the saved job')
        _print({'status':'scheduled', 'job_id':actual['id'], 'schedule':'every 1m',
                'no_agent':True, 'deliver':'local', 'scheduler_must_be_running':True})


def command(args):
    try:
        home = resolve_home(args.home)
        action = args.calendar_reminders_action
        if action == 'setup':
            return setup(args, home) or 0
        if action == 'migrate':
            return migrate(args, home) or 0
        if action in ('schedule', 'unschedule'):
            return schedule(args, home, remove=action == 'unschedule') or 0
        if action == 'connect':
            settings = _settings(home)
            argv = ['--home',str(home),'--calendar',settings['calendar_id']]
            if args.client_secret:
                argv += ['--client-secret',args.client_secret]
            return oauth_helper.main(argv)
        if action == 'scan':
            if not args.dry_run and not args.confirm:
                raise ValueError('Real delivery requires scan --confirm; use --dry-run to preview')
            argv = ['--home',str(home)] + (['--dry-run'] if args.dry_run else [])
            return watcher.entrypoint(argv)
        if action == 'calendar':
            argv = args.calendar_args
            if argv and argv[0] == '--':
                argv = argv[1:]
            return calendar_cli.main(['--home',str(home)] + argv)
        if action == 'status':
            if args.live:
                settings = _settings(home)
                return calendar_cli.main(['--home',str(home),'status','--calendar',settings['calendar_id']])
            configured = (home/SETTINGS).is_file()
            if configured:
                _settings(home)
            _print({'home':str(home), 'configured':configured,
                    'token_present':(home/'google_token.json').is_file(),
                    'delivery_history_present':(home/STATE).is_file(), 'live_verified':False})
            return 0
        raise ValueError('Unsupported Calendar reminders command')
    except (ValueError, FileExistsError) as exc:
        print(json.dumps({'error':str(exc)}), file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print(json.dumps({'error':'Operation cancelled'}), file=sys.stderr)
    except Exception as exc:
        print(json.dumps({'error':'Calendar reminders operation failed; inspect local setup and saved jobs before retrying',
                          'type':type(exc).__name__}), file=sys.stderr)
    return 1
