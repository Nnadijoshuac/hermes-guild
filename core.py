"""Guild core: a git-backed, reviewed skill library shared by a team's Hermes agents.

Layout of a guild repository (any git remote: GitHub/GitLab private repo, a bare repo on a
network share, or a local path):

    guild.yaml                     name, reviewers, review policy
    GUILD.md                       human readme written at init
    skills/<skill>/SKILL.md        approved team skills (+ references/, templates/, ...)
    skills/<skill>/.guild.json     provenance: author, reviewer, version, evidence
    log/decisions.jsonl            append-only approve / reject / revert audit trail

Proposals live on branches named ``proposal/<member>/<skill>``. Agents can propose; only a
human running ``hermes guild approve`` (or ``/guild approve``) merges into ``main``.

Two local clones per profile, under ``<HERMES_HOME>/plugin-data/guild/``:

    library/   read-only mirror of main; ``library/skills`` is added to skills.external_dirs
    work/      scratch clone used to build proposal branches and approvals
"""

from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import scrub

logger = logging.getLogger(__name__)

PLUGIN_ID = "guild"
MAIN = "main"
PROPOSAL_PREFIX = "proposal/"
GIT_TIMEOUT_S = 60
MAX_FILE_BYTES = 1_000_000
MAX_SKILL_BYTES = 5_000_000
META_FILE = ".guild.json"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_lock = threading.RLock()


class GuildError(Exception):
    """A user-facing failure; the message is shown as-is."""


# --------------------------------------------------------------------------- paths / state

