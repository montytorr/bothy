# Changelog

Kept because the closest prior art to Bothy has none, is at config schema version
45 with ≥44 unenumerated migrations, and its single piece of public user pushback
— *"users increasingly have to track every version closely just to avoid workflow
regressions"* — sat unanswered for three months.

Every release states its **Breaking Changes** heading even when it is empty.

## Unreleased

### Added
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
- `Alerter.deliver` and a delivery result from the webhook sinks, so a caller
  that must be heard can tell a message that arrived from one that did not.
- **Hash-chained audit log**, verifiable across rotation, joinable by one run id.
- **Two doors:** tailnet-only routes and public routes on separate ports, with
  tailscaled-verified callers and rate limiting ahead of signature checks.
- **Headless install** for launchd and systemd, with the 75/78 exit contract.
- 254 tests, standard library only.

### Breaking Changes
- None. First release.

### Known gaps
- Drove runs and attention forwarding are tested against a stand-in for `drove`
  that speaks the shapes in Drove's README. No run has yet been started by
  Bothy on a real herdr, and no item has reached a real channel.
- `test_proc` needs `/proc` and fails on macOS (two tests); that is not new.
- No live Slack workspace or Discord application has been connected.
- Not yet run on macOS.
- `bwrap` cannot start inside a nested container, so runs needing a shell fail
  there; `bothy doctor` reports it rather than leaving a heartbeat to find out.
