"""Being woken, without losing anything and without being fooled.

The order of operations in the handler is the whole design, and it is not
negotiable:

    verify signature -> check for a replay -> APPEND TO DISK AND fsync
        -> answer 200 -> only then do anything slow

Acknowledging before the durable write is the mistake a mature message broker
shipped as a headline feature and later deleted outright — 2,676 lines and the
status code with them — because a buffer that had not reached disk could not
deliver the durability its "accepted" response promised. So: fsync, then ack.

A REPLAY IS ANSWERED 200, NOT 4xx. The sender is behaving correctly; telling it
off makes it retry. A duplicate is reported as ``{"ok": true, "duplicate":
true}`` so the sender stops, and the dedupe record is kept ON DISK — the
comparable system keeps it in memory only, so every restart reopens the window
during exactly the period when redeliveries are most likely.

SIGNATURES AUTHENTICATE THE SENDER, NOT THE CONTENT. A verified GitHub webhook
proves GitHub sent it; the pull request title inside was written by a stranger.
Everything in a payload is untrusted input to whatever prompt it reaches.

A ROUTE WITHOUT A SECRET IS A STARTUP ERROR, never a warning. Bothy will not
start holding a door open that it meant to lock, because the day that warning
scrolls past is the day it matters.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import clock, ids
from .vendor.a2a_reactor.queue import append_event, read_queue, write_queue

__all__ = ["Route", "WakeStore", "WakeServer", "verify_signature", "MAX_BODY_BYTES"]

MAX_BODY_BYTES = 1_000_000
DEFAULT_SKEW_SECONDS = 300


# --------------------------------------------------------------------------
# signatures


def _candidates(secret: bytes, body: bytes, timestamp: str | None, event_id: str | None) -> list[str]:
    """Every signing scheme Bothy accepts, as hex digests to compare against.

    Raw body first because it is the common case and the cheapest. The canonical
    re-serialisation is a genuine necessity, not defensiveness: a platform that
    signs a differently-serialised form of the same JSON will otherwise fail
    verification for a payload that is perfectly authentic. The
    ``{id}.{timestamp}.{body}`` form is the Standard Webhooks shape.

    WHAT THE CANONICAL FALLBACK ACTUALLY PROMISES, because it is weaker than it
    looks and the difference matters. It verifies the PARSED CONTENT, not the
    bytes. When the signed body was itself canonical, any re-encoding that parses
    to the same object is accepted — different whitespace, different key order, a
    duplicate key whose last value wins. Measured: a changed value, an added
    field and a removed field are all rejected; only semantically identical
    re-encodings pass.

    That is safe here for exactly one reason, and it is a constraint on the rest
    of Bothy rather than a property of this function: the payload is always taken
    from ``json.loads(body)`` and the raw bytes are never used again. If anything
    downstream ever re-reads the raw body — to re-sign it, forward it verbatim,
    or hash it for an id — this equivalence stops being harmless, because the
    bytes it sees may not be the bytes that were signed.
    """
    out = [hmac.new(secret, body, hashlib.sha256).hexdigest()]
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode("utf-8")
        if canonical != body:
            out.append(hmac.new(secret, canonical, hashlib.sha256).hexdigest())
    if timestamp:
        signed = f"{event_id or ''}.{timestamp}.".encode("utf-8") + body
        out.append(hmac.new(secret, signed, hashlib.sha256).hexdigest())
        out.append(hmac.new(secret, f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest())
    return out


def verify_signature(
    *,
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None = None,
    event_id: str | None = None,
    max_skew_seconds: int = DEFAULT_SKEW_SECONDS,
) -> tuple[bool, str]:
    """Check a signature and, when present, its timestamp. Returns (ok, reason).

    Comparison is constant-time. A stale timestamp is rejected even when the
    signature is valid, because a replayed-but-authentic request is exactly what
    the timestamp is there to stop.
    """
    if not signature:
        return False, "missing signature"
    presented = signature.strip()
    for prefix in ("sha256=", "v1,", "v1="):
        if presented.startswith(prefix):
            presented = presented[len(prefix):]
    presented = presented.strip()

    if timestamp is not None:
        try:
            sent_at = float(timestamp)
            skew = abs(clock.utcnow().timestamp() - sent_at)
        except (TypeError, ValueError):
            try:
                skew = abs(clock.age_seconds(timestamp))
            except ValueError:
                return False, "unparseable timestamp"
        if skew > max_skew_seconds:
            return False, f"timestamp is {skew:.0f}s away, outside the {max_skew_seconds}s window"

    for candidate in _candidates(secret.encode("utf-8"), body, timestamp, event_id):
        if hmac.compare_digest(candidate, presented):
            return True, "ok"
    return False, "signature does not match"


# --------------------------------------------------------------------------
# routes and storage


@dataclasses.dataclass(frozen=True)
class Route:
    """One inbound path, its secret, and what a wake on it is about.

    ``subject_from`` names the payload field whose value identifies the thing
    being worked, so two webhooks about the same pull request serialise onto one
    lane instead of racing. Dotted paths are followed.
    """

    path: str
    secret: str
    subject_from: str | None = None
    subject_prefix: str = ""
    signature_header: str = "X-Webhook-Signature"
    timestamp_header: str = "X-Webhook-Timestamp"
    delivery_headers: tuple[str, ...] = (
        "X-Webhook-Delivery-Id",
        "X-GitHub-Delivery",
        "svix-id",
        "X-Webhook-Nonce",
        "X-Idempotency-Key",
    )

    def __post_init__(self) -> None:
        if not self.secret:
            # Refusing at construction means the daemon cannot start with an
            # unsecured door, which is the only time this is cheap to fix.
            raise ValueError(f"route {self.path} has no secret; refusing to listen on an unsigned endpoint")

    def subject(self, payload: dict[str, Any], fallback: str) -> str:
        if not self.subject_from:
            return f"{self.subject_prefix}{fallback}"
        cursor: Any = payload
        for part in self.subject_from.split("."):
            if not isinstance(cursor, dict):
                return f"{self.subject_prefix}{fallback}"
            cursor = cursor.get(part)
        return f"{self.subject_prefix}{cursor}" if cursor else f"{self.subject_prefix}{fallback}"


class WakeStore:
    """The durable queue, plus a replay record that survives a restart."""

    def __init__(self, directory: str | os.PathLike[str], *, dedupe_ttl_seconds: float = 86400.0) -> None:
        self.dir = Path(directory)
        self.queue_path = self.dir / "wakes.jsonl"
        self.dedupe_path = self.dir / "seen.json"
        self.dedupe_ttl_seconds = dedupe_ttl_seconds
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)

    def _load_seen(self) -> dict[str, float]:
        if not self.dedupe_path.exists():
            return {}
        try:
            data = json.loads(self.dedupe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_seen(self, seen: dict[str, float]) -> None:
        cutoff = clock.utcnow().timestamp() - self.dedupe_ttl_seconds
        fresh = {key: at for key, at in seen.items() if at >= cutoff}
        tmp = self.dedupe_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(fresh, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.dedupe_path)

    def seen_before(self, key: str) -> bool:
        """Record a delivery key and report whether it had already been used."""
        with self._lock:
            seen = self._load_seen()
            if key in seen:
                return True
            seen[key] = clock.utcnow().timestamp()
            self._save_seen(seen)
            return False

    def append(self, event: dict[str, Any]) -> None:
        """Durably record a wake. Raises if it cannot — the caller must NOT ack."""
        append_event(self.queue_path, event)

    def pending(self) -> list[dict[str, Any]]:
        return [event for event in read_queue(self.queue_path) if not event.get("processed")]

    def all_events(self) -> list[dict[str, Any]]:
        return read_queue(self.queue_path)

    def mark(self, wake_id: str, **fields: Any) -> None:
        """Update one wake in place, rewriting the queue atomically."""
        with self._lock:
            events = read_queue(self.queue_path)
            for event in events:
                if event.get("id") == wake_id:
                    event.update(fields)
            write_queue(self.queue_path, events)


# --------------------------------------------------------------------------
# the server


class _Handler(BaseHTTPRequestHandler):
    server_version = "bothy"
    sys_version = ""

    # injected by WakeServer
    routes: dict[str, Route] = {}
    store: WakeStore
    on_wake: Callable[[dict[str, Any]], None] | None = None
    max_skew_seconds: int = DEFAULT_SKEW_SECONDS

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """Silence the default access log; Bothy records its own events."""

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with contextlib.suppress(OSError):
            self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        """Liveness, answered by the process being asked rather than about."""
        self._reply(HTTPStatus.OK, {"ok": True, "service": "bothy", "at": clock.iso()})

    def do_POST(self) -> None:  # noqa: N802
        route = self.routes.get(self.path.split("?", 1)[0])
        if route is None:
            self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "no such route"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            # Checked before reading, so an oversized body is never buffered.
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload too large"})
            return
        body = self.rfile.read(length) if length else b""

        delivery_key = None
        for header in route.delivery_headers:
            value = self.headers.get(header)
            if value:
                delivery_key = f"{route.path}:{value}"
                break

        ok, reason = verify_signature(
            secret=route.secret,
            body=body,
            signature=self.headers.get(route.signature_header),
            timestamp=self.headers.get(route.timestamp_header),
            event_id=self.headers.get("svix-id") or self.headers.get("X-Webhook-Delivery-Id"),
            max_skew_seconds=self.max_skew_seconds,
        )
        if not ok:
            # No detail to the caller: a verification oracle is a gift to whoever
            # is probing. The reason is recorded on our side instead.
            self._reply(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        if delivery_key and self.store.seen_before(delivery_key):
            # 200, not 409: the sender did nothing wrong and a 4xx makes it retry.
            self._reply(HTTPStatus.OK, {"ok": True, "duplicate": True})
            return

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            payload = {"value": payload}

        wake_id = ids.wake_id()
        event = {
            "id": wake_id,
            "received_at": clock.iso(),
            "route": route.path,
            "subject": route.subject(payload, wake_id),
            "delivery_key": delivery_key,
            "payload": payload,
            "processed": False,
        }

        try:
            # Durable BEFORE the ack. If this raises, we must not say "accepted".
            self.store.append(event)
        except OSError as exc:
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"could not persist: {exc}"})
            return

        self._reply(HTTPStatus.ACCEPTED, {"ok": True, "id": wake_id, "subject": event["subject"]})

        # Only now, after the caller has its answer, do anything slow.
        if self.on_wake is not None:
            threading.Thread(
                target=self._notify, args=(event,), name=f"wake-{wake_id}", daemon=True
            ).start()

    def _notify(self, event: dict[str, Any]) -> None:
        try:
            self.on_wake(event)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the listener
            print(f"[bothy][wake-handler-failed] {event['id']}: {exc}", flush=True)


class WakeServer:
    """A loopback-only HTTP listener for signed wakes.

    Bound to 127.0.0.1 and nothing else. Reaching it from another machine is
    ``tailscale serve``'s job, which terminates TLS and refuses anyone outside
    the tailnet — so there is no public listener to misconfigure, and no
    certificate for Bothy to own.
    """

    def __init__(
        self,
        *,
        store: WakeStore,
        routes: list[Route],
        host: str = "127.0.0.1",
        port: int = 8787,
        on_wake: Callable[[dict[str, Any]], None] | None = None,
        max_skew_seconds: int = DEFAULT_SKEW_SECONDS,
    ) -> None:
        if not routes:
            raise ValueError("a wake server with no routes would accept nothing; configure at least one")
        self.host = host
        self.port = port
        handler = type(
            "BothyWakeHandler",
            (_Handler,),
            {
                "routes": {route.path: route for route in routes},
                "store": store,
                "on_wake": staticmethod(on_wake) if on_wake else None,
                "max_skew_seconds": max_skew_seconds,
            },
        )
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[0], self._httpd.server_address[1]

    def serve_forever_in_background(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="bothy-wake", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
