# Google Calendar → WhatsApp reminders for Hermes

A standalone Hermes skill: browser-based Calendar OAuth, a confirmed-mutation Calendar CLI, and an LLM-free WhatsApp reminder watcher. Source and synthetic tests only: packaging/testing does **not** install the skill, authorise Google, schedule jobs, or send messages.

Version 0.1.0, for local review. License choice is pending Muhammad Usman Anwar's approval; no LICENSE file or redistribution grant is included.

## Contents and requirements

- `SKILL.md`: agent workflow and safety boundaries.
- `scripts/connect_google_calendar.py`: explicit OAuth with guarded import and private atomic token saving.
- `scripts/personal_calendar.py`: read operations and approved event mutations with exact-target readback.
- `scripts/google_calendar_reminders.py`: paginated polling, locking, receipt validation and durable local deduplication.
- `examples/calendar_reminder_settings.example.json`: configuration template; destination intentionally empty.
- `tests/`: full adapted original suites, portability/OAuth regressions and offline safety barriers.

Python **3.10+**, Linux/macOS. The watcher uses Unix `fcntl.flock`; native Windows is unsupported. `requirements.txt` lists Google SDK/auth/OAuth, auth HTTP transport, httplib2 and timezone data. Versions are intentionally unpinned; resolve and lock them under your deployment policy.

## Dependencies and offline verification

From the repository root, using Python 3.10+:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

A managed Hermes installation can use its own installed dependencies:

```sh
hermes --run-module unittest discover -s tests -v
```

Tests isolate runtime homes in temporary directories, use synthetic credentials, and block network sockets and external subprocesses. Google requests, OAuth callbacks, mutations and message sends are mocked. Passing tests **do not prove live delivery**. Temporary directories use Python's defaults, including `TMPDIR` when configured.

## Install the whole skill (manual, opt-in)

Keep `SKILL.md`, `scripts/`, `README.md`, `requirements.txt` and `examples/` together. Copying only SKILL.md loses its helpers. Tests may be retained or omitted; omit `.git`, `.venv` and caches. After choosing the correct profile home, from this repository's root:

```sh
PROFILE_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$PROFILE_HOME/skills/google-calendar-whatsapp-reminders"
mkdir -p "$DEST"
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' ./ "$DEST/"
```

This is a **user-run example**, not an installation performed by packaging. Verify the intended profile and review before overwriting any existing skill. Start a new Hermes session to discover the installed skill. Keep credentials outside both the repository and installed skill directory.

## Portable runtime configuration

Runtime-home precedence for all three scripts:

1. Explicit `--home PATH`.
2. `CALENDAR_REMINDERS_HOME`.
3. `HERMES_HOME`, respecting the active profile.
4. `~/.local/share/hermes-calendar-reminders` when neither variable is set.

Choose an explicit dedicated runtime directory to avoid accidentally using existing profile credentials. Paths expand `~`; relative paths stay relative to the working directory. Use **absolute paths** in scheduled jobs.

| Runtime file (never commit) | Purpose |
|---|---|
| `google_client_secret.json` | User-supplied Desktop OAuth client |
| `google_token.json` | Durable credentials; private atomic replacement |
| `calendar_reminder_settings.json` | Personal calendar, timezone and destination |
| `calendar_reminder_state.json` | Delivery keys/receipts; private atomic writes |
| `calendar_reminder_state.lock` | Non-blocking Unix process lock |

Watcher settings precedence: `--settings PATH`, then `<runtime-home>/calendar_reminder_settings.json`, with legacy `<runtime-home>/scripts/calendar_reminder_settings.json` fallback if the first default file is absent. An explicit missing file never falls back. Calendar CLI and OAuth helper accept `--token PATH`; OAuth also accepts `--client-secret PATH` and `--calendar ID`. The watcher always uses `<runtime-home>/google_token.json`.

Manual setup:

```sh
export CALENDAR_REMINDERS_HOME="$HOME/.local/share/hermes-calendar-reminders"
mkdir -p "$CALENDAR_REMINDERS_HOME"
chmod 700 "$CALENDAR_REMINDERS_HOME"
cp examples/calendar_reminder_settings.example.json "$CALENDAR_REMINDERS_HOME/calendar_reminder_settings.json"
chmod 600 "$CALENDAR_REMINDERS_HOME/calendar_reminder_settings.json"
```

Edit the copied file outside the repository:

- `calendar_id`: `primary` or the intended Calendar ID.
- `lead_minutes`: integer 1–1440; strings, floats and booleans are rejected.
- `timezone`: valid IANA timezone, e.g. `UTC` or `America/New_York`.
- `target`: destination supplied and approved by the user. Syntax includes `whatsapp:<destination>` or `whatsapp_agent_platform:user:<destination>`, according to your configured Hermes transport. Synthetic examples are not real routes; the latter requires a matching adapter. Bare platform/home-channel defaults are deliberately rejected.
- `hermes`: executable name on PATH or absolute path; use an absolute path for scheduled execution.

Invalid targets, zones, lead times and required strings are rejected before auth. Syntax validation does not prove the recipient exists or is correct. Use one runtime home/state file per Calendar/destination; when changing routing, decide explicitly whether to retain delivery state.

## Google setup and connection (approved live operation)

