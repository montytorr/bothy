# Changelog

Kept because the closest prior art to Bothy has none, is at config schema version
45 with ≥44 unenumerated migrations, and its single piece of public user pushback
— *"users increasingly have to track every version closely just to avoid workflow
regressions"* — sat unanswered for three months.

Every release states its **Breaking Changes** heading even when it is empty.

## Unreleased

### Security

- **Prompt injection protection (BOTHY-23):** webhook payloads cannot replace
  the prompt; payloads, poll items and heartbeat checklists use fresh nonce
  fences with explicit truncation notices. Webhook and poll runs lose shared
  memory writes unless their profile sets `allow_from_untrusted: true`;
  checklist writes record SHA-256 digests, and public routes require explicit
  MCP tool allowlists.

### Added
- **Scheduled goals (BOTHY-29):** jobs can validate a run with an argv command
  or final-output regex and retry up to five attempts. Failures feed bounded,
  untrusted evidence into the next prompt with durable backoff; success is
  `verified`, exhaustion or budget refusal is `unverified` and alerted. Each
  attempt keeps normal budget admission and settlement, and API jobs cannot
  declare attempts whose worst-case spend exceeds the daily cap.
- **Deferred wake backoff:** capacity and transient Drove refusals retry after
  4 seconds, doubling to a 64-second cap. Deadlines survive restarts, and new
  wakes do not consume a waiting wake's attempts.
- **SOP jobs:** `bothy schedule add --sop NAME` gives a job a MUST/SHOULD/MAY
  procedure from `$BOTHY_HOME/sops/NAME.md`, injected into its prompt at fire time
  with its sha256 on the wake. A missing, empty or oversized (over 16 KB) file is
  refused (`sop_missing`), audited and alerted rather than truncated. Manage with
  `bothy sop list|show|set`. Ships `docs/sops/drove-detached-start.md`.
- **Wake sources:** signed webhooks, schedules, Slack (Socket Mode), Discord
  (gateway) and polling — all producing wakes on one durable queue and passing
  one admission gate.
- **Supervised Codex** over `app-server` stdio: approvals answered from policy,
  live token metering, a monotonic wall clock enforced with `turn/interrupt`.
- **Budget reserved at admission**, in dollars (API key) or percent-of-window
  (subscription), with reservations that expire when a run dies.
- **Subject lanes and a self-healing worker pool** sized from the recent failure
  ratio.
- **Capability profiles:** MCP servers, individual MCP tools, per-tool approval
  gating, skill roots, and tools Bothy hosts itself over the same connection.
- **Questions reach a person.** Codex asks through `.../requestUserInput`, which
  is not a permission request. It was routed into the approval handler and
  declined in milliseconds with nobody told, so the one moment an agent reached
  for a human was the one moment it could not get one. Questions are now
  recorded on the run, alerted while the run is still going, and carried into
  the audit entry as `questions_unanswered`. The turn is told plainly that no
  answer is coming, which it can act on — unlike a bare refusal.
- **Drove runs, with no model turn.** A schedule (`bothy schedule add ...
  --drove-next PROJECT`, `--drove-from-cairn REF`, `--drove-up RUN`) or a signed
  webhook route with a `drove` action runs `drove plan|up ... --detached --json`
  as a plain process, records the outcome in the audit, and watches
  `drove roster` until the run is down or `drove_run_deadline_seconds` pass.
  A refusal such as `herdr-down` is a recorded refusal; the ones that can clear
  wait under the same counter and age limit as any other wake. A webhook may
  supply one validated value to an action and nothing else. Bothy never stops a
  run. `bothy drove start|status|attention` do the same by hand.
- **What needs a person reaches chat once.** `drove_attention_interval_seconds`
  polls `drove attention --all --json` and sends each item that needs a person
  to the Slack or Discord webhook exactly once across restarts, deduped on
  Drove's item id and stored before it is sent, with one follow-up when it stops
  waiting. A failed read never resolves anything. Items Drove addresses to its
  planner (`for: planner`, or `answer.type: planner` on an older Drove) are not
  sent. A crash between the send and its record sends that item again, which
  is documented rather than hidden.
