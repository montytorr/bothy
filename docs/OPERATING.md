# Running Bothy

A runbook for whoever is looking after it, including a client who has never seen
it before.

## Is it working?

```bash
bothy doctor          # non-zero if anything would stop it working
bothy status          # what it is doing right now
bothy audit --verify  # is the record intact
```

`doctor` is the one to reach for first. It exits non-zero and speaks `--json`,
so it can gate a deploy — unlike health commands that always succeed and
therefore tell you nothing.

**A dead process cannot report its own death.** Bothy writes
`$BOTHY_HOME/state/heartbeat.json` every loop; whatever watches the machine
should watch that file's timestamp. Nothing inside Bothy can do this for you.

## Something went wrong

Every record carries one `run_id`, so a whole run is one command:

```bash
bothy audit --run run_20260918T061249Z_9b9f6971
```

`audit --run` includes every retained rotated segment. To see a run as a span
tree, or export it to an OpenTelemetry collector:

```bash
bothy trace run_20260918T061249Z_9b9f6971
bothy trace run_20260918T061249Z_9b9f6971 --json
bothy trace run_20260918T061249Z_9b9f6971 --otlp http://127.0.0.1:4318
```

The tree has a run root, turns beneath it, and Bothy-hosted tool calls beneath
each turn. Each record stores its deterministic trace and span ids inside the
hash chain. Old runs retain a root using their `admitted` and `finished` times;
they cannot supply historical tool spans that were never recorded. Rotated
segments participate while retained (eight archives by default).

`turn/finished` carries status, stop reason, error code, usage and up to 200
Codex execution, MCP and file-change summaries, with arguments limited to 500
characters and item start/end times. Full command output and file diffs are
excluded. `--json` prints OTLP/JSON with nanosecond strings and typed attributes.
`--otlp` sends once with a two-second deadline; headers come from
`OTEL_EXPORTER_OTLP_HEADERS`. Set `otlp_endpoint` to export automatically after
each run. An automatic export failure writes `export/failed` after the budget
reservation and worker slot have been returned; it does not fail the run.

| Symptom | Where to look |
|---|---|
| Nothing is running | `bothy status` — is the pool `probing`? Is budget headroom zero? |
| A wake never ran | `bothy audit \| grep refused` — every refusal names its gate |
| It stopped and stayed stopped | Exit **78** means restarting cannot help. Read the last log line |
| It keeps restarting | Exit **75** is transient. Check the supervisor's restart budget |
| A run cost more than expected | `bothy audit --run <id>` — the `finished` record carries usage and cost |
| Chat is silent | Allowlists are deny-by-default. Was the speaker listed? |
| Discord messages arrive empty | The `MESSAGE_CONTENT` privileged intent is not enabled |
| A poll source went quiet | `bothy poll <name>` runs it once by hand and shows what it saw |
| A scheduled Drove run did not begin | `bothy audit \| grep drove` — a refusal carries Drove's own code (`herdr-down`, `task-closed`, `no-repo`, `cairn`: no Cairn key for Bothy, see "Drove runs") |
| Drove needs a person and chat is silent | `bothy drove attention` shows what Drove says is waiting and what Bothy has done about each |

## Refusals are not failures

Bothy refuses work deliberately and records why. These are all normal:

- `lane_busy` — something else is already working that subject. It will be retried.
- `pool_full` / `pool_probing` — capacity. It will be retried.
- `budget` — the cap would be breached. Names the cap and when it resets.
- `profile` — a capability profile that does not exist. Without a goal it will **not** be retried.

A wake that is refused for capacity too often, or waits too long, is
**abandoned** with an alert rather than retried forever. Retries back off from
4 seconds to a maximum of 64 seconds; 30 refusals take about 28 minutes of
retries before abandonment. The six-hour age limit still applies.

A scheduled model job with a validation check stays pending after a failed
attempt, up to its `max_attempts` limit (1 by default, at most 5). Bothy writes
`goal_attempts` before starting each run, separately from the capacity refusal
counter `attempts`. A capacity refusal does not consume a goal attempt. Goal
retries wait 4, 8, 16 or 32 seconds, using the same durable backoff shape and
64-second ceiling as capacity retries; the drain moves on to other wakes while
they wait. Restarting does not replenish the attempt limit or bypass the delay.

