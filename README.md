# Session context cap

A per-session context cap for Claude Code. When a session's context passes the cap (200k tokens
by default), a Stop hook stops it once and directs it to hand over; the `session-cap:handover` skill writes a
status document from disk and records it, and a fresh session continues from that document.

Packaged as a Claude Code plugin, `session-cap`, because it bundles hooks, a skill and scripts
that belong together. Nothing loads until you install it. To have sessions start their own
successors and be managed from one place, add [`session-fleet`](https://github.com/OzturkArda/claude-session-fleet) on top.

## Why a cap

Answer quality falls as a session's context fills, well before the model's window is full, and a
long session also costs more per turn. A fixed cap turns "start a fresh session at some point" into
a defined moment with a defined procedure, so work moves to a clean session with its state intact
instead of degrading in place.

**No advance warning, by design.** A session told it is close to the cap rations itself for the
rest of its life and hands over in a worse state than it was really in. So the session hears about
the cap exactly once, at a turn boundary, as one directive. The cap is likewise kept out of
`CLAUDE.md` and launch prompts. The status line shows the owner the headroom; it is terminal chrome
and never reaches the model.

## What it does

| Part | Trigger | Effect |
|---|---|---|
| `session_cap.py trip` | `Stop` hook | Silent below the cap. At or over it, blocks the stop once with a directive to run the `session-cap:handover` skill (named in full, so a personal `handover` command cannot shadow it), writes a marker to `~/.claude/session-cap/tripped/<session id>` so it never fires twice, and logs an `at-cap` event |
| `/session-cap:handover` | the trip, or the user | Runs `session_cap.py gather` (git state, unpushed commits, MR, ledger, earlier handovers), writes `~/.claude/handover/<ticket>-<id8>-<nn>.md`, logs a `handover` event and prints the successor command |
| `session_cap.py retired` | `UserPromptSubmit` hook | In a session that has handed over, adds a note that it is retired and names the successor. It informs and leaves the decision to the user |
| `session_cap.py sync` | `SessionStart` hook | Copies `statusline.py` and `session_cap.py` to `~/.claude/session-cap/`, so the status line setting, and plugins built on this one such as [`session-fleet`](https://github.com/OzturkArda/claude-session-fleet), have a path that survives plugin updates |
| `/session-cap:context` | the user | Table of context used by every session active in the last 12 hours, against the cap, with `HANDOVER DUE` and `handed over` states |
| `statusline.py` | status line | Session name, id, ticket, directory and model; a bar against the cap, the share of the real window, `HANDOVER DUE` and cost |

Context used is the newest main-thread assistant message's `input + cache_creation + cache_read +
output` tokens. Subagent messages carry their own context and are skipped. The hook and the status
line count the same way, so the bar and the trip agree.

State lives under `~/.claude/session-cap/` (`ledger.jsonl`, `tripped/`) and handover documents
under `~/.claude/handover/`, outside the plugin directory so updates keep them.

## Install

Needs `python3` on the `PATH` (3.8 or later, standard library only) and `git`. `glab` or `gh`, when
present, adds the branch's MR to the handover facts.

```
/plugin marketplace add OzturkArda/claude-session-cap
/plugin install session-cap@session-cap
```

Start a new session so the hooks load. For the status line, merge
[`statusline.settings.json`](statusline.settings.json) into `~/.claude/settings.json`; the first
session after install puts the script in place.

On Windows, run Claude Code with Git Bash installed, which is what runs hook commands, and check
that `python3 --version` answers there. If only `python` does, change `python3` to `python` in the
status line setting.

## Settings

| Variable | Effect |
|---|---|
| `CLAUDE_CTX_CAP=<tokens>` | Changes the cap, e.g. `CLAUDE_CTX_CAP=300000` |
| `CLAUDE_CTX_CAP=0` | Turns the trip and the retired note off; the status line shows usage without a cap |

Set it in the shell before starting `claude`, or under `env` in `settings.json`. Raising the cap
leaves sessions that have already tripped as they are; delete the session's file under
`~/.claude/session-cap/tripped/` to re-arm it.

To turn the whole thing off: `/plugin disable session-cap`.

## Ledger

`~/.claude/session-cap/ledger.jsonl` holds one JSON object per line:

```json
{"ts": "2026-10-29T16:40:12+11:00", "event": "handover", "session": "5f0c…", "ticket": "PROJ-4879", "file": "/home/you/.claude/handover/PROJ-4879-5f0c1a2b-01.md", "successor": "PROJ-4879 dev #2"}
```

Events: `at-cap` (the trip), `handover` (document written), `note` (free text from
`session_cap.py log note`). The gather step lists the ticket's recent events in each new handover,
so a chain of sessions on one ticket stays traceable.

## Repository layout

```
.claude-plugin/plugin.json        plugin manifest (the context command lives here)
.claude-plugin/marketplace.json   single-plugin marketplace, so the repo installs directly
hooks/hooks.json                  SessionStart, UserPromptSubmit and Stop hooks
scripts/session_cap.py            trip, gather, save, log, retired, sync, ctx
scripts/statusline.py             status line
skills/handover/                  the handover skill and its evals
statusline.settings.json          status line snippet for ~/.claude/settings.json
```

## License

[MIT](LICENSE)