def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        d = Path(plugin_data_dir(PLUGIN_ID))
    except Exception:  # pragma: no cover - outside Hermes (tests set GUILD_DATA_DIR)
        d = Path(os.environ.get("GUILD_DATA_DIR") or (Path.home() / ".hermes-guild"))
    if os.environ.get("GUILD_DATA_DIR"):
        d = Path(os.environ["GUILD_DATA_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def library_dir() -> Path:
    return data_dir() / "library"


def library_skills_dir() -> Path:
    return library_dir() / "skills"


def work_dir() -> Path:
    return data_dir() / "work"


def _state_path() -> Path:
    return data_dir() / "state.json"


def load_state() -> dict[str, Any]:
    p = _state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("guild: state.json unreadable; keeping a copy and starting fresh")
        with _suppress():
            shutil.copy2(p, p.with_suffix(".corrupt.json"))
        return {}


def save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)


def update_state(fn) -> dict[str, Any]:
    with _lock:
        state = load_state()
        fn(state)
        save_state(state)
        return state


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_configured() -> bool:
    return bool(load_state().get("remote")) and (library_dir() / ".git").exists()


# --------------------------------------------------------------------------- identity

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "member"


def member_name() -> str:
    st = load_state()
    if st.get("member"):
        return st["member"]
    name = ""
    with _suppress():
        name = _git(["config", "user.name"], cwd=None, check=False).strip()
    if not name:
        with _suppress():
            name = getpass.getuser()
    return name or "member"


def member_slug() -> str:
    return _slug(member_name())


# --------------------------------------------------------------------------- git

def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"          # never hang on a credential prompt
    env.setdefault("GIT_ASKPASS", "")
    env["LC_ALL"] = env.get("LC_ALL", "C")
    return env


def _git(args: list[str], cwd: Optional[Path], *, check: bool = True, timeout: int = GIT_TIMEOUT_S,
         identity: bool = False) -> str:
    cmd = ["git"]
    if identity:
        # Commits are attributed to the guild member, without touching the user's git config.
        cmd += ["-c", f"user.name={member_name()}", "-c", f"user.email={member_slug()}@guild.local"]
    cmd += ["-c", "core.autocrlf=false", "-c", "core.hooksPath=" + os.devnull]
    cmd += args
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=_git_env())
    except FileNotFoundError as exc:
        raise GuildError("git is not installed or not on PATH; Guild needs git.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GuildError(f"git {' '.join(args[:2])} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GuildError(f"git {' '.join(args[:2])} failed: {msg[-1] if msg else proc.returncode}")
    return proc.stdout


def _is_local_path(remote: str) -> bool:
    if re.match(r"^[A-Za-z]:[\\/]", remote) or remote.startswith(("/", "~", "./", "../", "\\\\")):
        return True
    return "://" not in remote and "@" not in remote.split("/")[0] and Path(remote).exists()


def _remote_is_empty(remote: str) -> bool:
    out = _git(["ls-remote", "--heads", remote], cwd=None)
    return not out.strip()


# --------------------------------------------------------------------------- guild.yaml

def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except FileNotFoundError:
        return {}
    try:
        from ruamel.yaml import YAML  # Hermes ships ruamel.yaml
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        return dict(data) if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _dump_guild_yaml(cfg: dict[str, Any]) -> str:
    reviewers = cfg.get("reviewers") or []
    lines = [
        "# Guild settings. Edit by pull request like any other file.",
        f"name: {json.dumps(cfg.get('name', 'guild'))}",
        f"require_review: {'true' if cfg.get('require_review', True) else 'false'}",
        f"allow_self_approve: {'true' if cfg.get('allow_self_approve', False) else 'false'}",
        "# Members allowed to approve proposals. Empty = any member.",
        "reviewers:" + ("" if reviewers else " []"),
    ]
    lines += [f"  - {json.dumps(r)}" for r in reviewers]
    return "\n".join(lines) + "\n"


def guild_config() -> dict[str, Any]:
    cfg = _read_yaml(library_dir() / "guild.yaml")
    cfg.setdefault("name", "guild")
    cfg.setdefault("require_review", True)
    cfg.setdefault("allow_self_approve", False)
    cfg["reviewers"] = [str(r) for r in (cfg.get("reviewers") or [])]
    return cfg


_GUILD_MD = """# {name}

This repository is a **Hermes Guild**: the reviewed skill library shared by this team's
Hermes agents. Every member's agent loads `skills/` read-only; improvements arrive as
`proposal/<member>/<skill>` branches and land on `main` only after a human approves them.

- Join: `hermes plugins install guild && hermes guild join <this repo's URL>`
- Review: `hermes guild review`, then `hermes guild approve <skill>@<member>`
- Audit trail: `log/decisions.jsonl`
"""


# --------------------------------------------------------------------------- setup

@dataclass
class SetupResult:
    name: str
    remote: str
    created: bool
    seeded: bool
    skills: int
    external_dir: str
    notes: list[str] = field(default_factory=list)


def setup(remote: str, *, name: Optional[str] = None, create: bool = False, solo: bool = False,
          member: Optional[str] = None, reviewers: Optional[list[str]] = None) -> SetupResult:
    """Create (``create=True``) or join a guild at *remote* and mount its library."""
    remote = remote.strip()
    if not remote:
        raise GuildError("Give the guild's git remote (URL or path).")
    notes: list[str] = []
    created = False
    if _is_local_path(remote):
        rp = Path(os.path.expanduser(remote)).resolve()
        remote = str(rp)
        if not rp.exists():
            if not create:
                raise GuildError(f"No guild at {rp}. Use `hermes guild init {remote}` to create one.")
            rp.parent.mkdir(parents=True, exist_ok=True)
            _git(["init", "--bare", "--initial-branch", MAIN, str(rp)], cwd=None)
            created = True
            notes.append(f"Created a bare guild repository at {rp}")
    with _lock:
        if member:
            update_state(lambda s: s.__setitem__("member", member))
        empty = _remote_is_empty(remote)
        seeded = False
        for d in (library_dir(), work_dir()):
            if d.exists():
                _rmtree(d)
        if empty:
            if not create and not solo:
                raise GuildError("That repository is empty. Use `hermes guild init` to start a new guild there.")
            _git(["clone", "--quiet", remote, str(work_dir())], cwd=None)
            _git(["checkout", "-q", "-b", MAIN], cwd=work_dir(), check=False)
            gname = name or "guild"
            cfg = {"name": gname, "require_review": not solo, "allow_self_approve": solo,
                   "reviewers": reviewers or ([] if solo else [member_name()])}
            (work_dir() / "guild.yaml").write_text(_dump_guild_yaml(cfg), encoding="utf-8")
            (work_dir() / "GUILD.md").write_text(_GUILD_MD.format(name=gname), encoding="utf-8")
            (work_dir() / "skills").mkdir(exist_ok=True)
            (work_dir() / "skills" / ".gitkeep").write_text("", encoding="utf-8")
            (work_dir() / "log").mkdir(exist_ok=True)
            (work_dir() / "log" / "decisions.jsonl").write_text("", encoding="utf-8")
            _git(["add", "-A"], cwd=work_dir())
            _git(["commit", "-q", "-m", f"guild: found {gname}"], cwd=work_dir(), identity=True)
            _git(["push", "-q", "-u", "origin", MAIN], cwd=work_dir())
            seeded = True
        else:
            _git(["clone", "--quiet", remote, str(work_dir())], cwd=None)
        _git(["clone", "--quiet", "--branch", MAIN, remote, str(library_dir())], cwd=None)
        update_state(lambda s: s.update({"remote": remote, "joined_at": now_iso(), "last_sync": now_iso(),
                                         "known_skills": sorted(list_library_skills())}))
    ext = mount_library()
    cfg = guild_config()
    if not cfg["reviewers"] and not solo:
        notes.append("guild.yaml lists no reviewers, so any member may approve.")
    return SetupResult(name=cfg["name"], remote=remote, created=created, seeded=seeded,
                       skills=len(list_library_skills()), external_dir=ext, notes=notes)


def _rmtree(path: Path) -> None:
    def _retry(func, p, _exc):  # git pack files are read-only on Windows
        with _suppress():
            os.chmod(p, stat.S_IWRITE)
            func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=_retry)


