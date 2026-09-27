"""The deterministic core: turn a pile of log/command output into a ranked, budgeted summary.

Pipeline: lines -> records (continuation lines folded into their head) -> templates
(variable parts masked) -> groups (count, first/last seen, exemplars) -> render.
No dependencies, no network. Everything here is safe to run on any input.
"""
import hashlib
import json
import re

from .redact import redact

LEVELS = {"trace": 0, "debug": 1, "info": 2, "notable": 3, "warn": 4, "error": 5, "fatal": 6}
PINO_LEVELS = {10: "trace", 20: "debug", 30: "info", 40: "warn", 50: "error", 60: "fatal"}

# real escapes, plus the literal "^[[31m" form some tools (and GitHub's log API) write out as text
ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")
# `gh run view --log(-failed)` lines: "<job>\t<step>\t<timestamp> <message>"
GH_PREFIX = re.compile(r"^([^\t\n]{1,200})\t([^\t\n]{0,200})\t(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) ?")
# the same, after a terminal paste turned the tabs into runs of spaces
GH_PREFIX_SPACED = re.compile(r"^(\S.{0,200}?) {2,}(\S.{0,200}?) {2,}(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) ?")
# GitHub Actions web UI / raw logs, Buildkite, most CI: a bare ISO stamp at the start of every line
CI_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) ?")
CI_NOISE = re.compile(r"^(##\[endgroup\]|##\[debug\]|##\[(start|end)-action\b|::endgroup::|::debug::)")
CI_GROUP = re.compile(r"^(##\[group\]|::group::)")  # step headers: keep the text, drop the marker
CONTINUATION = re.compile(
    r"^(\s*at \S|\s*[^\s@]*@\S+:\d+:\d+\s*$|\s*>?\s*\d+\s*\||\s*\||Import trace|\s*\.\.\. \d+ more|"
    r"Caused by:|\s*[}\])]+[,;]?\s*$|\s*File \"[^\"]+\", line \d+|\s*\^+\s*$|\s*~+\^*~*\s*$)")
PASS_MARK = re.compile(r"^\s*(✓|✔|√|PASS\b|ok \d|\[\s*OK\s*\]|● Console\s*$)|\.\.\. (ok|skipped\b.*|expected failure)\s*$|"
                       r"\sPASSED(\s+\[\s*\d+%\])?\s*$")
FAIL_MARK = re.compile(
    r"^\s*(✘|✗|×|●(?! Console\s*$)|FAIL(ED)?\b|not ok \d|##\[error\]|::error\b|E\s{3})|^\s*\d+\) \S|^\s*[1-9]\d* (failed|errors?)\b|"
    r"\.{3,}\s*Failed\s*$|"
    r"^(error|fatal)(\[\w+\])?: |^\S+:\d+:\d+: error\b|^Traceback \(most recent call last\)|^panicked at|"
    r"^thread '.*' panicked at|^--- FAIL:|^FAIL\t|^npm ERR!|^ERR!|^\[ERROR\]|^Error: ")
TEXT_LEVEL = [
    ("fatal", re.compile(r"\b(FATAL|CRITICAL|PANIC|panicked)\b")),
    ("error", re.compile(
        r"\b(ERROR|ERR!?)\b|\b[A-Z]\w*(Error|Exception)\b|\bUnhandled\b|\buncaught\b|Module not found|"
        r"Cannot find module|Can't resolve|Failed to compile|^\s*⨯ |segmentation fault|"
        r"core dumped|\bexit (code|status) [1-9]", re.I)),
    ("warn", re.compile(r"(?<![-=])\bWARN(ING)?\b(?![-=])|\bdeprecat|^\s*⚠", re.I)),
    ("debug", re.compile(r"\b(DEBUG|TRACE|VERBOSE)\b")),
]
FAILED_WORD = re.compile(r"\bfail(ed|ure|ures)?\b", re.I)
ZERO_FAILED = re.compile(r"\b0 (failed|failures?)\b|\bfail(ed|ures?)\s*[:=]?\s*0\b|\bno failures\b|"
                         r"--fail|fail-fast|fail_on|failOn|allow.?fail|if-no-files-found|fail-on", re.I)
