"""Plugin RPC methods for Guild's Desktop page (served as ``plugin.guild.<name>``).

Desktop calls these over the gateway's own authenticated JSON-RPC socket. Each returns
``{"ok": True, ...}`` or ``{"ok": False, "error": "..."}`` and never raises.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from . import core, scrub


def _safe(fn: Callable[[dict], dict]) -> Callable[..., dict]:
    def handler(params: Any = None, **_: Any) -> dict:
        try:
            return {"ok": True, **fn(params if isinstance(params, dict) else {})}
        except core.GuildError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # never let a UI call take the gateway lane down
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return handler


def _prop(p: core.Proposal) -> dict:
    d = dataclasses.asdict(p)
    d["redactions"] = len(p.redactions)
    return d


def _overview(_: dict) -> dict:
    st = core.status()
    if not st.get("configured"):
        return {"configured": False}
    try:
        st["proposals"] = [_prop(p) for p in core.list_proposals()]
    except core.GuildError as exc:
        st["proposals"], st["proposals_error"] = [], str(exc)
    return st


def _show(params: dict) -> dict:
    d = core.show_proposal(str(params.get("id", "")), max_lines=400)
    return {"proposal": _prop(d["proposal"]), "meta": d["meta"], "stat": d["stat"], "diff": d["diff"],
            "similar": d["similar"], "scrub_marks": d["redaction_marks"]}


def _approve(params: dict) -> dict:
    r = core.approve(str(params.get("id", "")), note=str(params.get("note", ""))[:500])
    return {"skill": r["skill"], "version": r["version"], "author": r["author"], "approved_by": r["approved_by"]}


def _reject(params: dict) -> dict:
    return core.reject(str(params.get("id", "")), reason=str(params.get("reason", ""))[:300])


def _propose(params: dict) -> dict:
    p = core.propose(str(params.get("skill", "")), note=str(params.get("note", ""))[:500])
    return {"proposal": _prop(p), "scrubbed": scrub.summarize(p.redactions)}


def _sync(_: dict) -> dict:
    return core.sync()


METHODS: dict[str, Callable[..., dict]] = {
    "overview": _safe(_overview),
    "show": _safe(_show),
    "approve": _safe(_approve),
    "reject": _safe(_reject),
    "propose": _safe(_propose),
    "sync": _safe(_sync),
}