Passing the check processes the wake as `verified`. Each check's `goal` audit entry
names the wake, run, attempt, workspace, check command (or regex) and evidence.
Failed check evidence is capped at 4 KB and included in the next prompt inside
an untrusted fence. Missing commands, timeouts and empty check output fail;
a run that did not complete feeds its error and stop reason (including `token_cap`
or `wall_clock`) back without running its check.
Exhausting attempts processes the wake as `unverified` and raises one goal alert.
A `budget` or `budget_unknown` refusal also stops the goal and alerts, even with
attempts left. Each admitted attempt reserves and settles budget independently.

Wakes without a goal retain the ordinary behavior: capacity refusals are
retried; other outcomes are processed with the runner's status. Inspect goal
evidence with `bothy audit --json` (the text audit view shows the summary only).

## Drove runs

Bothy can start Drove runs (a planner and its workers in herdr panes, on a Cairn
task) and tell you when one needs a person. It runs Drove's CLI as a plain process: no model turn,
no Codex, no sandbox. Its settings are under "Drove" in `CONFIG.md`.

```bash
bothy drove start --from-cairn DROVE-5   # what a schedule or webhook does, by hand
bothy drove status                       # the runs it started and is watching
bothy drove attention                    # what needs a person, and whether chat has been told
```

**What the machine needs.** All of it, for the account Bothy runs as:

- a **herdr server running**. Drove opens a workspace in it and never starts it,
  so with none a start is refused `herdr-down` and **waits**, counted, like any
  capacity refusal (30 attempts, about 28 minutes of retries, then abandoned out loud);
- `drove`, `herdr`, `node` 22.18+, `git`, `cairn` and the planner's agent
  (`claude` or `codex`) on `PATH`, **and signed in**;
- the repository named by `drove_repo`, with its folder trusted once by hand.
  Drove never answers a trust dialog; it shows as an item that needs a person.

**`PATH` is the usual trap.** The generated launchd unit sets
`/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`, which has
neither `~/.local/bin` nor a Node version manager. The wrapper sources
`/usr/local/etc/bothy.env`, so a `PATH=...` line there is how a service finds
them. `bothy doctor` runs `drove --help` and says if it cannot.

**Cairn needs a key of its own for Bothy.** `drove plan --from-cairn` reads the
task through the `cairn` CLI, which works out which agent it is from the
environment of an interactive session (Claude Code, Codex). A service has none,
so the read is refused as `cairn: CAIRN_API_KEY is not set`. Pair a key named
`bothy`, then tell the CLI to use it. Names only, never a value in a chat or a
ticket:

1. Once, from a terminal on the machine, as the user Bothy runs as:
   `cairn setup --url <instance> --runtimes bothy --no-skill --no-hooks --no-jobs --no-herdr`.
   It prints a `/connect/XXXX-XXXX` link and waits ten minutes.
2. Open the link in a browser signed in to that instance. The page lists one
   runtime, `bothy`, and the host; approve it. The key belongs to whoever
   approves, and shows as `bothy on <host>` under **Your agent keys**
   (`/settings/keys`), where you revoke it.
3. It lands in `~/.cairn/instances/<name>/env` as `CAIRN_API_KEY_BOTHY` (mode
   `0600`; the other keys there are left alone).
4. In Bothy's `bothy.env` put one line, `CAIRN_AGENT=bothy`. The key itself is
   not copied there. Restart (`launchctl kickstart -k gui/$(id -u)/dev.bothy.<site>`),
   because the file is read at start.
5. Check: `CAIRN_AGENT=bothy cairn show <ref>` prints the task.

Anything Drove then writes to Cairn from a Bothy-started run is filed under
agent `bothy`. Drove hands a pane its environment as explicit flags and herdr
starts it, so the variable should not follow the run into its panes; that is read
from Drove's source and has not been confirmed on a live run, so look at the
planner pane's `CAIRN_AGENT` the first time. A `cairn` refusal is not retried:
a missing key does not clear by waiting, and a Cairn that is down reads the same.
These steps are written from Cairn's source and its `--dry-run`; a pairing has not
yet been completed here.

**What it does with a run.** A start returns at once with a run id, which Bothy
writes to `state/drove-runs.json` and the audit. It then reads
`drove roster --run ID --json` every `drove_poll_seconds` until the run is
`down`, saying what the last report was, or until `drove_run_deadline_seconds`
pass, saying the run is still up. **It never stops a run.** If the roster cannot
be read, the run is *unknown*, not down: after five misses you are told, and the
watch carries on.

