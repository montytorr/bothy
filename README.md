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

## Putting it on a machine

One command. No prompts, no TTY reads, idempotent, non-zero on failure — so it
works the same over ssh, in a script, and from a deployment tool.

```bash
sudo bin/bothy install --dry-run          # exactly what it will touch
sudo bin/bothy install                    # launchd on macOS, systemd on Linux
sudo bin/bothy install --serve            # and expose it to the tailnet
```

Reachability is `tailscale serve`'s job: it terminates TLS with the tailnet's
own certificate, refuses anyone outside, and forwards to loopback. There is no
public listener to misconfigure and no certificate for Bothy to own.

With `require_tailnet` on, Bothy also asks the **local tailscaled daemon** who
is calling, rather than believing a forwarded header — a header is a claim, the
daemon is the proof:

```json
"require_tailnet": true,
"tailnet_allow_logins": ["you@example.com"]
```

Tailnet identity says who *connected*; a webhook signature says who *wrote the
payload*. Both are checked, because they are different questions.

**The exit code is the contract, and the two service managers need opposite
handling of it.** systemd reads `78` directly via `RestartPreventExitStatus`.
launchd has no equivalent, so there the wrapper translates `78` into a clean
exit that `SuccessfulExit=false` reads as "do not restart". Getting this wrong
is subtle: an earlier version translated for *both*, and since the unit also
says `Restart=always` — which restarts on a clean exit too — systemd cheerfully
restarted the very thing that had just said restarting cannot help.

Proven on a real install, not rendered and eyeballed:

```
installed as a systemd service            active
signed webhook -> supervised Codex run    audit shows the full chain
secret removed                            status=78, NRestarts=0  (stops, does not loop)
secret restored                           active again
uninstalled                               no unit, no wrapper, no process
                                          secrets and state left alone
```

The wrapper carries the three things a service definition cannot express:
waiting for the tailscaled socket (launchd has no ordering graph, and systemd's
`After=` waits for a unit to be *active*, not for its socket to be *usable*),
reading secrets from a `0600` file (launchd has no `EnvironmentFile`, and a
plist is world-readable), and log rotation (launchd does none).

## Webhooks from the outside world

A tailnet-only box cannot receive a GitHub webhook — the sender has no tailnet
identity. **Tailscale Funnel is per-port, not per-path**: the serve config keys
`AllowFunnel` on `SNI:port` with no path dimension, and whichever of `serve` or
`funnel` ran last flips the *whole* port. So Bothy runs **two listeners**:

```json
"port":        18795,     // tailnet door — a port Funnel is not allowed to touch
"public_port": 8788,      // where Bothy listens for third parties
"funnel_port": 443,       // where tailscaled listens for the public
"routes": [
  { "path": "/ops",         "secret_env": "OPS_SECRET" },
  { "path": "/hook/github", "secret_env": "GH_SECRET", "public": true }
]
```

```bash
bin/bothy funnel            # the exact tailscale commands this implies
bin/bothy funnel --apply    # run them
```

`public_port` and `funnel_port` are different on purpose: tailscaled binds the
privileged one, Bothy never does. The daemon **refuses to start** if they are
confused, if a public route has no public port, or if the two doors share one.

Proven — same daemon, four requests:

```
tailnet :18795  /ops          202     operational route
tailnet :18795  /hook/github  404     the public route is not here
public  :8788   /hook/github  202     the third-party door
public  :8788   /ops          404     operational route is not exposed
```

Defence in depth on top: tailscaled sets `Tailscale-Funnel-Request: ?1` on
public traffic and **it cannot be forged either way** — it deletes any
client-supplied copy before setting it from connection context. A tailnet-only
route refuses any request carrying it. Note this is the right signal to gate on:
identity headers are *also* absent for **tagged** devices, so "no identity
header" would wrongly classify your own nodes as public.

**Assume the URL is public knowledge.** Enabling HTTPS publishes the hostname to
certificate transparency logs — a single crt.sh query returns thousands of
`.ts.net` names across a thousand tailnets. The hostname is not a secret and the
path is not a secret; the HMAC signature is the entire security boundary, which
is what signatures are for. Funnel also gives you **no DoS protection you
control** — every request on a mounted path reaches your process — which is why
the rate limiter runs before signature verification.

## Or keep the box sealed entirely

Polling reaches *out* on a schedule, so nothing has to reach *in*. No public
listener, nothing in certificate transparency logs as a live endpoint, no
unauthenticated path to defend, no signature to get wrong, no DoS vector at all.
It also survives the machine being off: events queue at the provider and you
catch up, where a missed webhook depends on the sender's retry policy.

```json
"poll_sources": [{
  "name": "commits", "url": "https://api.github.com/repos/you/repo/commits",
  "interval_seconds": 120, "id_path": "sha",
  "subject_from": "sha", "subject_prefix": "commit-",
  "header_env": { "Authorization": "GH_POLL_TOKEN" }
}]
```

```bash
bin/bothy poll commits     # by hand: does it answer, authenticate, and what is new
```

Proven against the real GitHub API:

```
first poll    http=200  fetched=9  new=9  dup=0
second poll   http=304  fetched=0  new=0  dup=0     ← costs no quota
after restart           fetched=9  new=0  dup=9     ← seen ids survived
```

