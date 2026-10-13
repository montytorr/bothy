# Configuration

One file: `$BOTHY_HOME/config.json`. Environment variables override it
(`BOTHY_PORT`, `BOTHY_SITE`, …), and arguments override both.

**The rule this surface is held to:** if a setting cannot have a default that is
correct at a client site with nobody watching, it does not get to be a setting.
Everything below has one.

**No secrets live here.** Anything sensitive is named by the environment variable
that holds it, and the value is read at startup from a `0600` file.

## Identity and paths

| Key | Default | |
|---|---|---|
| `site` | `bothy` | Names this deployment in alerts and the service label |
| `home` | `~/.bothy` | State directory |
| `workspace` | cwd | Where workers run |

## Standard operating procedures

A scheduled job can carry a procedure written in MUST / SHOULD / MAY form:
`bothy schedule add triage --sop triage ...`. The name is bare (`^[a-z0-9][a-z0-9-]{0,63}$`)
and resolves to `$BOTHY_HOME/sops/<name>.md`; the resolved path must stay inside
that directory, so a symlink out is refused. It is trusted text, written by an
operator: it is injected into the job's prompt at fire time (not fenced as
untrusted) and its sha256 is recorded on the wake. The agent has no tool that
writes it. `bothy sop list|show|set NAME [FILE]` manages them (`set` reads stdin
or a file, and writes atomically).

A SOP that is missing, empty or over 16 KB at fire time is never truncated or
skipped: the wake is recorded as processed with `outcome: refused`,
`gate: sop_missing`, audited and alerted, no run starts, and the job moves on to
its next time. `docs/sops/drove-detached-start.md` is a worked example for a
job that reports on Drove runs.

## The worker

| Key | Default | |
|---|---|---|
| `codex_binary` | `codex` | |
| `sandbox` | `readOnly` | The actual boundary. Widen deliberately |
| `approval_policy` | `never` | *Do not stop to ask* — not *allow anything* |
| `wall_clock_seconds` | `900` | Enforced with `turn/interrupt`, on a monotonic clock |
| `max_tokens` | `null` | Interrupt when cumulative input + output tokens reach the cap |
| `codex_credentials_dir` | `~/.codex` | Where `auth.json` is copied from |
| `codex_inherit_config` | `false` | Inheriting the host's config makes a client behave unlike your test |

Jobs can set `max_tokens` and `wall_clock_seconds`; unset values fall back to
the named profile's values, then these worker defaults. `bothy run` and
`bothy schedule add` accept `--max-tokens N` and `--wall-clock SECONDS`.
Token limits must be positive integers; wall clocks must be positive and finite.
Usage arrives in updates, so a token cap can overshoot by an update. API runs
also interrupt when their metered spend exceeds the reservation made at admission.

## Concurrency and money

| Key | Default | |
|---|---|---|
| `pool.max_workers` | `2` | Not 1: a pool that only ever runs one job is untested concurrency |
| `budget.mode` | `subscription` | or `api` |
| `budget.per_run_usd` | `2.0` | The **worst case**, reserved at admission |
| `budget.per_day_usd` | `25.0` | |
| `budget.per_run_percent` | `2.0` | Subscription mode |
| `budget.ceiling_percent` | `85.0` | Of the rate-limit window |
| `budget.reservation_ttl_seconds` | `3600` | A crashed run's reservation expires |

## Ingress

| Key | Default | |
|---|---|---|
| `port` | `8787` | The tailnet door. Keep it outside 443/8443/10000 so Funnel cannot touch it |
| `public_port` | `null` | Where **Bothy** listens for third parties |
| `funnel_port` | `443` | Where **tailscaled** listens. Different on purpose |
| `require_tailnet` | `false` | Verify callers against the local tailscaled |
| `tailnet_allow_nodes` | `[]` | `tag:bothy` preferred; a StableID or node name also works |
| `tailnet_allow_logins` | `[]` | Cannot be combined with tagged devices — they have no login |
| `max_skew_seconds` | `300` | Signature timestamp window |
| `routes[]` | `[]` | `{path, secret_env, subject_from, subject_prefix, profile, public, drove}` |

