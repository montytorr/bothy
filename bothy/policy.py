"""Typed approval policy: what Bothy answers when a worker asks "may I?".

Until now there was one policy, "decline everything", and it was silent. A tool
a profile listed under ``mcp_ask`` rendered ``approval_mode = "always"`` and was
then declined with no audit record and no alert, which is indistinguishable from
a tool that is broken. A policy needs four answers, not two:

  proceed   let the call through.
  deny      decline it, for the reason given.
  guide     decline it with a required reason. Bothy-hosted tools return the
            reason in contentItems text; modern Codex-side approvals record it
            in the audit only (their response schemas have no reason field).
  confirm   decline it until a human has said yes, then let it through for a
            bounded time. See below.

RULES come from a profile's ``approvals`` list::

    {"server": "mail", "tool": "send_message", "verdict": "confirm",
     "unless_approved_within": 900, "reason": "outbound mail needs a human"}

``server`` is an MCP server name, or ``bothy`` for a tool Bothy hosts itself.
Gating must never grant (DESIGN.md), so ``capability.Profile.validate`` refuses
a rule naming a tool the profile does not enable. Anything but ``proceed``
renders ``approval_mode = "always"``, so Codex asks us rather than deciding for
itself.

EVERY DECISION IS AUDITED. The runner writes one ``kind="approval"`` record per
decision: ``action`` is the verdict, ``status`` is ``ok``, ``refused`` or
``awaiting_human``, and ``data`` is ``{method, server, tool, rule, reason}``.
Calls to Bothy-hosted tools are audited only when a rule matched, because
BOTHY-26 already records every such call as ``kind="tool"``.

Codex 0.162.0 MCP tool approvals arrive as ``mcpServer/elicitation/request``.
The generated schema leaves ``_meta`` untyped: its ``codex_approval_kind``
marker and ``tool_name``/``tool_title`` keys are observed binary metadata.
Unmarked forms/URL elicitations are declined. Additional permission requests
are also denied and answered with an empty grant; a tool rule cannot grant them.

THE TIME RULE (``confirm`` with ``unless_approved_within``). A human's approval
is a fact in the audit log, not a flag in memory, so it survives a restart and
an operator can read it. This module only READS it. Whoever collects the answer
(BOTHY-31, the chat hold) writes exactly this record::

    kind="interrupt", action="answered", status="ok",
    data={"decision": "approve", "server": "<server>", "tool": "<tool>"}

The newest such record for that server and tool decides: if it is no older than
``unless_approved_within`` seconds the call proceeds, otherwise it is declined
as ``awaiting_human``. Any other ``decision`` value, or a record for another
tool, is ignored. A record dated in the future (a skewed clock) is ignored too:
it is not evidence that anyone approved anything yet.

An audit log that cannot be read means DENY (DESIGN.md rule 4: when an
exemption's input is missing, choose a direction deliberately). Approving
without being able to see whether the human really said yes would turn the
window into an open door.

THE SEAM FOR BOTHY-31. A ``confirm`` that is not yet approved returns
``status="awaiting_human"``. The runner handles it in ``_awaiting_human``, which
today only records; BOTHY-31 puts the chat hold there and may call the client's
``_interrupt("awaiting_human")``. Nothing in this module interrupts a turn.
"""

from __future__ import annotations

import dataclasses
import re
import datetime as _dt
from typing import Any, Literal

from . import clock
from .audit import AuditLog

__all__ = ["Verdict", "VERDICTS", "HOSTED_SERVER", "ApprovalRule", "ApprovalDecision", "Facts",
           "parse_rules", "evaluate", "AWAITING_CONFIRMATION"]

Verdict = Literal["proceed", "deny", "guide", "confirm"]
VERDICTS: tuple[str, ...] = ("proceed", "deny", "guide", "confirm")

#: The ``server`` a rule uses for a tool Bothy hosts itself (``item/tool/call``).
HOSTED_SERVER = "bothy"
HOSTED_METHOD = "item/tool/call"
AWAITING_CONFIRMATION = "awaiting human confirmation"

_RULE_KEYS = {"server", "tool", "verdict", "reason", "unless_approved_within"}


@dataclasses.dataclass(frozen=True)
class ApprovalRule:
    server: str
    tool: str
    verdict: str
    reason: str = ""
    unless_approved_within: float | None = None

    @property
    def label(self) -> str:
        return f"{self.server}.{self.tool}"


