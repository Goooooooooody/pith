#!/usr/bin/env python3
"""How often is the *first* thing pith shows the actual cause?

Compares, over bench/diagnosis/inputs/, the `LIKELY ROOT CAUSE` block of `pith --jev` summaries with
the first error entry of plain summaries, counting runs where it contains a labelled must-see line.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from recall import norm  # noqa: E402


def first_error(text):
    out, on = [], False
    for line in text.split("\n"):
        if re.match(r"^L\d+\s+✱", line):
            if on:
                break
            on = True
        elif on and re.match(r"^(L\d+ |-- job|==)", line):
            break
        if on:
            out.append(line)
    return "\n".join(out)


def main():
    cases = json.load(open(os.path.join(HERE, "cases.json")))
    d = os.path.join(HERE, "diagnosis", "inputs")
    jev_hits = plain_hits = 0
    for c in cases:
        keep = [norm(k) for k in c["must_keep"]]
        with open(os.path.join(d, "oss-%s.pith_jev.txt" % c["run_id"]), encoding="utf-8") as f:
            parts = f.read().split("== LIKELY ROOT CAUSE", 1)
        with open(os.path.join(d, "oss-%s.pith.txt" % c["run_id"]), encoding="utf-8") as f:
            plain = first_error(f.read())
        if len(parts) == 2 and any(k in norm(parts[1].split("\n== ", 1)[0]) for k in keep):
            jev_hits += 1
        if any(k in norm(plain) for k in keep):
            plain_hits += 1
    print("jev LIKELY ROOT CAUSE contains the labelled evidence: %d/%d" % (jev_hits, len(cases)))
    print("plain summary's first error contains it:            %d/%d" % (plain_hits, len(cases)))


if __name__ == "__main__":
    main()
