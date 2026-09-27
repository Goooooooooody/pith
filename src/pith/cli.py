"""pith - get to the pith of huge logs and command output before they reach your AI's context.

  pith [QUESTION...]                  summarise the clipboard (or stdin, when piped)
  pith FILE... [QUESTION...]          summarise files
  pith run CMD [ARGS...]              run a command, summarise its output (exit code preserved)
  pith gh [RUN|URL|PR] [QUESTION...]  a GitHub Actions run's failed-step logs (default: latest failure
                                      on this branch); --job ID for one job
  pith last [QUESTION...]             summarise the last input again (e.g. a paste pith caught)
  pith show ID[,ID...] [FILE]         full detail for groups from a summary
  pith show START-END [FILE]          raw lines from the last input (redacted)

In Claude Code, prefix with ! so only the summary enters context:  ! pith why is CI failing
"""
import argparse
import os
import re
import shutil
import stat
import sys

from . import __version__
from .engine import LEVELS, Sift, est_tokens, fmt_tokens, render, render_group
from .redact import redact
from .sources import (InputError, clipboard, github_actions, last, prune, read_lines, recent_stashes, remember,
                      run_command, split_lines, stash)

GROUP_ID = re.compile(r"^[0-9a-f]{4,40}(,[0-9a-f]{4,40})*$")
LINE_RANGE = re.compile(r"^L?(\d+)(?:-L?(\d+))?$")
TAKES_VALUE = {"--budget", "--level", "--grep", "--drop-below", "--job"}
PATHLIKE = re.compile(r"[/\\]|^~|^\.|\.(log|txt|out|err|json|jsonl|ndjson|csv)$", re.I)
TITLE_ROOM = 250  # the title line is added after rendering; keep the whole output within --budget


def _parser():
    p = argparse.ArgumentParser(prog="pith", usage="pith [FILE... | run CMD | gh [URL] | last | show ID] [QUESTION...] [options]",
                                description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.split("\n")[2:]))
    p.add_argument("--budget", type=_budget, default=os.environ.get("PITH_BUDGET", "16000"),
                   help="max characters of output (default 16000, ~4k tokens)")
    p.add_argument("--level", choices=list(LEVELS), help="only show groups at or above this level")
    p.add_argument("--grep", metavar="REGEX", type=_regex, help="only records matching REGEX (Python syntax, case-insensitive)")
    p.add_argument("--job", metavar="ID", help="with `pith gh`: only this job's logs")
    p.add_argument("--jev", action="store_true", default=None,
                   help="use TypeSafe Jev to hide confident noise and rank a root cause (needs TYPESAFE_API_KEY)")
    p.add_argument("--no-jev", dest="jev", action="store_false")
    p.add_argument("--drop-below", type=float, default=0.2, help=argparse.SUPPRESS)
    p.add_argument("--no-cache", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version="pith " + __version__)
    return p


