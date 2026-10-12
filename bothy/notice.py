"""A notice for a person, and how each chat sink draws it.

An alert that arrives as a wall of markers, run ids and an ISO timestamp teaches
whoever reads the channel to skim past it. A notice therefore carries the parts a
person reads (what happened, in one sentence, and what to do about it) apart from
the parts only a machine needs (the run id, the item id), and each sink draws
only what suits it: a Discord embed, Slack blocks, or plain text.

The internal item id is never part of a notice. It lives in the dedupe state.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

from . import clock

__all__ = ["Notice", "discord_payload", "slack_payload", "text_of", "RESOLVED_COLOUR", "COLOURS"]

COLOURS = {"critical": 0xE74C3C, "warning": 0xF1A431, "info": 0x3498DB}
RESOLVED_COLOUR = 0x95A5A6

_TITLE_LIMIT = 250      # Discord caps an embed title at 256, Slack a header at 150
_SLACK_HEADER_LIMIT = 150
_DESCRIPTION_LIMIT = 900
_FIELD_LIMIT = 900


@dataclasses.dataclass(frozen=True)
class Notice:
    title: str                      # plain words, the project first
    description: str                # one sentence a person understands
    severity: str = "warning"       # "info" | "warning" | "critical"
    command: str | None = None      # what to run, drawn in a code block
    advice: str | None = None       # what to do when it is not a single command
    run: str | None = None
    site: str = "bothy"
    at: str | None = None           # ISO time the thing began, or the resolution
    resolved: bool = False
    details: str | None = None      # the raw error, for whoever digs; drawn small, in a code block
    facts: tuple[tuple[str, str], ...] = ()     # short name/value pairs, e.g. subject


def _clip(text: str, limit: int) -> str:
    flat = text.strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _block(code: str) -> str:
    """A code block that its own content cannot close."""
    return "```\n" + code.replace("`", "'") + "\n```"


def _moment(notice: Notice) -> str:
    return notice.at or clock.iso()


def _what_to_do(notice: Notice) -> str:
    parts: list[str] = []
    if notice.advice:
        parts.append(_clip(notice.advice, _FIELD_LIMIT))
    if notice.command:
        parts.append(_block(_clip(notice.command, _FIELD_LIMIT)))
    return "\n".join(parts)


def discord_payload(notice: Notice) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    todo = _what_to_do(notice)
    if todo and not notice.resolved:
        fields.append({"name": "What to do", "value": todo, "inline": False})
    if not notice.resolved:
        fields.extend({"name": name, "value": _clip(value, 200), "inline": True} for name, value in notice.facts)
        if notice.details:
            fields.append({"name": "Details", "value": _block(_clip(notice.details, _FIELD_LIMIT)), "inline": False})
    if notice.run:
        fields.append({"name": "Run", "value": f"`{notice.run}`", "inline": False})
    embed = {
        "title": _clip(notice.title, _TITLE_LIMIT),
        "description": _clip(notice.description, _DESCRIPTION_LIMIT),
        "color": RESOLVED_COLOUR if notice.resolved else COLOURS.get(notice.severity, COLOURS["warning"]),
        "fields": fields,
        "footer": {"text": f"Bothy on {notice.site}"},
        "timestamp": _moment(notice),
    }
    # Worker-written words never ping anybody.
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_time(notice: Notice) -> str:
    moment = _moment(notice)
    try:
        stamp = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return f"<!date^{int(stamp.timestamp())}^{{date_short_pretty}} {{time}}|{moment}>"
    except ValueError:
        return moment


def slack_payload(notice: Notice) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(notice.title, _SLACK_HEADER_LIMIT)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _slack_escape(_clip(notice.description, _DESCRIPTION_LIMIT))}},
    ]
    todo = _what_to_do(notice)
    if todo and not notice.resolved:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*What to do*\n{_slack_escape(todo)}"}})
    if not notice.resolved and notice.facts:
        lines = "\n".join(f"*{_slack_escape(name)}:* {_slack_escape(_clip(value, 200))}" for name, value in notice.facts)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines}})
    if not notice.resolved and notice.details:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                    "text": _slack_escape(_block(_clip(notice.details, _FIELD_LIMIT)))}})
    context = [f"Bothy on {_slack_escape(notice.site)}", _slack_time(notice)]
    if notice.run:
        context.insert(0, f"`{notice.run}`")
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(context)}]})
    colour = RESOLVED_COLOUR if notice.resolved else COLOURS.get(notice.severity, COLOURS["warning"])
    # `text` is what a push notification shows; the colour bar needs an attachment.
    return {"text": _slack_escape(notice.title),
            "attachments": [{"color": f"#{colour:06x}", "blocks": blocks}]}


def text_of(notice: Notice) -> str:
    lines = [notice.title, notice.description]
    if notice.resolved:
        return "\n".join(lines)
    if notice.advice:
        lines.append(f"What to do: {notice.advice}")
    if notice.command:
        lines.append(f"What to do: {notice.command}" if not notice.advice else f"  {notice.command}")
    lines.extend(f"{name}: {value}" for name, value in notice.facts)
    if notice.details:
        lines.append(f"Details: {notice.details}")
    if notice.run:
        lines.append(f"Run: {notice.run}")
    return "\n".join(lines)
