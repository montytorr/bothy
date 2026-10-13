"""What a worker is allowed to reach, decided per job.

A worker starts with nothing beyond Codex's built-ins. Everything else — a
mail server, a browser, a skills directory, a tool Bothy hosts itself — is
added by a named PROFILE attached to the job. A mail job gets mail; a
code-review job does not.

That is least privilege, and here it is nearly free: every run already gets its
own ``CODEX_HOME`` for SQLite isolation, so scoping capability to a run costs a
generated file rather than an architecture.

WHY THIS MATTERS MORE THAN IT LOOKS. Bothy's own wake prompt tells the model
that a webhook payload is untrusted, because a signature authenticates the
SENDER and never the CONTENT. The moment a worker can also reach a mailbox,
that sentence stops being advice and becomes the only thing between a stranger's
pull request title and an outbound email. Profiles are how that chain is kept
short: the job that reads webhooks is not the job that can send mail, and
nothing in the config makes it easy to accidentally merge them.

THREE WAYS TO GIVE A WORKER CAPABILITY, and they are genuinely different:

  MCP servers     an external process or endpoint, declared in the generated
                  config.toml. This is how mail and browsers arrive.
  skill roots     prose the model reads. Behaviour, not mechanism.
  dynamic tools   a tool BOTHY HOSTS ITSELF, declared on thread/start and
                  called back over the same stdio connection. No subprocess, no
                  port, no credential — and the handler runs inside Bothy where
                  it can touch the ledger, the checklist and the audit log
                  directly.

The third is how the agent gets to maintain its own standing checklist, which
the heartbeat design always assumed and nothing previously implemented.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import policy
from .audit import AuditLog

__all__ = ["Profile", "DynamicTool", "ToolRegistry", "builtin_tools", "render_config_toml",
           "write_checklist"]


# --------------------------------------------------------------------------
# profiles


@dataclasses.dataclass
class Profile:
    """A named bundle of capability, attached to a job.

    ``mcp_servers`` maps a server name to its Codex ``[mcp_servers.<name>]``
    table. Values are written into the worker's generated config.toml verbatim,
    with one rule enforced elsewhere: secrets are referenced by environment
    variable name, never written into the file.
    """

    name: str
    mcp_servers: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    # Which of a server's tools this profile actually wants, by server name.
    # Granting a server grants every tool it offers unless this says otherwise,
    # and "mcp: gmail" reads as harmless while potentially including `send`.
    mcp_tools: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    # Tools that must stop and ask, by server name. Codex supports this per
    # tool; Bothy renders it so a profile author cannot forget to.
    mcp_ask: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    skill_roots: list[str] = dataclasses.field(default_factory=list)
    tools: list[str] = dataclasses.field(default_factory=list)
    allow_from_untrusted: bool = False
    sandbox: str | None = None
    model: str | None = None
    approval_policy: str | None = None
    developer_instructions: str | None = None
    config: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Per-profile run limits. The runner falls back to the config-wide ones.
    max_tokens: int | None = None
    wall_clock_seconds: float | None = None
    # Typed approval rules, see policy.py. A tool named here must also be in
    # mcp_tools (or tools, for server "bothy"): gating must not grant it.
    approvals: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "Profile":
        if not isinstance(raw, dict):
            raise ValueError(f"profile {name}: must be an object, not {type(raw).__name__}")
        # Derived from the fields, so a key a later change adds is accepted
        # without anyone extending a list. A key that is NOT a field is refused:
        # dropping it silently turns a typo'd safety setting into no setting.
        known = {field.name for field in dataclasses.fields(cls)} - {"name"}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"profile {name}: unknown key(s) {unknown}; known keys are {sorted(known)}")
        profile = cls(name=name, **raw)
        profile.validate()
        return profile

    def validate(self) -> None:
        """Refuse a profile whose grants contradict each other.

        Checked when the profile is read, not when a run uses it, so a
        contradiction is a startup error somebody can fix rather than a
        surprise at three in the morning.

        The contradiction that matters: naming a tool under ``mcp_ask`` while
        excluding it from ``mcp_tools``. Silently adding it would be a
        capability grant by side effect — an author writes "make send ask for
        approval" and unintentionally *enables* send. Silently dropping the gate
        would be worse. So it is refused and the author says what they meant.
        """
        if not isinstance(self.allow_from_untrusted, bool):
            raise ValueError(f"profile {self.name}: allow_from_untrusted must be true or false")
        if not isinstance(self.mcp_tools, dict):
            raise ValueError(f"profile {self.name}: mcp_tools must map server names to tool lists")
        for server, ask in self.mcp_ask.items():
            if server not in self.mcp_servers:
                raise ValueError(
                    f"profile {self.name}: mcp_ask names {server!r}, which this profile does not grant"
                )
            allowed = self.mcp_tools.get(server)
            if allowed is None:
                continue
            missing = sorted(set(ask) - set(allowed))
            if missing:
                raise ValueError(
                    f"profile {self.name}: {server} tools {missing} are gated by mcp_ask but not "
                    f"listed in mcp_tools. Add them to mcp_tools to grant them, or remove them from "
                    f"mcp_ask — listing a tool as 'ask for approval' must not be what enables it."
                )
        for server in self.mcp_tools:
            if server not in self.mcp_servers:
                raise ValueError(
                    f"profile {self.name}: mcp_tools names {server!r}, which this profile does not grant"
                )
        for rule in self.rules():
            if rule.server == policy.HOSTED_SERVER:
                granted, where = rule.tool in self.tools, "tools"
            elif rule.server not in self.mcp_servers:
                raise ValueError(
                    f"profile {self.name}: approvals names {rule.server!r}, which this profile does not grant"
                )
            else:
                allowed = self.mcp_tools.get(rule.server)
                granted, where = allowed is None or rule.tool in allowed, "mcp_tools"
            if not granted:
                raise ValueError(
                    f"profile {self.name}: approvals rule for {rule.label} names a tool that is not listed "
                    f"in {where}. Add it to {where} to grant it, or remove the rule — a rule that gates "
                    f"a tool must not be what enables it."
                )

    def rules(self) -> tuple[policy.ApprovalRule, ...]:
        return policy.parse_rules(self.name, self.approvals)

    def asked_tools(self, server: str) -> list[str]:
        """Tools of ``server`` Codex must ask us about: ``mcp_ask``, or a rule that gates."""
        gated = {rule.tool for rule in self.rules() if rule.server == server and rule.verdict != "proceed"}
        return sorted(set(self.mcp_ask.get(server) or []) | gated)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def summary(self) -> str:
        bits = []
        for server in sorted(self.mcp_servers):
            allowed = self.mcp_tools.get(server)
            ask = self.asked_tools(server)
            # Naming the tools, not just the server: "mcp: gmail" reads as
            # harmless and may include `send`.
            scope = ", ".join(allowed) if allowed else ("NO TOOLS" if allowed == [] else "ALL TOOLS")
            bits.append(f"mcp {server}({scope})" + (f" ask:{','.join(ask)}" if ask else ""))
        if self.tools:
            bits.append("tools: " + ", ".join(self.tools))
        if self.skill_roots:
            bits.append(f"{len(self.skill_roots)} skill root(s)")
        if self.sandbox:
            bits.append(f"sandbox {self.sandbox}")
        if self.model:
            bits.append(self.model)
        return "; ".join(bits) or "nothing beyond Codex built-ins"


def _toml_value(value: Any) -> str:
    """Render a Python value as TOML. Small on purpose — this writes config, not documents."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)          # JSON string escaping is valid TOML basic-string escaping
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + "}"
    raise TypeError(f"cannot render {type(value).__name__} as TOML")


