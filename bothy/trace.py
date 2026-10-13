"""Recorded run, turn and tool spans, in the same OTLP/JSON shape as Drove.

Exports are observations: one attempt, a two-second deadline, then drop. They
never hold a budget reservation or retry behind the operator's back.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from . import clock
from .audit import AuditLog
from .budget import Usage

__all__ = ["build_trace", "render_trace", "post_trace", "span_fields", "summarize_items"]

ITEM_LIMIT = 200
ARGUMENT_LIMIT = 500
# A stalled resolver must not accumulate one immortal thread per completed run.
_EXPORT_SLOTS = threading.BoundedSemaphore(2)


def _id(run_id: str, key: str, length: int) -> str:
    return hashlib.sha256(f"{run_id}\0{key}".encode("utf-8")).hexdigest()[:length]


def span_fields(run_id: str, key: str, *, parent: str | None = None,
                start: str, end: str | None = None, status: str = "pending") -> dict[str, Any]:
    """Make ids when the fact is recorded, rather than infer them at export."""
    return {"trace_id": _id(run_id, "trace", 32), "span_id": _id(run_id, key, 16),
            "parent_span_id": _id(run_id, parent, 16) if parent else None,
            "start": start, "end": end, "status": status}


def _short_arguments(value: Any) -> str:
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str))[:ARGUMENT_LIMIT]


def summarize_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep execution facts and times, never an unbounded transcript or diff."""
    summaries = []
    for item in items:
        if item.get("type") not in {"commandExecution", "mcpToolCall", "fileChange"}:
            continue
        summary = {key: item[key] for key in ("id", "type", "start", "end", "status", "exitCode", "server", "tool")
                   if key in item}
        arguments = item.get("arguments", item.get("command", item.get("changes")))
        if arguments is not None:
            summary["arguments"] = _short_arguments(arguments)
        summaries.append(summary)
        if len(summaries) >= ITEM_LIMIT:
            break
    return summaries


