# pith

[![CI](https://github.com/Goooooooooody/pith/actions/workflows/ci.yml/badge.svg)](https://github.com/Goooooooooody/pith/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

**Get to the pith of a failing CI run before it floods Claude's context.**

Paste a failing GitHub Actions link into Claude Code and ask why it broke. pith checks `gh` can read
the run, Claude pulls one ~4k-token summary covering the failed jobs, and diagnoses from that - instead
of 400,000 tokens of setup noise, or a truncated excerpt that misses the cause.

<p align="center"><img src="docs/demo.svg" alt="pith gh on a failed React CI run: 13,733 lines (~422k tokens) summarised to ~3.8k tokens, keeping the failing test and its snapshot diff" width="860"></p>

It works for anything you'd otherwise paste: `! pith` summarises your clipboard, `pith run` wraps
noisy commands, and a paste guard stops accidental 50,000-line pastes. Zero dependencies, local,
no API key needed.

## Does it help?

49 real failed GitHub Actions runs from 19 public projects (React, Next.js, Node, Rust, CPython,
Django, Deno, Grafana, VS Code, ...), each labelled with its root cause. Blind Claude agents were
given **one** input per run and asked for the root cause; a separate judge graded every answer.

| What Claude was given | Correct | Partial | Wrong | Avg size |
| --- | ---: | ---: | ---: | ---: |
| **pith summary** | **43 (88%)** | 6 | **0** | ~2.9k tokens |
| 30k-char excerpt of the raw log (first + last 15k) | 21 (43%) | 13 | 15 | ~7.4k tokens |

Share of the labelled must-see lines that survive: **pith 78.7%** vs 40.4% for the 30k excerpt and
53.2% for `tail -200`. All inputs, prompts, answers and verdicts are in [bench/](bench/README.md).

## Install

As a **Claude Code plugin** (the `!` command, the skill that teaches Claude to use it, and the hooks):

```text
/plugin marketplace add Goooooooooody/pith
/plugin install pith@pith
```

As a **standalone CLI** (any terminal, any AI tool):

```bash
pipx install git+https://github.com/Goooooooooody/pith      # or: uv tool install git+https://...
```

## Use it

**CI links - nothing to type.** Paste a run or job URL into your message. The plugin's hook checks
whether `gh` can read it and tells Claude which jobs failed; Claude runs `pith gh <url>` (one summary
for all failed jobs, `--job <id>` for more room on one) and answers. For a PR link it points Claude at
`pith gh <pr-url>`. If `gh` can't access the run, Claude asks you to copy the log and run `! pith`.

**Everything else - prefix with `!`** so the command runs in your shell and only its output enters
the conversation (Claude Code 2.1.186+ replies to it straight away).

| You want to... | Type |
| --- | --- |
| Hand Claude a log you copied | `! pith` (reads the clipboard) |
| ...and say what you want | `! pith why does checkout 500` |
| The latest failed Actions run on your branch | `! pith gh` |
| A specific run, job or PR | `! pith gh https://github.com/o/r/actions/runs/123/job/456` |
| Run something noisy, keep what matters | `! pith run pnpm test` (exit code preserved) |
| Pipe anything | `docker compose logs api \| pith`, `kubectl logs deploy/web \| pith` |
| A log file | `! pith server.log` |
| One group in full | `! pith show 820d` |
| Raw lines around line 211 | `! pith show 200-220` |
| Search the raw text | `! pith last --grep 'timeout\|ECONN'` |

zsh treats `?` as a glob: write `! pith why is CI failing` without the question mark, or quote it.

**The paste guard.** Paste a 13,000-line log out of habit and the prompt hook keeps it out of context
(real message, from a React CI log):

```text
● UserPromptSubmit operation blocked by hook:
  pith kept a 13,734-line paste (~422,627 tokens) out of Claude's context.
    saved:  ~/.cache/pith/inputs/20260927-114937-paste.log
    send a ~4k-token summary instead:   ! pith last "why is devtools CI failing"
    (send it as-is: start your message with raw:   ·   turn this off: PITH_GUARD=off)
```

It only acts on pastes that look like logs: code, diffs and docs pasted for review go through, and so
does anything piped into `claude -p`. (It relies on Claude Code marking pastes with
`<pasted_content>` tags; if that ever changes, the guard simply stops acting.)

**Giant command output.** When a command Claude runs succeeds but prints more than Claude Code keeps
inline (it saves the rest to a file and shows a short preview), and the output looks like logs, the
hook gives Claude a pith summary plus the full output's path. Failed commands can't be rewritten by
hooks, so the skill tells Claude to run noisy commands through `pith run`.

## What Claude sees

A title with the before/after size, which CI steps failed, then **failures in context** per failing
job - the lines around each distinct error and before each step failure, with assertions, code
frames and your code's stack frames - then other errors and warnings, the end of the output, and the
most frequent noise. `[820d]` ids and `L211` line numbers point into the saved raw file, so Claude
can drill in with `pith show 820d` or `pith show 200-220` instead of rereading everything.

## Privacy and security

- Local by default; no telemetry. Network use: `pith gh` (your `gh`), the prompt hook's `gh run view`
  when you send an Actions URL (`PITH_CI=off` disables it), and opt-in `--jev`.
- Everything pith prints is redacted first: cloud and API keys, GitHub/GitLab/Slack/npm/PyPI/OpenAI/
  Anthropic/Stripe/Hugging Face tokens, JWTs, connection strings, cookies, URL credentials,
  `password=`-style values. Private-key blocks are dropped while parsing. Best effort - see
  [SECURITY.md](SECURITY.md).
- Raw inputs are kept, **unredacted**, in a private cache (0700 directory, 0600 files) and deleted
  after 7 days (on the next run), so drill-down and `grep` can reach them.
- `pith run` executes what it is given: don't blanket-allow `Bash(pith *)`. The skill pre-approves
  only `pith show`.

## Optional: Jev root-cause ranking

`--jev` (or `PITH_JEV=1`) asks [TypeSafe's Jev](https://typesafe.ai), a fast classification model,
which groups are confident noise (hidden from the side sections, never from the failure view) and
which line is most likely the root cause (shown first). Needs your own `TYPESAFE_API_KEY`; the
project has no affiliation with TypeSafe.

- On the 49 public runs, the top pick contains the labelled cause in 26 vs 12 for the plain
  summary's first error line (`python bench/rootcause.py`).
- Only redacted, masked group templates (each with a short masked detail or stack excerpt) and your
  redacted question are sent. Cost is capped at the
  1,000 most severe groups per run (about $0.015 at most, usually far less); answers are cached for
  30 days.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `PITH_BUDGET` | `16000` | max characters of CLI output (`--budget`) |
| `PITH_GUARD` | on | `off` disables the paste guard |
| `PITH_GUARD_LINES` / `PITH_GUARD_CHARS` | `400` / `20000` | a paste must reach both (and look like a log) to be guarded |
| `PITH_CI` | on | `off` disables CI-link lookups in prompts |
| `PITH_HOOK` | on | `off` disables summarising giant Bash output |
| `PITH_HOOK_CHARS` / `PITH_HOOK_BUDGET` | `30000` / `8000` | output size that triggers the Bash hook / size of its summary |
| `PITH_JEV` | off | `1` turns on `--jev` by default |
| `PITH_JEV_MAX_GROUPS` | `1000` | cost cap for `--jev` |
| `TYPESAFE_API_KEY` (or `TYPESAFE_API_KEY_FILE`, default `~/.config/typesafe/api_key`) | - | key for `--jev` |
| `PITH_CLIPBOARD_CMD` | auto | clipboard command (auto: pbpaste, wl-paste, xclip, xsel, PowerShell, termux) |
| `PITH_CACHE_DIR` | `$XDG_CACHE_HOME/pith` (usually `~/.cache/pith`) | saved inputs and the Jev cache |
| `TYPESAFE_DEFAULT_MODEL` / `TYPESAFE_BASE_URL` | `jev-latest` / `https://api.typesafe.ai` | Jev model and endpoint |
| `PITH_DEBUG` | off | `1` makes hooks raise errors instead of failing silently |

## How it works

1. **Records**: stack frames, indented detail and code frames fold into the line that started them.
   CI prefixes (`gh` job/step columns, timestamps, ANSI colours - including GitHub's literal `^[[31m`)
   are stripped and the job name kept. Line numbers match `grep -n` on the raw file.
2. **Groups**: each record is masked (timestamps, ids, hashes, numbers, IPs) and grouped by template;
   output shows real example text, not masks.
3. **Levels**: structured JSON logs (pino, bunyan, winston, zap, ECS), level words, test-runner marks
   (✓ ✘ ● `FAIL`, `--- FAIL`, `... ok`, `N failed`), compiler errors, `##[error]` annotations.
4. **Failure windows**: context around the first occurrence of each distinct error and before every CI
   step failure; budget is shared round-robin across failing jobs, step failures first.
5. **Render** under a character budget, with library stack frames trimmed and the exception that ends a
   Python traceback always kept.

The hooks only act on text that looks like logs, using a classifier that scores timestamps, levels
and CI markers against code, markdown, diff and tabular-data signals.

## Limitations

- Heuristics: pith keeps ~79% of must-see lines on the benchmark, so for thorny failures Claude should
  drill in (`pith show`, `grep` the raw file); the footer says how many error lines were left out.
- `pith gh` is GitHub Actions only; other CI works by copying the log or piping it.
- Linters and type checkers: when you need every file:line, read their output directly.
- Windows: the CLI works with Python and PowerShell's clipboard; the plugin hooks need a POSIX shell
  (Git Bash or WSL) with Python.

## Related tools

pith handles what you *bring* to Claude - CI runs, pastes, log files. [rtk](https://github.com/rtk-ai/rtk)
and [headroom](https://github.com/headroomlabs-ai/headroom) compress output from commands Claude runs;
they combine well. [gh-fix-ci](https://agentskill.sh/plugins/shipshitdev/gh-fix-ci) has Claude pull
failing Actions logs too; pith adds the deterministic summary, the paste guard and a published
benchmark. Jev triage follows [jevlogs](https://github.com/reachjalil/jevlogs).

## Contributing

`python -m unittest discover -s tests` runs the suite (stdlib only). Engine changes should move the
benchmark: `python bench/fetch.py && python bench/recall.py bench/cases.json`. See
[CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
