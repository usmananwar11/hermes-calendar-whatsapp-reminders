"""Native Hermes plugin entry point; registration performs no setup or I/O."""
from pathlib import Path

from . import plugin_cli


def register(ctx):
    ctx.register_cli_command(
        name='calendar-reminders',
        help='Configure Google Calendar WhatsApp reminders',
        setup_fn=plugin_cli.register_cli,
        handler_fn=plugin_cli.command,
        description='Explicit setup, Google authorization, scans and script-only scheduling.',
    )
    ctx.register_skill(
        name='google-calendar-whatsapp-reminders',
        path=Path(__file__).resolve().parent/'SKILL.md',
        description='Send Google Calendar reminders through WhatsApp.',
    )
