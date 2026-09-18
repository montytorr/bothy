# The rules Bothy is built to

Every rule here was bought with somebody's outage. Most were paid for by systems
far larger than this one — an agent runtime that had been running unattended for
months, a message broker maintained by a company for a decade, a voice-assistant
protocol that outlived the company that wrote it. A few were paid for during
Bothy's own construction, and those say so.

They are ordered by how much pain each one prevented.

---

## 1. Own the whole process tree. A restart replaces; it never adds.

A Codex app-server is not one process. The entry point is a Node shim that execs
a Rust binary, and some runs add a third child. Signalling the shim leaves the
rest alive.

Kill the **group**, not the process. Refuse to start if a previous instance's
children survive — the alternative logs *"found left-over process … ignoring"*,
and ignoring is how five duplicate listeners once accumulated and fought over one
session until the oldest silently won.

Count from your own record, never from `pgrep`: matching a command line catches
the investigator's own shell. *(This bit during Bothy's own build: a leftover-process
check matched the very command running it.)*

> Thirteen orphaned app-servers, ~900 MB, found alive on one host. One documented
> runaway left 196 orphaned children and $1,193 of already-spent work.

## 2. Health timeouts come from measured cold-start times. A failed probe alone never restarts anything.

A 30-second health window against a 75-second boot cost 58 minutes of total
outage: every *successful* restart was reported as a failure, and the retry loop
killed each new process mid-boot until the supervisor's rate limit latched.

Classify with two independent signals, and have **three** outcomes — healthy,
unknown, proven-down. Only proven-down may license a destructive recovery.

## 3. "I cannot tell" is a third state. It must never be filed under "busy".

A guard that cannot distinguish busy from dead will protect a corpse. One
produced 181 identical deferrals over 58 minutes because "status unavailable" was
classified as "work in progress".

## 4. Every protective exemption carries an expiry.

"Never restart a channel with an active run" was correct and unbounded, so one
hung run pinned its channel healthy forever — trading aborted replies for a
permanently dead channel. Bound it, and when the exemption's input is missing,
choose a direction deliberately and write down which.

## 5. Liveness is proven, never assumed.

Not a PID file, not a file existing, not a string in a log, not `NRestarts`
(an external restart resets it). A request, a response, and an `as_of`.

*Bothy's own version of this bug:* `killpg(pgid, 0)` succeeds on an **unreaped
zombie**, so the first group killer sat out its entire grace period against a
corpse and reported "unkillable". Liveness now enumerates `/proc` and skips the
dead.

## 6. A signal with no consumer is storage, not observability.

Execution heartbeats were written for five months and read only by a render-time
predicate in a file named `*-ui.ts`. Five runs had been "running" for 163 days,
three still holding their task hostage.

If you cannot name the process that reads a field, delete the field. And a
sweeper is not shipped until it is in the deploy manifest and observed running.

## 7. Every exclusive slot needs exactly one automatic path back to empty.

A lease, a claim, a writer lock, a budget reservation. Prove the holder alive
with PID **and** boot id **and** start time. A lock whose only release is the
holder's cooperation is a deadlock waiting for a crash.

**Money is an exclusive slot too.** That is why Bothy *reserves* budget at
admission rather than measuring it afterwards: with several runs in flight,
five can each observe "$40 of $50 used" and all five start.

## 8. Decide whether you offer resumption or prevention. Never test for the one you lack.

An acceptance criterion once read "an interrupted turn still delivers" — which
tests for durable resumption the architecture did not have. If work must survive
a restart it lives *outside* the restartable process. If you only offer
prevention, the fence protecting live work must be bounded (rule 4).

## 9. Exit code 0 is not proof of work.

Consume the input only on a **verified effect**. A credential-blocked worker
exited 0 and its event was marked processed.

## 10. `fsync`, then acknowledge.

A mature broker shipped a local buffer returning `202 Accepted` as its headline
1.0 feature and deleted it years later — 2,676 lines and the status code with it
— because a buffer that had not reached disk could not deliver the durability the
response promised.

The corollary: **a replay is answered 200, not 4xx.** The sender did nothing
wrong, and a 4xx makes it retry.

## 11. Record facts when you know them. Never re-derive them by inference later.

Two individually-correct changes once removed the evidence and then asked the
question it answered. Sniff the format; never trust the label. Emit your own
breadcrumbs rather than parsing prose to discover what your own tool did.

## 12. A partial write is a failure. A check that cannot find its subject must fail, not skip.

42 of 42 sessions were half-records while every watched signal stayed green,
because a row count never drops. Count the expensive half separately. One
guardrail had been `SKIP`ping daily since a backend migration and exiting 0.

## 13. Measure health on the work product, per actor, against a scaled baseline — and stay silent on success.

`/health` stayed green through two days of recording nothing. Ask "is output
still being produced, by whom, of the right shape". A daily all-clear teaches
everyone to ignore the channel.

## 14. Identity is per-runtime. Derive it from the process; never default to a shared credential.

Per-user keys cannot work when one runtime runs as two users and two runtimes
share one. Every runtime on one host wrote as the same identity for weeks, and
an unused credential is itself a bug worth alarming on.

## 15. Make correct behaviour a side effect of the work. Make the wrong state unrepresentable.