def parse_rules(profile: str, raw: Any) -> tuple[ApprovalRule, ...]:
    """Turn a profile's ``approvals`` list into rules, refusing anything unclear.

    Structure only. Whether a rule names a tool the profile enables needs the
    rest of the profile, so ``Profile.validate`` checks that.
    """
    if not isinstance(raw, list):
        raise ValueError(f"profile {profile}: approvals must be a list of rules")
    rules: list[ApprovalRule] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(raw):
        where = f"profile {profile}: approvals[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        unknown = sorted(set(entry) - _RULE_KEYS)
        if unknown:
            raise ValueError(f"{where} has unknown key(s) {unknown}; known keys are {sorted(_RULE_KEYS)}")
        for key in ("server", "tool", "verdict"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"{where} needs a non-empty string {key!r}")
        verdict = entry["verdict"]
        if verdict not in VERDICTS:
            raise ValueError(f"{where} has unknown verdict {verdict!r}; use one of {list(VERDICTS)}")
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError(f"{where} reason must be a string")
        if verdict == "guide" and not reason.strip():
            raise ValueError(f"{where} is a guide rule and needs a reason: the reason guides hosted tools and is recorded in the audit")
        window = entry.get("unless_approved_within")
        if window is not None:
            if verdict != "confirm":
                raise ValueError(f"{where}: unless_approved_within only applies to a confirm rule")
            if isinstance(window, bool) or not isinstance(window, (int, float)) \
                    or not window > 0 or window == float("inf"):
                raise ValueError(f"{where}: unless_approved_within must be a positive number of seconds")
        key = (entry["server"], entry["tool"])
        if key in seen:
            raise ValueError(f"{where}: more than one rule for {key[0]}.{key[1]}")
        seen.add(key)
        rules.append(ApprovalRule(entry["server"], entry["tool"], verdict, reason.strip(),
                                  float(window) if window is not None else None))
    return tuple(rules)


@dataclasses.dataclass(frozen=True)
class Facts:
    """What a decision may depend on, handed over rather than reached for."""

    rules: tuple[ApprovalRule, ...] = ()
    #: Tools the profile pinned to "always ask", by server. With no rule for one
    #: of these the answer is deny, and somebody should be told.
    mcp_ask: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    audit: AuditLog | None = None
    #: Overridable so a test can age records without sleeping.
    now: _dt.datetime | None = None


@dataclasses.dataclass(frozen=True)
class ApprovalDecision:
    verdict: Verdict
    reason: str = ""
    #: ``server.tool`` of the matching rule, or why no rule decided.
    rule: str = "default"
    #: The audit ``status``: ok, refused or awaiting_human.
    status: str = "ok"
    method: str = ""
    server: str = ""
    tool: str = ""
    #: True when the tool was pinned by ``mcp_ask``, so a decline should be heard.
    gated: bool = False

    @property
    def proceeds(self) -> bool:
        return self.verdict == "proceed"

    @property
    def audit_action(self) -> str:
        return self.verdict

    def audit_data(self) -> dict[str, Any]:
        return {"method": self.method, "server": self.server, "tool": self.tool,
                "rule": self.rule, "reason": self.reason}


def _dig(params: dict[str, Any], keys: tuple[str, ...], nests: tuple[str, ...]) -> str:
    """First string under any of ``keys``, at the top or one level down.

    The app-server has carried an MCP call's identity under more than one key
    across versions, and unknown input must degrade to "no rule matched", which
    declines, never to a crash or an accept.
    """
    for container in (params, *(params.get(nest) for nest in nests)):
        if isinstance(container, dict):
            for key in keys:
                value = container.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def mcp_tool_approval(params: dict[str, Any]) -> bool:
    """Observed approval marker in an otherwise untyped MCP _meta payload."""
    meta = params.get("_meta")
    return isinstance(meta, dict) and meta.get("codex_approval_kind") == "mcp_tool_call"


