"""`hermes guild ...` terminal commands and the `/guild ...` slash command."""

from __future__ import annotations

import argparse
import shlex
from typing import Any, Optional

from . import core, scrub

HUMAN_SURFACES = {"", "cli", "tui", "desktop", "local"}


# ------------------------------------------------------------------ rendering

def render_status(st: dict[str, Any]) -> str:
    if not st.get("configured"):
        return ("Not in a guild yet.\n"
                "  Start one:  hermes guild init <git remote> --name \"Acme Eng\"\n"
                "  Join one:   hermes guild join <git remote>")
    out = [f"Guild: {st['name']}   member: {st['member']}   last sync: {st.get('last_sync') or 'never'}",
           f"Remote: {st['remote']}",
           f"Review: {'required' if st['require_review'] else 'off (personal guild)'}"
           + (f"   reviewers: {', '.join(st['reviewers'])}" if st.get("reviewers") else "")]
    skills = st.get("skills") or []
    out.append(f"\nTeam skills ({len(skills)}):")
    if not skills:
        out.append("  (none yet; propose one with `hermes guild propose <skill>`)")
    for s in skills:
        out.append(f"  {s['skill']:<32} v{s['version']}  by {s['author']}, approved by {s['approved_by']}"
                   + (f"  · used {s['uses_here']}x here" if s.get("uses_here") else ""))
    if st.get("candidates"):
        out.append("\nLearned here, not shared yet: " + ", ".join(st["candidates"]))
    for row in st.get("shadowed") or []:
        out.append(f"\nNote: your local '{row['skill']}' hides the team version "
                   f"({'identical' if row['identical'] == 'yes' else 'different'}) at {row['local_path']}")
    return "\n".join(out)


def render_sync(res: dict[str, Any]) -> str:
    parts = [f"Synced. {res['skills']} team skill(s)."]
    if res.get("added"):
        parts.append("New: " + ", ".join(res["added"]))
    if res.get("updated"):
        parts.append("Updated: " + ", ".join(res["updated"]))
    if res.get("removed"):
        parts.append("Removed: " + ", ".join(res["removed"]))
    if res.get("retired_local_copies"):
        parts.append("Now using the team copy of: " + ", ".join(res["retired_local_copies"])
                     + " (your local copy was backed up to plugin-data/guild/backups)")
    if res.get("saved_local_edits"):
        parts.append(f"Edits made inside the read-only library were saved to {res['saved_local_edits']}")
    return "\n".join(parts)


def render_proposal(p: core.Proposal) -> str:
    lines = [f"Proposed {p.id} ({p.kind})" if p.branch != core.MAIN else f"Added {p.skill} to the guild (review off)",
             f"Scrubbed: {scrub.summarize(p.redactions)}"]
    if p.similar:
        lines.append("Overlaps with team skills: " + ", ".join(f"{s['skill']} ({int(s['overlap'] * 100)}%)"
                                                             for s in p.similar))
    if p.branch != core.MAIN:
        lines.append(f"A reviewer approves with: hermes guild approve {p.id}")
    return "\n".join(lines)


def render_review(props: list[core.Proposal]) -> str:
    if not props:
        return "No open proposals."
    out = [f"Open proposals ({len(props)}):"]
    for p in props:
        ev = ", ".join(f"{k}={v}" for k, v in (p.evidence or {}).items())
        out.append(f"  {p.id:<36} {p.kind:<6} {p.proposed_at[:16]}  {p.note[:70]}" + (f"  [{ev}]" if ev else ""))
    out.append("\nInspect: hermes guild show <id>    Decide: hermes guild approve <id> | reject <id> --reason ...")
    return "\n".join(out)


def render_show(d: dict[str, Any]) -> str:
    p: core.Proposal = d["proposal"]
    meta = d.get("meta") or {}
    out = [f"{p.id}: {p.kind} skill '{p.skill}' from {p.member} at {p.proposed_at}",
           f"Note: {p.note or '(none)'}"]
    if p.evidence:
        out.append("Evidence: " + ", ".join(f"{k}={v}" for k, v in p.evidence.items()))
    out.append(f"Scrubbed before sending: {meta.get('redactions', 0)} item(s)"
               + (f"; skipped: {', '.join(meta['skipped_files'])}" if meta.get("skipped_files") else ""))
    if d.get("similar"):
        out.append("Overlaps with: " + ", ".join(f"{s['skill']} ({int(s['overlap'] * 100)}%)" for s in d["similar"]))
    out += ["", d.get("stat") or "", "", d.get("diff") or "(no diff)"]
    return "\n".join(out)


# ------------------------------------------------------------------ terminal CLI

def setup_parser(sub: argparse.ArgumentParser) -> None:
    sp = sub.add_subparsers(dest="guild_command")
    p = sp.add_parser("init", help="Start a new guild in a git remote (or a local path) and join it")
    p.add_argument("remote")
    p.add_argument("--name", default=None, help="Guild name, e.g. 'Acme Eng'")
    p.add_argument("--member", default=None, help="Your name in the guild (default: git user.name)")
    p.add_argument("--reviewer", action="append", default=None, help="Who may approve (repeatable)")
    p.add_argument("--solo", action="store_true", help="Personal guild: sync your own devices/profiles, no review")
    p = sp.add_parser("join", help="Join an existing guild")
    p.add_argument("remote")
    p.add_argument("--member", default=None)
    sp.add_parser("status", help="Team skills, learned-but-unshared skills, shadowing")
    sp.add_parser("sync", help="Pull the latest approved team skills")
    p = sp.add_parser("propose", help="Propose a skill from this profile to the guild")
    p.add_argument("skill")
    p.add_argument("--note", default="")
    sp.add_parser("review", help="List open proposals")
    p = sp.add_parser("show", help="Show a proposal's diff, scrub report and overlaps")
    p.add_argument("proposal")
    p = sp.add_parser("approve", help="Approve a proposal (reviewers only)")
    p.add_argument("proposal")
    p.add_argument("--note", default="")
    p = sp.add_parser("reject", help="Decline a proposal")
    p.add_argument("proposal")
    p.add_argument("--reason", default="")
    p = sp.add_parser("log", help="History of the guild or of one skill")
    p.add_argument("skill", nargs="?")
    p = sp.add_parser("revert", help="Roll a team skill back to an earlier version")
    p.add_argument("skill")
    p.add_argument("--to", default=None, help="Commit id from `hermes guild log <skill>`")
    sp.add_parser("leave", help="Leave the guild and unmount its library")
    sub.set_defaults(func=handle_cli)


