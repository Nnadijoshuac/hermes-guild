"""Scrub a skill before it leaves the machine.

Skills the agent writes from real work pick up real details: API keys pasted into a command,
the author's home directory, internal hostnames, a colleague's e-mail. Guild runs every text
file through Hermes' own secret redactor (``agent.redact.redact_sensitive_text``, forced on)
and then its own passes for personal paths, e-mail addresses and private network addresses.
Every change is reported to the person proposing and to the reviewer.
"""

from __future__ import annotations

import re
from typing import Any

TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".ps1", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
                 ".ini", ".cfg", ".sql", ".html", ".css", ".csv", ".xml", ".rb", ".go", ".rs", ".j2", ".tmpl"}

_HOME = re.compile(r"(?P<pre>(?:[A-Za-z]:[\\/]+Users[\\/]+)|(?:/Users/)|(?:/home/))(?P<user>[A-Za-z0-9._-]+)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PRIVATE_IP = re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")
_KEEP_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}
_KEEP_HOME_USERS = {"user", "username", "you", "me", "<user>", "name", "shared", "public"}
_KEEP_IPS = {"10.0.0.0", "192.168.0.0", "172.16.0.0"}
MARK = "[guild-redacted"


def is_text(rel: str, data: bytes) -> bool:
    suffix = ("." + rel.rsplit(".", 1)[-1].lower()) if "." in rel.rsplit("/", 1)[-1] else ""
    if suffix in TEXT_SUFFIXES or rel.endswith("SKILL.md"):
        return True
    if b"\0" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
        return not suffix  # extensionless UTF-8 files (scripts) are text
    except UnicodeDecodeError:
        return False


def _hermes_redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
    except Exception:
        return _fallback_secrets(text)
    try:
        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except TypeError:
        return redact_sensitive_text(text, force=True)


_FALLBACK = [
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
               r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|sk_live_[0-9A-Za-z]{16,}|pk_live_[0-9A-Za-z]{16,})\b"),
    re.compile(r"(?i)\b((?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*)(['\"]?)[^\s'\"]{8,}\2"),
]


def _fallback_secrets(text: str) -> str:  # only used when running outside Hermes
    text = _FALLBACK[0].sub("***", text)
    return _FALLBACK[1].sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", text)


# Credentials typed as assignments, which log redaction tends to leave alone in prose.
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b((?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
    r"\s*[:=]\s*)(['\"]?)(?!\$\{|<|\*\*\*|your|changeme|example|xxx)[^\s'\"`]{6,}\2")

_URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I)

_TOKEN_STOP = set(" \t\r\n`'\"()[]{}<>,;")


def _mask_secrets(line: str) -> str:
    """Use Hermes' redactor to *find* secrets, then mask each whole token it touched.

    Hermes' log redaction keeps a short prefix/suffix (``rnd_8f...gH2j``) so humans can tell keys
    apart. That is fine in a local log but not in a file leaving the machine, so every token the
    redactor changed is replaced in full, keeping the surrounding quotes and backticks intact."""
    red = _hermes_redact(line)
    if red == line:
        return line
    import difflib
    marks = [(m.start(), m.end()) for m in re.finditer(re.escape(MARK) + r"[^\]]*\]|\*\*\*(?::\*\*\*)?", line)]
    spans: list[tuple[int, int]] = []
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, line, red, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        s, e = i1, max(i2, i1 + 1)
        while s > 0 and line[s - 1] not in _TOKEN_STOP and line[s - 1] not in "=:":
            s -= 1
        while e < len(line) and line[e] not in _TOKEN_STOP:
            e += 1
        while e > s + 1 and (line[e - 1] in _TOKEN_STOP or line[e - 1] in ":."):
            e -= 1
        if any(s < me and e > ms for ms, me in marks):
            continue  # already masked by an earlier scrub (re-proposing a team skill)
        if spans and s <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(e, spans[-1][1]))
        else:
            spans.append((s, e))
    if not spans:
        return line
    out, last = [], 0
    for s, e in spans:
        out.append(line[last:s])
        out.append(f"{MARK}:secret]")
        last = e
    out.append(line[last:])
    return "".join(out)


def scrub_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (clean_text, findings). Findings name the line and kind, never the secret itself."""
    findings: list[dict[str, Any]] = []
    out_lines = []
    for n, line in enumerate(text.splitlines(keepends=True), start=1):
        new = _URL_USERINFO.sub(lambda m: f"{m.group(1)}***:***@", line)
        new = _mask_secrets(new)
        new = _ASSIGNED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{MARK}:secret]{m.group(2)}"
                                   if MARK not in m.group(0) else m.group(0), new)
        if new != line:
            findings.append({"line": n, "kind": "secret"})

        def _home(m: re.Match) -> str:
            if m.group("user").lower() in _KEEP_HOME_USERS:
                return m.group(0)
            findings.append({"line": n, "kind": "home-path"})
            return m.group("pre") + "<user>"
        new = _HOME.sub(_home, new)

        def _email(m: re.Match) -> str:
            if m.group("domain").lower() in _KEEP_EMAIL_DOMAINS or m.group(0).startswith(("noreply@", "no-reply@")):
                return m.group(0)
            findings.append({"line": n, "kind": "email"})
            return f"{MARK}:email]"
        new = _EMAIL.sub(_email, new)

        def _ip(m: re.Match) -> str:
            if m.group(0) in _KEEP_IPS:
                return m.group(0)
            findings.append({"line": n, "kind": "private-ip"})
            return f"{MARK}:private-ip]"
        new = _PRIVATE_IP.sub(_ip, new)
        out_lines.append(new)
    return "".join(out_lines), findings


def summarize(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "nothing sensitive found"
    by: dict[str, list[str]] = {}
    for f in findings:
        by.setdefault(f.get("kind", "?"), []).append(f"{f.get('file', 'SKILL.md')}:{f.get('line')}")
    return "; ".join(f"{len(v)} {k} ({', '.join(v[:4])}{', ...' if len(v) > 4 else ''})" for k, v in by.items())


def count_marks(text: str) -> int:
    return text.count(MARK) + text.count("***")
