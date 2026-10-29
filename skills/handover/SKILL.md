---
name: handover
description: Hands this session's work over to a fresh session - gathers state from disk, writes a status-only handover document, records it in the session-cap ledger and prints the command that starts the successor. Use when the context cap trips, or when the user says "hand over", "write a handover" or "retire this session".
---

# Handover

## Required output

A handover document at the path the gather step prints, with exactly these sections, then a short
report: the document path and the successor command.

```markdown
# Handover: <ticket or topic>

## Where things stand
## Code state
## Decisions taken
## Open threads
## How to verify
## References
```

The document is **assembled from disk, not from recollection**: the gather step reads git, the
transcript and the ledger, so a session at its context cap writes the same quality document as a
fresh one. Gather first, write second.

## Steps

1. **Gather.** Run:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_cap.py" gather --session-id ${CLAUDE_SESSION_ID} --cwd "<cwd>" [--ticket <TICKET>] [--name "<session name>"]
   ```

   `$ARGUMENTS` may carry a ticket key; otherwise the script reads it from the branch. Save the
   document to the `handover target` it prints, because that name keeps concurrent sessions on one
   ticket from overwriting each other.

2. **Read the recorded trail.** When a Jira or GitLab MCP server or CLI is available, read the
   ticket's comments and the MR threads: decisions recorded there belong in the document with a
   link. Skip this step quietly when neither is available.

3. **Write the document** to the target path through the script, with the document on stdin:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_cap.py" save --file "<handover target>" <<'HANDOVER'
   # Handover: ...
   HANDOVER
   ```

   The script writes it because Claude Code asks for approval before its file tools write under
   `~/.claude`, which would stall a background session at the moment it retires. Sections:

   - **Where things stand**: the factual state of the work right now.
   - **Code state**: branch, worktree, pushed and unpushed commits, uncommitted changes, MR and
     pipeline state, lifted from the gathered facts.
   - **Decisions taken**: each with its reason and a link to where it is recorded.
   - **Open threads**: what is in flight or unresolved, written as status: what was observed and
     what is not yet known.
   - **How to verify**: the exact commands that reproduce the current state (build, test, repro).
   - **References**: ticket, MR, pages and local paths.

   The document records status and leaves the next steps to the person who assigns the successor's
   work, so the successor starts from facts rather than from a plan made at the end of a full
   context. Only **Open threads** may hold knowledge that is not on disk; every other section traces
   to the gathered facts or the ticket.

4. **Start the successor, when the fleet is installed.** If `command -v session-fleet` finds the
   `session-fleet` command, start the successor before recording, so the ledger carries its
   session id:

   ```
   session-fleet launch --cwd "<cwd>" --name "<this session's name, or <TICKET> dev>" "Read <document path> and continue from there."
   ```

   It adds the next free ` #<n>` to the name and prints `name`, `id` and `session`. Skip this step
   when the command is absent or the user has said not to start one.

5. **Record it**, once `save` has printed the path. The ledger is what the retired note and later
   handovers trust, so `log handover` refuses a document that does not exist yet. The successor
   name is the one step 4 printed; without step 4, it is this session's name with any trailing
   ` #<n>` replaced by the next number (`PROJ-1234 dev` becomes `PROJ-1234 dev #2`), or
   `<TICKET> handover` when the session has no name. Then run:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_cap.py" log handover --session ${CLAUDE_SESSION_ID} --cwd "<cwd>" --file "<document path>" --successor "<successor name>" [--successor-session <session from step 4>] [--ticket <TICKET>] [--name "<session name>"]
   ```

   From then on the `UserPromptSubmit` hook tells this session it is retired whenever it receives a
   new prompt.

6. **Tell the manager, when there is one.** If `session-fleet manager` prints a name, send that
   session one line with `SendMessage`: retiring, ticket, document path, successor name. When it
   prints nothing, there is no manager; skip this step without looking for one.

7. **Report and stop.** Give the document path and how to reach the successor: `claude attach <id>`
   when step 4 started it, otherwise the command to start it from the same directory:

   ```
   claude -n "<successor name>" "Read <document path> and continue from there."
   ```

   When the user asks for that successor to be started for them, run the same command with `--bg`
   added, which returns once the session is up and prints its id for `claude attach <id>`.
   Then take no further work in this session.

Steps 2, 4 and 6 and the ticket detection may come up empty for a session outside any ticket or
fleet; say what was skipped in one clause of the report. The document (steps 1 and 3) and the
ledger entry (step 5) are the parts that must happen.
