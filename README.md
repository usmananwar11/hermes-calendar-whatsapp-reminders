# Google Calendar → WhatsApp Reminders

Advance Google Calendar reminders through your existing Hermes WhatsApp connection,
with script-only scans and no LLM call per scan.

## Install

```sh
hermes plugins install usmananwar11/hermes-calendar-whatsapp-reminders
```

**Publication prerequisite:** the native plugin source changes must be committed
and pushed to this GitHub repository before this command can install the new
version. Local edits are not a published release. A GitHub install is not a
reviewed catalog listing; no catalog approval is claimed here.

Review the source, security findings, and dependency prompts. Consent to enabling
only when ready; if installation leaves the plugin disabled, use:

```sh
hermes plugins enable calendar-whatsapp-reminders
```

Dependency installation has separate consent; enabling does not bypass it.
Restart your Hermes session after enabling to discover its CLI and skill.

### Requirements

- **Hermes 0.21.6 or newer**, on **Linux or macOS**; native Windows is unsupported
  because the watcher uses Unix file locking.
- An operational WhatsApp connection in the intended Hermes profile, an approved
  explicit recipient, and `hermes send` with JSON receipt support.
- A Google account, Calendar API access, and a **Desktop app** OAuth client.
- A local browser able to reach the OAuth loopback callback.

Hermes manages the Python dependencies declared in `plugin.yaml`; no manual
clone, skill-directory copy, or separate virtual environment is needed for users.

## 1. Guided setup

Run in the Hermes profile that owns your WhatsApp connection:

```sh
hermes calendar-reminders setup
```

The prompts ask for the calendar (`primary` by default), approved target, IANA
timezone (`UTC`), lead time (15 minutes), and Hermes executable. Lead time must be
1–1440 minutes. Use an absolute executable path for reliable scheduling.
Targets must be explicit, such as `whatsapp:<destination>` or
`whatsapp_agent_platform:user:<destination>` for a matching configured adapter;
replace the placeholder with the recipient you actually approved.

Setup writes local configuration only: it does not authorize Google, send a
message, or create a schedule. Existing settings require deliberate `setup
--replace`; a route change conflicting with saved history is rejected.

By default, runtime data lives under the active profile at
`plugin-data/calendar-whatsapp-reminders/`, outside the installed code. To choose
another directory, put `--home /absolute/path` **before** the subcommand on every
invocation. The native CLI does not use `CALENDAR_REMINDERS_HOME`.

## 2. Connect Google Calendar

1. In Google Cloud Console, select a project and enable **Google Calendar API**.
2. Configure the OAuth consent screen; add your account as a test user if needed.
3. Create an OAuth client of type **Desktop app** and download its JSON file.
4. Keep the file outside the repository/plugin directory and restrict access.

```sh
chmod 600 "$HOME/Downloads/calendar-desktop-client.json"
hermes calendar-reminders connect --client-secret "$HOME/Downloads/calendar-desktop-client.json"
```

Replace that path with your downloaded file. This explicitly starts a browser
flow with a `127.0.0.1` callback, verifies Calendar access, and stores a private
OAuth token in the runtime home. Do not paste credentials or authorization codes
into chat. This flow requires an interactive local browser, not unattended cron.

The requested `calendar` OAuth scope permits Calendar reads **and event changes**;
it is not read-only. No Gmail or Drive scope is requested. External/Testing app
authorizations can expire after seven days; reconnect deliberately when necessary
without deleting delivery history.

## 3. Check and preview

```sh
hermes calendar-reminders status
hermes calendar-reminders status --live
hermes calendar-reminders scan --dry-run
```

Plain `status` checks local configuration/file presence, not live authentication
or scheduler health. `--live` contacts Google. Dry-run reads live events and may
refresh/save OAuth credentials and create a directory/lock; it **never sends** or
writes delivery history. Its counts do not prove WhatsApp delivery.

After separately approving real delivery, a single scan is:

```sh
hermes calendar-reminders scan --confirm
```

This can send for **every eligible event** in the current window, not just one.

## 4. Optional automatic reminders

Only after approving ongoing delivery:

```sh
hermes calendar-reminders schedule --confirm
```

