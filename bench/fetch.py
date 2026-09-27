#!/usr/bin/env python3
"""Download the benchmark's public GitHub Actions logs (failed steps) into bench/logs/ with `gh`.

GitHub keeps Actions logs for 90 days (by default), so older runs may have expired; missing cases
are reported and skipped by recall.py. Logs are public but large (~125 MB total).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    cases = json.load(open(os.path.join(HERE, "cases.json")))
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    ok = 0
    for c in cases:
        path = os.path.join(HERE, os.pardir, c["file"])
        if os.path.exists(path) and os.path.getsize(path) > 0:
            ok += 1
            continue
        proc = None
        for extra in ([], ["--attempt", "1"]):  # re-run workflows keep the original attempt's logs
            proc = subprocess.run(["gh", "run", "view", c["run_id"], "-R", c["repo"], "--log-failed"] + extra,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode == 0 and proc.stdout.strip():
                break
        if proc.returncode != 0 or not proc.stdout.strip():
            print("skip %s %s: %s" % (c["repo"], c["run_id"], proc.stderr.decode("utf-8", "replace").strip()[:120]))
            continue
        with open(path, "wb") as f:
            f.write(proc.stdout)
        ok += 1
    print("%d/%d logs available in bench/logs/" % (ok, len(cases)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
