Reporting on Drove runs, for a model-run job. Bothy starts Drove runs itself, from
a scheduled `--drove-from-cairn` job, with no model turn; this job only reports on them.

- MUST run `bothy drove status` before reporting, and look for a run on the same target.
- MUST NOT retry a start that timed out. The run may already exist; check status instead.
- MUST NOT call `drove plan` yourself. A start by a model bypasses `drove_max_runs`.
- SHOULD include the run id in what you report.
- MAY say nothing when there is no run to report on.