**What it does about attention.** With `drove_attention_interval_seconds` set,
every item in `drove attention --all --json` that needs a person is sent to your
webhook once, and a follow-up is sent when it stops waiting. The follow-up means
*it is no longer in Drove's list*: someone answered it, the dialog cleared, or
the run went down — the message says which of the last it knows. Items Drove
addresses to its planner (`for: planner`: `stalled`, `idle-without-report`,
`budget`, `pending-delivery`) are not sent; a channel that carries them is one
people stop reading. An item with no `for` field, from an older Drove, is a
person's unless its `answer.type` is `planner`. Bothy does not pass
`--for human`, so it works against either.

**What the message looks like.** On Discord an embed, on Slack blocks, on
anything else plain text. The title names the project first (read from the run
id: `dispofi-api · the planner stopped`), one sentence says what happened, and
"What to do" holds the command in a code block (or says which pane to answer in,
with a command to look at it first). The run id is below, the colour follows how
bad it is (red: the planner stopped; blue: a question; amber: the rest), and the
time is when Drove first saw it. The internal item id is never shown; it is in
`state/drove-attention.json` and the audit. A resolution edits the Discord
message to a grey "Resolved" embed instead of adding a second one (the message
id is kept in that state file; if the message was deleted, a fresh resolution is
posted). Slack's incoming webhooks cannot edit, so there it is a short follow-up.
Bothy's own alerts (a job failing, a run going down, a lost loop pass) use the
same shape, with the raw error small under "Details" and `Bothy` or the project
leading the title. Every `drove` command in a message carries `--run <id>`.

**Told once, and the one way it is not.** An item is written to
`state/drove-attention.json` as *to tell* before it is sent and as *told* after.
A restart tells nothing twice. A crash in the instant between the send and the
second write sends that one item again: a duplicate is the cheaper mistake when
the message is "a person is needed". Ids are Drove's and are never reused, so an
item that went away and came back is a new message.

**If chat is down**, an item stays *to tell* and is retried every poll. After 20
failed attempts it is abandoned and audited, and the alert goes to the log if
chat is what is down. It is still open in Drove, which `bothy drove attention`
will show.

**Spend.** Drove's models are paid from the Claude or Codex account signed in on
the machine, not from Bothy's budget. `drove_max_runs` (default 1) is the cap.

**Not yet proven live:** a run started by Bothy's own cron on a real herdr, and
an item reaching a real channel. Everything above is tested against a stand-in
for `drove` that speaks the shapes in Drove's README.

## A job that did not run: `sop_missing`

A job with `--sop NAME` whose file has gone missing, is empty or is over 16 KB
does not run. Its wake is recorded as processed (`refused`, gate `sop_missing`),
an alert says which SOP, and the job waits for its next time as usual. Fix it
with `bothy sop show NAME` and `bothy sop set NAME FILE`; nothing needs
restarting.

## Things that need a human

| Alert | What it means |
|---|---|
| `budget` | Raise the cap, wait for the window, or switch auth mode |
| `slack` / `discord` **critical** | A token is wrong or revoked. Waiting will not fix it |
| `audit` | Bothy cannot record what it is doing. Check the disk before trusting the log |
| `wake … abandoned` | Something has held a lane for hours, or the pool never recovered |
| `daemon unclean_restart` | The previous run did not shut down. Check memory pressure |
| `drove:<route>` | A start Drove refused, with its code and the first thing to do about it |
| `drove:<run id>` | A run was still up at the deadline, or Drove has forgotten it, or its state cannot be read |
| `drove:attention` | Drove could not be asked, or an item could not be delivered after 20 tries |

Repeated identical failures are **one** incident, not a thousand pings: alerts
are fingerprinted on the error text and suppressed until it changes.

## Stopping and starting

Stop one live Codex run by its Bothy run id (from `bothy status` or the audit):

```bash
bothy cancel run_20261013T090000Z_1234abcd
```

Cancellation writes and fsyncs `state/control/<run_id>.cancel`, then audits the
request. The runner checks it every 100 ms and sends `turn/interrupt`; a quiet
worker gets one second to finish before the runner closes its process group,
settles the reservation and frees the lane. This works for daemon jobs and
`bothy run`. If the worker is still registered after two seconds, the command
rechecks its boot and start identity, kills the owned group and settles its
reservation conservatively, like `reap`. The command reports whether it used a
clean interrupt or a fallback kill. Unknown or malformed ids fail without signals.

The run's `finished` audit data includes `stop_reason`: `wall_clock`, `token_cap`,
`budget`, `awaiting_human`, `cancelled`, or `null`. A fallback also records its
group disposal result. Control files are removed when the run ends.