def mount_library() -> str:
    """Add ``library/skills`` to this profile's ``skills.external_dirs`` (idempotent)."""
    target = str(library_skills_dir())
    try:
        from hermes_cli.config import read_raw_config, save_config
    except Exception:
        return target
    cfg = read_raw_config()
    skills = cfg.setdefault("skills", {}) if isinstance(cfg.get("skills", {}), dict) else {}
    cfg["skills"] = skills
    dirs = skills.get("external_dirs") or []
    if isinstance(dirs, str):
        dirs = [dirs]
    norm = {os.path.normcase(os.path.abspath(os.path.expanduser(str(d)))) for d in dirs}
    if os.path.normcase(os.path.abspath(target)) not in norm:
        dirs.append(target)
        skills["external_dirs"] = dirs
        save_config(cfg, merge_existing=True)
    return target


def unmount_library() -> None:
    target = os.path.normcase(os.path.abspath(str(library_skills_dir())))
    try:
        from hermes_cli.config import read_raw_config, save_config
    except Exception:
        return
    cfg = read_raw_config()
    skills = cfg.get("skills")
    if not isinstance(skills, dict):
        return
    dirs = skills.get("external_dirs") or []
    if isinstance(dirs, str):
        dirs = [dirs]
    kept = [d for d in dirs if os.path.normcase(os.path.abspath(os.path.expanduser(str(d)))) != target]
    if kept != dirs:
        skills["external_dirs"] = kept
        save_config(cfg)


def leave() -> None:
    unmount_library()
    for d in (library_dir(), work_dir()):
        if d.exists():
            _rmtree(d)
    update_state(lambda s: [s.pop(k, None) for k in ("remote", "known_skills", "last_sync", "joined_at")])


# --------------------------------------------------------------------------- library

def list_library_skills() -> list[str]:
    root = library_skills_dir()
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob("*/SKILL.md"))