NOTABLE = re.compile(
    r"fail|timeout|timed out|refused|denied|unauthori[sz]ed|forbidden|ECONN|ENOTFOUND|EADDRINUSE|"
    r"\b(status|statusCode|code)\W{0,3}5\d\d\b|\b5\d\d (Internal|Bad|Service|Gateway)|exited with|killed|OOM|retry|"
    r"not found|missing|invalid|unable to|could not|cannot", re.I)
MASKS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<tok>"),
    (re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?"), "<n>"),  # also "12ms", "3.2s"; keeps codes like E501
]
JSON_SKIP = {"level", "time", "timestamp", "msg", "message", "err", "error", "pid", "hostname", "v", "stack", "@timestamp"}
LONG_PATH = re.compile(r"(?:/[^\s/():]+){4,}(/[^\s/():]+/[^\s/():]+/[^\s/():]+)")
NOISY_FRAME = re.compile(r"node_modules|node:internal|\(internal/|\(<anonymous>\)|at <anonymous>\s*$|webpack-internal|\.next/|site-packages/|"
                         r"/usr/lib/python|<frozen |/rustc/|\.cargo/registry|/go/pkg/mod/|java\.base/")
FRAME = re.compile(r"^\s*(at |File \"|[^\s@]*@\S+:\d+:\d+\s*$|\d+: |#\d+ )")
MAX_CONT = 300
MASK_MAX = 1000
# private key material is dropped while parsing: PEM/OpenSSH/PGP blocks, SSH2 and PuTTY key files
PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|---- BEGIN SSH2 [A-Z ]*PRIVATE KEY ----|"
                       r"^\s*Private-Lines: \d+")
PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|---- END SSH2 [A-Z ]*PRIVATE KEY ----|"
                     r"^\s*Private-MAC:")
# a bare base64 line (the body of a key or certificate that arrived without its header): no
# diagnostic value, possibly secret
B64_LINE = re.compile(r"^\s*(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{56,}={0,2}\s*$")
# CI chatter that shouldn't use up the context before a step failure
CI_BORING = re.compile(r"^\s*(Collecting |Downloading |Installing |Using cached |Requirement already satisfied|"
                       r"Successfully installed |env:|with:|shell: |▶ Post )")
CI_CLEANUP = re.compile(r"^(Post job cleanup\.|▶ Post |Cleaning up orphan processes|Evaluate and set job outputs)")
CONTEXT_BEFORE, CONTEXT_AFTER, CONTEXT_CONT = 3, 3, 3  # records around a new error's first occurrence
CI_EXIT_BEFORE = 12  # records before a CI step failure (##[error] ...): the step's final output
MAX_ANCHORS = 2000
GAP = 3  # window lines further apart than this start a new block
END_RESERVE = 1800  # budget kept for the end-of-output section


def mask(s):
    s = s[:MASK_MAX]  # templates are cut to 300 chars anyway; bounds regex work on megabyte lines
    for rx, rep in MASKS:
        s = rx.sub(rep, s)
    return s


def _flatten(obj, prefix="", depth=0):
    for k, v in obj.items():
        key = "%s%s" % (prefix, k)
        if isinstance(v, dict):
            if depth < 6:
                for item in _flatten(v, key + ".", depth + 1):
                    yield item
        elif not isinstance(v, list) and v not in (None, ""):
            yield key, v


def parse_json(line):
    """Structured (pino/bunyan/winston/zap/ECS-style) log line -> (level, msg, ctx, stack, ts) or None."""
    try:
        return _parse_json(line)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def _parse_json(line):
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    lvl = obj.get("level", obj.get("severity", obj.get("log.level", "info")))
    level = PINO_LEVELS.get(lvl) if isinstance(lvl, int) else str(lvl).lower()
    level = {"warning": "warn", "err": "error", "critical": "fatal", "panic": "fatal", "dpanic": "fatal"}.get(level, level)
    if level not in LEVELS:
        level = "info"
    msg = str(obj.get("msg") or obj.get("message") or "")
    err = obj.get("err") or obj.get("error")
    stack = ""
    if isinstance(err, dict):
        stack = err.get("stack") or ""
        if not isinstance(stack, str):
            stack = str(stack)
        msg = ("%s | %s: %s" % (msg, err.get("type", "Error"), err.get("message", ""))).strip(" |")
    elif isinstance(err, str):
        msg = ("%s | %s" % (msg, err)).strip(" |")
    if level in ("info", "debug") and err:
        level = "error"
    ctx = " ".join("%s=%s" % (k, v) for k, v in _flatten(obj) if k.split(".")[0] not in JSON_SKIP)
    ts = obj.get("time") or obj.get("timestamp") or obj.get("@timestamp")
    return level, msg or ctx[:200], ctx, stack.splitlines()[1:], str(ts) if ts else None


