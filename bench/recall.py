#!/usr/bin/env python3
"""Evidence-recall benchmark: does the summary still contain the lines a human needs?

Input: a JSON list of cases ``{"path": ..., "must_keep": ["verbatim substring", ...]}`` labelled by
reading each log (see bench/README.md). For every case we measure, against the raw log:

  pith      - pith's default summary (16k-char budget)
  30k-excerpt - the raw log cut to 30,000 characters: first 15k + last 15k
  tail200   - the last 200 lines (a common manual habit)

and report the share of must-keep substrings present plus the output size.

  python3 bench/recall.py cases.json [--budget 12000] [--jev] [--per-case]
"""
import argparse
import html
import io
import json
import os
import re
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))
from pith import cli  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")
WS = re.compile(r"\s+")


def norm(s):
    return WS.sub(" ", ANSI.sub("", html.unescape(s))).strip()


def pith_output(path, budget, jev):
    buf = io.StringIO()
    argv = [path, "--budget", str(budget)] + (["--jev"] if jev else ["--no-jev"])
    with redirect_stdout(buf):
        cli.main(argv)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("--budget", type=int, default=16000)
    ap.add_argument("--jev", action="store_true")
    ap.add_argument("--per-case", action="store_true")
    ap.add_argument("--filter", help="only cases whose path contains this")
    a = ap.parse_args()
    cases = json.load(open(a.cases))
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
    for c in cases:  # bench/cases.json stores repo-relative "file"; private corpora may use "path"
        c.setdefault("path", os.path.join(base, c.get("file", "")))
    missing = [c for c in cases if not os.path.exists(c["path"])]
    if missing:
        print("skipping %d cases whose logs are missing (run bench/fetch.py)" % len(missing))
    cases = [c for c in cases if os.path.exists(c["path"])]
    if a.filter:
        cases = [c for c in cases if a.filter in c["path"]]
    totals = {k: [0, 0, 0] for k in ("pith", "30k-excerpt", "tail200")}  # found, wanted, chars
    perfect = {k: 0 for k in totals}
    jev_cost = [0.0]
    for c in cases:
        raw = open(c["path"], encoding="utf-8", errors="replace").read()
        clean = ANSI.sub("", raw)
        outs = {
            "pith": pith_output(c["path"], a.budget, a.jev),
            "30k-excerpt": clean[:15000] + "\n...\n" + clean[-15000:] if len(clean) > 30000 else clean,
            "tail200": "\n".join(clean.splitlines()[-200:]),
        }
        m = re.search(r"tokens · \$([\d.]+)", outs["pith"])
        if m:
            jev_cost[0] += float(m.group(1))
        row = []
        for k, out in outs.items():
            n = norm(out)
            found = sum(1 for m in c["must_keep"] if norm(m) in n)
            totals[k][0] += found
            totals[k][1] += len(c["must_keep"])
            totals[k][2] += len(out)
            perfect[k] += found == len(c["must_keep"])
            row.append("%s %d/%d %6dc" % (k, found, len(c["must_keep"]), len(out)))
        if a.per_case:
            print("%-60s raw %8dc | %s" % (os.path.basename(c["path"])[:60], len(raw), " | ".join(row)))
    if a.jev:
        print("jev spend: $%.4f over %d cases" % (jev_cost[0], len(cases)))
    raw_chars = sum(os.path.getsize(c["path"]) for c in cases)
    print("\n%d cases, %d must-keep lines, %.1f MB raw" % (len(cases), totals["pith"][1], raw_chars / 1e6))
    print("%-10s %8s %14s %14s" % ("method", "recall", "all-evidence", "avg tokens"))
    for k, (f, w, ch) in totals.items():
        print("%-10s %7.1f%% %8d/%-5d %14s" % (k, 100.0 * f / max(w, 1), perfect[k], len(cases), "{:,}".format(ch // 4 // max(len(cases), 1))))


if __name__ == "__main__":
    main()