def skill_meta(skill: str, base: Optional[Path] = None) -> dict[str, Any]:
    base = base or library_skills_dir()
    try:
        return json.loads((base / skill / META_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_library_path(path: str | os.PathLike) -> bool:
    try:
        p = os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
        root = os.path.normcase(os.path.abspath(str(library_dir())))
        return p == root or p.startswith(root + os.sep)
    except Exception:
        return False


def sync(*, quiet: bool = False) -> dict[str, Any]:
    """Fast-forward the library to the guild's main. Local edits inside the read-only library are
    saved as a patch under ``backups/`` first, never silently lost."""
    if not is_configured():
        raise GuildError("Not in a guild yet. Run `hermes guild init <remote>` or `hermes guild join <remote>`.")
    with _lock:
        before = set(list_library_skills())
        lib = library_dir()
        dirty = _git(["status", "--porcelain"], cwd=lib).strip()
        backup = None
        if dirty:
            bdir = data_dir() / "backups"
            bdir.mkdir(exist_ok=True)
            backup = bdir / f"library-local-edits-{_dt.datetime.now():%Y%m%d-%H%M%S}.patch"
            backup.write_text(_git(["diff", "HEAD"], cwd=lib, check=False), encoding="utf-8")
        _git(["fetch", "-q", "--prune", "origin"], cwd=lib)
        _git(["reset", "-q", "--hard", f"origin/{MAIN}"], cwd=lib)
        _git(["clean", "-qfd"], cwd=lib)
        after = set(list_library_skills())
        added, removed = sorted(after - before), sorted(before - after)
        changed = []
        st = load_state()
        hashes = st.get("hashes") or {}
        new_hashes = {s: _skill_hash(library_skills_dir() / s) for s in after}
        for s in sorted(after & before):
            if hashes.get(s) and hashes.get(s) != new_hashes[s]:
                changed.append(s)

        def _upd(s: dict[str, Any]) -> None:
            s["last_sync"] = now_iso()
            s["hashes"] = new_hashes
            s["known_skills"] = sorted(after)
            pend = set(s.get("announce_new") or []) | set(added)
            s["announce_new"] = sorted(pend - set(removed))
            s["announce_changed"] = sorted(set(s.get("announce_changed") or []) | set(changed))
        update_state(_upd)
        retired = _retire_identical_shadows()
    return {"added": added, "removed": removed, "updated": changed, "skills": len(after),
            "saved_local_edits": str(backup) if backup else None, "retired_local_copies": retired}


def _skill_hash(d: Path) -> str:
    h = hashlib.sha256()
    if not d.is_dir():
        return ""
    for f in sorted(p for p in d.rglob("*") if p.is_file() and p.name != META_FILE):
        h.update(f.relative_to(d).as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _local_skill_dirs() -> dict[str, Path]:
    """Skills this profile has outside the guild library, by name (first wins)."""
    out: dict[str, Path] = {}
    try:
        from tools.skills_tool import skill_search_dirs
        roots, _active = skill_search_dirs()
        dirs = [Path(d) for _tier, d in roots]
    except Exception:
        dirs = []
    for d in dirs:
        if is_library_path(d) or not Path(d).is_dir():
            continue
        for md in Path(d).rglob("SKILL.md"):
            if ".archive" in md.parts or ".hub" in md.parts:
                continue
            out.setdefault(md.parent.name, md.parent)
    return out


def shadowed_skills() -> list[dict[str, str]]:
    """Guild skills hidden by a same-named local skill (local wins in Hermes' precedence)."""
    local = _local_skill_dirs()
    rows = []
    for s in list_library_skills():
        if s in local:
            same = _skill_hash(local[s]) == _skill_hash(library_skills_dir() / s)
            rows.append({"skill": s, "local_path": str(local[s]), "identical": "yes" if same else "no"})
    return rows


def _retire_identical_shadows() -> list[str]:
    """A local skill with a guild skill's name hides the team version (local wins in Hermes'
    precedence), so the member would never receive teammates' improvements. Retire the local copy
    to backups/ when it is identical, or when this member proposed it and the guild accepted it
    (their copy differs only by scrubbing and later reviewed changes). Anything else is left alone
    and reported by `hermes guild status`."""
    retired = []
    st = load_state()
    mine = set((st.get("proposed") or {}).keys())
    me = {member_name(), member_slug()}
    for row in shadowed_skills():
        meta = skill_meta(row["skill"])
        contributed = row["skill"] in mine and bool(me & {str(x) for x in (meta.get("contributors") or [])}
                                                    | {str(meta.get("author", "")), _slug(str(meta.get("author", "")))})
        if row["identical"] != "yes" and not contributed:
            continue
        src = Path(row["local_path"])
        dst = data_dir() / "backups" / "retired-local" / f"{row['skill']}-{_dt.datetime.now():%Y%m%d%H%M%S}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            retired.append(row["skill"])
        except Exception:
            logger.debug("guild: could not retire %s", src, exc_info=True)
    return retired


# --------------------------------------------------------------------------- proposals

@dataclass
class Proposal:
    id: str
    skill: str
    member: str
    branch: str
    kind: str                    # "new" | "update"
    note: str = ""
    proposed_at: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    redactions: list[dict[str, Any]] = field(default_factory=list)
    similar: list[dict[str, Any]] = field(default_factory=list)


def _read_skill_files(src: Path) -> tuple[dict[str, bytes], list[str]]:
    files: dict[str, bytes] = {}
    skipped: list[str] = []
    total = 0
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src).as_posix()
        if rel == META_FILE or rel.startswith((".git/", "__pycache__/")) or "/__pycache__/" in rel:
            continue
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            skipped.append(f"{rel} (over {MAX_FILE_BYTES // 1000} KB)")
            continue
        total += size
        if total > MAX_SKILL_BYTES:
            raise GuildError(f"Skill is larger than {MAX_SKILL_BYTES // 1_000_000} MB; trim it before sharing.")
        files[rel] = f.read_bytes()
    if "SKILL.md" not in files:
        raise GuildError(f"{src} has no SKILL.md")
    return files, skipped


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "use", "when", "this", "is"}
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in stop}