- **Readable chat alerts for what needs a person (BOTHY-21).** It arrived as raw
  markdown: shouting markers, run and pane ids first, an ISO timestamp, Drove's
  internal item id, and no hint which project it was about. Discord now gets an
  embed and Slack gets blocks, both in one shape: a plain title with the project
  first ("dispofi-api · the planner stopped"), one sentence, a "What to do"
  field with the command in a code block, the run id, colour by severity, the
  time Drove first saw it, and "Bothy on <site>". The item id is no longer
  shown. Each Drove attention kind (dialog, permission, trust, hook-trust,
  channel-confirm, codex-version-mismatch, usage-limit, gone, question, asking,
  blocked, promote, pane-budget) has its own wording, and a kind Drove adds later
  falls back to the item's own text. Other sinks get the same words as plain text.
- **A daemon that only forwards Drove's attention starts (BOTHY-21).** With
  `drove_attention_interval_seconds` and a webhook set and nothing else, it
  exited 78 ("nothing to wake on") and launchd did not restart it. Forwarding to
  a webhook now counts as something to do; the interval with no webhook still
  does not, and an empty config still exits 78.
- **Bothy's own alerts are drawn the same way.** Jobs failing, a run going down,
  a refused Drove start, a lost loop pass, a dropped Slack or Discord connection
  and the heartbeat go through the same embed and blocks: "Bothy · the cairn
  poll is failing" (or the project first, for a Drove run), one sentence, a "What
  to do" field with the command in a code block, the raw error small under
  "Details". Repeats are still one message.
- **Every suggested `drove` command names its run.** A person runs it from their
  own terminal where no `DROVE_RUN` is set, so a command Drove wrote without
  `--run` gets `--run <id>` added before its first option
  (`drove send w-api --run <id> --kind answer <answer>`).
- **A resolved alert edits the original on Discord.** The post asks for the
  message back (`?wait=true`), its id is kept in `drove-attention.json`, and the
  resolution PATCHes it to a grey "Resolved" embed instead of posting a second
  message; if the original was deleted the resolution is posted fresh. Slack's
  incoming webhooks cannot edit, so a resolution there is a short grey follow-up.
- **Process groups on macOS.** Liveness, whole-tree kill and orphan reaping read
  `/proc`, which macOS lacks, so on a Mac every group answered "nobody is there":
  a kill reported `already-gone` without signalling anything and a restart could
  not find its orphans. The same table now comes from `ps`, with `kern.boottime`
  for the boot and `ps` start time for the pid fingerprint, and the lifecycle
  ledger reads the same boot id, so a reboot can be told from a crash there too.
  Linux is unchanged.
- **`bothy install --kind launchagent`**: a per-user LaunchAgent for a Mac you sit
  at. No sudo, loaded into your `gui/<uid>` domain, so it can reach your herdr
  server, your Keychain login and `~/.local/bin`, which a system LaunchDaemon
  cannot. `--dry-run` shows the PATH and interpreter it will write; `uninstall
  --kind launchagent` removes it. It starts at login, not at boot. The daemon
  stays the default for an unattended machine.
- **A schedule alone is a reason to run.** `bothy serve` refused to start with no
  webhook route and exited 78, which made a cron-only install stop. Routes,
  scheduled jobs, poll sources and chat each count.
- `Alerter.deliver` and a delivery result from the webhook sinks, so a caller
  that must be heard can tell a message that arrived from one that did not.
- **Hash-chained audit log**, verifiable across rotation, joinable by one run id.
- **Two doors:** tailnet-only routes and public routes on separate ports, with
  tailscaled-verified callers and rate limiting ahead of signature checks.
- **Headless install** for launchd and systemd, with the 75/78 exit contract.
- 271 tests, standard library only.

### Breaking Changes
- None. First release.

### Known gaps
- Drove runs and attention forwarding are tested against a stand-in for `drove`
  that speaks the shapes in Drove's README. No run has yet been started by
  Bothy on a real herdr, and no item has reached a real channel.
- No live Slack workspace or Discord application has been connected.
- Not yet run on macOS.
- `bwrap` cannot start inside a nested container, so runs needing a shell fail
  there; `bothy doctor` reports it rather than leaving a heartbeat to find out.
