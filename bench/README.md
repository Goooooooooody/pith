# Benchmarks

Two questions, measured on 49 real failed GitHub Actions runs from 19 public repositories
(`cases.json`: repo, run id, URL, labelled root cause and 2-5 verbatim "must-see" lines per run).

## 1. Does Claude diagnose the failure correctly? (`diagnosis-results.json`)

For every run, a fresh Claude agent was given exactly one input file and asked for the root cause,
with no other files, tools or web access:

- **pith** - `pith <log>` with default settings (16k-character budget)
- **30k excerpt** - the raw log cut to 30,000 characters: first 15,000 + last 15,000 with a
  truncation marker. This stands in for "a truncated log"; current Claude Code versions instead save
  oversized command output to a file and show Claude a short preview plus the path.

A separate judge agent compared each answer with the label: *correct* (same root cause, no
significant wrong claim), *partial* (right area or only part of a multi-cause failure), or *wrong*
(different cause, only generic symptoms such as "exit code 1", or "can't tell"). Every diagnosis
and judgement is in `diagnosis-results.json`.

| Input | Correct | Partial | Wrong |
| --- | ---: | ---: | ---: |
| pith | 43 | 6 | 0 |
| 30k excerpt | 21 | 13 | 15 |

`pith --jev` summaries are in `diagnosis/inputs/` too; they are scored with `rootcause.py` (is the
cause the first thing shown?) rather than by agents.

The labels were produced by agents reading each raw log (grep plus targeted reads) and choosing
verbatim evidence lines, each verified to occur in the log. Inputs, prompts and protocol:
[`diagnosis/`](diagnosis/README.md).

## 2. How much of the evidence survives? (`recall.py`)

```bash
python bench/fetch.py                  # downloads the logs with `gh` into bench/logs/ (~126 MB)
python bench/recall.py bench/cases.json --per-case
```

`recall.py` reports, for pith / the 30k excerpt / `tail -200`, the share of must-see substrings present in
the output, how many runs keep *all* of their evidence, and the average output size (chars / 4).

GitHub keeps Actions logs for 90 days by default, so some runs will expire; missing logs are skipped
and reported. Everything runs offline once the logs are fetched; `--jev` needs a TypeSafe key.

## Caveats

- 49 runs is a modest sample, and CI logs are pith's home turf. Dev-server and application logs are
  covered by the engine but not by this benchmark.
- Agent judges can be wrong; read `diagnosis-results.json` to check any verdict.
- The 30k excerpt is a stand-in, not an exact model of any tool: pasting a log puts all of it in
  context (often more than fits), and Claude Code shows a ~2KB preview of oversized command output
  plus a file path Claude can read in chunks.
