# Vigil Technical Issue Tracker Agent

An agent that periodically scans the `#vigil-technical-issues` Slack channel,
qualifies recurring technical issues from user messages and their diagnostic-bot
replies, and maintains a Google Sheet that tracks each distinct issue — how
often it occurs, who reported it, and when.

It is built to the [requirements spec](./vigil-issue-tracker-requirements.md):
it reads top-level messages and their threaded replies, qualifies a message only
when it contains a Mercor Studio task link **and** a diagnostic bot diagnosed a
real issue, groups qualifying reports into distinct categories with Claude,
counts occurrences (deduplicated by Slack message timestamp), and surfaces the
most common issues first. It runs on a schedule and processes only new or
previously deferred activity.

## How it works

```
Slack (#vigil-technical-issues)
        │  history + threaded replies (paginated, rate-limit aware)
        ▼
   Qualification              ──►  no task link            → ignore
   (task link? bot reply?)    ──►  task link, no bot reply  → Pending (re-check next run)
                              ──►  bot "no issue/not stuck" → ignore permanently
                              ──►  ambiguous bot reply       → Needs Review
                              ──►  issue diagnosed           ▼
                                                    Classification (Claude)
                                                    match existing category ≥ threshold
                                                    else mint a new category
                                                              ▼
                                              Occurrence (keyed by Slack ts)
                                                              ▼
   Google Sheet:  Issue Summary  |  Issue Log  |  Needs Review  |  _State (hidden)
```

The occurrence store (the **Issue Log** tab) is keyed by Slack message
timestamp, so re-processing a message overwrites its row rather than
double-counting. The **Issue Summary** and all counts are recomputed from that
store on every run, which makes edit handling fall out naturally: a reclassified
message overwrites its occurrence, a message edited into a "no issue" verdict
deletes its occurrence, and counts recompute automatically.

### Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | Load/validate YAML config; env vars override secrets |
| `task_links.py` | Detect & normalize Mercor Studio task links (configurable regex) |
| `slack_client.py` | Channel history, threaded replies, user names, permalinks; pagination + 429 backoff |
| `classifier.py` | Verdict detection (phrase rules + Claude tie-break) and issue categorization via Claude structured outputs |
| `state.py` | Cursor, processed set (with edit ts), registry, pending, needs-review — serialized to the `_State` tab |
| `sheets_client.py` | Google Sheets/Drive I/O for the four tabs; 429 backoff |
| `aggregate.py` | Pure summary builder (counts, distinct reporters, sorting) |
| `tracker.py` | Run orchestration: qualify → classify → persist, idempotently |
| `scheduler.py` | Interval or cron run loop |
| `factory.py` / `__main__.py` | Wiring and CLI |

## Setup

### 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Slack app

Create a Slack app with a **bot token** (`xoxb-…`) and these scopes:

- `channels:history` (public channel) **or** `groups:history` (private)
- `channels:read` / `groups:read` (resolve the channel by name)
- `users:read` (resolve reporter display names)

Install the app to the workspace and invite the bot to
`#vigil-technical-issues`. Export the token:

```bash
export SLACK_BOT_TOKEN="xoxb-…"
```

### 3. Google service account

1. In Google Cloud, enable the **Google Sheets API** and **Google Drive API**.
2. Create a **service account** and download its JSON key.
3. Either:
   - point `google.sheet_id` at an existing spreadsheet and **share it as
     Editor** with the service account's email, **or**
   - set `google.sheet_name` (and optionally `google.drive_folder_id`) to let
     the agent create the sheet — share the target Drive folder with the
     service account so it can place the file there.

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service_account.json"
```

### 4. Anthropic API key (classification)

```bash
export ANTHROPIC_API_KEY="sk-ant-…"
```

Classification defaults to `claude-opus-4-8`. For high-volume channels you can
set `classification.model` to a cheaper model (e.g. `claude-sonnet-4-6` or
`claude-haiku-4-5`).

### 5. Configure

```bash
cp config.example.yaml config.yaml
# edit channel, bot_identities, sheet target, schedule, etc.
```

Every configurable parameter from the spec is exposed: `slack_channel`,
`slack_auth`, `bot_identities`, `task_link_pattern`, `no_issue_detection`,
`run_interval` (interval or cron), `google_sheet_target`, `google_auth`,
`first_run_lookback` (default 24h), `match_confidence_threshold`, and
`timezone` (default `America/Los_Angeles`).

## Running

```bash
# One cycle, then exit (good for cron / CI / a first smoke test)
python -m vigil_tracker --config config.yaml --once

# Run forever on the configured schedule
python -m vigil_tracker --config config.yaml
```

Scheduling uses `schedule.interval_minutes`, or a `schedule.cron` expression if
set. Each run processes only new history (since the stored cursor) plus any
previously deferred (pending) messages.

## Output tabs

- **Issue Summary** — one row per issue, sorted by occurrences descending:
  Issue ID, Issue Name, Description, Occurrences, Distinct Reporters, Reporters,
  First Reported, Last Reported, Example Message, Example Diagnosis.
- **Issue Log** — one row per occurrence: Report Timestamp, Issue ID, Issue
  Name, Reporter, Reporter ID, Message Permalink, Task Link, Bot Diagnosis,
  Slack Message TS (the dedup key).
- **Needs Review** — ambiguous bot replies and unmatched separate bot messages.
- **_State** (hidden) — cursor, registry, processed/pending/needs-review sets as
  a chunked JSON blob, so the agent is self-contained in one spreadsheet.

## Idempotency, errors, and edge cases

- **No double counting** — occurrences are keyed by Slack message ts; the log
  and summary are recomputed from that store each run.
- **Late bot replies** — handled by the pending set, which is re-checked every
  run independently of the cursor.
- **Edited messages** — the processed set stores each message's last-seen
  `edited` ts; when a re-fetched message's edit ts changes it is reclassified,
  its log row updated in place, and category counts re-derived. (Edits are
  caught for messages that re-appear in the fetch window or are in the pending
  set; an edit to a message far older than the cursor won't be re-scanned —
  raise `first_run_lookback`/cursor handling if you need a wider edit window.)
- **Separate top-level bot diagnoses** — matched to a recent user message by the
  normalized `task_<id>` within `reply_match_window_minutes`; if it can't be
  matched to exactly one user message it goes to Needs Review.
- **Rate limits & pagination** — Slack and Google calls paginate and back off on
  429s with retries.
- **Partial run failure** — data tabs are written before the cursor is advanced,
  so a failed run is safely retried without skipping messages.
- **Timezone** — timestamps are stored/displayed in `America/Los_Angeles` (named
  zone, so PDT/PST track automatically).

## Tests

The qualification, classification, dedup, state, and task-link logic are
unit-tested with fakes — no Slack/Google/Anthropic credentials needed:

```bash
pip install pytest
python -m pytest -q
```