CONFIG_KV = re.compile(r"^\s*[\w.-]+\s*[:=]\s*[\"']?(error|warn|warning|info|debug|trace|true|false)[\"']?\s*$", re.I)


def text_level(line):
    if PASS_MARK.search(line) or CONFIG_KV.match(line):  # "key: error" is a setting, not an error
        return "info"
    if FAIL_MARK.search(line):
        return "error"
    if line.lstrip().startswith(("##[warning]", "::warning")):
        return "warn"
    for name, rx in TEXT_LEVEL:
        if rx.search(line):
            return name
    if FAILED_WORD.search(line) and not ZERO_FAILED.search(line):
        return "error"
    return "notable" if NOTABLE.search(line) else "info"


def _indent(line):
    return len(line) - len(line.lstrip())


STRONG_ERROR = re.compile(r"\b(ERROR|FATAL|CRITICAL)\b|\b[A-Z]\w*(Error|Exception):|\bpanicked\b|"
                          r"^\s*(error|fatal)(\[\w+\])?:|: error\b|\bUnhandled\b|segmentation fault", re.I)


def _strong_error(line):
    return bool(STRONG_ERROR.search(line)) and not PASS_MARK.search(line)


class Record(object):
    __slots__ = ("no", "head", "cont", "ts", "job", "step", "head_error")

    def __init__(self, no, head, ts, job, step):
        self.no, self.head, self.cont, self.ts, self.job, self.step = no, head, [], ts, job, step
        self.head_error = None  # computed lazily: is the head itself an error line?


def records(lines):
    """Yield Records. A line continues the record above when it is indented deeper than the head
    or looks like a stack/code frame. CI prefixes are stripped (the gh job/step names are kept)."""
    rec = None
    in_key = False
    for no, raw in enumerate(lines, 1):
        line = ANSI.sub("", raw.rstrip("\n")).replace("\ufeff", "")  # GitHub logs put BOMs mid-line
        if in_key:  # never let private key material into groups, examples or output
            in_key = not PEM_END.search(line)
            continue
        if PEM_END.search(line) and rec is not None:  # the tail of a key that started before our input
            if re.match(r"^\s*[A-Za-z0-9+/=]{4,}\s*$", rec.head) or rec.head.strip() == "<base64 data>":
                rec.head = "<private-key>"
                rec.cont = []
            continue
        if PEM_BEGIN.search(line):
            if rec is not None:
                yield rec
            rec = Record(no, "<private-key>", None, rec.job if rec is not None else None, None)
            in_key = not PEM_END.search(line)
            continue
        ts = job = step = None
        m = GH_PREFIX.match(line) or GH_PREFIX_SPACED.match(line)
        if m:
            job, step, ts, line = m.group(1).strip(), m.group(2).strip(), m.group(3), line[m.end():]
            if step == "UNKNOWN STEP":
                step = None
        else:
            m = CI_PREFIX.match(line)
            if m:
                ts, line = m.group(1), line[m.end():]
        if not line.strip() or CI_NOISE.match(line):
            continue
        line = CI_GROUP.sub("▶ ", line)
        if B64_LINE.match(line):
            line = line[:len(line) - len(line.lstrip())] + "<base64 data>"
        event = PASS_MARK.search(line) or FAIL_MARK.search(line)  # test results / errors always start a record
        if rec is not None and not event and job == rec.job and not line.lstrip().startswith("{") and (
                CONTINUATION.match(line) or _indent(line) > _indent(rec.head)):
            if rec.head_error is None:
                rec.head_error = text_level(rec.head) in ("error", "fatal")
            if not rec.head_error and not PASS_MARK.search(rec.head) and not FRAME.match(line) and _strong_error(line):
                yield rec  # an error inside indented output of an ordinary line: its own record
                rec = Record(no, line, ts, job, step)
                continue
            if len(rec.cont) < MAX_CONT:
                rec.cont.append(line)
            continue
        if rec is not None:
            yield rec
        rec = Record(no, line, ts, job, step)
    if rec is not None:
        yield rec