Enable Calendar API in Google Cloud, configure the OAuth consent screen, and create a **Desktop app** OAuth client. For an External app in Testing, add the intended test user. Place its downloaded JSON at `<runtime-home>/google_client_secret.json` and restrict it to mode 0600. Never paste credentials or approval codes into chat.

With explicit approval:

```sh
.venv/bin/python scripts/connect_google_calendar.py --home "$CALENDAR_REMINDERS_HOME"
.venv/bin/python scripts/personal_calendar.py --home "$CALENDAR_REMINDERS_HOME" status
```

The helper binds a loopback callback on `127.0.0.1`, opens the browser, requests offline access, checks durable credentials, and verifies serialized credentials against the **exact returned Calendar ID** before saving the token atomically at mode 0600. Importing it alone neither opens a browser nor reads a client file. Errors omit raw credential/transport exception details.

The scope is `https://www.googleapis.com/auth/calendar`: Calendar-only, no Gmail, Drive or other Workspace permissions. It permits event management because the CLI supports it; it is **not read-only** and is not the minimum scope for a reminder-only reader. For read-only deployments, adapt scope handling and tests before use; this package does not claim read-only-token compatibility. Never broaden scopes just to silence failures.

Google External/Testing apps normally issue refresh tokens expiring after **seven days** for Calendar scopes. Publication/verification and organisational policies may affect authorization. Reconnect deliberately when needed; never print tokens. See Google's OAuth reference below.

## Preview and delivery (approved live operation)

```sh
.venv/bin/python scripts/google_calendar_reminders.py --home "$CALENDAR_REMINDERS_HOME" --dry-run
```

Dry-run reads **live** Calendar and may refresh/save OAuth. It creates a directory/lock as necessary, but never sends or writes delivery state. Its JSON contains scanned/due/pending counts and the configured target/timezone; treat this output as personal configuration.

Only after delivery approval, omit `--dry-run`. The watcher invokes `hermes send --to TARGET --json -- MESSAGE`. It records a reminder only after a structured receipt includes `success: true` and a nonempty string `message_id`; exit code zero alone is insufficient. A receipt acknowledges transport acceptance, not a human reading the message. Verify your configured WhatsApp transport works and the installed Hermes supports JSON receipts.

## Native scheduling without an LLM (manual, opt-in)

Native cron scripts must resolve inside the active profile's scripts directory. After approval, use `write_file` to create `$HERMES_HOME/scripts/calendar-reminders.sh` (or the chosen default profile's scripts directory). Replace placeholders with **absolute** paths:

```sh
#!/usr/bin/env bash
set -euo pipefail
exec '/absolute/skill/.venv/bin/python' \
  '/absolute/skill/scripts/google_calendar_reminders.py' \
  --home '/absolute/runtime-home' \
  --settings '/absolute/runtime-home/calendar_reminder_settings.json'
```

Ask Hermes to create a native `cronjob` with `action="create"`, `schedule="every 1m"`, `script="calendar-reminders.sh"`, `no_agent=True`, `deliver="local"`, and an explicit name. Read the job back to verify its fields. Keeping stdout local avoids duplicate notifications: the script itself already sends reminders. The scheduler must be running. Never force a live cron test without delivery approval.

This follows the official script-only cron schema linked below. No jobs or wrappers are installed by this repository. Explicit runtime flags and an absolute Hermes executable avoid dependence on scheduler PATH or inherited environment variables.

## Calendar CLI safeguards

Read commands: `status`, `calendars`, `list`, `get EVENT_ID`. Put global `--home`/`--token` **before** the subcommand. Explicit list ranges need both `--start` and `--end`; use RFC3339 with seconds and an offset or `Z`.

Create/update/delete require `--confirm` after user approval, including guest notifications. Create also requires a nonempty `--request-id`; retain it when checking uncertain retries. Deterministic IDs, request-ID conflict checks, exact-event readback, ETag preconditions and zero automatic mutation retries reduce accidental writes. Meet creation may be pending or missing; don't claim a join link until returned. Read the exact target before retrying any uncertain mutation.

## Delivery guarantees and recovery

- Only future timed events inside the lead window qualify. All-day, cancelled and self-declined events are excluded. Already-started events never catch up.
- Downtime can miss reminders entirely. There is **no past catch-up**; polling jitter means the configured lead time is not exact.
- Local locking and persisted event-ID/start-instant keys prevent ordinary same-home duplicates. Changed start times produce new keys and can remind again.
- State retains roughly 30 days of recorded deliveries. Avoid deleting it or running multiple independent homes for the same recipient.
- A send/save crash gap can duplicate a message: if sending succeeds but state saving fails, a later scan may resend. This is **at-least-once-style retry, not exactly-once**; it does not guarantee every reminder is delivered.
- Failed scans are not success. Inspect concise local errors, permissions, connectivity and auth rather than assuming a message arrived.

## References

- Hermes skills: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- Script-only cron/schema: https://hermes-agent.nousresearch.com/docs/guides/cron-script-only
- Native cron: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- Hermes script sends: https://hermes-agent.nousresearch.com/docs/guides/pipe-script-output
- Calendar Python quickstart: https://developers.google.com/workspace/calendar/api/quickstart/python
- OAuth expiry: https://developers.google.com/identity/protocols/oauth2#expiration