```bash
systemctl restart dev.bothy.<site>       # Linux
launchctl kickstart -k system/dev.bothy.<site>   # macOS
```

`SIGTERM` drains: the listener stops accepting, in-flight work is given time, and
the lifecycle ledger records a clean stop.

**After an unclean stop**, the next start refuses to run beside orphaned workers
and names them. Clear them with:

```bash
bothy reap    # kills leftover process groups and releases their budget
```

## On a Mac you sit at: a LaunchAgent

The default macOS install is a **LaunchDaemon**: it starts at boot with nobody
logged in, which is right for a client's unattended mini and wrong for anything
that needs your session. A daemon has no GUI session, so it cannot reach the
herdr server you started, the Claude login in your Keychain, or `~/.local/bin`.
For a machine you use, install a per-user **LaunchAgent** instead. No sudo:

```bash
bin/bothy install --kind launchagent --dry-run   # what it will write, with the PATH and interpreter
bin/bothy install --kind launchagent
bin/bothy uninstall --kind launchagent           # state and secrets are left alone unless --purge
```

It writes `~/Library/LaunchAgents/dev.bothy.<site>.plist`, a launcher at
`$BOTHY_HOME/bothy-run`, a `0600` `$BOTHY_HOME/bothy.env` for secrets and logs to
`~/Library/Logs/bothy/bothy.log`, and loads itself into `gui/<your uid>`.

- **It starts when you log in, not when the machine boots**, and stops when you
  log out. A power cut leaves it down until someone logs in.
- **PATH is the PATH of the shell that ran the installer**, minus directories that
  do not exist, so `drove`, `herdr`, `node`, `claude` and `cairn` are found. It is
  printed by `--dry-run` and written into the plist. Pass `--path` to give another.
  The interpreter is the one that ran the installer, written into the launcher.
- **Run it from a login that has the session** (a terminal on the Mac, or ssh into
  an account that is logged in at the console). With no GUI session there is no
  `gui/<uid>` domain to load into and `launchctl bootstrap` says so.
- **Run it without sudo.** Under sudo it would load into root's session, which is
  the account this exists to avoid; it refuses.
- **No log rotation**: `newsyslog` needs root. The log is a few lines per wake, but
  look at its size now and then.
- It does not wait for `tailscaled`. Bothy binds loopback either way; `tailscale
  serve` is a separate command (`bothy funnel`).

Logs and state are read the same way as under a daemon: `bothy status`,
`bothy audit`, `tail -f ~/Library/Logs/bothy/bothy.log`, and restart with
`launchctl kickstart -k gui/$(id -u)/dev.bothy.<site>`.

## Upgrading

```bash
git pull && make check && sudo bothy install && systemctl restart dev.bothy.<site>
```

`make check` first, always. The install is idempotent and rewrites the wrapper
and unit; it leaves your config, state and secrets alone.

## What lives where

```
$BOTHY_HOME/
  config.json           settings; never secrets
  audit.jsonl           hash-chained record, rotated with the chain intact
  sops/                 standard operating procedures (<name>.md), see `bothy sop`
  state/
    heartbeat.json      watch this from outside
    lifecycle.json      proves whether the last stop was clean
    bothy.lock          one instance per state directory
    budget.json         reservations and accrued spend
    workers.json        process groups we started
    jobs.json           the schedule
    poll.json           cursors, ETags and seen ids
    drove-runs.json     Drove runs it started, and where each watch stands
    drove-attention.json  which items it has told chat about
    checklist.md        the standing checklist, editable by hand or by the agent
    wakes/wakes.jsonl   the durable queue
    homes/              one disposable CODEX_HOME per run
    control/            operator controls; cancellation files expire with the run
/usr/local/etc/bothy.env   secrets, 0600, owned by the service user
/var/log/bothy/bothy.log   stdout and stderr
```

Everything is a plain file. An operator can read the audit trail with `cat` and
mail it to you, with no tooling and no lock to contend for.

## Security posture, in short

- Bothy binds **127.0.0.1 only**. Reachability is `tailscale serve`'s job.
- A route is **tailnet-only unless it says otherwise**, and public routes live on
  a separate port that carries nothing else.
- Chat allowlists are **deny by default** and the daemon refuses to start empty.
- A webhook signature authenticates the **sender, never the content**. Everything
  inside a payload is untrusted input, and the prompt says so.
- A worker gets **nothing** beyond Codex's built-ins unless a profile grants it.
- The **sandbox** is the boundary, not the approval policy: `approvalPolicy:
  "never"` means *do not stop to ask*, not *allow anything*.