**Conditional requests are the whole economy.** With `ETag` and
`Last-Modified`, an unchanged poll costs a 304 and nothing else — a poller that
ignores them is why people believe polling is expensive.

**Polling is at-least-once too, and more obviously so.** A webhook is
redelivered when an ack is missed; a poller re-reads an overlapping window every
time, so duplicates are the *normal* case. Dedupe is on the item's own id and
persisted, because replaying work in an agent harness means spending money twice.

A failing source backs off on its own and never stalls the others. A missing
credential is reported as a configuration problem — visible, not retried forever.

The cost is honest: latency in seconds to minutes, API quota, and state to keep.

## Talking to it

Slack, over **Socket Mode** — an *outbound* WebSocket. No public URL, no inbound
port, no TLS certificate to own, no request signatures to verify. It works
unchanged on a machine that accepts no connections at all, which is the whole
deployment story.

```json
"slack_app_token_env": "SLACK_APP_TOKEN",
"slack_bot_token_env": "SLACK_BOT_TOKEN",
"slack_allow_from": ["U012ABCDEF"],
"slack_profile": "selfcare"
```

**Deny by default, and it refuses to start without an allowlist.** An agent that
runs commands on a client's machine should not take instructions from anyone who
can find its channel, and "the bot is only in a private channel" is a
configuration nobody audits.

A Slack message becomes a wake on the same durable queue as a webhook, so it is
admitted, budgeted and lane-serialised identically. Lanes are keyed on the
**conversation** — channel plus thread — so a follow-up joins the run already
working on it instead of starting a second agent on the same discussion.

An envelope is acknowledged **only once it is durably stored**. Slack redelivers
what it has not seen acknowledged, and that is a safety net worth keeping: an
ack for something we then lost is exactly the bug the rule exists to prevent.

The WebSocket client is ours — a few hundred lines of RFC 6455, verified against
the RFC's own example frames, because a test built from the same
misunderstanding as the code will agree with it happily. That keeps the
zero-dependency promise, which is worth more at a client site than it costs
here.

## Capability is per job, not per install

A worker starts with nothing beyond Codex's built-ins. Everything else comes
from a named **profile** attached to the job:

```json
"profiles": {
  "mail": {
    "mcp_servers": { "gmail": { "command": "mcp-gmail", "args": ["--readonly"],
                                "env_vars": ["GMAIL_TOKEN"] } },
    "tools": ["bothy_note"], "sandbox": "readOnly"
  },
  "selfcare": {
    "tools": ["bothy_checklist_read", "bothy_checklist_update", "bothy_status"]
  }
}
```

A mail job gets mail; a code-review job does not. It is nearly free, because
every run already has its own `CODEX_HOME` — so scoping capability to a run
costs a generated file rather than an architecture. The config is **generated,
never inherited**: a worker shaped by the host's own `config.toml` behaves
differently on a client's machine than it did on yours, and that difference is
found in the field rather than in a test. **No secret is ever written to it** —
an MCP server names the environment variable holding its token.

Three kinds of capability, and they are genuinely different:

| | what it is |
|---|---|
| **MCP servers** | an external process or endpoint — mail, a browser, anything |
| **skill roots** | prose the model reads. Behaviour, not mechanism |
| **dynamic tools** | a tool **Bothy hosts itself**, declared on `thread/start` and answered over the same connection — no subprocess, no port, no credential |

The third is how the agent maintains its own standing checklist, which the
heartbeat design always assumed and nothing previously implemented. Verified:
given `bothy_checklist_update`, a run read its checklist, added an item about
verifying the audit chain, and the file on disk changed.

**This is also the prompt-injection surface.** Bothy's wake prompt already tells
the model a payload is untrusted, because a signature authenticates the sender
and never the content. The moment a worker can also reach a mailbox, that
sentence stops being advice. Profiles keep the chain short: the job that reads
webhooks is not the job that can send mail, and a job that names no profile gets
nothing at all.

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
| `capability.py` | Profiles, generated worker config, and the tools Bothy hosts. |
| `ws.py` | RFC 6455 client. No dependency, no server role, no extensions. |
| `slack.py` | Socket Mode in, Web API out, deny by default. |
| `tailnet.py` | Who is calling, asked of tailscaled rather than of a header. |
| `install.py` | launchd, systemd, the wrapper, and the exit-code contract. |
| `ratelimit.py` | Token buckets, per route and per caller, before the HMAC. |
| `daemon.py` | Four loops: listen, drain, schedule, sweep. |

`bothy/vendor/a2a_reactor/` is vendored verbatim from
[a2a-comms](https://github.com/montytorr/a2a-comms) (MIT) — event triage,
semantic dedupe, turn budgets, and a process-wide lease. Do not edit it; fixes
go upstream.

## Status

**P0 through P4**, proven end to end against a real Codex app-server and a real
service install.

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

It talks: Slack over Socket Mode, deny-by-default, replies landing in the thread
they came from. And it installs: one headless command, tailnet-only ingress,
tailscaled-verified callers, and an exit-code contract both service managers
honour.

Not yet: Discord inbound, and a live Slack workspace has never been connected —
the framing, ack discipline and allowlist are proven, the credentials path is
not.

## Licence

MIT.
