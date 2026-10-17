"""Native plugin contract tests; no real profiles, accounts or sends."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from offline_safety import OfflineTestCase

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = 'calendar_plugin_under_test'
    for key in list(sys.modules):
        if key == name or key.startswith(name+'.'):
            del sys.modules[key]
    spec = importlib.util.spec_from_file_location(name, ROOT/'__init__.py',
                                                submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PluginTests(OfflineTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.profile = Path(os.environ['HERMES_HOME']).resolve()
        self.home = self.profile/'plugin-data'/'calendar-whatsapp-reminders'
        self.constants = types.ModuleType('hermes_constants')
        setattr(self.constants, 'get_hermes_home', lambda:self.profile)
        guard = patch.dict(sys.modules, {'hermes_constants':self.constants})
        guard.start()
        self.addCleanup(guard.stop)

    def parser(self, module):
        parser = argparse.ArgumentParser()
        module.plugin_cli.register_cli(parser)
        return parser

    def invoke(self, module, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return module.plugin_cli.command(self.parser(module).parse_args(argv))

    def configure(self, module):
        return self.invoke(module, ['setup','--target','whatsapp:example-recipient',
                                   '--timezone','UTC','--hermes',sys.executable])

    def test_registration_is_side_effect_free(self):
        module = load_plugin()
        ctx = Mock()
        with patch('builtins.input', side_effect=AssertionError('No prompts on registration')):
            module.register(ctx)
        kwargs = ctx.register_cli_command.call_args.kwargs
        self.assertEqual(kwargs['name'], 'calendar-reminders')
        self.assertTrue(callable(kwargs['setup_fn']))
        self.assertTrue(callable(kwargs['handler_fn']))
        ctx.register_skill.assert_called_once()
        self.assertEqual(ctx.register_skill.call_args.kwargs['path'], ROOT/'SKILL.md')
        self.assertFalse(self.home.exists())

    def test_status_does_not_create_storage_or_authorize(self):
        module = load_plugin()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = module.plugin_cli.command(self.parser(module).parse_args(['status']))
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output.getvalue())['configured'])
        self.assertFalse(self.home.exists())

    def test_setup_uses_profile_storage_not_legacy_environment(self):
        module = load_plugin()
        self.assertEqual(self.configure(module), 0)
        path = self.home/'calendar_reminder_settings.json'
        settings = json.loads(path.read_text())
        self.assertEqual(settings['target'], 'whatsapp:example-recipient')
        self.assertEqual(settings['lead_minutes'], 15)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.home/'google_token.json').exists())
        self.assertFalse((self.profile/'calendar_reminder_settings.json').exists())

    def test_setup_does_not_overwrite_without_replace(self):
        module = load_plugin()
        self.configure(module)
        path = self.home/'calendar_reminder_settings.json'
        before = path.read_bytes()
        self.assertEqual(self.configure(module), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_setup_writes_nothing(self):
        module = load_plugin()
        self.assertEqual(self.invoke(module, ['setup','--target','telegram:example',
                                              '--timezone','UTC','--hermes','hermes']), 1)
        self.assertFalse((self.home/'calendar_reminder_settings.json').exists())

    def test_dry_run_delegates_explicit_home_and_live_send_requires_confirmation(self):
        module = load_plugin()
        with patch.object(module.plugin_cli, 'watcher') as watcher:
            watcher.entrypoint.return_value = 0
            self.assertEqual(self.invoke(module, ['scan','--dry-run']), 0)
            watcher.entrypoint.assert_called_once_with(['--home',str(self.home),'--dry-run'])
            watcher.reset_mock()
            self.assertEqual(self.invoke(module, ['scan']), 1)
            watcher.entrypoint.assert_not_called()
            self.assertEqual(self.invoke(module, ['scan','--confirm']), 0)
            watcher.entrypoint.assert_called_once_with(['--home',str(self.home)])

    def test_connect_is_explicit_and_delegates_calendar_setting(self):
        module = load_plugin()
        self.configure(module)
        with patch.object(module.plugin_cli, 'oauth_helper') as oauth:
            oauth.main.return_value = 0
            self.assertEqual(self.invoke(module, ['connect','--client-secret','/example/client.json']), 0)
            oauth.main.assert_called_once_with(['--home',str(self.home),'--calendar','primary',
                                                '--client-secret','/example/client.json'])

    def test_calendar_pass_through_preserves_confirmation_guard(self):
        module = load_plugin()
        with patch.object(module.plugin_cli, 'calendar_cli') as calendar:
            calendar.main.return_value = 0
            self.assertEqual(self.invoke(module, ['calendar','get','abcde']), 0)
            calendar.main.assert_called_once_with(['--home',str(self.home),'get','abcde'])

    def test_replace_rejects_route_change_with_existing_state(self):
        module = load_plugin()
        self.configure(module)
        state = {'version':1, 'sent':{}, 'routing':{
            'calendar_id':'primary','target':'whatsapp:example-recipient'}}
        (self.home/'calendar_reminder_state.json').write_text(json.dumps(state))
        path = self.home/'calendar_reminder_settings.json'
        before = path.read_bytes()
        self.assertEqual(self.invoke(module, ['setup','--replace','--target','whatsapp:other',
                                              '--timezone','UTC','--hermes','hermes']), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_migration_is_explicit_private_and_non_overwriting(self):
        module = load_plugin()
        source = self.profile/'legacy'
        source.mkdir()
        settings = {'calendar_id':'primary','target':'whatsapp:example-recipient',
                    'timezone':'UTC','hermes':'hermes','lead_minutes':15}
        (source/'calendar_reminder_settings.json').write_text(json.dumps(settings))
        (source/'google_token.json').write_text(json.dumps({'token':'synthetic-token'}))
        args = ['migrate','--from-home',str(source)]
        self.assertEqual(self.invoke(module, args), 1)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.invoke(module, args+['--confirm']), 0)
        self.assertEqual(json.loads((self.home/'google_token.json').read_text()), {'token':'synthetic-token'})
        self.assertEqual((self.home/'google_token.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue((source/'google_token.json').exists())
        self.assertEqual(self.invoke(module, args+['--confirm']), 1)

    def test_legacy_history_needs_explicit_binding_during_migration(self):
        module = load_plugin()
        source = self.profile/'legacy'
        source.mkdir()
        settings = {'calendar_id':'primary','target':'whatsapp:example-recipient',
                    'timezone':'UTC','hermes':'hermes','lead_minutes':15}
        (source/'calendar_reminder_settings.json').write_text(json.dumps(settings))
        sent = {'event|instant':{'delivered_at':'2026-10-10T10:00:00+00:00','message_id':'synthetic'}}
        (source/'calendar_reminder_state.json').write_text(json.dumps({'version':1,'sent':sent}))
        args = ['migrate','--from-home',str(source),'--confirm']
        self.assertEqual(self.invoke(module, args), 1)
        self.assertFalse((self.home/'calendar_reminder_settings.json').exists())
        self.assertEqual(self.invoke(module, args+['--bind-legacy-route']), 0)
        state = json.loads((self.home/'calendar_reminder_state.json').read_text())
        self.assertEqual(state['sent'], sent)
        self.assertEqual(state['routing'], {'calendar_id':'primary','target':settings['target']})

    def test_schedule_requires_confirmation_before_importing_scheduler(self):
        module = load_plugin()
        self.assertEqual(self.invoke(module, ['schedule']), 1)
        self.assertFalse((self.profile/'scripts'/'calendar-whatsapp-reminders.sh').exists())

    def test_schedule_is_idempotent_and_verified(self):
        module = load_plugin()
        self.configure(module)
        (self.home/'google_token.json').write_text('{}')
        jobs = {}
        def create(**kwargs):
            job = dict(kwargs, id='synthetic-job', enabled=True)
            jobs[job['id']] = job
            return job
        api = Mock()
        api.list_jobs.side_effect = lambda **kw:list(jobs.values())
        api.get_job.side_effect = lambda identifier:jobs.get(identifier)
        api.create_job_with_scheduler_registration.side_effect = create
        with patch.object(module.plugin_cli, 'cron_api', return_value=api):
            self.assertEqual(self.invoke(module, ['schedule','--confirm']), 0)
            self.assertEqual(self.invoke(module, ['schedule','--confirm']), 0)
        api.create_job_with_scheduler_registration.assert_called_once()
        payload = api.create_job_with_scheduler_registration.call_args.kwargs
        self.assertTrue(payload['no_agent'])
        self.assertEqual(payload['deliver'], 'local')
        self.assertEqual(payload['failure_deliver'], 'local')
        wrapper = self.profile/'scripts'/payload['script']
        text = wrapper.read_text()
        self.assertIn('HERMES_HOME=', text)
        self.assertIn('calendar-reminders', text)
        self.assertIn('--confirm', text)
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)

    def test_unschedule_verifies_removal(self):
        module = load_plugin()
        self.configure(module)
        (self.home/'google_token.json').write_text('{}')
        jobs = {}
        api = Mock()
        def create(**kwargs):
            jobs['synthetic-job'] = dict(kwargs, id='synthetic-job', enabled=True)
            return jobs['synthetic-job']
        api.create_job_with_scheduler_registration.side_effect = create
        api.list_jobs.side_effect = lambda **kw:list(jobs.values())
        api.get_job.side_effect = lambda identifier:jobs.get(identifier)
        api.remove_job.side_effect = lambda identifier:jobs.pop(identifier, None) is not None
        with patch.object(module.plugin_cli, 'cron_api', return_value=api):
            self.assertEqual(self.invoke(module, ['schedule','--confirm']), 0)
            self.assertEqual(self.invoke(module, ['unschedule']), 1)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(self.invoke(module, ['unschedule','--confirm']), 0)
        self.assertEqual(jobs, {})


if __name__ == '__main__':
    unittest.main()
