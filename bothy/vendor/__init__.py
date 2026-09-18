"""Third-party code vendored verbatim, kept separate from Bothy's own modules.

Vendored rather than depended on because Bothy is installed at client sites
where a package index may not be reachable and a virtualenv is friction we do
not want. Everything here is standard-library-only, which is what makes
vendoring cheap enough to prefer.

a2a_reactor — the Operator Reactor Pattern reference implementation from
github.com/montytorr/a2a-comms (MIT). It decides which events deserve an
agent's attention: triage, semantic dedupe, turn budgets, closure outcomes and
a process-wide lease. It touches no network and no API. Bothy supplies the
three adapters it asks for (WorkerRuntime, TaskTracker, AlertSink).

Do not edit vendored files. Fixes go upstream and come back with the next copy.
"""
