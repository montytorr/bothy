# Vendored from a2a-comms

This directory is a **verbatim snapshot** of `reactor/a2a_reactor/` from
[a2a-comms](https://github.com/montytorr/a2a-comms). Do not edit the files here
— edit them upstream and re-vendor, or the next sync silently discards the
change.

|            |                              |
|------------|------------------------------|
| upstream   | `e55d074`                       |
| vendored   | 2026-09-19                   |
| upstream date | 2026-09-19                     |

## Why the provenance line matters

The previous snapshot carried no record of what it was a copy of. It drifted
four files behind without anyone noticing, and the only way to find out was to
diff every file by hand. A stale copy is fine; a stale copy you cannot date is
not.

## Re-vendoring

    cp <a2a-comms>/reactor/a2a_reactor/*.py bothy/vendor/a2a_reactor/
    # update the table above with the upstream commit
    make check

## What bothy actually uses

Two modules: `lease.reactor_lease` (daemon.py) and the queue functions
(wake.py, retention.py). The rest is carried so the snapshot stays whole and
diffable rather than a subset nobody can compare to upstream — `triage_event`
in particular is never called here; bothy has its own drain in daemon.py.