def run(cmd: str, a: Any) -> str:
    if cmd in ("init", "join"):
        r = core.setup(a.remote, name=getattr(a, "name", None), create=cmd == "init",
                       solo=getattr(a, "solo", False), member=getattr(a, "member", None),
                       reviewers=getattr(a, "reviewer", None))
        lines = [f"{'Founded' if r.seeded else 'Joined'} guild '{r.name}' at {r.remote}",
                 f"{r.skills} team skill(s) mounted read-only from {r.external_dir}"]
        lines += r.notes
        lines.append("Teammates join with: hermes guild join " + r.remote)
        return "\n".join(lines)
    if cmd == "status":
        return render_status(core.status())
    if cmd == "sync":
        return render_sync(core.sync())
    if cmd == "propose":
        return render_proposal(core.propose(a.skill, note=a.note))
    if cmd == "review":
        return render_review(core.list_proposals())
    if cmd == "show":
        return render_show(core.show_proposal(a.proposal))
    if cmd == "approve":
        r = core.approve(a.proposal, note=getattr(a, "note", ""))
        return (f"Approved {r['skill']} v{r['version']} from {r['author']}. Every member's agent gets it on "
                f"their next sync.\n" + render_sync(r["sync"]))
    if cmd == "reject":
        r = core.reject(a.proposal, reason=getattr(a, "reason", ""))
        return f"Declined {r['skill']} from {r['author']}." + (f" Reason: {r['reason']}" if r["reason"] else "")
    if cmd == "log":
        rows = core.history(getattr(a, "skill", None))
        return "\n".join(f"{r['commit']}  {r['date']}  {r['who']:<16} {r['what']}" for r in rows) or "No history."
    if cmd == "revert":
        r = core.revert(a.skill, getattr(a, "to", None))
        return f"Reverted {r['skill']} to {r['restored_to']}. Members get it on their next sync."
    if cmd == "leave":
        core.leave()
        return "Left the guild. Team skills are no longer loaded in this profile."
    return HELP


HELP = """Guild: one reviewed skill library for your whole team's Hermes agents.

  hermes guild init <remote> --name "Acme Eng"   start a guild (git URL or a folder path)
  hermes guild join <remote>                     join one
  hermes guild status | sync | log [skill]
  hermes guild propose <skill> --note "..."      share a skill your agent learned
  hermes guild review | show <id>                see open proposals
  hermes guild approve <id> | reject <id>        decide (reviewers)
  hermes guild revert <skill> [--to <commit>]    roll a team skill back
  hermes guild leave"""


def handle_cli(args: argparse.Namespace) -> int:
    cmd = getattr(args, "guild_command", None)
    if not cmd:
        print(HELP)
        return 0
    try:
        print(run(cmd, args))
        return 0
    except core.GuildError as exc:
        print(f"guild: {exc}")
        return 1


# ------------------------------------------------------------------ slash command

class _NS:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def handle_slash(raw_args: str, *, platform: str = "", **_: Any) -> str:
    try:
        toks = shlex.split(raw_args or "")
    except ValueError:
        toks = (raw_args or "").split()
    if not toks or toks[0] in ("help", "-h", "--help"):
        return HELP.replace("hermes guild", "/guild")
    cmd, rest = toks[0], toks[1:]
    if cmd in ("approve", "reject", "revert", "init", "join", "leave") and (platform or "") not in HUMAN_SURFACES:
        return (f"/guild {cmd} only runs from the Hermes CLI, TUI or Desktop, where the person at the keyboard "
                "is the reviewer. Use `hermes guild " + cmd + " ...` there.")
    try:
        if cmd in ("propose",):
            if not rest:
                return "Usage: /guild propose <skill> [note...]"
            return run(cmd, _NS(skill=rest[0], note=" ".join(rest[1:])))
        if cmd in ("show", "approve"):
            if not rest:
                return f"Usage: /guild {cmd} <skill@member>"
            return run(cmd, _NS(proposal=rest[0], note=" ".join(rest[1:])))
        if cmd == "reject":
            if not rest:
                return "Usage: /guild reject <skill@member> [reason...]"
            return run(cmd, _NS(proposal=rest[0], reason=" ".join(rest[1:])))
        if cmd == "log":
            return run(cmd, _NS(skill=rest[0] if rest else None))
        if cmd == "revert":
            if not rest:
                return "Usage: /guild revert <skill> [commit]"
            return run(cmd, _NS(skill=rest[0], to=rest[1] if len(rest) > 1 else None))
        if cmd in ("init", "join"):
            if not rest:
                return f"Usage: /guild {cmd} <remote>"
            return run(cmd, _NS(remote=rest[0], name=None, member=None, reviewer=None, solo="--solo" in rest))
        return run(cmd, _NS())
    except core.GuildError as exc:
        return f"guild: {exc}"