def _budget(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("budget must be a number of characters, e.g. 16000")
    if n < 1000:
        raise argparse.ArgumentTypeError("budget must be at least 1000 characters")
    return n


def _regex(value):
    try:
        return re.compile(value, re.I)
    except re.error as e:
        raise argparse.ArgumentTypeError("invalid --grep regex %r: %s (use | for alternatives, not \\|)" % (value, e))


def _split(argv):
    """Options may appear anywhere before `run`; everything after `run` belongs to the command."""
    opts, rest, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a == "run" and not rest:
            return opts, argv[i:]
        if a == "--":
            rest += argv[i + 1:]
            break
        if a.startswith("--") or a in ("-h",):
            opts.append(a)
            if a in TAKES_VALUE and "=" not in a and i + 1 < len(argv):
                opts.append(argv[i + 1])
                i += 1
        else:
            rest.append(a)
        i += 1
    return opts, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if argv[:1] == ["hook"]:
        from . import hooks
        return hooks.run(argv[1] if len(argv) > 1 else "")
    opts, rest = _split(argv)
    if rest[:1] == ["run"]:
        cmd = rest[1:]
        while cmd and cmd[0].startswith("--") and cmd[0] != "--":  # `pith run --budget 5000 pnpm test`
            opts.append(cmd.pop(0))
            if opts[-1] in TAKES_VALUE and cmd:
                opts.append(cmd.pop(0))
        if cmd[:1] == ["--"]:
            cmd = cmd[1:]
        return _guard(lambda: _cmd_run(cmd, _parser().parse_args(opts)))
    args = _parser().parse_args(opts)
    return _guard(lambda: _dispatch(rest, args))


def _guard(fn):
    try:
        return fn()
    except InputError as e:
        sys.stderr.write("pith: %s\n" % e)
        return 2
    except BrokenPipeError:
        return 0


def _dispatch(rest, args):
    prune()
    head = rest[0] if rest else ""
    if head == "show":
        return _cmd_show(rest[1:])
    if head == "last":
        path, label = last()
        return _summarise_files([path], rest[1:], args, label=label, stash_it=False)
    if head == "gh":
        ref = rest[1] if len(rest) > 1 and (rest[1].isdigit() or "github.com" in rest[1]) else None
        text, label = github_actions(ref, args.job)
        return _summarise_text(text, label, rest[2:] if ref else rest[1:], args)
    files = []
    while rest and os.path.isfile(os.path.expanduser(rest[0])):
        files.append(os.path.expanduser(rest.pop(0)))
    if rest and not files and PATHLIKE.search(rest[0]) and not os.path.exists(os.path.expanduser(rest[0])):
        raise InputError("no such file: %s" % rest[0])
    if files:
        return _summarise_files(files, rest, args)
    if _stdin_has_data():
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8", "replace")
        return _summarise_text(data.decode("utf-8", "replace"), "stdin", rest, args)
    return _summarise_text(clipboard(), "clipboard", rest, args)


def _stdin_has_data():
    """Piped or redirected stdin (pipe, socket, non-empty file). A terminal or /dev/null (Claude Code's
    ! mode) means: read the clipboard instead."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return False
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return False
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return True
    return stat.S_ISREG(mode) and os.fstat(sys.stdin.fileno()).st_size > 0


def _summarise_text(text, label, question, args):
    if not text.strip():
        raise InputError("%s is empty" % label)
    path = stash(text, label)
    return _emit(Sift(args.grep).feed(split_lines(text)), label, path, question, args)


def _summarise_files(files, question, args, label=None, stash_it=True):
    if len(files) > 1 and stash_it:  # one combined file, so line numbers and `pith show` stay consistent
        parts = []
        for p in files:
            with open(p, "rb") as f:
                parts.append(f.read().decode("utf-8", "replace"))
        text = "".join(t if t.endswith("\n") else t + "\n" for t in parts)
        label = label or ", ".join(os.path.basename(p) for p in files)
        path = stash(text, "files")
        return _emit(Sift(args.grep).feed(split_lines(text)), label, path, question, args)
    path = files[0]
    s = Sift(args.grep).feed(read_lines(path))
    if stash_it:
        remember(path, label or os.path.basename(path))
    return _emit(s, label or os.path.basename(path), path, question, args)


def _cmd_run(cmd, args):
    if not cmd:
        raise InputError("usage: pith run CMD [ARGS...]")
    if len(cmd) > 1 and not shutil.which(cmd[0]) and not os.path.exists(cmd[0]):
        sys.stderr.write("pith: command not found: %s\n" % cmd[0])
        return 127
    try:
        text, code, secs = run_command(cmd)
    except InputError as e:
        sys.stderr.write("pith: %s\n" % e)
        return 127
    label = "`%s` · exit %d · %.1fs" % (" ".join(cmd)[:120], code, secs)
    if not text.strip():
        sys.stdout.write(redact("pith · %s · no output\n" % label))
        return code
    path = stash(text, "run " + os.path.basename(cmd[0]))
    _emit(Sift(args.grep).feed(split_lines(text)), label, path, [], args)
    return code


def _cmd_show(rest):
    if rest and LINE_RANGE.match(rest[0]):
        return _show_lines(rest)
    if not rest or not GROUP_ID.match(rest[0]):
        raise InputError("usage: pith show ID[,ID...] [FILE]  or  pith show START-END [FILE] - ids are the [xxxx] "
                         "tags and L numbers in a pith summary")
    ids = rest[0].split(",")
    if len(rest) > 1:
        if not os.path.isfile(rest[1]):
            raise InputError("no such file: %s" % rest[1])
        candidates = [rest[1]]
    else:
        try:
            candidates = [last()[0]]
        except InputError:
            candidates = []
        candidates += [p for p in recent_stashes() if p not in candidates]
    found, missing = {}, list(ids)
    for path in candidates:
        if not missing:
            break
        s = Sift().feed(read_lines(path))
        for gid in list(missing):
            if gid in s.groups:
                found[gid] = (path, s.groups[gid])
                missing.remove(gid)
    for gid in ids:
        if gid in found:
            path, g = found[gid]
            sys.stdout.write(redact("%s\n(from %s)" % (render_group(g, full=True), path)) + "\n\n")
    for gid in missing:
        sys.stdout.write("[%s] not found in the last input or the %d most recent inputs\n" % (gid, len(candidates)))
    return 1 if missing else 0


def _show_lines(rest):
    """Raw lines START-END of the last input (or FILE), redacted: drill-down without reading the file."""
    m = LINE_RANGE.match(rest[0])
    start, end = int(m.group(1)), int(m.group(2) or m.group(1))
    if end < start or end - start > 400:
        raise InputError("line ranges are limited to 400 lines, e.g. pith show 120-160")
    path = rest[1] if len(rest) > 1 else last()[0]
    if not os.path.isfile(path):
        raise InputError("no such file: %s" % path)
    out = []
    for no, line in enumerate(read_lines(path), 1):
        if no > end:
            break
        if no >= start:
            out.append("L%-6d %s" % (no, line.rstrip("\n")[:2000]))
    sys.stdout.write(redact("\n".join(out)) + "\n(from %s)\n" % redact(os.path.abspath(path)))
    return 0


def _use_jev(args):
    if args.jev is not None:
        return args.jev
    return os.environ.get("PITH_JEV", "").lower() in ("1", "on", "true", "yes")


def _emit(s, label, path, question, args):
    question = " ".join(question).strip()
    order, jev_lines = None, []
    if _use_jev(args):
        from .jev import JevError, triage
        try:
            order, jev_lines = triage(s, question, args.drop_below, not args.no_cache)
        except JevError as e:
            jev_lines = ["jev skipped: %s" % e]
    path = os.path.abspath(path)
    levels = s.levels
    steps = s.failed_steps
    header = ["", "question: " + question if question else None,
              "failed: " + "; ".join(steps[:8]) + (" (+%d more)" % (len(steps) - 8) if len(steps) > 8 else "")
              if steps else ("jobs: " + "; ".join(s.jobs[:8]) if s.jobs else None),
              "levels: " + " · ".join("%s %s" % (lv, "{:,}".format(levels[lv][1]))
                                      for lv in sorted(levels, key=lambda x: -LEVELS[x])),
              "raw: %s  ·  detail: pith show <id>  ·  lines: pith show 120-160  ·  search: pith last --grep REGEX" % path]
    header = [h for h in header if h is not None] + jev_lines
    if order:  # jev: keep the failure view, lead with the most likely root cause
        top = order[0]
        header += ["", "== LIKELY ROOT CAUSE (jev rc=%.2f)" % (top.rc or 0), render_group(top)]
    body = render(s, budget=max(1000, args.budget - TITLE_ROOM), level_floor=args.level, header=header)
    before, after = est_tokens(s.chars), est_tokens(len(body) + 150)
    saved = 100.0 * (1 - after / float(before)) if before > after else 0.0
    title = "pith · %s · %s lines → %d groups · ~%s → ~%s tokens%s" % (
        label, "{:,}".format(s.lines), len(s.groups), fmt_tokens(before), fmt_tokens(after),
        " (%.1f%% smaller)" % saved if saved >= 1 else "")
    sys.stdout.write(redact(title + "\n" + body) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
