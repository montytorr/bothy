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
- **Hash-chained audit log**, verifiable across rotation, joinable by one run id.
- **Two doors:** tailnet-only routes and public routes on separate ports, with
  tailscaled-verified callers and rate limiting ahead of signature checks.
- **Headless install** for launchd and systemd, with the 75/78 exit contract.
- 191 tests, standard library only.

### Breaking Changes
- None. First release.

### Known gaps
- No live Slack workspace or Discord application has been connected.
- Not yet run on macOS.
- `bwrap` cannot start inside a nested container, so runs needing a shell fail
  there; `bothy doctor` reports it rather than leaving a heartbeat to find out.
