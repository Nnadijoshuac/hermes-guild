"""End-to-end tests: two teammates (separate Hermes homes) sharing one bare guild repository.

Run inside a Hermes checkout's environment:  python -m pytest tests -q
Each test switches HERMES_HOME between members, the way two machines would differ.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SECRET_SKILL = """---
name: deploy-preview
description: Deploy a pull request to a preview environment and post the URL back.
---
# Deploy a PR preview

1. `cd C:\\Users\\alice\\code\\web` (or /home/alice/code/web) and build.
2. `RENDER_API_KEY=rnd_8fK2qLx9VmP3sT7wQz1aB4cD6eF0gH2j` then
   `curl -H "Authorization: Bearer rnd_8fK2qLx9VmP3sT7wQz1aB4cD6eF0gH2j" https://api.render.com/v1/services`
3. password = "hunter2hunter2"
4. Ping alice.okafor@acme-internal.com or check 10.20.1.15; clone https://bob:pa55word@git.acme.dev/x.git
"""


def _load_plugin():
    if "guild" in sys.modules:
        return sys.modules["guild"]
    spec = importlib.util.spec_from_file_location("guild", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["guild"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def team(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    pkg = _load_plugin()
    core = sys.modules["guild.core"]
    homes = {n: tmp_path / n for n in ("alice", "bob")}
    for h in homes.values():
        (h / "skills").mkdir(parents=True)
    remote = tmp_path / "acme.git"

    def be(name: str):
        monkeypatch.setenv("HERMES_HOME", str(homes[name]))
        monkeypatch.setenv("GUILD_DATA_DIR", str(homes[name] / "plugin-data" / "guild"))
        for mod in ("hermes_cli.config", "agent.skill_utils", "tools.skills_tool"):
            m = sys.modules.get(mod)
            for attr in ("_EXTERNAL_DIRS_CACHE",):
                if m is not None and isinstance(getattr(m, attr, None), dict):
                    getattr(m, attr).clear()
        return core

    be("alice")
    core.setup(str(remote), name="Acme", create=True, member="Alice", reviewers=["Alice", "Bob"])
    be("bob")
    core.setup(str(remote), member="Bob")
    return {"be": be, "homes": homes, "remote": remote, "pkg": pkg}


def _write_skill(home: Path, name: str, text: str) -> Path:
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


def test_scrub_masks_whole_tokens():
    _load_plugin()
    scrub = sys.modules["guild.scrub"]
    clean, findings = scrub.scrub_text(SECRET_SKILL)
    for leaked in ("rnd_8f", "gH2j", "hunter2", "alice.okafor", "10.20.1.15", "pa55word", "\\alice\\", "/alice/"):
        assert leaked not in clean, leaked
    kinds = {f["kind"] for f in findings}
    assert {"secret", "email", "private-ip", "home-path"} <= kinds
    again, more = scrub.scrub_text(clean)
    assert again == clean and more == []  # idempotent on already-scrubbed text


def test_propose_review_approve_sync(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    p = core.propose("deploy-preview", note="saves 10 min per preview")
    assert p.id == "deploy-preview@alice" and p.redactions

    core = team["be"]("bob")
    props = core.list_proposals()
    assert [x.id for x in props] == ["deploy-preview@alice"]
    shown = core.show_proposal("deploy-preview@alice")
    assert "rnd_8f" not in shown["diff"] and "hunter2" not in shown["diff"]
    res = core.approve("deploy-preview@alice", note="lgtm")
    assert res["version"] == 1 and "deploy-preview" in res["sync"]["added"]
    assert (core.library_skills_dir() / "deploy-preview" / "SKILL.md").exists()
    assert core.list_proposals() == []

    log = (core.library_dir() / "log" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"action": "approve"' in log and '"by": "Bob"' in log


def test_self_approval_and_non_reviewer_refused(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="x")
    with pytest.raises(core.GuildError, match="your own proposal"):
        core.approve("deploy-preview@alice")


def test_reject_and_revert(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="v1")
    core = team["be"]("bob")
    core.approve("deploy-preview@alice")
    lib_md = core.library_skills_dir() / "deploy-preview" / "SKILL.md"
    v1 = lib_md.read_text(encoding="utf-8")
    core.propose("deploy-preview", note="v2", content=v1 + "\n- New pitfall from Bob\n")
    core = team["be"]("alice")
    core.approve("deploy-preview@bob")
    assert "New pitfall from Bob" in (core.library_skills_dir() / "deploy-preview" / "SKILL.md").read_text(encoding="utf-8")
    core.revert("deploy-preview")
    assert "New pitfall" not in (core.library_skills_dir() / "deploy-preview" / "SKILL.md").read_text(encoding="utf-8")
    core.propose("deploy-preview", note="bad idea", content=v1 + "\nrm -rf /\n")
    core = team["be"]("bob")
    core.reject("deploy-preview@alice", reason="unsafe")
    assert core.list_proposals() == []


def test_guard_blocks_team_skill_writes_and_allows_reads(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="x")
    core = team["be"]("bob")
    core.approve("deploy-preview@alice")
    hooks = sys.modules["guild.hooks"]
    lib = core.library_skills_dir() / "deploy-preview"
    blocked = [
        ("skill_manage", {"operations": [{"action": "patch", "name": "deploy-preview", "old_string": "a", "new_string": "b"}]}),
        ("skill_manage", {"action": "delete", "name": "deploy-preview"}),
        ("write_file", {"path": str(lib / "SKILL.md"), "content": "x"}),
        ("terminal", {"command": f'echo x >> "{lib / "SKILL.md"}"'}),
    ]
    for tool, args in blocked:
        d = hooks.on_pre_tool_call(tool_name=tool, args=args)
        assert d and d["action"] == "block" and "guild_propose" in d["message"], (tool, d)
    allowed = [
        ("terminal", {"command": f'cat "{lib / "SKILL.md"}"'}),
        ("read_file", {"path": str(lib / "SKILL.md")}),
        ("skill_manage", {"operations": [{"action": "create", "name": "something-else", "content": "---\nname: x\n---"}]}),
    ]
    for tool, args in allowed:
        assert hooks.on_pre_tool_call(tool_name=tool, args=args) is None, tool
    assert hooks.on_pre_tool_call(tool_name="guild_propose", args={"skill": "x"})["action"] == "approve"


def test_announce_and_nudge(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="x")
    core = team["be"]("bob")
    core.approve("deploy-preview@alice")
    hooks = sys.modules["guild.hooks"]
    ctx = hooks.on_pre_llm_call()
    assert ctx and "deploy-preview" in ctx["context"]
    assert hooks.on_pre_llm_call() is None  # announced once
    _write_skill(team["homes"]["bob"], "invoice-ngn", "---\nname: invoice-ngn\ndescription: Naira invoice with VAT\n---\n")
    hooks.on_skill_lifecycle(action="created", skill_name="invoice-ngn", provenance="local", use_count=0)
    ctx = hooks.on_pre_llm_call()
    assert ctx and "/guild propose invoice-ngn" in ctx["context"]
    assert hooks.on_pre_llm_call() is None  # nudged once


def test_own_local_copy_retired_after_approval(team):
    core = team["be"]("alice")
    d = _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="x")
    core = team["be"]("bob")
    core.approve("deploy-preview@alice")
    core = team["be"]("alice")
    res = core.sync()
    assert "deploy-preview" in res["retired_local_copies"]
    assert not d.exists()
    assert list((core.data_dir() / "backups" / "retired-local").iterdir())


def test_tools_return_json_and_never_raise(team):
    core = team["be"]("bob")
    tools = sys.modules["guild.tools"]
    for fn, args in ((tools.guild_status, {}), (tools.guild_sync, {}), (tools.guild_review, {}),
                     (tools.guild_review, {"proposal": "nope@nobody"}), (tools.guild_propose, {"skill": "../etc"}),
                     (tools.guild_propose, {"skill": "missing-skill", "note": "x"})):
        out = json.loads(fn(args))
        assert "success" in out


def test_repropose_uses_guild_main_not_stale_library(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="v1")
    core = team["be"]("bob")
    core.approve("deploy-preview@alice")
    core = team["be"]("alice")  # Alice has not synced since Bob approved
    with pytest.raises(core.GuildError, match="nothing to propose"):
        core.propose("deploy-preview", note="same again")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL + "\n- New pitfall\n")
    p = core.propose("deploy-preview", note="adds a pitfall")
    assert p.kind == "update"
    shown = core.show_proposal("deploy-preview@alice")
    assert "New pitfall" in shown["diff"]


def test_rpc_methods_return_ok_or_error(team):
    core = team["be"]("alice")
    _write_skill(team["homes"]["alice"], "deploy-preview", SECRET_SKILL)
    core.propose("deploy-preview", note="x")
    core = team["be"]("bob")
    rpc = sys.modules["guild.rpc"].METHODS
    ov = rpc["overview"]({})
    assert ov["ok"] and [p["id"] for p in ov["proposals"]] == ["deploy-preview@alice"]
    shown = rpc["show"]({"id": "deploy-preview@alice"})
    assert shown["ok"] and "deploy-preview" in shown["diff"]
    assert rpc["show"]({"id": "nope@x"})["ok"] is False
    ap = rpc["approve"]({"id": "deploy-preview@alice"})
    assert ap["ok"] and ap["version"] == 1
    assert rpc["overview"]({})["skills"][0]["skill"] == "deploy-preview"