`subject_from` is what makes two webhooks about one thing share a lane instead of
racing.

## Reaching out instead

| Key | Default | |
|---|---|---|
| `poll_sources[]` | `[]` | `{name, url, interval_seconds, id_path, subject_from, header_env, …}` |

## Chat

| Key | Default | |
|---|---|---|
| `slack_app_token_env` / `slack_bot_token_env` | `null` | Socket Mode; no public URL |
| `slack_allow_from[]` | `[]` | **Deny by default.** Empty refuses to start |
| `discord_bot_token_env` | `null` | Gateway |
| `discord_allow_from[]` | `[]` | **Deny by default.** Empty refuses to start |
| `discord_channels[]` | `[]` | Empty means any channel the bot can see |
| `discord_message_content` | `true` | A privileged intent; without it messages arrive empty |

## Capability

| Key | Default | |
|---|---|---|
| `profiles{}` | `{}` | Named bundles: `mcp_servers`, `mcp_tools`, `mcp_ask`, `approvals`, `tools`, `skill_roots`, `sandbox`, `model`, `max_tokens`, `wall_clock_seconds` |
| `default_profile` | `null` | A job naming none gets **nothing** beyond Codex's built-ins |

`mcp_tools` scopes which of a server's tools are enabled; `mcp_ask` pins
individual ones to `approval_mode = always`. Naming a tool in `mcp_ask` that is
absent from `mcp_tools` is refused — gating a capability must not be what grants
it.

### Approval rules

A profile's `approvals` list answers "may I?" for a tool, instead of the old
decline-everything default. Each rule is an object:

| Key | Required | |
|---|---|---|
| `server` | yes | An MCP server the profile grants, or `bothy` for a tool Bothy hosts (listed in `tools`) |
| `tool` | yes | Must be enabled by the profile (`mcp_tools`, or `tools` for `bothy`); a rule that names anything else is refused |
| `verdict` | yes | `proceed`, `deny`, `guide` or `confirm` |
| `reason` | `guide` only | `guide` declines **and tells the model the reason**, so it is required. Optional text for the others |
| `unless_approved_within` | no, `confirm` only | Seconds. A human approval this recent lets the call through |

| Verdict | Effect |
|---|---|
| `proceed` | The call runs |
| `deny` | Declined, with `reason` |
| `guide` | Declined; `reason` reaches the model on the wire |
| `confirm` | Declined as "awaiting human confirmation" and audited with `status: awaiting_human`, unless a human approved this server and tool within `unless_approved_within` seconds |

```json
{"server": "mail", "tool": "send_message", "verdict": "confirm",
 "unless_approved_within": 900, "reason": "outbound mail needs a person"}
```

Any verdict other than `proceed` renders `approval_mode = "always"` for that
tool, so Codex asks Bothy. A tool with no rule that Codex asks about is declined,
as before, but now audited. A declined `mcp_ask` tool also raises an alert.
Every decision is a `kind: approval` audit record (`action` is the verdict,
`status` is `ok`, `refused` or `awaiting_human`, `data` is
`{method, server, tool, rule, reason}`). Rules for `bothy` tools are audited when
they match.

