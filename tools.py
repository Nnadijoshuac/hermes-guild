"""Agent-facing Guild tools. Agents can read the guild, sync it and propose skills; approving is a
human-only action (CLI, TUI, Desktop), so no tool here can merge anything."""

from __future__ import annotations

import json
from typing import Any

from . import core, scrub

GUILD_STATUS = {
    "name": "guild_status",
    "description": (
        "Show the team skill guild this agent belongs to: the shared skills (version, author, reviewer), "
        "skills you learned that the team doesn't have yet, open proposals awaiting review, and when the "
        "library last synced. Use when the user asks what their team's agents know or what's pending."),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

GUILD_SYNC = {
    "name": "guild_sync",
    "description": "Pull the latest reviewed skills from the team guild now. Returns what was added or updated.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

GUILD_PROPOSE = {
    "name": "guild_propose",
    "description": (
        "Propose a skill to the team guild for review. Without `content`, packages this profile's skill "
        "of that name (SKILL.md plus references/templates/scripts). With `content`, proposes that full "
        "SKILL.md as an improvement to an existing team skill (team skills are read-only locally; this is "
        "how you change one). Files are scrubbed for secrets, personal paths, e-mails and private IPs "
        "first. Only call it when the user asked to share or when improving a team skill you were told "
        "is read-only. A human teammate reviews before anyone receives it."),
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill name (lowercase, hyphens), e.g. 'deploy-preview'."},
            "note": {"type": "string", "description": "One or two lines for the reviewer: what it does or what changed, and why."},
            "content": {"type": "string", "description": "Optional full replacement SKILL.md (frontmatter + body) when improving a team skill."},
        },
        "required": ["skill", "note"],
    },
}

GUILD_REVIEW = {
    "name": "guild_review",
    "description": (
        "List the guild's open proposals (skill, who proposed it, note, evidence) or, with `proposal`, "
        "show one proposal's diff, scrub marks and overlap with existing team skills, so you can help a "
        "reviewer decide. You cannot approve; tell the user to run `/guild approve <id>` or "
        "`hermes guild approve <id>`."),
    "parameters": {
        "type": "object",
        "properties": {"proposal": {"type": "string", "description": "Optional proposal id like 'skill@member'."}},
        "required": [],
    },
}


def _ok(**data: Any) -> str:
    return json.dumps({"success": True, **data}, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def guild_status(args: dict, **_: Any) -> str:
    try:
        st = core.status()
        if not st.get("configured"):
            return _err("This profile is not in a guild. The user can run `hermes guild init <git remote>` "
                        "(new guild) or `hermes guild join <git remote>`.")
        try:
            st["open_proposals"] = [{"id": p.id, "skill": p.skill, "by": p.member, "note": p.note}
                                    for p in core.list_proposals()]
        except core.GuildError as exc:
            st["open_proposals_error"] = str(exc)
        return _ok(**st)
    except core.GuildError as exc:
        return _err(str(exc))
    except Exception as exc:  # never raise into the agent loop
        return _err(f"guild_status failed: {exc}")


def guild_sync(args: dict, **_: Any) -> str:
    try:
        return _ok(**core.sync())
    except core.GuildError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"guild_sync failed: {exc}")


def guild_propose(args: dict, **_: Any) -> str:
    try:
        skill = str(args.get("skill") or "").strip()
        note = str(args.get("note") or "").strip()
        content = args.get("content")
        if content is not None and not isinstance(content, str):
            return _err("content must be the full SKILL.md text.")
        evidence = {}
        cand = (core.load_state().get("candidates") or {}).get(skill) or {}
        if cand.get("use_count") is not None:
            evidence["uses_by_author"] = cand.get("use_count")
        if cand.get("first_seen"):
            evidence["learned_at"] = cand.get("first_seen")
        p = core.propose(skill, note=note, content=content, evidence=evidence)
        return _ok(proposal=p.id, branch=p.branch, kind=p.kind,
                   scrubbed=scrub.summarize(p.redactions), similar_team_skills=p.similar,
                   next=("Merged straight into the guild (review is off)." if p.branch == core.MAIN else
                         "Waiting for a reviewer: `hermes guild review` / `/guild review`."))
    except core.GuildError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"guild_propose failed: {exc}")


def guild_review(args: dict, **_: Any) -> str:
    try:
        pid = str(args.get("proposal") or "").strip()
        if pid:
            d = core.show_proposal(pid, max_lines=120)
            p = d["proposal"]
            return _ok(id=p.id, skill=p.skill, by=p.member, kind=p.kind, note=p.note, evidence=p.evidence,
                       stat=d["stat"], diff=d["diff"], similar_team_skills=d["similar"],
                       scrub_marks=d["redaction_marks"], approve_with=f"/guild approve {p.id}")
        rows = [{"id": p.id, "skill": p.skill, "by": p.member, "kind": p.kind, "note": p.note,
                 "proposed_at": p.proposed_at, "evidence": p.evidence} for p in core.list_proposals()]
        return _ok(open_proposals=rows)
    except core.GuildError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"guild_review failed: {exc}")
