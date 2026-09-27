"""Secret redaction.

Everything pith prints (and everything it can send to an optional model) passes through ``redact``.
Logs routinely contain credentials - CI env dumps, curl traces, crash reports - and pith's output
lands in an LLM context window, transcripts and screenshots. The patterns favour recall: a false
positive costs a few characters, a miss leaks a key. Multi-line private keys are removed earlier,
when the input is parsed (see engine.records), so no key material reaches grouping at all.
"""
import re
import unicodedata

_KEYWORD = (r"password|passwd|passphrase|secret|token|api[_ -]?key|apikey|access[_-]?key|private[_-]?key|"
            r"client[_-]?secret|auth(?:orization)?\b|credential|signing[_-]?key|encryption[_-]?key|\bpat\b|_pat\b|dsn|session[_-]?id|"
            r"sessionid|cookie|webhook[_-]?url|connection[_-]?string|conn[_-]?str")

# (pattern, replacement). Order matters: specific formats first, generic key=value last.
_RULES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"),
     "<private-key>"),
    (re.compile(r"(?<![A-Za-z0-9])(AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}(?![A-Za-z0-9])"), "<aws-key-id>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "<github-token>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"), "<github-token>"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "<gitlab-token>"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"), "<slack-token>"),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"), "<slack-webhook>"),
    (re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"), "<discord-webhook>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "<anthropic-key>"),
    (re.compile(r"sk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}"), "<api-key>"),
    (re.compile(r"[rsp]k_(?:live|test)_[A-Za-z0-9]{16,}"), "<stripe-key>"),
    (re.compile(r"whsec_[A-Za-z0-9+/=]{16,}"), "<webhook-secret>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "<google-api-key>"),
    (re.compile(r"NRAK-[A-Z0-9]{27}"), "<newrelic-key>"),
    (re.compile(r"npm_[A-Za-z0-9]{36}"), "<npm-token>"),
    (re.compile(r"pypi-[A-Za-z0-9_-]{50,}"), "<pypi-token>"),
    (re.compile(r"hf_[A-Za-z0-9]{30,}"), "<huggingface-token>"),
    (re.compile(r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"), "<sendgrid-key>"),
    (re.compile(r"(?<![A-Za-z0-9])SK[0-9a-f]{32}(?![A-Za-z0-9])"), "<twilio-key>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"glrt-[A-Za-z0-9_-]{20,}"), "<gitlab-token>"),
    (re.compile(r"shp(?:at|ss|ca|pa)_[a-fA-F0-9]{32}"), "<shopify-token>"),
    (re.compile(r"\bhv[sbr]\.[A-Za-z0-9_-]{20,}"), "<vault-token>"),
    (re.compile(r"dop_v1_[a-f0-9]{64}"), "<digitalocean-token>"),
    (re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"), "<telegram-token>"),
    (re.compile(r"dckr_pat_[A-Za-z0-9_-]{20,}"), "<docker-token>"),
    (re.compile(r"ATATT[A-Za-z0-9_=-]{30,}"), "<atlassian-token>"),
    (re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{40,}"), "<age-key>"),
    (re.compile(r"\bdapi[a-f0-9]{32}\b"), "<databricks-token>"),
    (re.compile(r"\bgl(?:sa|c)_[A-Za-z0-9_=+/-]{20,}"), "<grafana-token>"),
    # long base64 runs: embedded keys/certs (kubeconfig client-key-data, one-line PEM)
    (re.compile(r"[A-Za-z0-9+/]{100,}={0,2}"), "<base64 data>"),
    # connection strings and signed URLs
    (re.compile(r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature)=([^;\s\"']+)"), r"\1=<redacted>"),
    (re.compile(r"(?i)(;\s*(?:Password|Pwd)=)([^;\s\"']+)"), r"\1<redacted>"),  # ADO/ODBC connection strings
    (re.compile(r"(?i)([?&](?:sig|signature|X-Amz-Signature|X-Goog-Signature|token|access_token|key)=)[^&\s\"']+"), r"\1<redacted>"),
    # credentials in URLs (user optional: redis://:pw@host), including Sentry-style https://key@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]{0,64}:)[^@\s/]{1,256}@"), r"\1<redacted>@"),
    (re.compile(r"(?i)(\bhttps?://)[0-9a-f]{16,}@"), r"\1<redacted>@"),
    # CLI flags carrying credentials
    (re.compile(r"(?i)(\s-u\s+|\s--user[= ])([^\s:]+):(\S+)"), r"\1\2:<redacted>"),
    (re.compile(r"(?i)(\blogin\b[^\n]{0,200}?\s(?:-p|--password)[= ]\s*)(\S+)"), r"\1<redacted>"),
    (re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]{0,200}?\s-p)([^\s-]\S*)"), r"\1<redacted>"),
    # HTTP auth and cookies
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b(token)\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{20,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)(.+)$", re.M), r"\1<redacted>"),
    # key = value / "key": "value" / key: value, with the keyword anywhere in the name; values may
    # be quoted (including \" escaped quotes inside JSON strings) and contain spaces when quoted
    (re.compile(r"(?i)((?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,120}(?:" + _KEYWORD + r")[A-Za-z0-9_.-]{0,60})"
                r"(\\?[\"']?[ \t]{0,3}[:=][ \t]{0,3})(\\?[\"'])((?:(?!\3).){4,200}?)\3"), r"\1\2\3<redacted>\3"),
    (re.compile(r"(?i)((?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,120}(?:" + _KEYWORD + r")[A-Za-z0-9_.-]{0,60})"
                r"(\\?[\"']?[ \t]{0,3}[:=][ \t]{0,3})(?!<|\*{3}|\$\{|\{\{|\\?[\"'])([^\s\"'&,;}\\]{4,512})"), r"\1\2<redacted>"),
]
# base64 body lines of a key block that reached us without its BEGIN line (e.g. a truncated paste)
_KEY_BODY = re.compile(r"(?m)^(\s*)(?=[A-Za-z0-9+/]*[0-9])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{60,76}={0,2}\s*$")
_MAX = 200000  # redact() bounds its own work; callers pass rendered output, not raw logs


def redact(text):
    """Return ``text`` with anything that looks like a credential replaced by a placeholder."""
    if not text:
        return text
    if len(text) > _MAX:
        return "\n".join(redact(text[i:i + _MAX]) for i in range(0, len(text), _MAX))
    if any(ord(c) > 0xff00 for c in text):  # fullwidth look-alikes (ＡＰＩ_KEY=) normalise first
        text = unicodedata.normalize("NFKC", text)
    for rx, rep in _RULES:
        text = rx.sub(rep, text)
    return _KEY_BODY.sub(r"\1<redacted>", text)
