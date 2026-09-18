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

## Refusals are not failures

Bothy refuses work deliberately and records why. These are all normal:

- `lane_busy` — something else is already working that subject. It will be retried.
- `pool_full` / `pool_probing` — capacity. It will be retried.
- `budget` — the cap would be breached. Names the cap and when it resets.
- `profile` — a capability profile that does not exist. It will **not** be retried.

A wake that is refused for capacity too often, or waits too long, is
**abandoned** with an alert rather than retried forever.

## Things that need a human

| Alert | What it means |
|---|---|
| `budget` | Raise the cap, wait for the window, or switch auth mode |
| `slack` / `discord` **critical** | A token is wrong or revoked. Waiting will not fix it |
| `audit` | Bothy cannot record what it is doing. Check the disk before trusting the log |
| `wake … abandoned` | Something has held a lane for hours, or the pool never recovered |
| `daemon unclean_restart` | The previous run did not shut down. Check memory pressure |

Repeated identical failures are **one** incident, not a thousand pings: alerts
are fingerprinted on the error text and suppressed until it changes.

## Stopping and starting

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
  state/
    heartbeat.json      watch this from outside
    lifecycle.json      proves whether the last stop was clean
    bothy.lock          one instance per state directory
    budget.json         reservations and accrued spend
    workers.json        process groups we started
    jobs.json           the schedule
    poll.json           cursors, ETags and seen ids
    checklist.md        the standing checklist, editable by hand or by the agent
    wakes/wakes.jsonl   the durable queue
    homes/              one disposable CODEX_HOME per run
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