This writes a profile-local shell wrapper and registers an enabled native Hermes
job for **every minute**, with `no_agent=True` and local success/failure output.
The Hermes scheduler must be running. No LLM is invoked for each scan; the script
itself sends reminders. Installation and setup do not create this job.

To stop future scheduled scans:

```sh
hermes calendar-reminders unschedule --confirm
```

This removes this runtime home's matching job, not its wrapper or credentials.
It cannot retract messages already handed to the transport. Check scheduler
errors/saved jobs before retrying a failed scheduling operation.

## Agent skill and Calendar CLI

The plugin registers the skill under the namespace
`calendar-whatsapp-reminders:google-calendar-whatsapp-reminders`. Ask Hermes to
load it with:

```text
skill_view(name="calendar-whatsapp-reminders:google-calendar-whatsapp-reminders")
```

This is a skill-tool name, **not** a promise of an automatically created slash
command. The original Calendar CLI is also available through:

```sh
hermes calendar-reminders calendar -- calendars
hermes calendar-reminders calendar -- --help
```

Event mutations require explicit approval and `--confirm`; creation additionally
requires a request ID. See [standalone and advanced usage](docs/STANDALONE.md).

## Delivery boundaries

- Only future timed events within the lead window qualify; all-day, cancelled,
  and self-declined events are skipped. Polling jitter makes lead time approximate.
- Downtime can miss reminders; there is no past catch-up. Transport delays can
  cause late arrival even when the event was future at send time.
- Local locking and persisted event/start keys limit ordinary same-home repeats.
  Changed start times can remind again; history is retained for roughly 30 days.
- A send followed by a state-save failure can resend later. This is **not
  exactly-once**, and does not guarantee every reminder is delivered.
- Only a structured successful receipt with a message ID counts as accepted.
  Transport acceptance does not mean the recipient read the message.

## Security and data handling

- Google authorization, token refresh, and Calendar operations contact Google;
  reminder content and recipient details go through your configured WhatsApp
  transport. Installation/dependencies contact GitHub/package infrastructure.
- The plugin reads the explicitly selected client file and its own runtime data;
  migration reads an explicitly selected legacy home. Tokens are stored/refreshed
  locally, with private atomic writes. Keep client JSON, tokens, settings,
  receipts, and backups out of Git and public diagnostics.
- Sends spawn the configured `hermes send --to TARGET --json -- MESSAGE` child.
  Scheduling writes a shell wrapper that launches the CLI under the selected
  profile; it relies on Hermes's scheduler, not a plugin-owned daemon.
- The child uses that profile's existing WhatsApp credentials/transport. The
  plugin does not import another application's login or add telemetry.
- Current catalog credential policy disallows direct OAuth token-file handling;
  this implementation needs an upstream credential route/design change before
  catalog admission. See [publishing](docs/PUBLISHING.md); do not claim approval.

## Migration, updates, and removal

Stop the old watcher/schedule and make a private backup before migrating:

```sh
hermes calendar-reminders migrate --from-home /absolute/legacy/home --confirm
```

Migration copies selected settings, credentials, and history into an empty native
runtime home, leaving the source unchanged and scheduling nothing. Nonempty
legacy history without route metadata also requires `--bind-legacy-route`, only
**after verifying its original calendar, Google account, and recipient**. Never
guess that binding; keep one runtime home per account/calendar/recipient route.

Before updating, unschedule, back up runtime data privately, and review changes.
Use `hermes plugins update calendar-whatsapp-reminders` for an unpinned Git install;
exact-SHA installs require an explicitly chosen replacement SHA instead. Run local
status and an approved dry-run before scheduling again. Preserve delivery history.

To remove: unschedule first, then run
`hermes plugins remove calendar-whatsapp-reminders`. Remove the leftover wrapper
and dedicated runtime data only after reviewing them; never delete the whole
profile. Deleting local tokens does not revoke Google access—revoke the app in
your Google account separately if desired.

## Development and license

See [standalone/development notes](docs/STANDALONE.md) and the
[publication checklist](docs/PUBLISHING.md). Licensed under the [MIT License](LICENSE).
