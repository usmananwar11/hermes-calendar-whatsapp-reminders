---
name: google-calendar-whatsapp-reminders
description: Send Google Calendar reminders through WhatsApp.
version: 0.1.0
author: Muhammad Usman Anwar, Hermes Agent
license: UNLICENSED
platforms: [linux, macos]
metadata:
  hermes:
    tags: [calendar, whatsapp, reminders, scheduling]
    related_skills: []
---

# Google Calendar WhatsApp Reminders

Poll upcoming timed Google Calendar events and send reminders with Hermes' existing WhatsApp transport, without an LLM per scan. The accompanying Calendar CLI can also manage events after explicit user approval. Installation, OAuth, scheduling and real delivery are separate opt-in steps; packaging or testing must not perform them.

## When to Use

- A user wants advance WhatsApp reminders for their Google Calendar events.
- A user explicitly approves connecting Calendar or scheduling reminder scans.
- Don't use for past-event catch-up, all-day notifications, guaranteed exactly-once delivery, or unauthorised guest invitations.

## Prerequisites

- Python 3.10+, Linux/macOS (the watcher uses Unix `fcntl` locking).
- Install `requirements.txt` into a user-managed virtual environment.
- An operational Hermes WhatsApp transport and a destination supplied and approved by the user; never infer a destination from an unrelated chat.
- A Google Desktop OAuth application with Calendar API enabled. Keep client JSON and tokens outside this skill repository. Request Calendar-only access; this package's scope also permits explicitly approved event mutations.
- Read `README.md` for setup, path precedence, transport receipts, and scheduler integration.

## How to Run

Use the `terminal` tool from the skill directory. The paths below assume a local `.venv`; use its absolute interpreter path for scheduled execution.

```python
terminal(command=".venv/bin/python scripts/connect_google_calendar.py --home '<runtime-home>'")
terminal(command=".venv/bin/python scripts/personal_calendar.py --home '<runtime-home>' status")
terminal(command=".venv/bin/python scripts/google_calendar_reminders.py --home '<runtime-home>' --settings '<settings-json>' --dry-run")
```

These commands access live Calendar and may refresh credentials. The OAuth command opens a browser. Do not run them merely to verify the package.

## Quick Reference

- Runtime home: `--home`, then `CALENDAR_REMINDERS_HOME`, then `HERMES_HOME`, then `~/.local/share/hermes-calendar-reminders`.
- Watcher settings: `--settings`, then `<runtime-home>/calendar_reminder_settings.json`, with legacy `<runtime-home>/scripts/calendar_reminder_settings.json` fallback.
- Calendar CLI: `status`, `calendars`, `list`, `get EVENT_ID`; mutations `create`, `update`, `delete` require `--confirm` after user approval. Global `--home` and `--token` precede the subcommand.
- Offline verification via `terminal(command=".venv/bin/python -m unittest discover -s tests -v")`. No credentials required.

## Procedure

1. Confirm approved runtime home, timezone, lead time (integer 1–1440), calendar and WhatsApp destination. Copy the example outside the repository and fill the empty target; completion means settings validate before any credential access.
2. With explicit OAuth approval, run the connection helper. The user approves in a browser, never shares codes or tokens in chat. Completion means `CALENDAR_CONNECTED` after exact Calendar-ID readback and private atomic token saving.
3. With approval for a live read, run `status` and the watcher `--dry-run`. Completion means the intended Calendar and pending count are checked; no delivery state is marked by dry-run.
4. Only after delivery approval, run the watcher without `--dry-run`. Completion means a successful structured Hermes message receipt exists before recording each reminder. Never treat exit code alone as delivery evidence.
5. With scheduling approval, follow README to create a wrapper inside the active profile's scripts directory. Use native `cronjob` scheduling with `schedule="every 1m"`, `script="calendar-reminders.sh"`, `no_agent=True`, `deliver="local"`; read the job back to verify all fields. Do not route cron stdout to WhatsApp as the script already sends reminders.
6. Report the installed paths, job ID if created, actual checks, and unresolved limitations without exposing credential contents or personal identifiers unnecessarily.

## Pitfalls

- Offline tests prove mocked behavior, not live Google access or WhatsApp delivery.
- Dry-run can refresh OAuth and creates the lock/runtime directory, but neither sends nor marks reminders delivered.
- All-day, cancelled, self-declined and already-started events are excluded; downtime can miss reminders entirely.
- Locking and persisted event-ID/start-time keys avoid ordinary local duplicates. A crash after send but before state save can duplicate a message: delivery is at-least-once-style retry, not exactly-once.
- Reuse one runtime home per destination/calendar. Changing routing while retaining state can suppress reminders at the new destination.
- Refresh credentials or verify readback before retrying uncertain mutations. Do not blindly replay writes or invitations.
- External Google apps in Testing typically have seven-day refresh tokens for Calendar scopes; reconnect when required. A failed scan is not a successful reminder.
- License choice is pending human review; no redistribution permission is asserted by this package.

## Verification

Run the offline suite and inspect Git status before sharing. For an explicitly authorised live workflow, check Calendar readback, structured WhatsApp receipt (`success` and `message_id`), persisted delivery key, and the scheduler's readback/logs. Never claim a test fixture receipt is a real delivery.