36% of closed tasks were never claimed despite the rule being documented for
weeks. *"A third paragraph telling agents to try harder would have been the third
version of the same non-fix."*

## 16. A verification claim must name its artefact and its command.

"E2E proven" was recorded twice and was not true. Verify the deployed bundle, not
the deploy's success report. **Assert the test suite actually ran** — fifteen
tests once sat dead because the launcher was a shim whose module was never
installed. Record the gap you did *not* close beside the claim.

## 17. Safety-critical config is code: enforce it by event *and* timer.

Events are lossy across restarts; timers are not. Back up before every repair,
log the exact key repaired, and offer a repair-without-acting mode.

## 18. Derive every inventory from the running system.

A migration list built from one systemd scope left the main agent gateway
unmigrated while 23/23 units reported active — it was a `--user` unit. Monitors
are created and destroyed *with* the job; an orphaned monitor trains people to
ignore a channel.

## 19. Secrets reach a process by 0600 file or secret store. Never inline in a unit, a doc, or a config an agent can read.

`chmod` does not un-expose: exposure implies tracked rotation. One tracker still
read `"initial"` for all eight keys seven months on.

*Bothy's own version:* the installer created the secrets file `0600 root:root`
while the service ran as an ordinary user, so the daemon could not read its own
environment and died with a bare "Permission denied" two layers below anything
that mentioned Bothy.

## 20. Roll back to an immutable digest captured before the change.

A failed deploy put the new build live, because the rollback started "whatever is
in the tag" and the build had already replaced it. A rollback that worked must
still report the original failure.

## 21. Every outbound call has a deadline, and a pre-decided behaviour past it.

Queue-and-replay for observations; fail fast for anything allocating identity or
ownership. Prefer duplicates over loss for facts; prefer loss over ambiguity for
claims.

## 22. Timezone-aware end to end. Parse by name, never positionally.

Three separate incidents: `mktime` on a `Z` timestamp shifted a 20-second-old
reply into looking idle and it was aborted; `systemctl --value` field reordering
broke a probe; a field rename across a repo boundary silently reinstated old
behaviour. Pin every cross-component wire format with a test, including a legacy
payload.

## 23. Retention and resource ceilings are written the same day as the writer.

One agent's state directory reached 600 MB with a **single** session rollout of
275 MB, unbounded and append-only. Measure CPU pressure, not free memory — one
host-wide stall was 41% CPU PSI with 52 of 62 GiB free.

## 24. Test recovery on a schedule, against a disposable target, from where the client lives.

Two gateway fixes were finally proven on a disposable canary unit and a
disposable channel, never on the live one. Two bridges looked green from the host
while being unreachable from the containers that were their only clients.

## 25. Absence of a success record is the alarm.

One cron job had been failing every 60 seconds against a deleted file — 706 times
— entirely unnoticed, because the alarm was defined as the presence of an error
nobody grepped for. Debounce and require N consecutive before paging. Never let
an observability failure take down the thing being observed.

## 26. Every path that requeues work must advance a counter that eventually gives up.

A claim that never spawned spun for two hours across eleven reclaims because the
reclaim path bypassed the failure counter.

## 27. Ship a hard spend cap on day one.

The closest prior art to Bothy has none. A forensic post-mortem of a single
instruction records **19 hours, 1,393 subagents, 93,284 API calls, $19,302.59** —
58% of it *cache writes*, because child runs inherited a cache TTL meant for a
long-running parent.

## 28. An unattended install is a feature, not packaging.

That same project is effectively undeployable to a third party: a 3,945-line
installer that reads `/dev/tty` so even a piped shell gets a wizard, a setup
command that refuses to run headless, no `--yes`, and 939 config keys with no way
to ship one vetted profile to many sites. Its own maintainers name this as the
main obstacle to handing it to anyone.

**Bothy's corollary:** if a setting cannot have a default that is correct at a
client site with nobody watching, it does not get to be a setting.

---

## Rules learned building Bothy itself

These cost nothing but an afternoon, which is the point of writing them down.

- **A zombie satisfies `killpg(pgid, 0)`.** Liveness by cheap syscall lied.
- **A refusal must not manufacture an incident.** Detecting an unclean death and
  *claiming* the ledger in one step meant a correctly-refused start faked a
  phantom death for the next one. An alert channel that cries wolf gets muted.
- **Recovery must be paced by recent outcomes, not the whole window.** A time
  window alone pinned the worker pool at one for fifteen minutes after a burst,
  making "degraded" indistinguishable from "hung".
- **Two service managers can need opposite handling of the same thing.** systemd
  reads exit 78 via `RestartPreventExitStatus`; launchd cannot, so it needs the
  code translated. Translating for *both* defeated the mechanism it was imitating,
  because `Restart=always` restarts on a clean exit too.
- **A patch that silently does not apply leaves a test measuring the old
  behaviour.** A string replacement targeted text that did not exist; the test
  afterwards "passed". Assert the target matched.
- **Gating a capability must not be what grants it.** Listing an MCP tool as
  "ask for approval" while excluding it from the allowed set is a contradiction —
  resolving it either way silently is worse than refusing it.
- **State the number from the run, not from memory.** The README claimed 184
  tests when the suite collected 176.