class Group(object):
    """All records sharing one masked template."""
    __slots__ = ("id", "template", "level", "count", "first", "last", "first_ts", "last_ts", "examples",
                 "job", "score", "rc", "keep", "cleanup")

    def __init__(self, gid, template, level, job):
        self.id, self.template, self.level, self.job = gid, template, level, job
        self.count, self.first, self.last, self.first_ts, self.last_ts = 0, 0, 0, None, None
        self.examples, self.score, self.rc, self.keep = [], None, None, True
        self.cleanup = False  # first seen in CI post-job cleanup


class Line(object):
    """One record as it appears in a failure window."""
    __slots__ = ("no", "job", "gid", "level", "text", "cont")

    def __init__(self, no, job, gid, level, text, cont):
        self.no, self.job, self.gid, self.level, self.text, self.cont = no, job, gid, level, text, cont


class Sift(object):
    """Accumulates groups (and failure windows) from one or more inputs."""

    def __init__(self, grep=None):
        self.grep = grep
        self.groups = {}
        self.lines = self.records = self.chars = 0
        self.jobs = []  # in order of appearance (gh logs)
        self.failed_steps = []
        self.tail = []  # last Lines, for the "end of output" section
        self.windows = {}  # line no -> Line, the union of all failure windows
        self.anchors = []  # line numbers that opened a window, in order
        self._recent = []  # ring buffer of recent Lines
        self._pending = 0  # how many upcoming records still belong to the last window
        self._offset = 0  # line numbering continues across multiple inputs
        self._step_header = {}  # job -> last "▶ Run ..." Line
        self.exit_anchors = set()  # anchors that are CI step failures
        self._ids = {}  # (level, template) -> group id; ids are unique within one input
        self._cleanup = set()  # jobs that reached "Post job cleanup." (their tail is noise)

    def feed(self, lines):
        counted = _Counting(lines)
        for rec in records(counted):
            rec.no += self._offset
            self.records += 1
            self._add(rec)
        self.lines += counted.lines
        self.chars += counted.chars
        self._offset += counted.lines
        return self

    def _add(self, rec):
        parsed = parse_json(rec.head) if rec.head.lstrip().startswith("{") else None
        if parsed:
            level, msg, ctx, stack, ts = parsed
            cont = stack + rec.cont
        else:
            level, msg, ctx, ts, cont = text_level(rec.head), rec.head, "", None, rec.cont
            if level in ("info", "notable") and not PASS_MARK.search(rec.head) and \
                    any(TEXT_LEVEL[1][1].search(c) for c in cont[:3]):
                level = "error"
            if not ts:
                m = MASKS[0][0].search(rec.head)
                ts = rec.ts or (m.group(0) if m else None)
        if self.grep and not (self.grep.search(rec.head) or any(self.grep.search(c) for c in cont[:50])):
            return
        if rec.job and rec.job not in self.jobs:
            self.jobs.append(rec.job)
        template = mask(msg.strip())[:300]
        key = (level, template)
        gid = self._ids.get(key)
        new = gid is None
        if new:
            gid = self._new_id(key)
        g = self.groups.get(gid)
        if new:
            g = self.groups[gid] = Group(gid, template, level, rec.job)
            g.first = rec.no
        g.count += 1
        g.last = rec.no
        if ts:
            g.first_ts = g.first_ts or ts
            g.last_ts = ts
        if len(g.examples) < 3:
            g.examples.append((rec.no, msg if parsed else rec.head, ctx, cont))

        line = Line(rec.no, rec.job, gid, level, (msg if parsed else rec.head).strip(), cont)
        if CI_CLEANUP.match(rec.head):
            self._cleanup.add(rec.job)
        if rec.job in self._cleanup:
            if new:
                g.cleanup = True
            return  # post-job cleanup: counted in its group, kept out of windows and the tail
        if self._pending > 0:
            self.windows[rec.no] = line
            self._pending -= 1
        is_error = LEVELS[level] >= LEVELS["error"]
        ci_exit = rec.head.startswith(("##[error]", "::error"))
        if rec.head.startswith("▶ "):
            self._step_header[rec.job] = line
        if (is_error and new) or ci_exit:
            name = ("%s / %s" % (rec.job, rec.step) if rec.job else rec.step) if rec.step else None
            if ci_exit and name and name not in self.failed_steps:
                self.failed_steps.append(name)
            if len(self.anchors) < MAX_ANCHORS:
                before = CI_EXIT_BEFORE if ci_exit else CONTEXT_BEFORE
                for prev in self._recent[-before:]:
                    if prev.job == rec.job and not (ci_exit and CI_BORING.match(prev.text)):
                        self.windows.setdefault(prev.no, prev)
                self.windows[rec.no] = line
                self.anchors.append(rec.no)
                if ci_exit:
                    self.exit_anchors.add(rec.no)
                    hdr = self._step_header.get(rec.job)
                    if hdr is not None:
                        self.windows.setdefault(hdr.no, hdr)
                if not ci_exit:  # after a CI step fails comes post-job cleanup: look back only
                    self._pending = max(self._pending, CONTEXT_AFTER)
        self._recent.append(line)
        if len(self._recent) > CI_EXIT_BEFORE:
            del self._recent[0]
        self.tail.append(line)
        if len(self.tail) > 80:
            del self.tail[:-80]

    def _new_id(self, key):
        digest = hashlib.sha1(("%s|%s" % key).encode("utf-8", "replace")).hexdigest()
        for n in range(4, 41):  # 4 hex chars unless that id is taken by another template
            gid = digest[:n]
            if gid not in self.groups:
                self._ids[key] = gid
                return gid
        raise RuntimeError("group id space exhausted")

    @property
    def levels(self):
        out = {}
        for g in self.groups.values():
            t = out.setdefault(g.level, [0, 0])
            t[0] += 1
            t[1] += g.count
        return out


