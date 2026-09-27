# Diagnosis evaluation

`inputs/` holds exactly what each blind agent saw, for the 49 public runs in `../cases.json`:

- `oss-<run>.pith.txt` - `pith <log>` (default 16k budget) from this release
- `oss-<run>.pith_jev.txt` - `pith <log> --jev` (scored by `../rootcause.py`, not by agents)
- `oss-<run>.excerpt.txt` - the raw log (ANSI stripped) cut to 30,000 characters: first 15,000 + a
  `... [N characters truncated] ...` marker + last 15,000 (unchanged when the log is shorter)

In every file the `raw:` path and the log's file name were replaced with placeholders so no agent
could open the original.

## Protocol

Each (run, input) pair went to a fresh Claude agent (Claude Code workflow subagent, Opus 5.5) with
this prompt, where FILE is the input's path:

> You are debugging a failed CI run. The ONLY information you may use is the file FILE - read it
> (with the Read tool; it may be long). Do not open, search or list any other file or directory, do
> not run commands, and do not use the web. The file may be a summary or a truncated copy of the
> log; some context is missing on purpose. Based only on it, state the root cause of the failure as
> specifically as you can (which test/step/error and why), the evidence lines, and your confidence.
> If the file genuinely does not show the cause, say so.

A second, separate agent graded each answer against the run's label (`root_cause` and `must_keep`
in `../cases.json`):

> Verdict: "correct" = identifies the same root cause (for multi-cause runs: the main cause(s)) with
> no significant wrong claims; "partial" = right area/symptom or only a minor part of the causes, or
> right cause mixed with a significant wrong claim; "wrong" = different cause, only generic symptoms
> (e.g. just "exit code 1"/"tests failed"), or says it can't tell. Be strict and consistent.

Every answer and verdict is in `../diagnosis-results.json`. The labels themselves were produced by
agents reading each full log with grep and targeted reads; each `must_keep` line was verified to
occur verbatim in the log.

Regenerating the pith inputs from the raw logs reproduces them exactly only when the log's path has
the same length as ours (the `raw:` header line counts against the output budget); at other paths a
few summaries differ by a line or two.

The pith inputs were produced and graded with pith 0.1.0 as first published (commit b812d8b); later
patch releases may produce slightly different summaries. The excerpt inputs don't depend on pith;
they were graded in an earlier run with the same prompts, and are unchanged.
