"""Who is on the other end, asked of the tailnet rather than of the request.

Bothy binds to 127.0.0.1 and nothing else. Reaching it from another machine is
``tailscale serve``'s job: it terminates TLS with the tailnet's own certificate,
refuses anyone outside the tailnet, and forwards to loopback. So there is no
public listener to misconfigure and no certificate for Bothy to own.

That leaves one question worth answering in-process: WHICH tailnet peer is this?
The forwarded request carries headers claiming an identity, and a header is a
claim, not a proof. So Bothy asks the local tailscaled daemon instead —
``whois`` on the peer's address returns the node and the login that tailscaled
itself has authenticated. A spoofed header fails because the daemon answers
about the address, not about the header.

Three properties of this that matter:

  The socket is the authority.  It is a unix socket owned by root and readable
  by anyone on the box; reading it proves nothing about the caller, which is
  fine, because the ANSWER is what we use.

  Unknown is not allowed.  If the daemon cannot be reached or returns no node,
  the answer is "I could not tell", and that is refused rather than waved
  through. A verifier that fails open is not a verifier.

  It is a second gate, not the only one.  Signed webhooks still verify their
  signatures. Tailnet identity says who connected; the signature says who wrote
  the payload, and they are different questions.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import socket
from pathlib import Path
from typing import Any

__all__ = ["Peer", "TailnetError", "whois", "socket_path", "available"]

# The open-source daemon uses these; the macOS App Store build is sandboxed and
# exposes its API differently, which is why the install notes say to use the
# standalone or brew tailscaled on a Mac that needs identity checks.
CANDIDATE_SOCKETS = (
    "/run/tailscale/tailscaled.sock",
    "/var/run/tailscale/tailscaled.sock",
    "/var/run/tailscaled.socket",
)


class TailnetError(RuntimeError):
    """The tailnet could not answer. Never treated as permission."""


@dataclasses.dataclass(frozen=True)
class Peer:
    """A tailnet identity as tailscaled reports it."""

    login: str
    node: str
    node_id: str
    addresses: tuple[str, ...] = ()
    is_tagged: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def label(self) -> str:
        return f"{self.login}@{self.node}"


def socket_path(explicit: str | None = None) -> str | None:
    """Find the local API socket, or None if this box has no tailscaled."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    for candidate in CANDIDATE_SOCKETS:
        if Path(candidate).exists():
            return candidate
    return None


def available(explicit: str | None = None) -> bool:
    return socket_path(explicit) is not None


class _UnixConnection(http.client.HTTPConnection):
    """HTTP over a unix socket, which http.client does not do on its own."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self._path = path

    def connect(self) -> None:  # noqa: D102 - http.client's contract
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def whois(address: str, *, sock: str | None = None, timeout: float = 5.0) -> Peer:
    """Ask tailscaled who owns an address. Raises rather than guessing.

    ``address`` may be a bare IP or ``ip:port``; the daemon wants a port, so one
    is supplied when absent.
    """
    path = socket_path(sock)
    if path is None:
        raise TailnetError("no tailscaled socket on this machine")
    target = address if ":" in address and not address.endswith("]") else f"{address}:0"

    connection = _UnixConnection(path, timeout)
    try:
        connection.request(
            "GET", f"/localapi/v0/whois?addr={target}",
            headers={"Host": "local-tailscaled.sock"},
        )
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise TailnetError(f"whois {target}: HTTP {response.status}")
        data = json.loads(body)
    except TailnetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TailnetError(f"whois {target}: {exc}") from exc
    finally:
        connection.close()

    node = data.get("Node") or {}
    profile = data.get("UserProfile") or {}
    if not node:
        # No node means the address is not a tailnet peer. Refused, never
        # softened into an anonymous allow.
        raise TailnetError(f"{target} is not a tailnet peer")
    return Peer(
        login=str(profile.get("LoginName") or ""),
        node=str(node.get("Name") or "").rstrip("."),
        node_id=str(node.get("StableID") or ""),
        addresses=tuple(node.get("Addresses") or ()),
        is_tagged=bool(node.get("Tags")),
    )


def check(address: str, *, allow_logins: list[str] | None = None,
          allow_nodes: list[str] | None = None, sock: str | None = None) -> Peer:
    """Resolve a peer and enforce the allowlists. Raises TailnetError on refusal.

    An empty allowlist means "any tailnet peer", which is a real choice on a
    single-user tailnet and a bad one on a shared tailnet — so it is stated in
    the config rather than being the silent default of an absent key.
    """
    peer = whois(address, sock=sock)
    if allow_logins and peer.login not in allow_logins:
        raise TailnetError(f"{peer.label()} is not an allowed login")
    if allow_nodes and peer.node_id not in allow_nodes and peer.node not in allow_nodes:
        raise TailnetError(f"{peer.label()} is not an allowed device")
    return peer
