# Guild

![Guild](docs/banner.png)

Hermes writes a skill whenever it figures something out. On a team, each person's agent does this on its own. If Alice's agent works out how to deploy a preview build on Monday, Bob's agent has to work it out again on Thursday.

Guild gives a team's agents one shared skill library. The library is a git repository the team already has. A skill goes in only after a person on the team reviews it, and once it is in, every member's agent loads it.

```
Alice's agent saves a skill
   hermes guild propose deploy-preview
      -> API keys, home folder paths, e-mail addresses and private IPs are removed
      -> pushed to the branch proposal/alice/deploy-preview
Bob reviews it
   hermes guild show deploy-preview@alice
   hermes guild approve deploy-preview@alice
      -> merged into main and recorded in log/decisions.jsonl
Every member's agent syncs and can use it
```

Team skills are read-only on each machine. If Bob's agent tries to edit one, Guild stops the edit and tells the agent to send the change as a proposal, and a person reviews it like any other.

## What the plugin adds to Hermes

- A `pre_tool_call` hook refuses `skill_manage`, `write_file`, `patch` and shell commands that would change a team skill. The refusal message tells the agent to call `guild_propose`.
- The agent can propose, sync and read the review queue. It cannot approve. Approving, rejecting and reverting only run from the terminal, the TUI or Desktop, and never from a tool call or a messaging platform.
- `guild_propose` goes through Hermes' approval prompt before anything is sent.
- Before a skill is sent, each text file is passed through Hermes' secret redactor (`agent.redact.redact_sensitive_text`, forced on). Guild then masks the whole token the redactor found, since the log format keeps the first and last characters of a key. It also removes home folder paths, e-mail addresses and private IP addresses. The person proposing and the reviewer both see a count of what was removed and on which lines.
- An `on_skill_lifecycle` hook notices when your agent saves a new skill, and the agent mentions once that you can share it. When a teammate's skill is approved, your agent is told about it on its next turn.
- Everything lives in git, so you get history for free and `hermes guild revert <skill>` puts back an earlier version.

## Install

```bash
hermes plugins install guild
```

You need `git` on your PATH.

## Commands

```bash
# Start a guild in any git remote you can push to, or in a folder
hermes guild init git@github.com:acme/agent-skills.git --name "Acme Eng" --reviewer Alice --reviewer Bob

# Everyone else joins it
hermes guild join git@github.com:acme/agent-skills.git

hermes guild propose deploy-preview --note "saves about 10 minutes per preview"
hermes guild review                        # open proposals
hermes guild show deploy-preview@alice     # diff, what was removed, similar team skills
hermes guild approve deploy-preview@alice  # or: hermes guild reject <id> --reason "..."
hermes guild status
hermes guild sync
hermes guild log [skill]
hermes guild revert <skill> [--to <commit>]
hermes guild leave
```

In a chat, `/guild status`, `/guild propose <skill>`, `/guild review` and `/guild sync` do the same thing.

In Hermes Desktop, Guild adds a **Guild** page to the sidebar (also in ⌘K as "Guild: open team skills"). It lists proposals waiting for review, opens each one's diff with a count of what was removed before sending, and has Approve and Decline buttons. Below that are the team's skills and any skills you learned that the team doesn't have yet, each with a button to propose it. The page talks to the plugin through Hermes' plugin RPC (`plugin.guild.*`), so there is no extra server.

If you work alone across several machines or profiles, `hermes guild init <remote> --solo` creates a guild with no review step, so each of your machines picks up what the others learn.

## Agent tools

| Tool | Purpose |
|---|---|
| `guild_status` | Lists team skills with version, author and reviewer, plus skills you have that the team doesn't |
| `guild_sync` | Pulls newly approved skills |
| `guild_propose` | Proposes a local skill, or a full replacement SKILL.md for a team skill. Asks you first. |
| `guild_review` | Shows open proposals and a proposal's diff so the agent can help a reviewer. It cannot approve. |

## Settings

Under `plugins.entries.guild.settings`, or Desktop → Settings → Plugins.

| Key | Default | Meaning |
|---|---|---|
| `auto_sync_minutes` | 15 | At session start, sync in the background if the last sync is older than this. 0 turns it off. |
| `suggest_sharing` | true | The agent mentions once when a skill it saved could be shared |
| `approve_before_propose` | true | Hermes asks you before `guild_propose` sends anything |

## How it works

The guild repository holds `guild.yaml` (name, reviewers, policy), one folder per approved skill under `skills/` with a `.guild.json` file recording author, reviewer and version, and `log/decisions.jsonl` with every approval, rejection and revert.

Each profile keeps two clones in `<HERMES_HOME>/plugin-data/guild/`. `library/` tracks `main`, and its `skills/` folder is added to `skills.external_dirs`, so team skills appear in Hermes' skill index next to your own. `work/` is where proposal branches and approvals are built.

Hermes prefers a local skill over an external one with the same name. When your own proposal is approved, Guild moves your local copy to `plugin-data/guild/backups/` so that you get later team changes. Other name clashes are listed in `hermes guild status` and left alone.

## Disclosure

- Network: Guild runs `git fetch`, `git push` and `git clone` against the remote you configure, and contacts nothing else.
- What leaves your machine: the files of a skill that you propose, or that your agent proposes after you approve the prompt, after scrubbing. Commits include your guild member name and the time.
- Subprocesses: `git`, with `GIT_TERMINAL_PROMPT=0`, repository hooks disabled and a 60 second timeout.
- Background work: an optional sync thread at session start (see `auto_sync_minutes`).
- Config changes: `init` and `join` add the library folder to the profile's `skills.external_dirs`. `leave` removes it.
- Credentials: Guild stores none. Git uses your existing credential helper or SSH key.
- Limits: the read-only check works like Hermes' own skill lock. It reads tool arguments and does not inspect code run through `execute_code`. The reviewer list in `guild.yaml` is enforced by Guild only, so turn on branch protection for `main` on your git host if you need it enforced server side.

## Tests

```bash
cd tests && python -m pytest -q   # run inside a Hermes environment
hermes plugins validate .
```

The tests create two members and a bare repository and run the full propose, review, approve, reject and revert cycle.

MIT, NNADI Joshua
