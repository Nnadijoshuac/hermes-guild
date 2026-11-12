"""Guild hooks.

pre_tool_call       keeps team skills read-only (the agent proposes instead of overwriting) and
                    asks the human before guild_propose sends a skill to the team remote
on_skill_lifecycle  notices skills this agent learns, and counts uses of team skills
pre_llm_call        tells the agent once about new team skills, and once about a skill it
                    learned that the team doesn't have yet
on_session_start    background sync with the team remote (rate-limited, never blocks a turn)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import core

logger = logging.getLogger(__name__)

_MUTATING = {"create", "edit", "patch", "write_file", "remove_file", "delete"}
_WRITEISH = re.compile(
    r"(>|\b(rm|mv|cp|tee|touch|mkdir|rmdir|chmod|chown|ln|truncate|dd|install|rsync|unzip|tar|git)\b|sed\s+-i|perl\s+-i"
    r"|remove-item|set-content|add-content|out-file|move-item|copy-item|new-item|rename-item|clear-content)",
    re.I)
_settings: dict[str, Any] = {"auto_sync_minutes": 15, "suggest_sharing": True, "approve_before_propose": True}
_sync_thread: Optional[threading.Thread] = None


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _safe(value: Any, limit: int = 64) -> str:
    """Text from the shared repo (names, authors) before it goes into the agent's context:
    no newlines or control characters, and capped, so a teammate can't inject instructions."""
    text = " ".join(_CONTROL.sub(" ", str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def configure(settings: dict[str, Any]) -> None:
    _settings.update({k: v for k, v in settings.items() if v is not None})


# ------------------------------------------------------------------ pre_tool_call

def _block(skill: str) -> dict[str, str]:
    meta = core.skill_meta(skill)
    name = _safe(core.guild_config().get("name", "team"), 40)
    skill = _safe(skill)
    who = f", by {_safe(meta['author'], 40)}" if meta.get("author") else ""
    return {"action": "block", "message": (
        f"Guild: '{skill}' is a team skill from the {name} guild (v{_safe(meta.get('version', 1), 8)}{who}). "
        "It is read-only here so every teammate's agent keeps running the same reviewed version. "
        f"To improve it, call guild_propose with skill='{skill}', content set to the full improved SKILL.md, "
        "and a one-line note saying what changed and why. A teammate reviews it before it reaches everyone.")}


def _library_skill_target(name: str) -> Optional[str]:
    """The guild skill a skill_manage op would change, or None when it resolves elsewhere."""
    skill = Path(str(name)).name
    if skill not in set(core.list_library_skills()):
        return None
    try:
        return skill if core.is_library_path(core.resolve_local_skill(skill)) else None
    except core.GuildError:
        return skill


def _ops(args: dict[str, Any]) -> list[dict[str, Any]]:
    ops = args.get("operations")
    if isinstance(ops, list):
        return [o for o in ops if isinstance(o, dict)]
    return [args]


def _cwd() -> Path:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return Path(resolve_agent_cwd())
    except Exception:
        return Path.cwd()


def _norm(p: str) -> str:
    return os.path.normcase(str(p)).replace("\\", "/").rstrip("/")


def on_pre_tool_call(tool_name: str = "", args: Optional[dict[str, Any]] = None, **_: Any):
    try:
        args = args or {}
        if tool_name == "guild_propose" and _settings.get("approve_before_propose", True):
            if not core.is_configured():
                return None
            remote = core.load_state().get("remote", "the guild remote")
            skill = args.get("skill", "?")
            return {"action": "approve", "message": (
                f"Share skill '{_safe(skill)}' with the {_safe(core.guild_config().get('name'), 40)} guild? Its files are scrubbed "
                f"for secrets, then pushed to {remote} as a proposal for review.")}
        if not core.is_configured():
            return None
        if tool_name == "skill_manage":
            for op in _ops(args):
                if str(op.get("action", "")) in _MUTATING and op.get("name"):
                    if (hit := _library_skill_target(op["name"])) is not None:
                        return _block(hit)
            return None
        if tool_name in ("write_file", "patch"):
            path = args.get("path") or args.get("file_path")
            if isinstance(path, str) and path:
                p = Path(os.path.expanduser(path))
                p = p if p.is_absolute() else _cwd() / p
                if core.is_library_path(p):
                    rel = Path(os.path.relpath(p, core.library_skills_dir())).parts
                    return _block(rel[0] if rel and rel[0] != ".." else "library")
            return None
        if tool_name == "terminal":
            cmd = args.get("command")
            if isinstance(cmd, str) and cmd:
                lib = _norm(core.library_dir())
                if lib in _norm(cmd) and _WRITEISH.search(cmd):
                    m = re.search(re.escape(lib) + r"/skills/([a-z0-9_-]+)", _norm(cmd))
                    return _block(m.group(1) if m else "library")
        return None
    except Exception:
        logger.debug("guild pre_tool_call failed open", exc_info=True)
        return None


# ------------------------------------------------------------------ on_skill_lifecycle

def on_skill_lifecycle(action: str = "", skill_name: str = "", provenance: str = "", use_count: Any = None,
                       **_: Any) -> None:
    try:
        if not skill_name or not core.is_configured():
            return
        library = set(core.list_library_skills())
        skill = Path(skill_name).name

        def _upd(s: dict[str, Any]) -> None:
            if skill in library and action == "loaded":
                usage = s.setdefault("usage", {})
                usage[skill] = int(usage.get(skill, 0)) + 1
                return
            if skill in library or provenance in ("installed",):
                return
            cands = s.setdefault("candidates", {})
            if action in ("created", "patched", "edited"):
                row = cands.setdefault(skill, {"first_seen": core.now_iso(), "nudged": False})
                row["last_change"] = core.now_iso()
                row["last_action"] = action
                if use_count is not None:
                    row["use_count"] = use_count
            elif action == "loaded" and skill in cands and use_count is not None:
                cands[skill]["use_count"] = use_count
        core.update_state(_upd)
    except Exception:
        logger.debug("guild on_skill_lifecycle failed", exc_info=True)


# ------------------------------------------------------------------ pre_llm_call

def on_pre_llm_call(**_: Any):
    try:
        if not core.is_configured():
            return None
        notes: list[str] = []
        st = core.load_state()
        new, changed = st.get("announce_new") or [], st.get("announce_changed") or []
        gname = _safe(core.guild_config().get("name", "team"), 40)
        if new or changed:
            def _who(s: str) -> str:
                m = core.skill_meta(s)
                return (f"{_safe(s)} (by {_safe(m.get('author', '?'), 40)}, "
                        f"approved by {_safe(m.get('approved_by', '?'), 40)})")
            parts = []
            if new:
                parts.append("new: " + ", ".join(_who(s) for s in new[:8]))
            if changed:
                parts.append("updated: " + ", ".join(_safe(c) for c in changed[:8]))
            notes.append(f"[Guild] Your team's {gname} guild has shared skills you can use now, {'; '.join(parts)}. "
                         "They are in your skill index. Prefer them over improvising when they fit, and briefly "
                         "tell the user when a team skill helped.")
        nudge = None
        if _settings.get("suggest_sharing", True):
            proposed = st.get("proposed") or {}
            for skill, row in sorted((st.get("candidates") or {}).items()):
                if row.get("nudged") or skill in proposed:
                    continue
                nudge = skill
                shown = _safe(skill)
                notes.append(f"[Guild] You saved the skill '{shown}' and your {gname} guild doesn't have it yet. "
                             f"At the end of this reply, add one short line telling the user they can share it "
                             f"with their team via `/guild propose {shown}`. Don't call guild_propose unless the "
                             "user asks.")
                break

        if new or changed or nudge:
            def _clear(s: dict[str, Any]) -> None:
                s["announce_new"], s["announce_changed"] = [], []
                if nudge:
                    s.setdefault("candidates", {}).setdefault(nudge, {})["nudged"] = True
            core.update_state(_clear)
        return {"context": "\n".join(notes)} if notes else None
    except Exception:
        logger.debug("guild pre_llm_call failed open", exc_info=True)
        return None


# ------------------------------------------------------------------ background sync

def maybe_background_sync(**_: Any) -> None:
    global _sync_thread
    try:
        minutes = float(_settings.get("auto_sync_minutes") or 0)
        if minutes <= 0 or not core.is_configured():
            return
        last = core.load_state().get("last_sync") or ""
        try:
            import datetime as dt
            age = time.time() - dt.datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
        except Exception:
            age = 1e9
        if age < minutes * 60 or (_sync_thread and _sync_thread.is_alive()):
            return

        def _run() -> None:
            try:
                core.sync(quiet=True)
            except Exception as exc:
                logger.info("guild: background sync skipped: %s", exc)
        _sync_thread = threading.Thread(target=_run, name="guild-sync", daemon=True)
        _sync_thread.start()
    except Exception:
        logger.debug("guild background sync failed", exc_info=True)