class _Counting(object):
    def __init__(self, it):
        self.it, self.lines, self.chars = it, 0, 0

    def __iter__(self):
        for line in self.it:
            self.lines += 1
            self.chars += len(line)
            yield line


CODE_FRAME = re.compile(r"^\s*(\d+ \||\|\s*[\^~]+\s*$|\|\s*$)")  # unmarked source lines / caret lines


EXC_LINE = re.compile(r"^\s*[\w.]*(Error|Exception|Exit|Interrupt|Failure)\b[^(]*(:|$)")


def trim_stack(lines, keep=5, other_max=12):
    frames, other, under = [], [], None
    for l in lines:
        if FRAME.match(l):
            frames.append(l)
            under = _indent(l) if l.lstrip().startswith('File "') else None
            continue
        if under is not None and _indent(l) > under:
            continue  # the source line Python prints under each `File "..."` frame
        under = None
        if not CODE_FRAME.match(l):
            other.append(l)
    exceptions = [l for l in other if EXC_LINE.match(l)]
    other = other[:other_max]
    if exceptions and exceptions[-1] not in other:
        other.append(exceptions[-1])  # the exception that ends a traceback is the headline
    app = [f for f in frames if not NOISY_FRAME.search(f)] or frames[:2]
    shown = app[:keep]
    out = [LONG_PATH.sub(r"…\1", l.strip())[:240] for l in other + shown]
    hidden = len(frames) - len(shown)
    if hidden > 0:
        out.append("(+%d frames hidden)" % hidden)
    return out


def _fmt_count(g):
    return " (×%s, last L%d)" % ("{:,}".format(g.count), g.last) if g.count > 1 else ""


def render_group(g, full=False):
    """A group on its own: header, example line(s), continuation. Used by `pith show` and --jev."""
    tags = ""
    if g.score:
        tags = "  act=%.2f val=%.1f/4" % (g.score["a"], g.score["v"])
        if g.rc is not None and g.rc >= 0.02:
            tags += " rc=%.2f" % g.rc
    job = "  job: %s" % g.job if g.job else ""
    lines = ["[%s] %s ×%s%s  L%d%s%s" % (g.id, g.level.upper(), "{:,}".format(g.count), tags, g.first,
                                         " … L%d" % g.last if g.count > 1 else "", job)]
    for i, (no, text, ctx, cont) in enumerate(g.examples if full else g.examples[:1]):
        if full:
            lines.append("  --- example %d (L%d)" % (i + 1, no))
        lines.append("  " + text.strip()[:(4000 if full else 400)])
        if ctx:
            lines.append("  ctx: " + (ctx if full else ctx[:300]))
        body = cont if full else trim_stack(cont)
        lines += ["    " + l for l in body[: (MAX_CONT if full else 20)]]
    return redact("\n".join(lines))


def _example(g):
    return g.examples[0][1].strip() if g.examples else g.template


