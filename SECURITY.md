# Security

pith processes logs, which often contain secrets. What it guarantees:

- **Redaction.** Everything pith prints - titles, headers, summaries, hook messages - passes through
  `src/pith/redact.py`. Multi-line private keys are dropped while parsing, before grouping.
  Redaction is best effort: it covers common token formats, credentials in URLs, connection strings,
  cookies, `key=value` secrets and JWTs, but it can't recognise every secret.
- **Local by default.** Inputs are saved only on your machine (`$XDG_CACHE_HOME/pith`, a 0700
  directory with 0600 files, written without following symlinks) and pruned after 7 days, on the
  next run. There is no telemetry.
- **Network use, all through tools you already trust or opt into:**
  - `pith gh` runs your own `gh` CLI.
  - The plugin's prompt hook runs `gh run view` when a message you send contains a GitHub Actions
    URL (disable with `PITH_CI=off`). The job/branch names it reports are sanitised and labelled as
    untrusted data.
  - `--jev` / `PITH_JEV=1` sends redacted, masked group templates and your (redacted) question to
    TypeSafe's API. Off by default.
- **Hooks fail open**: an error in pith never blocks or breaks a Claude Code session.
- **Permissions.** `pith run` executes the command it is given, and `pith gh`/`--jev` reach the
  network, so the skill pre-approves only `pith show`. Don't blanket-allow `Bash(pith *)`.

Please report vulnerabilities or secret formats that get through privately, via GitHub's
"Report a vulnerability" button on the Security tab, rather than in a public issue.