def similar_skills(skill: str, skill_md: str, *, exclude: Optional[str] = None) -> list[dict[str, Any]]:
    fm = _frontmatter(skill_md)
    mine = _tokens(skill.replace("-", " ") + " " + fm.get("description", ""))
    rows = []
    for other in list_library_skills():
        if other == exclude:
            continue
        try:
            otext = (library_skills_dir() / other / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        theirs = _tokens(other.replace("-", " ") + " " + _frontmatter(otext).get("description", ""))
        if not mine or not theirs:
            continue
        score = len(mine & theirs) / len(mine | theirs)
        if score >= 0.34:
            rows.append({"skill": other, "overlap": round(score, 2)})
    return sorted(rows, key=lambda r: -r["overlap"])[:3]


def resolve_local_skill(skill: str) -> Path:
    if not _SKILL_NAME_RE.match(skill or ""):
        raise GuildError(f"'{skill}' is not a valid skill name.")
    try:
        from tools.skills_tool import locate_skill, skill_lookup_path_error, skill_search_dirs
        if err := skill_lookup_path_error(skill):
            raise GuildError(err)
        roots, _active = skill_search_dirs()
        error, skill_dir, _md = locate_skill(skill, None, roots)
        if not error and skill_dir:
            return Path(skill_dir)
    except GuildError:
        raise
    except Exception:
        logger.debug("guild: locate_skill unavailable", exc_info=True)
    local = _local_skill_dirs()
    if skill in local:
        return local[skill]
    if (library_skills_dir() / skill / "SKILL.md").exists():
        return library_skills_dir() / skill
    raise GuildError(f"No skill named '{skill}' in this profile.")


def propose(skill: str, *, note: str = "", content: Optional[str] = None,
            evidence: Optional[dict[str, Any]] = None) -> Proposal:
    """Package *skill* (or, with *content*, a new SKILL.md for it), scrub it, and push it as a
    proposal branch. In a guild without review (solo mode) it lands on main directly."""
    if not is_configured():
        raise GuildError("Not in a guild yet. Run `hermes guild init <remote>` or `hermes guild join <remote>`.")
    if not _SKILL_NAME_RE.match(skill or ""):
        raise GuildError(f"'{skill}' is not a valid skill name (lowercase letters, digits, - and _).")
    in_library = (library_skills_dir() / skill / "SKILL.md").exists()
    if content is not None:
        if not content.strip():
            raise GuildError("Proposed SKILL.md content is empty.")
        base = library_skills_dir() / skill if in_library else None
        files: dict[str, bytes] = {}
        skipped: list[str] = []
        if base is not None:
            files, skipped = _read_skill_files(base)
        files["SKILL.md"] = content.encode("utf-8")
    else:
        src = resolve_local_skill(skill)
        files, skipped = _read_skill_files(src)

    clean: dict[str, bytes] = {}
    redactions: list[dict[str, Any]] = []
    for rel, data in files.items():
        if scrub.is_text(rel, data):
            text, found = scrub.scrub_text(data.decode("utf-8", errors="replace"))
            for f in found:
                f["file"] = rel
            redactions += found
            clean[rel] = text.encode("utf-8")
        else:
            clean[rel] = data
    skill_md = clean["SKILL.md"].decode("utf-8", errors="replace")
    sim = similar_skills(skill, skill_md, exclude=skill)
    member, mslug = member_name(), member_slug()
    branch = f"{PROPOSAL_PREFIX}{mslug}/{skill}"
    cfg = guild_config()
    direct = not cfg.get("require_review", True)
    prev = skill_meta(skill)
    meta = {
        "skill": skill, "author": prev.get("author") or member, "proposed_by": member,
        "proposed_at": now_iso(), "note": note.strip()[:500], "kind": "update" if in_library else "new",
        "version": int(prev.get("version") or 0) + (1 if direct else 0), "status": "approved" if direct else "proposed",
        "evidence": evidence or {}, "redactions": len(redactions), "skipped_files": skipped,
        "contributors": sorted(set((prev.get("contributors") or []) + [member])),
    }
    if direct:
        meta.update({"approved_by": member, "approved_at": now_iso()})
    with _lock:
        w = work_dir()
        _git(["fetch", "-q", "--prune", "origin"], cwd=w)
        _git(["checkout", "-q", "-f", "-B", MAIN if direct else branch, f"origin/{MAIN}"], cwd=w)
        _git(["clean", "-qfd"], cwd=w)
        dest = w / "skills" / skill
        if dest.exists():
            _rmtree(dest)
        for rel, data in clean.items():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        (dest / META_FILE).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        if direct:
            _append_decision(w, {"action": "approve", "skill": skill, "member": member, "by": member,
                                 "mode": "solo", "note": note[:200]})
        _git(["add", "-A"], cwd=w)
        if not _git(["status", "--porcelain"], cwd=w).strip():
            raise GuildError(f"'{skill}' is already in the guild exactly as you have it; nothing to propose.")
        verb = "add" if not in_library else "update"
        _git(["commit", "-q", "-m", f"guild: {verb} {skill}" + (f"\n\n{note.strip()}" if note.strip() else "")],
             cwd=w, identity=True)
        if direct:
            _git(["push", "-q", "origin", MAIN], cwd=w)
        else:
            _git(["push", "-q", "--force", "origin", f"{branch}:{branch}"], cwd=w)
        _git(["checkout", "-q", "--detach", f"origin/{MAIN}"], cwd=w, check=False)
    if direct:
        with _suppress():
            sync(quiet=True)
    update_state(lambda s: s.setdefault("proposed", {}).__setitem__(skill, now_iso()))
    return Proposal(id=f"{skill}@{mslug}", skill=skill, member=member, branch=branch if not direct else MAIN,
                    kind=meta["kind"], note=meta["note"], proposed_at=meta["proposed_at"],
                    evidence=meta["evidence"], redactions=redactions, similar=sim)


def _append_decision(repo: Path, entry: dict[str, Any]) -> None:
    log = repo / "log" / "decisions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": now_iso(), **entry}
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def list_proposals() -> list[Proposal]:
    if not is_configured():
        raise GuildError("Not in a guild yet.")
    with _lock:
        w = work_dir()
        _git(["fetch", "-q", "--prune", "origin"], cwd=w)
        out = _git(["for-each-ref", "--format=%(refname:short)", f"refs/remotes/origin/{PROPOSAL_PREFIX}"], cwd=w)
        props = []
        for ref in out.split():
            branch = ref[len("origin/"):]
            parts = branch.split("/")
            if len(parts) != 3:
                continue
            _, mslug, skill = parts
            meta = {}
            with _suppress():
                meta = json.loads(_git(["show", f"{ref}:skills/{skill}/{META_FILE}"], cwd=w))
            props.append(Proposal(id=f"{skill}@{mslug}", skill=skill, member=meta.get("proposed_by", mslug),
                                  branch=branch, kind=meta.get("kind", "new"), note=meta.get("note", ""),
                                  proposed_at=meta.get("proposed_at", ""), evidence=meta.get("evidence") or {},
                                  redactions=[{"count": meta.get("redactions", 0)}] if meta.get("redactions") else []))
        return sorted(props, key=lambda p: p.proposed_at)


