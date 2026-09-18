<div align="center">

# Bothy

**A small agent harness you can leave somewhere.**

*Wakes on signed webhooks. Runs Codex under supervision. Remembers in Cairn.*
*Speaks up when it needs you. Closed to everything but the tailnet.*

`stdlib only` · `no virtualenv` · `no build step` · `~3.5k lines`

</div>

---

A **bothy** is an unlocked shelter in the Scottish hills. Nobody staffs it. It
looks after itself. Whoever passes through can use it, and it is still standing
when they leave.

That is the entire design brief. Bothy is the agent harness you install on a
client's Mac mini, walk away from, and monitor from a distance.

```
                      ┌─────────────────────────────────────┐
  signed webhook ────▶│  verify → dedupe → fsync → 202      │
  schedule       ────▶│         (durable before the ack)     │
  message        ────▶└──────────────────┬──────────────────┘
                                         │
                            ┌────────────▼────────────┐
                            │   admission gate         │
                            │   lane · slot · budget   │──▶ refused, and why
                            └────────────┬────────────┘
                                         │
                            ┌────────────▼────────────┐
                            │  codex app-server        │
                            │  approvals · metering    │
                            │  wall clock · interrupt  │
                            └────────────┬────────────┘
                                         │
              ┌──────────────┬───────────┴───────┬──────────────┐
              ▼              ▼                   ▼              ▼
          Cairn          audit log           Discord         process
        (memory)       (hash-chained)        / Slack       group killed
```

## Why it is small

The systems Bothy learns from are excellent and very large. One is 182 MB of
bundled JavaScript across 109 SQLite tables. Another is ~1.9M lines of Python
whose own maintainers name its install path as the main obstacle to handing it
to anyone else — a 3,945-line installer that refuses to run headless, and 939
configuration keys with no way to ship one vetted profile to many sites.

Bothy is the part you can read in an afternoon and defend to a client.

It does not replace those systems. It is what you deploy when the thing has to
run somewhere you cannot reach, on hardware you do not own, for someone who
will phone you when it breaks.

## Rules it is built to

Every one of these was bought with somebody's outage. They are in the code with
the reason written beside them.

**Budget is reserved at admission, never measured afterwards.** With several
runs in flight, *"are we under the cap"* cannot be answered by looking — five
runs each see `$40 of $50` and all five start. So a run leases its worst case
before it begins, reports actuals as it goes, and settles at the end. A crashed
run's lease expires like any other lease. Money is an exclusive slot, and every
exclusive slot needs exactly one automatic path back to empty.

**Workers are killed as process groups, never as processes.** A Codex
app-server is a tree — a Node shim, a Rust binary, sometimes a third child.
Signalling the parent leaves the rest alive. That is how one reference host
accumulated thirteen orphaned app-servers, and how one documented runaway left
196 children and $1,193 of already-spent work behind.

**`fsync`, then acknowledge.** A wake is on disk before the sender is told
"accepted". A mature message broker shipped the opposite as a headline feature
and later deleted it — 2,676 lines and the status code with them — because a
buffer that had not reached disk could not deliver the durability its response
promised.

**Liveness is proven, not assumed.** A zombie process satisfies
`killpg(pgid, 0)`, so the first version of the group killer sat out its entire
grace period against a corpse. Bothy enumerates the group and ignores the dead.

**A replay is answered `200`, not `4xx`.** The sender did nothing wrong, and a
`4xx` makes it retry. The dedupe record lives on disk, so a restart does not
reopen the replay window.

**Refusals are events.** *"It did not start, and which gate said no"* is the
question an operator actually asks. Every refusal names its gate and carries
the numbers to act on.

**`doctor` exits non-zero and speaks JSON**, so a deploy can gate on it. A
health command that cannot fail is decoration.

**A restart replaces; it does not accumulate.** If a previous instance's workers
are still alive, Bothy refuses to start beside them and names them. The
alternative logs "found left-over process … ignoring", which is how five
duplicate listeners once accumulated and fought over one session.

**The exit code is a message to the supervisor.** `75` means restart me, `78`
means stop — so a typo in a config file does not burn the restart budget that
exists for real crashes. Bothy never self-supervises and never daemonises.

**It cannot page you when it is dead.** Nothing can emit its own zero. So it
writes a heartbeat file every loop and the deployment watches that from outside.

**Nothing is said when there is nothing to say.** A scheduled job that finds
everything fine replies `NO_REPLY`, and that is filtered from every outgoing
path. A report that speaks every day teaches everyone to ignore it, and then the
day it matters it is ignored too.

**A schedule fires a *wake*, not a run.** Scheduled work joins the same durable
queue as a webhook and passes the same admission gate, so lanes, budget and
concurrency are enforced in exactly one place. A second path would eventually
disagree with the first, and the disagreement would be found in production.

**A signature authenticates the sender, not the content.** A verified GitHub
webhook proves GitHub sent it. The pull request title inside was written by a
stranger.

## Try it

