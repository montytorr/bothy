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

## The worker

| Key | Default | |
|---|---|---|
| `codex_binary` | `codex` | |
| `sandbox` | `readOnly` | The actual boundary. Widen deliberately |
| `approval_policy` | `never` | *Do not stop to ask* — not *allow anything* |
| `wall_clock_seconds` | `900` | Enforced with `turn/interrupt`, on a monotonic clock |
| `codex_credentials_dir` | `~/.codex` | Where `auth.json` is copied from |
| `codex_inherit_config` | `false` | Inheriting the host's config makes a client behave unlike your test |

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
| `routes[]` | `[]` | `{path, secret_env, subject_from, subject_prefix, profile, public}` |

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
| `profiles{}` | `{}` | Named bundles: `mcp_servers`, `mcp_tools`, `mcp_ask`, `tools`, `skill_roots`, `sandbox`, `model` |
| `default_profile` | `null` | A job naming none gets **nothing** beyond Codex's built-ins |

`mcp_tools` scopes which of a server's tools are enabled; `mcp_ask` pins
individual ones to `approval_mode = always`. Naming a tool in `mcp_ask` that is
absent from `mcp_tools` is refused — gating a capability must not be what grants
it.

## Alerting

| Key | Default | |
|---|---|---|
| `discord_webhook_url` | `null` | Outbound only; no bot needed |
| `slack_webhook_url` | `null` | |

## Housekeeping

| Key | Default | |
|---|---|---|
| `janitor_interval_seconds` | `300` | |
| `max_wake_attempts` | `30` | Then abandoned, visibly |
| `max_wake_age_seconds` | `21600` | Acting on a six-hour-old webhook can be worse than not acting |