def est_tokens(chars):
    return max(1, int(round(chars / 4.0)))


def fmt_tokens(n):
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)


def _render_line(sift, ln, first_sight):
    g = sift.groups[ln.gid]
    err = LEVELS[ln.level] >= LEVELS["error"]
    mark = "✱" if err else " "
    out = ["L%-6d %s [%s] %s%s" % (ln.no, mark, ln.gid, ln.text[:300], _fmt_count(g) if first_sight else "")]
    if err:
        body = trim_stack(ln.cont) if first_sight else trim_stack(ln.cont, keep=1, other_max=3)
    else:
        body = [c.strip()[:200] for c in ln.cont[:CONTEXT_CONT]]
        if len(ln.cont) > CONTEXT_CONT:
            body.append("(+%d lines)" % (len(ln.cont) - CONTEXT_CONT))
    out += ["          " + b for b in body]
    return redact("\n".join(out))


def _window_blocks(sift, floor):
    """Group window lines into per-job blocks of nearby lines, in order."""
    by_job = {}
    for no in sorted(sift.windows):
        ln = sift.windows[no]
        if LEVELS[ln.level] < floor:
            continue  # (jev's keep=False only trims the side sections: context around failures stays)
        by_job.setdefault(ln.job, []).append(ln)
    blocks = {}
    for job, lns in by_job.items():
        cur = []
        for ln in lns:
            if cur and ln.no - cur[-1].no > GAP:
                blocks.setdefault(job, []).append(cur)
                cur = []
            cur.append(ln)
        if cur:
            blocks.setdefault(job, []).append(cur)
    order = [j for j in sift.jobs if j in blocks] + [j for j in blocks if j not in sift.jobs]
    return order, blocks