def render_config_toml(profile: Profile | None, *, extra: dict[str, Any] | None = None) -> str:
    """Generate the worker's config.toml.

    Generated rather than inherited, so a worker behaves the same on a client's
    machine as it did on ours — inheriting the host's file means the deployment
    is shaped by whatever that account happened to have configured, which is the
    difference between a test that passes here and a run that surprises someone
    there.

    No secrets are written. An MCP server that needs a token names the
    environment variable holding it, and Codex reads it from the worker's
    environment.
    """
    lines: list[str] = [
        "# Generated by Bothy for a single run. Edits here are lost.",
        "# Secrets are referenced by environment variable, never written.",
        "",
    ]
    top: dict[str, Any] = {}
    if profile is not None:
        if profile.model:
            top["model"] = profile.model
        top.update(profile.config or {})
    top.update(extra or {})
    for key, value in top.items():
        if isinstance(value, dict):
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    for key, value in top.items():
        if isinstance(value, dict):
            lines += ["", f"[{key}]"]
            for sub, item in value.items():
                lines.append(f"{sub} = {_toml_value(item)}")
    if profile is not None:
        for name, table in sorted(profile.mcp_servers.items()):
            lines += ["", f"[mcp_servers.{name}]"]
            allowed = profile.mcp_tools.get(name)
            for key, value in table.items():
                if isinstance(value, dict) or key in {"enabled_tools", "tools"}:
                    continue
                lines.append(f"{key} = {_toml_value(value)}")
            if allowed is not None:
                # Least privilege, rendered rather than remembered. Without this
                # a profile that wanted one read-only tool gets every tool the
                # server happens to expose, including whatever it adds next.
                lines.append(f"enabled_tools = {_toml_value(sorted(allowed))}")
            for key, value in table.items():
                if isinstance(value, dict) and key != "tools":
                    lines += [f"[mcp_servers.{name}.{key}]"]
                    for sub, item in value.items():
                        lines.append(f"{sub} = {_toml_value(item)}")
            for tool in profile.asked_tools(name):
                lines += [f"[mcp_servers.{name}.tools.{tool}]", 'approval_mode = "always"']
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# tools Bothy hosts itself