def _attributes(data: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded = {"boolValue": value}
        elif isinstance(value, int):
            encoded = {"intValue": str(value)}
        elif isinstance(value, float) and math.isfinite(value):
            encoded = {"doubleValue": value}
        else:
            encoded = {"stringValue": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}
        attributes.append({"key": key, "value": encoded})
    return attributes


def _nano(at: str) -> str:
    delta = clock.parse(at).astimezone(dt.timezone.utc) - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return str(((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000)


def _usage(data: dict[str, Any]) -> dict[str, int]:
    usage = Usage.from_app_server(data)
    return {"gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": usage.cached_input_tokens,
            "gen_ai.usage.cache_creation.input_tokens": usage.cache_write_tokens,
            "gen_ai.usage.reasoning.output_tokens": usage.reasoning_output_tokens}


def build_trace(records: Iterable[dict[str, Any]] | AuditLog | str | Path, run_id: str) -> dict[str, Any]:
    """Export recorded spans from all retained segments; accept records for replay.

    A path names the live audit log (or its directory). Historical runs without
    span fields retain a root spanning their admitted and finished records.
    """
    if isinstance(records, (str, Path)):
        path = Path(records)
        records = AuditLog(path / "audit.jsonl" if path.is_dir() else path)
    if isinstance(records, AuditLog):
        records = records.all_records()
    own = sorted((record for record in records if record.get("run_id") == run_id),
                 key=lambda record: record.get("seq", 0))
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in own:
        fields = record.get("span")
        if fields and record.get("kind") in {"run", "turn", "tool"}:
            groups.setdefault(fields["span_id"], []).append(record)
    admitted = next((r for r in own if r.get("kind") == "run" and r.get("action") == "admitted"), None)
    finished = next((r for r in reversed(own) if r.get("kind") == "run" and r.get("action") == "finished"), None)
    if admitted and not admitted.get("span"):
        end = (finished or own[-1])["ts"]
        fallback = {**(finished or admitted), "span": span_fields(
            run_id, "run", start=admitted["ts"], end=end,
            status=(finished or admitted).get("status", "pending"))}
        groups.setdefault(fallback["span"]["span_id"], []).append(fallback)

    spans = []
    for group in sorted(groups.values(), key=lambda group: {"run": 0, "turn": 1, "tool": 2}[group[0]["kind"]]):
        first, last = group[0], group[-1]
        fields = last["span"]
        data = {key: value for record in group for key, value in (record.get("data") or {}).items()}
        kind = first["kind"]
        name = (f"run {run_id}" if kind == "run" else
                f"turn {data.get('turn_id') or '1'}" if kind == "turn" else f"tool {data.get('tool') or first['action']}")
        start = first["span"]["start"]
        end = fields.get("end") or (finished or own[-1])["ts"]
        attrs = {"bothy.run": run_id, "bothy.subject": first.get("subject"),
                 **{f"bothy.{key}": value for key, value in data.items() if key != "usage"}}
        if "usage" in data:
            attrs.update(_usage(data["usage"]))
        made = {"traceId": fields["trace_id"], "spanId": fields["span_id"], "name": name, "kind": 1,
                "startTimeUnixNano": _nano(start), "endTimeUnixNano": str(max(int(_nano(start)), int(_nano(end)))),
                "attributes": _attributes(attrs), "events": [],
                "status": {"code": 1 if fields["status"] in {"ok", "completed"} else
                           0 if fields["status"] == "pending" else 2}}
        if fields.get("parent_span_id"):
            made["parentSpanId"] = fields["parent_span_id"]
        spans.append(made)
    # Approval decisions are owned by policy. Their attached span supplies an
    # event destination, never another span with the same identity.
    by_id = {span["spanId"]: span for span in spans}
    for record in own:
        if record.get("kind") == "approval":
            parent = by_id.get((record.get("span") or {}).get("span_id")) or by_id.get(_id(run_id, "run", 16))
            if parent:
                parent["events"].append({"name": f"approval/{record['action']}", "timeUnixNano": _nano(record["ts"]),
                                         "attributes": _attributes(record.get("data") or {})})
    return {"resourceSpans": [{"resource": {"attributes": _attributes({"service.name": "bothy"})},
                               "scopeSpans": [{"scope": {"name": "bothy"}, "spans": spans}]}]}


def render_trace(trace: dict[str, Any]) -> str:
    spans = [span for resource in trace["resourceSpans"] for scope in resource["scopeSpans"] for span in scope["spans"]]

    def visit(parent: str | None, depth: int) -> list[str]:
        lines = []
        for span in spans:
            if span.get("parentSpanId") == parent:
                elapsed = (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1_000_000
                lines.append(f"{'  ' * depth}{span['name']} ({elapsed:.1f}ms) status={span['status']['code']}")
                lines.extend(visit(span["spanId"], depth + 1))
        return lines

    return "\n".join(visit(None, 0))


def post_trace(trace: dict[str, Any] | Callable[[], dict[str, Any]], endpoint: str, *, timeout: float = 2.0) -> None:
    """One POST with a total deadline, including DNS and response headers.

    urllib's socket timeout alone is per-operation, not a total deadline. A
    daemon thread bounds the caller even when DNS or a slow peer ignores it.
    """
    deadline = clock.monotonic() + timeout
    if not _EXPORT_SLOTS.acquire(blocking=False):
        raise RuntimeError("OTLP exporter busy; dropping trace")
    done = threading.Event()
    errors: list[Exception] = []

    def send() -> None:
        try:
            url = urllib.parse.urlsplit(endpoint)
            path = url.path.rstrip("/")
            if not path.endswith("/v1/traces"):
                path += "/v1/traces"
            target = urllib.parse.urlunsplit(url._replace(path=path))
            headers = {}
            for entry in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
                name, equal, value = entry.partition("=")
                if equal and name.strip():
                    headers[name.strip()] = urllib.parse.unquote(value.strip())
            headers["Content-Type"] = "application/json"
            payload = trace() if callable(trace) else trace
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise TimeoutError("OTLP export deadline exceeded")
            request = urllib.request.Request(target, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"OTLP export failed: HTTP {response.status}")
        except Exception as exc:  # noqa: BLE001 - surfaced on the caller thread
            errors.append(exc)
        finally:
            _EXPORT_SLOTS.release()
            done.set()

    threading.Thread(target=send, name="bothy-otlp", daemon=True).start()
    if not done.wait(max(0.0, deadline - clock.monotonic())):
        raise TimeoutError("OTLP export deadline exceeded")
    if errors:
        raise errors[0]