def render(sift, budget=12000, level_floor=None, header=None, order=None, footer=None):
    """Budgeted summary.

    Default view: FAILURES (merged windows around each distinct error and each CI step failure,
    per job, round-robin so every failing job gets space), then WARNINGS, END OF OUTPUT, and the
    most frequent remaining lines. ``order`` (list of groups, from --jev) switches to a ranked list.
    """
    floor = LEVELS[level_floor] if level_floor else 0
    out = list(header or [])
    used = sum(len(l) + 1 for l in out)
    omitted = 0
    shown_groups = set()

    def add(text):
        out.append(text)
        return len(text) + 1

    if order is not None:  # --jev: ranked groups
        out.append("")
        out.append("== MOST LIKELY FIRST")
        for g in order:
            if LEVELS[g.level] < floor:
                continue
            block = render_group(g)
            if used + len(block) > budget:
                block = redact("[%s] %s ×%s  %s" % (g.id, g.level.upper(), "{:,}".format(g.count), _example(g)[:200]))
                if used + len(block) > budget:
                    omitted += 1
                    continue
            used += add(block)
        if omitted:
            out.append("")
            out.append("(%d more groups not shown: raise --budget or `pith show <id>`)" % omitted)
        return "\n".join(out + list(footer or []))

    # 1. failures in context, round-robin across jobs
    jobs, blocks = _window_blocks(sift, floor)
    fail_budget = int(budget * 0.75)
    if jobs:
        heading_at = len(out)
        used += 80
        def priority(blk):
            has_exit = any(ln.no in sift.exit_anchors for ln in blk)
            has_fail = any(FAIL_MARK.search(ln.text) for ln in blk)
            return (0 if has_exit else 1 if has_fail else 2, blk[0].no)
        queues = {j: sorted(blocks[j], key=priority) for j in jobs}
        rendered = {j: [] for j in jobs}
        progress = True
        while progress:
            progress = False
            for j in jobs:
                if not queues[j]:
                    continue
                blk = queues[j].pop(0)
                parts, block_groups, size, cut = [], set(), 0, 0
                room = fail_budget - used - 2
                for k, ln in enumerate(blk):
                    err = LEVELS[ln.level] >= LEVELS["error"]
                    seen = ln.gid in shown_groups or ln.gid in block_groups
                    if not err and seen:
                        continue  # repeated context line: already visible
                    piece = _render_line(sift, ln, not seen)
                    if size + len(piece) + 1 > room and err:  # compact form: the error line alone
                        piece = redact("L%-6d ✱ [%s] %s" % (ln.no, ln.gid, ln.text[:200]))
                    if size + len(piece) + 1 > room:
                        cut = len(blk) - k
                        break
                    parts.append(piece)
                    block_groups.add(ln.gid)
                    size += len(piece) + 1
                if not parts:
                    omitted += 1
                    continue
                if cut:
                    parts.append("   (+%d more lines here: `pith show <id>` or grep the raw file)" % cut)
                shown_groups.update(block_groups)
                text = "\n".join(parts)
                rendered[j].append((blk[0].no, text))
                used += len(text) + 2
                progress = True
        if any(rendered.values()):
            out[heading_at:heading_at] = ["", "== FAILURES (✱ = error; lines shown in context, L = line number in raw)"]
        for j in jobs:
            if not rendered[j]:
                continue
            if j:
                out.append("-- job: %s" % j)
            for i, (_, text) in enumerate(sorted(rendered[j])):
                if i:
                    out.append("   …")
                out.append(text)

    # 2. warnings and other notable lines not already shown (one line each, first occurrence order)
    extra = sorted((g for g in sift.groups.values() if g.keep and g.id not in shown_groups and
                    LEVELS[g.level] >= max(floor, LEVELS["notable"])), key=lambda g: (-LEVELS[g.level], g.first))
    if extra:
        section = []
        for g in extra:
            line = redact("L%-6d %s [%s] %s%s" % (g.first, g.level[:4].upper(), g.id, _example(g)[:180], _fmt_count(g)))
            if used + len(line) + 1 > budget - END_RESERVE:
                omitted += 1
                continue
            section.append(line)
            shown_groups.add(g.id)
            used += len(line) + 1
        if section:
            out.append("")
            out.append("== OTHER ERRORS & WARNINGS")
            out.extend(section)

    # 3. end of output (the last distinct lines usually hold the summary / final error)
    tail, seen = [], set()
    for ln in reversed(sift.tail):
        g = sift.groups.get(ln.gid)
        if g and g.keep and ln.gid not in seen and ln.no not in sift.windows and LEVELS[ln.level] >= floor:
            seen.add(ln.gid)
            tail.append(ln)
        if len(tail) == 15:
            break
    if tail:
        section = []
        for ln in reversed(tail):
            line = redact("L%-6d   [%s] %s" % (ln.no, ln.gid, ln.text[:200]))
            if used + len(line) + 1 > budget:
                break
            section.append(line)
            shown_groups.add(ln.gid)
            used += len(line) + 1
        if section:
            out.append("")
            out.append("== END OF OUTPUT")
            out.extend(section)

    # 4. most frequent remaining lines (what the noise is)
    rest = sorted((g for g in sift.groups.values() if g.keep and not g.cleanup and g.id not in shown_groups and
                   floor <= LEVELS[g.level] < LEVELS["notable"]), key=lambda g: -g.count)
    section = []
    for g in rest[:10]:
        line = redact("×%-7s [%s] %s" % ("{:,}".format(g.count), g.id, _example(g)[:140]))
        if used + len(line) + 1 > budget:
            break
        section.append(line)
        used += len(line) + 1
    if section:
        out.append("")
        out.append("== MOST FREQUENT OTHER LINES")
        out.extend(section)
    shown_groups.update(g.id for g in rest[:len(section)])
    hidden = [g for g in sift.groups.values() if g.keep and not g.cleanup and g.id not in shown_groups]
    hidden_err = [g for g in hidden if LEVELS[g.level] >= LEVELS["error"]]
    if hidden:
        out.append("")
        note = "(not shown: %d groups, %s lines" % (len(hidden), "{:,}".format(sum(g.count for g in hidden)))
        if hidden_err:
            note += " - including %d error groups, %s error lines" % (
                len(hidden_err), "{:,}".format(sum(g.count for g in hidden_err)))
        out.append(note + ". Narrow with --grep REGEX or --level error, raise --budget, or grep the raw file.)")
    return "\n".join(out + list(footer or []))


_TS_ANY = re.compile(r"\d{4}[-./]\d{2}[-./]\d{2}[T :\-_]\d{2}[:.]\d{2}|(^|[\s\[])\d{2}:\d{2}:\d{2}([.,]\d+)?\b|"
                     r"\b1[5-9]\d{8}(\.\d+)?\b|\d{2}/\w{3}/\d{4}:\d{2}|\b\d{4}[./-]\d{2}[./-]\d{2}\b|\b\d{6} \d{6}\b")
