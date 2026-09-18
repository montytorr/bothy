"""Every knob Bothy has, and a correct default for each.

THE RULE THIS FILE EXISTS TO ENFORCE: if a setting cannot have a default that is
right on an unattended machine with nobody watching, it does not get to be a
setting. The closest comparable project reached 939 configuration keys with no
way to ship one vetted profile to many sites, and its own maintainers name that
as the main obstacle to handing it to anyone else. Bothy is FOR handing to
someone else, so the config surface is a feature and staying small is the work.

Precedence is file, then environment, then explicit argument — the environment
wins over the file so a site can override one value without editing a profile
we shipped, and an argument wins over both so a test never has to mutate state.

Secrets are read from the environment or a mode-0600 file, never from this
config, and never inline in a service definition. A world-readable plist or unit
carrying a token is how ten credentials ended up needing rotation on the
reference host, where the tracker still reads "initial" seven months later.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from .budget import BudgetPolicy
from .pool import PoolPolicy

__all__ = ["Config", "load"]

ENV_PREFIX = "BOTHY_"


def _home() -> Path:
    return Path(os.environ.get(f"{ENV_PREFIX}HOME") or (Path.home() / ".bothy"))


@dataclasses.dataclass
class Config:
    """Bothy's whole configuration surface."""

    # where state lives
    home: Path = dataclasses.field(default_factory=_home)
    # identity, so an operator can tell two deployments apart in one Discord channel
    site: str = "bothy"
    # the wake listener. Loopback only, deliberately: reachability is
    # tailscale serve's job and there is no setting here that can expose it.
    port: int = 8787
    max_skew_seconds: int = 300
    # Inbound routes. Each entry names the ENV VAR holding its secret; the
    # secret itself never appears in this file, because a config an agent can
    # read is a config an agent can leak.
    routes: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    janitor_interval_seconds: float = 300.0
    # A SECOND listener, for routes a third party must reach. Separate because
    # Tailscale Funnel is per-PORT, not per-path: whichever of `serve` or
    # `funnel` ran last flips the whole port, so a public route sharing a port
    # with operational routes is one mistyped command from exposing everything.
    #
    # TWO DIFFERENT PORTS, and conflating them is easy. `public_port` is where
    # BOTHY listens, on loopback, and it is an ordinary high port needing no
    # privilege. `funnel_port` is where TAILSCALED listens for the public, and
    # it must be 443, 8443 or 10000 because those are the only ones Funnel is
    # permitted to use. tailscaled binds the privileged one; Bothy never does.
    public_port: int | None = None
    funnel_port: int = 443
    # Reaching OUT instead of being reached. A deployment that polls needs no
    # public ingress at all — no listener in certificate transparency logs, no
    # unauthenticated path to defend, nothing to flood.
    poll_sources: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Tailnet identity as a second gate in front of the listener. Off unless
    # switched on, because a box without tailscaled would otherwise refuse
    # everything and the failure would look like a Bothy bug.
    require_tailnet: bool = False
    tailnet_allow_logins: list[str] = dataclasses.field(default_factory=list)
    tailnet_allow_nodes: list[str] = dataclasses.field(default_factory=list)
    # the worker
    codex_binary: str = "codex"
    # Each run gets its own CODEX_HOME for SQLite isolation, which means each run
    # also needs its own copy of the credentials. Seeded from here.
    codex_credentials_dir: Path = dataclasses.field(default_factory=lambda: Path.home() / ".codex")
    # Whether a worker inherits the host's config.toml (model choice, MCP servers,
    # hooks). Off by default: a worker should be shaped by Bothy, not by whatever
    # the host account happens to have configured.
    codex_inherit_config: bool = False
    # Named capability bundles. A job names one; a job that names none gets
    # nothing beyond Codex's built-ins, which is the safe default and the
    # reason capability is opt-in rather than opt-out.
    profiles: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    default_profile: str | None = None
    workspace: Path = dataclasses.field(default_factory=Path.cwd)
    sandbox: str = "readOnly"
    approval_policy: str = "never"
    wall_clock_seconds: float = 900.0
    # concurrency and money
    pool: PoolPolicy = dataclasses.field(default_factory=PoolPolicy)
    budget: BudgetPolicy = dataclasses.field(default_factory=BudgetPolicy)
    # memory
    cairn_binary: str = "cairn"
    cairn_agent: str = "bothy"
    cairn_project: str | None = None
    # where a human finds out
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    # Two-way Slack, over Socket Mode: an OUTBOUND connection, so no public URL
    # and no inbound port. Token names, never tokens.
    slack_app_token_env: str | None = None     # xapp-..., scope connections:write
    slack_bot_token_env: str | None = None     # xoxb-...
    slack_allow_from: list[str] = dataclasses.field(default_factory=list)
    slack_profile: str | None = None
    slack_ack_emoji: str = "eyes"

    # ---- derived paths -------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def audit_path(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def budget_path(self) -> Path:
        return self.state_dir / "budget.json"

    @property
    def registry_path(self) -> Path:
        return self.state_dir / "workers.json"

    @property
    def wake_dir(self) -> Path:
        return self.state_dir / "wakes"

    @property
    def homes_dir(self) -> Path:
        """Per-worker CODEX_HOME directories.

        Under Bothy's own state directory and never under /tmp: Codex refuses to
        create its helper binaries in a temporary directory and says so on every
        start. Found on the very first smoke test.
        """
        return self.state_dir / "homes"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.state_dir, self.wake_dir, self.homes_dir):
            path.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form. Secrets are shown as presence, never as values."""
        return {
            "site": self.site,
            "home": str(self.home),
            "port": self.port,
            "workspace": str(self.workspace),
            "sandbox": self.sandbox,
            "approval_policy": self.approval_policy,
            "wall_clock_seconds": self.wall_clock_seconds,
            "pool": dataclasses.asdict(self.pool),
            "budget": dataclasses.asdict(self.budget),
            "codex_credentials": "present" if (self.codex_credentials_dir / "auth.json").exists() else "MISSING",
            "codex_inherit_config": self.codex_inherit_config,
            "require_tailnet": self.require_tailnet,
            "public_port": self.public_port,
            "poll_sources": [src.get("name") for src in self.poll_sources],
            "funnel_port": self.funnel_port,
            "slack_socket_mode": "configured" if (self.slack_app_token_env and self.slack_bot_token_env)
                                 else "not configured",
            "slack_allow_from": len(self.slack_allow_from),
            "profiles": sorted(self.profiles),
            "default_profile": self.default_profile,
            "routes": [r.get("path") for r in self.routes],
            "cairn_agent": self.cairn_agent,
            "cairn_project": self.cairn_project,
            "discord_webhook": "configured" if self.discord_webhook_url else "not configured",
            "slack_webhook": "configured" if self.slack_webhook_url else "not configured",
        }


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load(path: str | os.PathLike[str] | None = None, **overrides: Any) -> Config:
    """Build a Config from file, then environment, then arguments."""
    config = Config()
    source = Path(path) if path is not None else config.home / "config.json"

    raw: dict[str, Any] = {}
    if source.exists():
        try:
            loaded = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError) as exc:
            # A config we cannot parse is a startup error, not a shrug: running
            # with silently-default settings is how a site ends up unprotected.
            raise ValueError(f"{source} is not valid JSON: {exc}") from exc

    nested = {"pool": PoolPolicy, "budget": BudgetPolicy}
    for key, value in raw.items():
        if key in nested and isinstance(value, dict):
            current = dataclasses.asdict(getattr(config, key))
            current.update(value)
            setattr(config, key, nested[key](**current))
        elif hasattr(config, key):
            setattr(config, key, Path(value) if key in {"home", "workspace", "codex_credentials_dir"} else value)

    for field in dataclasses.fields(config):
        env_key = f"{ENV_PREFIX}{field.name.upper()}"
        if env_key in os.environ and field.name not in nested:
            value = os.environ[env_key]
            path_fields = {"home", "workspace", "codex_credentials_dir"}
            setattr(config, field.name, Path(value) if field.name in path_fields else _coerce(value))

    for key, value in overrides.items():
        if hasattr(config, key):
            setattr(config, key, value)

    return config
