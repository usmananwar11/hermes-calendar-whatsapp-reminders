---
name: google-calendar-whatsapp-reminders
description: Send Google Calendar reminders through WhatsApp.
version: 0.2.0
author: Muhammad Usman Anwar, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [calendar, whatsapp, reminders, scheduling]
    related_skills: []
---

# Google Calendar WhatsApp Reminders

Use the native plugin CLI to send advance Google Calendar reminders through Hermes' existing WhatsApp transport, without an LLM per scan. Calendar event mutations remain explicitly confirmed. Installation and registration never authorize Google, configure a route, schedule jobs or send messages.

## When to Use

- A user wants advance WhatsApp reminders for their Google Calendar events.
- A user explicitly approves connecting Calendar or scheduling reminder scans.
- Don't use for past-event catch-up, all-day notifications, guaranteed exactly-once delivery, or unauthorised guest invitations.

## Prerequisites

- Hermes 0.21.6+, Linux/macOS (the watcher uses Unix `fcntl` locking).
- Install and enable the `calendar-whatsapp-reminders` plugin, consenting to its declared Python dependencies. Do not create a separate environment for normal plugin use.
- An operational Hermes WhatsApp transport and a destination supplied and approved by the user; never infer a destination from an unrelated chat.
- A Google Desktop OAuth application with Calendar API enabled. Keep client JSON and tokens outside this skill repository. Request Calendar-only access; this package's scope also permits explicitly approved event mutations.
- Read `README.md` for setup, path precedence, transport receipts, and scheduler integration.

## How to Run

Use the `terminal` tool; no repository working directory is required. `setup` writes only local settings and prompts for an explicitly approved route. `connect` opens the browser only when invoked deliberately.

```python
terminal(command="hermes calendar-reminders setup")
terminal(command="hermes calendar-reminders connect --client-secret '<desktop-client-json>'")
terminal(command="hermes calendar-reminders status --live")
terminal(command="hermes calendar-reminders scan --dry-run")
```

Only `setup` is local-only in this example. Live status and dry-run access Google and may refresh credentials. Do not connect or scan merely to verify the package. Paths supplied in terminal commands must be shell-quoted, never interpolated unescaped from event text.

## Quick Reference

- Plugin home: `<active-profile>/plugin-data/calendar-whatsapp-reminders`; override with `hermes calendar-reminders --home PATH <command>`. The plugin ignores `CALENDAR_REMINDERS_HOME` and never silently adopts legacy profile credentials.
- Local status: `hermes calendar-reminders status`; live Calendar readback: add `--live`.
- Single scan: `hermes calendar-reminders scan --dry-run`; sending requires `scan --confirm` after user approval.
- Scheduling: `schedule --confirm` and `unschedule --confirm` under the same CLI. Commands verify the saved native job; scheduling never runs an immediate scan.
- Calendar CLI: `hermes calendar-reminders calendar list`, `calendar get EVENT_ID`; mutations `calendar create`, `calendar update`, `calendar delete` retain their original `--confirm` guards and create request-ID requirement.
- Legacy migration: `migrate --from-home PATH --confirm`; nonempty unbound history also requires `--bind-legacy-route` after verifying the original route and Google account.
- Offline repository tests: `terminal(command="hermes --run-module unittest discover -s tests -v")` with declared dependencies available; no credentials required. Standalone script path precedence remains documented separately.

## Procedure

1. Confirm approved profile/runtime home, timezone, lead time (integer 1–1440), calendar and WhatsApp destination. Run plugin `setup`; completion means private settings validate without any credential access or sends. Replacing settings requires `--replace` and cannot silently relabel delivery history.
2. With explicit OAuth approval, run plugin `connect` with the user's Desktop OAuth client path. The user approves in a browser, never shares codes or tokens in chat. Completion means `CALENDAR_CONNECTED` after exact Calendar-ID readback and private atomic token saving.
3. With approval for a live read, run `status` and the watcher `--dry-run`. Completion means the intended Calendar and pending count are checked; no delivery state is marked by dry-run.
4. Only after delivery approval, run `scan --confirm`. Completion means a successful structured Hermes message receipt exists before recording each reminder. Never treat exit code alone as delivery evidence.
5. With scheduling approval, run `schedule --confirm`. The plugin writes a profile-pinned wrapper and creates a native every-minute, `no_agent=True` job with success/failure output local; it verifies the exact saved job. Keep the scheduler running. Do not schedule an additional duplicate watcher or route stdout to WhatsApp.
6. Report the installed paths, job ID if created, actual checks, and unresolved limitations without exposing credential contents or personal identifiers unnecessarily.

## Pitfalls

- Offline tests prove mocked behavior, not live Google access or WhatsApp delivery.
- Dry-run can refresh OAuth and creates the lock/runtime directory, but neither sends nor marks reminders delivered.
- All-day, cancelled, self-declined and already-started events are excluded; downtime can miss reminders entirely.
- Locking and persisted event-ID/start-time keys avoid ordinary local duplicates. A crash after send but before state save can duplicate a message: delivery is at-least-once-style retry, not exactly-once.
- Reuse one runtime home per destination/calendar/Google account. State binds the configured calendar and target and rejects changes; use a separate home for a new route. The `primary` alias cannot detect an account change. Nonempty legacy state without routing requires the explicitly verified migration in README; never guess its original route or delete history to bypass the check.
- Refresh credentials or verify readback before retrying uncertain mutations. Do not blindly replay writes or invitations.
- External Google apps in Testing typically have seven-day refresh tokens for Calendar scopes; reconnect when required. A failed scan is not a successful reminder.
- This package is licensed under MIT; retain the copyright and permission notice when redistributing it.

## Verification

Run the offline suite and inspect Git status before sharing. For an explicitly authorised live workflow, check Calendar readback, structured WhatsApp receipt (`success` and `message_id`), persisted delivery key, and the scheduler's readback/logs. Never claim a test fixture receipt is a real delivery.
