"""Claude Code hooks. All of them fail open: any problem and the hook does nothing.

prompt (UserPromptSubmit)
  paste guard: a log dump pasted into the prompt box is saved to the stash and blocked before it
    costs a single token; the user sees how to send a summary instead (``! pith last "<question>"``)
    or the paste as-is (``raw:``). Hooks can block or add to a prompt but not rewrite it, so this is
    the only way to keep a paste out of context. Only text inside Claude Code's <pasted_content>
    tags is considered, so prompts piped into ``claude -p`` are never blocked.
  CI links: a GitHub Actions run URL in the prompt is checked with ``gh``; Claude is told whether the
    run is readable and which jobs failed, so it fetches summaries itself with ``pith gh``.

post-bash (PostToolUse on Bash)
  when a successful command prints more than Claude Code keeps inline (it saves the rest to a file
  and shows Claude a short preview), and the output looks like logs, Claude gets a pith summary plus
  the path of the full output instead. Failed commands can't be rewritten by hooks, which is why the
  skill tells Claude to run noisy commands through ``pith run``.
"""
import json
import os
import re
import sys

from .engine import Sift, log_likeness, render
from .redact import redact
from .sources import GH_PR_URL, GH_URL, UNSAFE_CHARS, github_run_summaries, prune, split_lines, stash