def _find_proposal(pid: str) -> Proposal:
    pid = pid.strip()
    props = list_proposals()
    for p in props:
        if p.id == pid or p.branch == pid:
            return p
    matches = [p for p in props if p.skill == pid]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GuildError(f"Several proposals for '{pid}': " + ", ".join(p.id for p in matches))
    raise GuildError(f"No open proposal '{pid}'. Run `hermes guild review` to list them.")


def show_proposal(pid: str, *, max_lines: int = 160) -> dict[str, Any]:
    p = _find_proposal(pid)
    w = work_dir()
    stat = _git(["diff", "--stat", f"origin/{MAIN}...origin/{p.branch}", "--", f"skills/{p.skill}"], cwd=w)
    diff = _git(["diff", f"origin/{MAIN}...origin/{p.branch}", "--", f"skills/{p.skill}",
                 f":(exclude)skills/{p.skill}/{META_FILE}"], cwd=w)
    lines = diff.splitlines()
    truncated = len(lines) > max_lines
    skill_md = ""
    with _suppress():
        skill_md = _git(["show", f"origin/{p.branch}:skills/{p.skill}/SKILL.md"], cwd=w)
    meta = {}
    with _suppress():
        meta = json.loads(_git(["show", f"origin/{p.branch}:skills/{p.skill}/{META_FILE}"], cwd=w))
    return {"proposal": p, "meta": meta, "stat": stat.strip(),
            "diff": "\n".join(lines[:max_lines]) + ("\n... (diff truncated)" if truncated else ""),
            "similar": similar_skills(p.skill, skill_md, exclude=p.skill),
            "redaction_marks": scrub.count_marks(skill_md)}


