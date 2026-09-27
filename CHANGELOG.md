# Changelog

## 0.1.0 - 2026-09-27

First release.

- `pith` CLI: clipboard, stdin (pipes and sockets), files, `run` (exit code preserved), `gh`
  (GitHub Actions run/job/PR URLs, or the latest failure on the current branch), `last`, `show`.
- Engine: continuation folding, CI prefix and ANSI stripping (including GitHub's literal `^[[31m`
  form), template grouping with unique ids, level detection for structured logs, test runners,
  compilers and CI annotations; per-job failure windows; Python traceback trimming; secret redaction
  (private-key blocks removed at parse time); ~4k-token budget.
- Claude Code plugin: skill, `bin/pith` on PATH, paste guard, CI-link hints, giant-output hook.
- Optional Jev triage (`--jev`): noise hiding and root-cause ranking, cost-capped and cached.
- Benchmarks on 49 public CI failures (`bench/`), including every input and graded diagnosis.
