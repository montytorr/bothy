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
import math
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
    question_hold_seconds: float = 0.0
    wall_clock_seconds: float = 900.0
    max_tokens: int | None = None
    # Optional observation export. Credentials belong in the OTEL environment.
    otlp_endpoint: str | None = None
    # concurrency and money
    pool: PoolPolicy = dataclasses.field(default_factory=PoolPolicy)
    budget: BudgetPolicy = dataclasses.field(default_factory=BudgetPolicy)
    # memory
    cairn_binary: str = "cairn"
    cairn_agent: str = "bothy"
    cairn_project: str | None = None
    # Read the task's Cairn digest into a --ref run's prompt at start.
    cairn_brief: bool = True
    # Daily memory uses the system's local zone unless a named zone is set.
    timezone: str | None = None
    memory_retention_days: int = 90
    # Drove, run as a plain process: no model turn, so none of the worker
    # settings above apply to it. A run is bound to the folder it is started in,
    # so there is deliberately no default repo: a daemon's working directory is
    # Bothy's own checkout, and guessing it would plan work against Bothy.
    drove_binary: str = "drove"
    drove_repo: str | None = None
    # Left unset, Drove uses its own default. Set it when a person answers items
    # as another user: every command that touches a run must share one.
    drove_home: str | None = None
    # A start returns at once in the normal case; this is the ceiling on one
    # that does not, after which the outcome is reported as unknown.
    drove_start_timeout_seconds: float = 120.0
    # How long Bothy watches a run it started. Past it, Bothy stops watching and
    # says so; it does not stop the run.
    drove_run_deadline_seconds: float = 21600.0
    drove_poll_seconds: float = 60.0
    # Drove's spend is not in Bothy's budget, so the number of runs it may have
    # going is the cap. One is the safe number on a machine nobody is watching.
    drove_max_runs: int = 1
    # null: not forwarded. Set to a number of seconds to send what needs a
    # person to the webhook alerter below, once each.
    drove_attention_interval_seconds: float | None = None
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
    # Two-way Discord, over the gateway. Also an OUTBOUND connection, so it
    # needs no ingress either. Token name, never the token.
    discord_bot_token_env: str | None = None
    discord_allow_from: list[str] = dataclasses.field(default_factory=list)
    discord_channels: list[str] = dataclasses.field(default_factory=list)
    discord_profile: str | None = None
    discord_message_content: bool = True

    # ---- derived paths -------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def sops_dir(self) -> Path:
        """Standard operating procedures: trusted text, outside the workspace."""
        return self.home / "sops"

    @property
    def memory_dir(self) -> Path:
        """Per-profile persona and notes, outside the worker's workspace."""
        return self.home / "memory"

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

    @property
    def control_dir(self) -> Path:
        """Durable operator controls, shared by daemon and one-shot runs."""
        return self.state_dir / "control"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.state_dir, self.wake_dir, self.homes_dir, self.control_dir):
            path.mkdir(parents=True, exist_ok=True)

    def validate_profiles(self) -> None:
        """Every profile must parse, so a bad one is a startup error and not a refused run."""
        from .capability import Profile

        if type(self.memory_retention_days) is not int or self.memory_retention_days < 1:
            raise ValueError("memory_retention_days must be an integer of at least 1")
        if self.timezone is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(self.timezone)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                raise ValueError("timezone must name a valid IANA timezone") from None
        if not isinstance(self.profiles, dict):
            raise ValueError("profiles must map profile names to objects")
        for name, raw in self.profiles.items():
            profile = Profile.from_dict(name, raw)
            if profile.memory != "off" and self.memory_dir.resolve().is_relative_to(self.workspace.resolve()):
                raise ValueError(f"profile {name}: memory directory must be outside the workspace; move BOTHY_HOME")

    def validate_public_profiles(self, routes: list[Any] | None = None) -> None:
        """Public input must never reach an MCP server with an implicit grant."""
        from .capability import Profile

        for route in self.routes if routes is None else routes:
            entry = route if isinstance(route, dict) else vars(route)
            if not entry.get("public"):
                continue
            name = entry.get("profile") or self.default_profile
            if not name:
                continue
            if name not in self.profiles:
                raise ValueError(f"public route {entry.get('path')}: no capability profile called {name!r}")
            profile = Profile.from_dict(name, self.profiles[name])
            for server in profile.mcp_servers:
                if not isinstance(profile.mcp_tools.get(server), list):
                    raise ValueError(
                        f"public route {entry.get('path')}: profile {name} grants MCP server "
                        f"{server!r} without an mcp_tools list; list the allowed tools explicitly"
                    )

    def validate_hold(self, wall_clock_seconds: float | None = None) -> None:
        hold = self.question_hold_seconds
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not math.isfinite(hold) or hold < 0:
            raise ValueError("question_hold_seconds must be finite and non-negative")
        if hold == 0:
            return
        wall = self.wall_clock_seconds if wall_clock_seconds is None else wall_clock_seconds
        if (isinstance(wall, bool) or not isinstance(wall, (int, float)) or not math.isfinite(wall)
                or wall <= 0 or hold + wall >= self.budget.reservation_ttl_seconds):
            raise ValueError("question hold plus wall clock must be under reservation_ttl_seconds")

    def validate_holds(self) -> None:
        self.validate_hold()
        if self.question_hold_seconds == 0:
            return
        for profile in self.profiles.values():
            if profile.get("wall_clock_seconds") is not None:
                self.validate_hold(profile["wall_clock_seconds"])
        for entry in [*self.routes, *self.poll_sources]:
            if entry.get("wall_clock_seconds") is not None:
                self.validate_hold(entry["wall_clock_seconds"])
        path = self.state_dir / "jobs.json"
        if path.exists():
            try:
                jobs = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot validate held job limits in {path}: {exc}") from exc
            if isinstance(jobs, dict):
                for job in jobs.values():
                    if isinstance(job, dict) and job.get("wall_clock_seconds") is not None:
                        self.validate_hold(job["wall_clock_seconds"])

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
            "question_hold_seconds": self.question_hold_seconds,
            "max_tokens": self.max_tokens,
            "otlp_endpoint": self.otlp_endpoint,
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
            "discord_gateway": "configured" if self.discord_bot_token_env else "not configured",
            "discord_allow_from": len(self.discord_allow_from),
            "profiles": sorted(self.profiles),
            "default_profile": self.default_profile,
            "routes": [r.get("path") for r in self.routes],
            "cairn_agent": self.cairn_agent,
            "cairn_project": self.cairn_project,
            "cairn_brief": self.cairn_brief,
            "timezone": self.timezone,
            "memory_retention_days": self.memory_retention_days,
            "drove": {
                "binary": self.drove_binary,
                "repo": self.drove_repo,
                "max_runs": self.drove_max_runs,
                "run_deadline_seconds": self.drove_run_deadline_seconds,
                "attention": f"every {self.drove_attention_interval_seconds:g}s"
                             if self.drove_attention_interval_seconds else "not forwarded",
            },
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

    # A key that is not a Config field is refused, not dropped: a misspelt
    # safety setting would otherwise load as its default and look configured.
    # Derived from the fields so a key a later change adds is accepted for free.
    known = {field.name for field in dataclasses.fields(Config)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"{source}: unknown config key(s) {unknown}")

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

    config.validate_profiles()
    # A dream grant is valid only against the current configured profile.
    from .schedule import Schedule

    Schedule(config.state_dir / "jobs.json", profiles=config.profiles).jobs()
    config.validate_holds()
    config.validate_public_profiles()
    return config