def _check_reviewer(p: Proposal) -> str:
    cfg = guild_config()
    me = member_name()
    reviewers = cfg.get("reviewers") or []
    if reviewers and me not in reviewers and member_slug() not in {_slug(r) for r in reviewers}:
        raise GuildError(f"{me} is not a reviewer of this guild (reviewers: {', '.join(reviewers)}).")
    if _slug(p.member) == member_slug() and not cfg.get("allow_self_approve"):
        raise GuildError("You can't approve your own proposal. Ask another reviewer "
                         "(or set allow_self_approve: true in guild.yaml for a personal guild).")
    return me


def approve(pid: str, *, note: str = "") -> dict[str, Any]:
    p = _find_proposal(pid)
    me = _check_reviewer(p)
    with _lock:
        w = work_dir()
        _git(["checkout", "-q", "-f", "-B", MAIN, f"origin/{MAIN}"], cwd=w)
        _git(["clean", "-qfd"], cwd=w)
        dest = w / "skills" / p.skill
        if dest.exists():
            _rmtree(dest)
        _git(["checkout", f"origin/{p.branch}", "--", f"skills/{p.skill}"], cwd=w)
        mpath = dest / META_FILE
        meta = {}
        with _suppress():
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        prev_version = 0
        with _suppress():
            prev_version = int(json.loads(_git(["show", f"origin/{MAIN}:skills/{p.skill}/{META_FILE}"], cwd=w))
                               .get("version") or 0)
        meta.update({"status": "approved", "approved_by": me, "approved_at": now_iso(),
                     "version": prev_version + 1, "review_note": note.strip()[:500]})
        mpath.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        _append_decision(w, {"action": "approve", "skill": p.skill, "member": p.member, "by": me,
                             "version": meta["version"], "note": note[:200]})
        _git(["add", "-A"], cwd=w)
        _git(["commit", "-q", "-m", f"guild: approve {p.skill} v{meta['version']} from {p.member}\n\n"
                                     f"Reviewed-by: {me}"], cwd=w, identity=True)
        _git(["push", "-q", "origin", MAIN], cwd=w)
        _git(["push", "-q", "origin", "--delete", p.branch], cwd=w, check=False)
        _git(["checkout", "-q", "--detach", f"origin/{MAIN}"], cwd=w, check=False)
    res = sync(quiet=True)
    return {"skill": p.skill, "version": meta["version"], "author": p.member, "approved_by": me, "sync": res}