def identify(method: str, params: dict[str, Any]) -> tuple[str, str]:
    """The (server, tool) a request is about, or empty strings if it names none."""
    params = params if isinstance(params, dict) else {}
    if method == HOSTED_METHOD:
        return HOSTED_SERVER, str(params.get("tool") or "")
    if method == "mcpServer/elicitation/request":
        server = _dig(params, ("serverName",), ())
        if not mcp_tool_approval(params):
            return server, ""
        # These keys and the quoted message template are observed in the
        # installed binary. _meta is untyped in the generated 0.162.0 schema.
        tool = _dig(params["_meta"], ("tool_name", "tool_title"), ())
        if not tool:
            message = params.get("message")
            match = re.search(r'\btool "([^"\r\n]+)"', message) if isinstance(message, str) else None
            tool = match.group(1) if match else ""
        return server, tool
    server = _dig(params, ("server", "serverName", "server_name", "mcpServer", "mcp_server"),
                  ("item", "invocation", "call", "toolCall", "request", "_meta"))
    tool = _dig(params, ("tool", "toolName", "tool_name"), ("item", "invocation", "call", "toolCall", "request", "_meta"))
    return server, tool


def _human_approved_age(audit: AuditLog, server: str, tool: str, now: _dt.datetime | None) -> float | None:
    """Seconds since the newest human approval for this tool, or None if there is none."""
    newest: float | None = None
    for record in audit.records():
        if record.get("kind") != "interrupt" or record.get("action") != "answered":
            continue
        data = record.get("data")
        if not isinstance(data, dict) or data.get("decision") != "approve":
            continue
        if data.get("server") != server or data.get("tool") != tool:
            continue
        try:
            age = clock.age_seconds(str(record.get("ts")), reference=now)
        except (ValueError, TypeError):
            continue
        if age < 0:
            continue
        # Oldest first on disk, so the last match is the newest.
        newest = age
    return newest


def evaluate(method: str, params: dict[str, Any], facts: Facts) -> ApprovalDecision:
    """Decide one approval request. Never raises; anything unclear declines."""
    params = params if isinstance(params, dict) else {}
    server, tool = identify(method, params)
    base = {"method": method, "server": server, "tool": tool}
    if method == "item/permissions/requestApproval":
        return ApprovalDecision("deny", "Bothy does not grant additional permissions", status="refused", **base)
    if method == "mcpServer/elicitation/request" and not mcp_tool_approval(params):
        return ApprovalDecision("deny", "MCP forms and URL elicitations are unsupported", status="refused", **base)
    gated = tool in (facts.mcp_ask.get(server) or [])
    rule = next((r for r in facts.rules if r.server == server and r.tool == tool), None) if tool else None

    if rule is None:
        if method == HOSTED_METHOD:
            return ApprovalDecision("proceed", "", rule="default", **base)
        if gated:
            return ApprovalDecision(
                "deny", f"{server}.{tool} is pinned to ask for approval and no approvals rule allows it",
                rule="mcp_ask", status="refused", gated=True, **base)
        reason = (f"no approval policy configured for {method}" if "requestApproval" in method
                  else f"{method} is not a request Bothy answers")
        return ApprovalDecision("deny", reason, status="refused", **base)

    if rule.verdict == "proceed":
        return ApprovalDecision("proceed", rule.reason, rule=rule.label, gated=gated, **base)
    if rule.verdict == "deny":
        return ApprovalDecision("deny", rule.reason or f"{rule.label} is denied by policy",
                                rule=rule.label, status="refused", gated=gated, **base)
    if rule.verdict == "guide":
        return ApprovalDecision("guide", rule.reason, rule=rule.label, status="refused", gated=gated, **base)

    if rule.unless_approved_within is not None:
        try:
            if facts.audit is None:
                raise OSError("no audit log to read approvals from")
            age = _human_approved_age(facts.audit, server, tool, facts.now)
        except (OSError, ValueError) as exc:
            return ApprovalDecision(
                "deny", f"the audit log could not be read, so a human approval cannot be confirmed ({exc})",
                rule=rule.label, status="refused", gated=gated, **base)
        if age is not None and age <= rule.unless_approved_within:
            return ApprovalDecision("proceed", f"approved by a human {int(age)}s ago",
                                    rule=rule.label, gated=gated, **base)
    reason = AWAITING_CONFIRMATION + (f": {rule.reason}" if rule.reason else "")
    return ApprovalDecision("confirm", reason, rule=rule.label, status="awaiting_human", gated=gated, **base)
