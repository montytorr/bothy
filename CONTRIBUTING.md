# Contributing

## The one rule

**Standard library only.** No runtime dependency, ever. Bothy is installed at
client sites where a package index may not be reachable and a virtualenv is
friction nobody wants. If something genuinely needs a library, vendor it verbatim
under `bothy/vendor/` with a note saying where it came from — and do not edit it
there; fixes go upstream and come back with the next copy.

## Before you open a change

```bash
make check
```

Non-zero means stop. The suite runs in about thirty seconds, touches no network,
spends no money, and writes nothing outside a temporary directory.

## How to write a test here

Tests assert **properties**, not implementation. The ordering matters: prove it
by hand against the real thing first, then write the test as the record of what
you proved. A test written before the demonstration is a guess about what the
code does.

If you are testing a protocol, **do not derive the expected values from the code
under test** — a test built from the same misunderstanding will agree with it
happily. Use the specification's own vectors, or write the other side
independently. `tests/test_ws.py` does the first; `tests/test_slack.py` does the
second.

If a change fixes a bug, add a test that pins it and **say so in the docstring**.
Several already do.

## Comments

Explain *why*, not *what*. Most of the comments here name the incident that
caused the line to exist, because six months later that is the only thing that
stops someone "simplifying" it back into the bug.

If a rule in [docs/DESIGN.md](docs/DESIGN.md) is the reason for a line, reference
the reasoning rather than restating it.

## Things that will be sent back

- A new config key without a default that is correct on an unattended machine.
- A signal with no consumer — a field written and never read.
- Anything that acknowledges before it has durably stored.
- An exclusive slot with no automatic path back to empty.
- A liveness check that can lie: a PID file, a string in a log, a bare `pgrep`.
- Widening capability by side effect.
- A number in the README stated from memory rather than from a run.