def reject(pid: str, *, reason: str = "") -> dict[str, Any]:
    p = _find_proposal(pid)
    me = member_name()
    with _lock:
        w = work_dir()
        _git(["checkout", "-q", "-f", "-B", MAIN, f"origin/{MAIN}"], cwd=w)
        _append_decision(w, {"action": "reject", "skill": p.skill, "member": p.member, "by": me,
                             "reason": reason[:300]})
        _git(["add", "-A"], cwd=w)
        _git(["commit", "-q", "-m", f"guild: decline {p.skill} from {p.member}\n\n{reason}".strip()],
             cwd=w, identity=True)
        _git(["push", "-q", "origin", MAIN], cwd=w)
        _git(["push", "-q", "origin", "--delete", p.branch], cwd=w, check=False)
        _git(["checkout", "-q", "--detach", f"origin/{MAIN}"], cwd=w, check=False)
    with _suppress():
        sync(quiet=True)
    return {"skill": p.skill, "author": p.member, "declined_by": me, "reason": reason}


def history(skill: Optional[str] = None, limit: int = 15) -> list[dict[str, str]]:
    if not is_configured():
        raise GuildError("Not in a guild yet.")
    args = ["log", f"-{limit}", "--format=%h%x09%ad%x09%an%x09%s", "--date=short", f"origin/{MAIN}"]
    if skill:
        args += ["--", f"skills/{skill}"]
    out = _git(args, cwd=library_dir())
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            rows.append({"commit": parts[0], "date": parts[1], "who": parts[2], "what": parts[3]})
    return rows


def revert(skill: str, commit: Optional[str] = None) -> dict[str, Any]:
    """Restore a guild skill to its state at *commit* (default: the version before the latest)."""
    if not is_configured():
        raise GuildError("Not in a guild yet.")
    if not _SKILL_NAME_RE.match(skill or ""):
        raise GuildError(f"'{skill}' is not a valid skill name.")
    if guild_config().get("reviewers") and member_name() not in guild_config()["reviewers"]:
        raise GuildError("Only a guild reviewer can revert a skill.")
    with _lock:
        w = work_dir()
        _git(["fetch", "-q", "origin"], cwd=w)
        _git(["checkout", "-q", "-f", "-B", MAIN, f"origin/{MAIN}"], cwd=w)
        if not commit:
            revs = _git(["log", "--format=%H", "-2", f"origin/{MAIN}", "--", f"skills/{skill}"], cwd=w).split()
            if len(revs) < 2:
                raise GuildError(f"'{skill}' has no earlier version to go back to.")
            commit = revs[1]
        if not re.match(r"^[0-9a-fA-F]{4,40}$", commit):
            raise GuildError("Give a commit id from `hermes guild log <skill>`.")
        dest = w / "skills" / skill
        if dest.exists():
            _rmtree(dest)
        _git(["checkout", commit, "--", f"skills/{skill}"], cwd=w)
        _append_decision(w, {"action": "revert", "skill": skill, "to": commit[:10], "by": member_name()})
        _git(["add", "-A"], cwd=w)
        _git(["commit", "-q", "-m", f"guild: revert {skill} to {commit[:10]}"], cwd=w, identity=True)
        _git(["push", "-q", "origin", MAIN], cwd=w)
        _git(["checkout", "-q", "--detach", f"origin/{MAIN}"], cwd=w, check=False)
    sync(quiet=True)
    return {"skill": skill, "restored_to": commit[:10]}


# --------------------------------------------------------------------------- status

def status() -> dict[str, Any]:
    st = load_state()
    if not is_configured():
        return {"configured": False}
    cfg = guild_config()
    skills = []
    usage = st.get("usage") or {}
    for s in list_library_skills():
        m = skill_meta(s)
        skills.append({"skill": s, "version": m.get("version", 1), "author": m.get("author", "?"),
                       "approved_by": m.get("approved_by", "?"), "uses_here": int(usage.get(s, 0))})
    return {"configured": True, "name": cfg["name"], "remote": st.get("remote"), "member": member_name(),
            "reviewers": cfg.get("reviewers") or [], "require_review": cfg.get("require_review", True),
            "last_sync": st.get("last_sync"), "skills": skills, "shadowed": shadowed_skills(),
            "candidates": sorted((st.get("candidates") or {}).keys())}