_LEVEL_ANY = re.compile(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL|CRITICAL|NOTICE)\b|\blevel[\"=:]|"
                        r"\b(error|failed|exception|traceback|panic)\b", re.I)
_CI_ANY = re.compile(r"^(##\[|::\w+|\s*[✓✔✘✗●]|\s*(PASS|FAIL|ok|not ok)\b|npm (ERR|WARN)|\s*at \S+ \(|"
                     r"\s*File \"[^\"]+\", line \d+|\[\d+/\d+\]|\s*\d+ (passed|failed))")
_CODE = re.compile(r"^\s*(import |from \S+ import|export |const |let |var |function |def |class |return\b|if \(|"
                   r"for \(|while \(|public |private |protected |package |func |#include|using |namespace |"
                   r"resource \"|variable \"|module \"|SELECT |INSERT |CREATE |}\s*else|@\w+\()|[;{}\[\](),]\s*$|=>\s*\{?$")
_MARKDOWN = re.compile(r"^(#{1,6} |\s*[-*+] |\s*\d+\. |```|\|.*\|$|> )")
_DIFF = re.compile(r"^(diff --git|index [0-9a-f]+\.\.|@@ |[+-]{3} [ab]/|[+-](?![+-]))")


def log_likeness(text, sample=400):
    """0..1: how much ``text`` looks like logs/command output rather than code, docs, diffs or data.
    Used by the hooks: only log-like text gets summarised or guarded (tuned on real CI/dev logs vs
    source files, markdown and git diffs: a paste of your code for review must never be blocked)."""
    lines = [l for l in text.splitlines()[:sample * 3] if l.strip()][:sample]
    if len(lines) < 20:
        return 0.0
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            json.loads(stripped)
            return 0.0  # one JSON document: summarising would destroy it
        except ValueError:
            pass
    n = float(len(lines))
    clean = [ANSI.sub("", l) for l in lines]
    for sep in (",", "\t", ";", "|"):  # tabular data (CSV/TSV): same column count on nearly every line
        counts = [l.count(sep) for l in clean]
        top = max(set(counts), key=counts.count)
        if top >= 3 and counts.count(top) >= 0.8 * n and not any(
                re.search(r"\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", l) for l in clean[:50]):
            return 0.0
    frac = lambda rx: sum(1 for l in clean if rx.search(l)) / n
    ts, lvl, ci = frac(_TS_ANY), frac(_LEVEL_ANY), frac(_CI_ANY)
    frame = re.compile(r"^\s*>?\s*\d+\s*\||^\s*at \S|^\s*File \"|Import trace|^\s*\.{0,2}/\S+:\d+")
    jsonl = 0
    for l in clean[:60]:
        t = l.strip()
        if t.startswith("{") and t.endswith("}"):
            try:
                obj = json.loads(t)
                jsonl += isinstance(obj, dict) and any(k in obj for k in ("level", "msg", "message", "time", "timestamp", "severity", "@timestamp"))
            except ValueError:
                pass
    if jsonl >= min(30, len(clean[:60]) * 0.5):
        return 0.9  # structured (JSON-lines) logs
    strict = re.compile(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL|NOTICE)\b\s*[:\]|]|\blevel[\"'=:]|^\[\w+\]\s")
    code = sum(1 for l in clean if _CODE.search(l) and not frame.search(l) and not strict.search(l)
               and not _TS_ANY.search(l)) / n
    ci = max(ci, sum(1 for l in clean if frame.search(l)) / n)
    md = sum(1 for l in clean if _MARKDOWN.search(l) and not _TS_ANY.search(l) and not _LEVEL_ANY.search(l)) / n
    is_diff = re.search(r"^(diff --git|@@ -\d)", text, re.M) is not None
    diff = frac(_DIFF) if is_diff else 0.0
    gh = sum(1 for l in clean if GH_PREFIX.match(l) or GH_PREFIX_SPACED.match(l) or CI_PREFIX.match(l)) / n
    templates = set(mask(l).strip()[:80] for l in clean)
    repetition = 1.0 - len(templates) / n
    score = 0.9 * max(ts, gh) + 0.5 * lvl + 0.6 * ci + 0.3 * repetition - 1.0 * code - 0.6 * md - 1.2 * diff
    return max(0.0, min(1.0, score))