```bash
export BOTHY_HOME=~/.bothy && mkdir -p $BOTHY_HOME
cat > $BOTHY_HOME/config.json <<'JSON'
{
  "site": "acme-mini",
  "workspace": "/srv/work",
  "sandbox": "readOnly",
  "pool":   { "max_workers": 2 },
  "budget": { "mode": "api", "per_run_usd": 0.50, "per_day_usd": 25.0 }
}
JSON

bin/bothy doctor                 # non-zero if anything would stop it working
bin/bothy run "..." --subject weekly --ref ACME-12
bin/bothy serve                  # the daemon, in the foreground, for a real supervisor
bin/bothy status
bin/bothy audit --run run_20260918T061249Z_9b9f6971
bin/bothy reap                   # kill what a previous instance left behind

bin/bothy schedule add heartbeat --kind every --spec 3600 --heartbeat \
    --prompt "Check the harness itself: anything stuck, over budget, unreported?"
bin/bothy checklist --set - < notes.md
```

Routes are declared in config; **secrets are named, never stored**:

```json
"routes": [
  { "path": "/hook/github", "secret_env": "GITHUB_WEBHOOK_SECRET",
    "subject_from": "issue.id", "subject_prefix": "issue-" }
]
```

`subject_from` is what makes two webhooks about one issue share a lane instead
of racing. A route whose secret is unset is a **startup error**, not a warning —
Bothy will not hold a door open it meant to lock.

Real output:

```
$ bin/bothy doctor
  [ok  ] state directory writable: /home/caladmin/.bothy/state
  [ok  ] audit chain intact: 14 records verified
  [ok  ] codex present: codex-cli 0.154.0
  [ok  ] cairn reachable: memory and task ledger
  [ok  ] no orphaned workers: 0 process group(s) left by a previous instance
  [warn] alert route configured: a failure nobody hears about is not handled
  [ok  ] subscription window readable: 97.0% used, resets 2026-09-20T11:31:41+00:00

$ bin/bothy run "Reply with exactly: BOTHY" --subject demo
completed  run_20260918T061227Z_a5b90382  0.0199 usd
  BOTHY

$ bin/bothy status
bothy 0.1.0 — acme-mini
  pool     healthy  0/2 busy (max 2, failure ratio 0.0 over 0)
  budget   0.0597 used + 0 reserved of 5.0 usd  (4.9403 left, api mode)
  lane     nothing in flight
  workers  0 live process group(s)
```

And when it should not run, it says so in terms you can act on:

```
$ bin/bothy run "..." --subject demo
refused  run_20260918T061120Z_b3ec3ddd  0.0000 percent
  refused at gate 'budget': would reach 99.00 percent against a 85.00 ceiling
  (97.00 used, 0.00 already reserved, 2.00 requested)
  → resets 2026-09-20T11:31:42+00:00
```

## Two modes, one gate

`budget.mode` decides the unit and who is counting:

| mode | unit | who owns the total |
|---|---|---|
| `api` | dollars | **Bothy** — prices each turn from token counts, with cache writes on their own line |
| `subscription` | % of window | **Codex** — read from `account/rateLimits/read`; the ledger tracks only what is in flight |

Getting that backwards double-counts in one mode and under-counts in the other,
so it is explicit in the code rather than implied.

## Layout

| Module | Owns |
|---|---|
| `clock.py` | Time, always with an offset. Deadlines are monotonic. |
| `ids.py` | One run id, stamped into every layer, so a run is one grep. |
| `audit.py` | Append-only, hash-chained. `verify()` names the broken link. |
| `budget.py` | The reservation ledger. |
| `proc.py` | Process groups, zombie-aware liveness, orphan reaping. |
| `pool.py` | Subject lanes, bounded pool, size that heals itself. |
| `codex.py` | The app-server client. |
| `tracker.py` | Cairn, via its CLI — for the offline outbox and ownership generations. |
| `alert.py` | Discord/Slack out, fingerprinted so it is not noise. |
| `wake.py` | Signed, deduped, durable-before-ack wakes on loopback. |
| `runner.py` | One run, door to record. Group killed and budget settled in `finally`. |
| `config.py` | The whole surface. *If a setting cannot have a correct unattended default, it does not get to be a setting.* |
| `lifecycle.py` | One instance, unclean-death detection, the 75/78 exit contract. |
| `retention.py` | The janitor. Written the same day as the writers. |
| `cronspec.py` | Five fields, in a named timezone, with cron's real OR rule. |
| `schedule.py` | Jobs, the heartbeat, and the guards that stop it spamming. |
| `daemon.py` | Four loops: listen, drain, schedule, sweep. |

`bothy/vendor/a2a_reactor/` is vendored verbatim from
[a2a-comms](https://github.com/montytorr/a2a-comms) (MIT) — event triage,
semantic dedupe, turn budgets, and a process-wide lease. Do not edit it; fixes
go upstream.

## Status

**P0, P1 and P2**, proven end to end against a real Codex app-server.

A signed webhook arrives on loopback, is verified and deduped, hits disk before
the `202`, and becomes a supervised Codex run whose lane, slot and budget were
all granted together — then settles, records itself in Cairn, and lands in a
hash-chained audit log you can replay by run id. Two subjects run at once; a
third on a busy subject is turned away by name. Killed with `SIGKILL`, it leaves
orphans exactly as physics requires and reaps them on the next start, releasing
their budget in the same pass.

It also wakes *itself*. Given a heartbeat job and a standing checklist, it
fired on schedule, ran, worked through the checklist, hit a real sandbox
limitation and reported it — rather than failing quietly, and rather than
saying something when there was nothing to say.

Not yet: inbound chat (P3), launchd and `tailscale serve` packaging (P4).

## Licence

MIT.
