"""Bothy — a small agent harness you can leave at a client site.

A bothy is an unlocked shelter in the hills: nobody staffs it, it looks after
itself, and whoever passes through can use it. That is the whole design brief.

Bothy wakes on signed webhooks, schedules and messages; runs Codex as its
worker under supervision; keeps its memory and its tasks in Cairn; says
something when it needs a human; and is closed to everything except the
tailnet. It is deliberately far smaller than the systems it learns from.

Standard library only. See docs/DESIGN.md for the rules it is built to, each
of which was bought with somebody's outage.
"""

__version__ = "0.1.0"