`confirm` finds the human's approval in the audit log: the newest `kind:
interrupt`, `action: answered` record with `data.decision: "approve"` and the
same `data.server` and `data.tool`. Without a window, or with no such record,
the call stays declined. If the log cannot be read, the call is **denied**.
Nothing writes that record yet; the chat hold in BOTHY-31 will.

Unknown keys are refused at load, in the config and in every profile, naming the
key. `max_tokens` and `wall_clock_seconds` are profile keys too: per-profile
limits that apply between a run's own limit and the config-wide one.

Webhook and polling runs cannot use `bothy_checklist_update` or `bothy_note`,
even when their profile lists those tools. These writes persist into later
runs, so allowing them requires `"allow_from_untrusted": true` in that profile
(default: `false`). Schedule, Slack and Discord runs keep the tools their
profile grants. Checklist replacements are atomic and record the before and after SHA-256
digests in the audit log, and heartbeat checklists are fenced as untrusted data.

A profile used by a public route must give every `mcp_servers` entry an explicit
list in `mcp_tools`, including when the route inherits `default_profile`.
Missing lists are a startup error; an empty list grants no tools.

## Alerting

| Key | Default | |
|---|---|---|
| `discord_webhook_url` | `null` | Outbound only; no bot needed |
| `slack_webhook_url` | `null` | |

## Drove

Drove's CLI, run as a plain process. No model turn is involved, so the worker
settings above (`sandbox`, `wall_clock_seconds`, `profiles`) do not apply to it,
and **its spend is not in Bothy's budget**: `drove_max_runs` is the cap.

| Key | Default | |
|---|---|---|
| `drove_binary` | `drove` | An absolute path is safest: a service's `PATH` is short |
| `drove_repo` | `null` | The repository a started run is bound to. **No default**: a daemon's working directory is Bothy's own checkout, so a start with no repo is refused as `no-repo` |
| `drove_home` | `null` | `DROVE_HOME` for every command Bothy runs. Unset leaves Drove's own default. A person answering items must use the same one |
| `drove_start_timeout_seconds` | `120` | Past it a start is reported as failed and is **not** retried: it may have made a run |
| `drove_run_deadline_seconds` | `21600` | How long Bothy watches a run it started. Past it Bothy says so and stops watching. It does not stop the run |
| `drove_poll_seconds` | `60` | How often a watched run's roster is read |
| `drove_max_runs` | `1` | Runs Bothy will have going at once. A start beyond it waits, counted, like any capacity refusal |
| `drove_attention_interval_seconds` | `null` | Not forwarded. A number polls `drove attention --all --json` that often and sends what needs a person to the webhook alerter |

`drove_attention_interval_seconds` without `discord_webhook_url` or
`slack_webhook_url` is refused at start: items would be read and told to nobody.

**Starting a run from a schedule:**

```bash
bothy schedule add nightly --kind cron --spec "0 2 * * *" --tz Europe/Paris --drove-next DROVE
bothy schedule add resume  --kind at --spec 2026-10-12T07:00:00+00:00 --drove-up drove-20261011-0200-a1b2
bothy schedule add one     --kind at --spec 2026-10-12T07:00:00+00:00 --drove-from-cairn DROVE-5
```

**Starting a run from a webhook:** a route may carry a `drove` action instead of
being a prompt.

```json
"routes": [
  { "path": "/drove/nightly", "secret_env": "DROVE_HOOK_SECRET",
    "drove": { "action": "plan", "next": "DROVE" } },
  { "path": "/drove/task", "secret_env": "DROVE_HOOK_SECRET",
    "drove": { "action": "plan", "from_cairn": { "payload": "task.ref" } } }
]
```

An action is `plan` with `from_cairn` (a Cairn ref), `next` (a project key), or
`up` with `run` (a run id), and optionally an absolute `repo`. The one thing a
payload may supply is **one value**, written `{"payload": "dotted.path"}`, and it
must be a plain ref, key or id that cannot begin with a dash. An action found
inside a payload is data and is never obeyed. A route whose payload has no valid
value answers `422` and queues nothing.

## Housekeeping

| Key | Default | |
|---|---|---|
| `janitor_interval_seconds` | `300` | |
| `max_wake_attempts` | `30` | Then abandoned, visibly |
| `max_wake_age_seconds` | `21600` | Acting on a six-hour-old webhook can be worse than not acting |