LOGISH = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|\[?\d{2}:\d{2}|\[?(INFO|WARN|ERROR|DEBUG|TRACE|FATAL)\b|at \S|\{)|\t")
MIN_SCORE = 0.35  # log_likeness cut-off (tuned so code, diffs, docs and data are never treated as logs)
QUESTION_START = re.compile(r"^(why|what|how|where|when|which|who|can|could|please|help|fix|explain|debug|is|are|does|did)\b", re.I)
PASTE_TAG = re.compile(r"<pasted_content[^>]*>\n?(.*?)\n?</pasted_content[^>]*>", re.S)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _off(name):
    return os.environ.get(name, "").lower() in ("0", "off", "false", "no")


def split_paste(prompt):
    """Claude Code wraps pasted text in <pasted_content> tags: return (pasted, typed)."""
    pasted = PASTE_TAG.findall(prompt)
    typed = " ".join(PASTE_TAG.sub(" ", prompt).split())
    return "\n".join(pasted), typed


def _clean_question(text):
    """The question is shown inside double quotes in a command the user copies: keep it inert (and
    redacted, since running that command puts it back in the conversation)."""
    text = UNSAFE_CHARS.sub(" ", redact(text)).replace("<redacted>", "REDACTED")
    return " ".join(re.sub(r"[`$\"'\\|;&<>(){}!*?\[\]]", "", text).split())[:200]


def _question(pasted):
    """Best guess at the user's own words at the edge of a paste: a short, prose-like line."""
    lines = [l.strip() for l in pasted.strip().splitlines() if l.strip()]
    for line in lines[:3] + lines[-3:][::-1]:
        if 4 <= len(line) <= 200 and re.search(r"[a-zA-Z]{3}", line) and \
                (line.endswith("?") or QUESTION_START.match(line)) and not LOGISH.search(line):
            return _clean_question(line)
    return ""


def paste_guard(data):
    prompt = data.get("prompt") or ""
    if not isinstance(prompt, str) or _off("PITH_GUARD") or prompt.lstrip().lower().startswith("raw:"):
        return None
    pasted, typed = split_paste(prompt)
    if not pasted:
        return None  # typed or piped (claude -p) prompts are never blocked
    lines = pasted.count("\n") + 1
    if lines < _env_int("PITH_GUARD_LINES", 400) or len(pasted) < _env_int("PITH_GUARD_CHARS", 20000) \
            or log_likeness(pasted) < MIN_SCORE:
        return None
    prune()
    path = stash(pasted, "paste")
    question = _clean_question(typed) or _question(pasted)
    reason = ("pith kept a %s-line paste (~%s tokens) out of Claude's context.\n"
              "  saved:  %s\n"
              "  send a ~4k-token summary instead:   ! pith last%s\n"
              "  (send it as-is: start your message with raw:   ·   turn this off: PITH_GUARD=off)"
              % ("{:,}".format(lines), "{:,}".format(len(pasted) // 4), path,
                 ' "%s"' % question if question else "   (add your question after it)"))
    return {"decision": "block", "reason": reason,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}}


def ci_links(data):
    """GitHub Actions run URLs in the prompt: check gh can read them and tell Claude which jobs failed."""
    prompt = data.get("prompt") or ""
    if not isinstance(prompt, str) or _off("PITH_CI") or len(prompt) > 50000:
        return None
    urls, prs = [], []
    for m in GH_URL.finditer(prompt):
        url = "https://" + m.group(0)
        if url not in urls:
            urls.append(url)
    for m in GH_PR_URL.finditer(prompt):
        url = "https://" + m.group(0)
        if url not in prs:
            prs.append(url)
    if not urls and not prs:
        return None
    notes = []
    for url, (ok, text) in zip(urls[:3], github_run_summaries(urls[:3])):
        if ok:
            notes.append(text + "\n  -> run `pith gh %s` (all failed jobs) or `pith gh %s --job <id>` for one job; "
                                "each prints a ~4k-token summary. Don't fetch the raw log." % (url, url))
        else:
            notes.append("%s: %s. Ask the user to open the failing job's log, copy it, and run `! pith` "
                         "(or paste the output of `gh run view --log-failed`)." % (url, text))
    for url in prs[:3]:
        notes.append("%s is a pull request: `pith gh %s` summarises its latest failed Actions run." % (url, url))
    context = "[pith] GitHub links in this message:\n" + "\n".join(notes)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": redact(context)}}


def prompt(data):
    """UserPromptSubmit: guard huge pastes first; otherwise look for CI links."""
    return paste_guard(data) or ci_links(data)


def post_bash(data):
    if _off("PITH_HOOK") or data.get("tool_name") != "Bash":
        return None
    resp = data.get("tool_response")
    if not isinstance(resp, dict) or resp.get("isImage"):
        return None
    command = (data.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or re.search(r"(^|[\s|;&/])pith(\s|$)", command):
        return None
    full = resp.get("persistedOutputPath")  # Claude Code saved the complete output; stdout is a preview
    if isinstance(full, str) and os.path.isfile(full):
        with open(full, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    else:
        stdout, stderr = resp.get("stdout") or "", resp.get("stderr") or ""
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            return None
        text = stdout + ("\n" + stderr if stderr else "")
    if len(text) < _env_int("PITH_HOOK_CHARS", 30000) or log_likeness(text) < MIN_SCORE:
        return None
    prune()
    path = full if isinstance(full, str) and os.path.isfile(full) else stash(text, "bash", remember_it=False)
    s = Sift().feed(split_lines(text))
    body = render(s, budget=_env_int("PITH_HOOK_BUDGET", 8000))
    head = ("[pith] this command printed %s lines (~%s tokens); showing a summary. Full output: %s  "
            "(grep -n PATTERN it, or `pith show <id> %s` for a group)"
            % ("{:,}".format(s.lines), "{:,}".format(len(text) // 4), path, path))
    updated = {k: v for k, v in resp.items() if k not in ("persistedOutputPath", "persistedOutputSize")}
    updated.update({"stdout": redact(head + "\n" + body), "stderr": ""})
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}


HOOKS = {"prompt": prompt, "paste-guard": paste_guard, "ci-links": ci_links, "post-bash": post_bash}


def run(name):
    """Entry point for `pith hook <name>`: JSON on stdin, JSON (or nothing) on stdout, always exit 0."""
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        data = json.loads(raw.decode("utf-8", "replace"))
        result = HOOKS[name](data) if isinstance(data, dict) else None
        if result:
            sys.stdout.write(json.dumps(result))
    except Exception:  # noqa: BLE001 - a hook must never break the session
        if os.environ.get("PITH_DEBUG"):
            raise
    return 0
