"""Guild: one reviewed skill library for a whole team's Hermes agents.

Hermes learns skills from the work it does, but each agent learns alone. Guild connects a team's
agents (or one person's laptops and profiles) through a git repository they already control:
skills an agent learns are scrubbed and proposed, a human reviewer approves them, and every
member's agent loads the approved version read-only, so nobody's agent can silently overwrite
the team's copy.
"""

from __future__ import annotations

import logging

from . import cli, core, hooks, rpc, tools

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    def _setting(key, default):
        try:
            return ctx.get_config(key, default=default)
        except Exception:
            return default

    hooks.configure({
        "auto_sync_minutes": _setting("auto_sync_minutes", 15),
        "suggest_sharing": _setting("suggest_sharing", True),
        "approve_before_propose": _setting("approve_before_propose", True),
    })

    ctx.register_tool(name="guild_status", toolset="guild", schema=tools.GUILD_STATUS, handler=tools.guild_status)
    ctx.register_tool(name="guild_sync", toolset="guild", schema=tools.GUILD_SYNC, handler=tools.guild_sync)
    ctx.register_tool(name="guild_propose", toolset="guild", schema=tools.GUILD_PROPOSE, handler=tools.guild_propose)
    ctx.register_tool(name="guild_review", toolset="guild", schema=tools.GUILD_REVIEW, handler=tools.guild_review)

    ctx.register_hook("pre_tool_call", hooks.on_pre_tool_call)
    ctx.register_hook("on_skill_lifecycle", hooks.on_skill_lifecycle)
    ctx.register_hook("pre_llm_call", hooks.on_pre_llm_call)
    ctx.register_hook("on_session_start", hooks.maybe_background_sync)

    ctx.register_command("guild", handler=cli.handle_slash,
                         description="Team skill guild: status, sync, propose, review, approve",
                         args_hint="status|sync|propose <skill>|review|show <id>|approve <id>|reject <id>")
    ctx.register_cli_command(name="guild", help="Share reviewed skills across your team's Hermes agents",
                             setup_fn=cli.setup_parser, handler_fn=cli.handle_cli)

    # Methods for the Desktop page (desktop/plugin.js), served as plugin.guild.<name>.
    register_rpc = getattr(ctx, "register_rpc", None)
    if callable(register_rpc):
        for name, handler in rpc.METHODS.items():
            try:
                register_rpc(name, handler)
            except Exception:
                logger.debug("guild: could not register rpc %s", name, exc_info=True)