@dataclasses.dataclass
class DynamicTool:
    """A tool declared to Codex and answered by Bothy over the same connection."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """The dynamic tools available to a run, and the dispatch for their calls."""

    def __init__(self, tools: list[DynamicTool] | None = None,
                 gate: Callable[[str, Any], str | None] | None = None) -> None:
        self._tools: dict[str, DynamicTool] = {tool.name: tool for tool in (tools or [])}
        self._gate = gate

    def gated(self, gate: Callable[[str, Any], str | None]) -> "ToolRegistry":
        """Put this registry's tools behind an approval gate, and return it.

        ``gate(name, arguments)`` returns None to let the call run, or the
        reason it was declined. A gate that raises declines: a policy that
        cannot answer has not approved anything.
        """
        self._gate = gate
        return self

    def add(self, tool: DynamicTool) -> None:
        self._tools[tool.name] = tool

    def select(self, names: list[str], *, untrusted: bool = False,
               allow_from_untrusted: bool = False) -> "ToolRegistry":
        """A registry holding only the named tools. Unknown names are ignored.

        Ignored rather than rejected because a profile naming a tool that a
        future version renamed should degrade to less capability, never to a
        run that refuses to start.
        """
        blocked = {"bothy_checklist_update", "bothy_note"} if untrusted and not allow_from_untrusted else set()
        return ToolRegistry([self._tools[name] for name in names
                             if name in self._tools and name not in blocked], gate=self._gate)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __bool__(self) -> bool:
        return bool(self._tools)

    def call(self, name: str, arguments: Any) -> tuple[bool, str]:
        """Run a tool. Returns (success, text). Never raises into the protocol."""
        tool = self._tools.get(name)
        if tool is None:
            return False, f"{name} is not a tool this run was given"
        if self._gate is not None:
            try:
                declined = self._gate(name, arguments)
            except Exception as exc:  # noqa: BLE001 - fail closed
                declined = f"approval check failed: {type(exc).__name__}: {exc}"
            if declined is not None:
                return False, declined
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"value": arguments}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        try:
            return True, tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001 - a bad tool must not end the turn
            return False, f"{name} failed: {type(exc).__name__}: {exc}"


def write_checklist(path: Path, text: str, audit: AuditLog) -> None:
    """Record the old and new contents' hashes for every checklist writer."""
    try:
        before = path.read_bytes()
    except FileNotFoundError:
        before = b""
    after = (text.strip() + "\n").encode("utf-8")
    hashes = {"before_sha256": hashlib.sha256(before).hexdigest(),
              "after_sha256": hashlib.sha256(after).hexdigest()}
    # Record intent before touching shared memory; a failed audit denies the write.
    audit.append(kind="checklist", action="replace", status="pending", data=hashes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=path.name,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    audit.append(kind="checklist", action="replaced", data=hashes)


def builtin_tools(
    *,
    checklist_path: Path,
    audit: AuditLog,
    note: Callable[[str, str], bool] | None = None,
    status: Callable[[], dict[str, Any]] | None = None,
) -> ToolRegistry:
    """The tools Bothy offers about itself.

    Deliberately few. These exist because the run cannot reach them any other
    way — the shell is the sandboxed part, and on some hosts it does not work at
    all, so "write to a file" is not a substitute.
    """
    registry = ToolRegistry()

    def read_checklist(_: dict[str, Any]) -> str:
        try:
            return checklist_path.read_text(encoding="utf-8") or "(the checklist is empty)"
        except OSError:
            return "(no checklist yet)"

    def update_checklist(args: dict[str, Any]) -> str:
        text = str(args.get("checklist") or "").strip()
        if not text:
            return "refused: an empty checklist would silently disable every future heartbeat"
        write_checklist(checklist_path, text, audit)
        return f"checklist replaced, {len(text)} characters"

    registry.add(DynamicTool(
        name="bothy_checklist_read",
        description="Read the standing checklist this harness works through on each heartbeat.",
        input_schema={"type": "object", "properties": {}},
        handler=read_checklist,
    ))
    registry.add(DynamicTool(
        name="bothy_checklist_update",
        description=(
            "Replace the standing checklist. Use it when you learn something worth checking "
            "every time, or when an item is no longer worth checking. Keep it short and "
            "specific; it is read on every heartbeat."
        ),
        input_schema={
            "type": "object",
            "properties": {"checklist": {"type": "string", "description": "the complete new checklist"}},
            "required": ["checklist"],
        },
        handler=update_checklist,
    ))

    if note is not None:
        registry.add(DynamicTool(
            name="bothy_note",
            description=(
                "Record a finding against a task in shared memory, so the next session starts "
                "knowing it. Use it for what you learnt or ruled out, not for narration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "task reference, e.g. ACME-12"},
                    "note": {"type": "string", "description": "what is worth knowing later"},
                },
                "required": ["ref", "note"],
            },
            handler=lambda args: (
                "recorded" if note(str(args.get("ref", "")), str(args.get("note", ""))) else "memory unreachable"
            ),
        ))

    if status is not None:
        registry.add(DynamicTool(
            name="bothy_status",
            description="Read this harness's own concurrency and budget position.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda _: json.dumps(status(), indent=1),
        ))

    return registry
