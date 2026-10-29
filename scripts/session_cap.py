"""Session context cap: measure, trip once, hand over, and keep a ledger.

One module behind every hook and command of the plugin, so the hooks, the
`handover` skill and the context report all count tokens the same way.

    session_cap.py trip       Stop hook: blocks one stop when the session passes the cap
    session_cap.py retired    UserPromptSubmit hook: tells a handed-over session it is retired
    session_cap.py sync       SessionStart hook: keeps stable copies of the scripts current
    session_cap.py gather     facts for a handover document, as markdown
    session_cap.py save       write a handover document from stdin to its target path
    session_cap.py log        append one event to the ledger
    session_cap.py ctx        context used by recently active sessions

Hooks exit 0 on anything unexpected: a hook that errors or hangs disrupts every
session, which costs more than one missed trip.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
STATE = os.path.join(CLAUDE, "session-cap")
LEDGER = os.path.join(STATE, "ledger.jsonl")
TRIPPED = os.path.join(STATE, "tripped")
HANDOVERS = os.path.join(CLAUDE, "handover")
PROJECTS = os.path.join(CLAUDE, "projects")
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CAP = 200000
TAIL_BYTES = 2 * 1024 * 1024

BRANCH_TICKET_RE = re.compile(r"(?:issue|feature|bugfix|hotfix)/([A-Za-z][A-Za-z0-9]+-\d+)", re.I)
TICKET_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")


def get_cap():
    """CLAUDE_CTX_CAP overrides the default; 0 disables every hook."""
    raw = os.environ.get("CLAUDE_CTX_CAP")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_CAP


def read_hook_input():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def emit(obj):
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def _tail_lines(path, nbytes):
    # Seek to a window at the end and read forward: transcript lines can be
    # megabytes wide, so walking backwards line by line is far too slow.
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - nbytes)
        fh.seek(start)
        data = fh.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is cut mid-way
    return lines


def context_used(transcript_path):
    """Tokens in the session's context: the prompt the model last saw plus its output.

    Read from the newest main-thread assistant message. Subagent messages
    (isSidechain) carry their own separate context and are skipped.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    size = os.path.getsize(transcript_path)
    for window in (TAIL_BYTES, size):
        try:
            lines = _tail_lines(transcript_path, window)
        except OSError:
            return None
        for raw in reversed(lines):
            if b'"assistant"' not in raw or b'"isSidechain":true' in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if obj.get("type") != "assistant" or obj.get("isSidechain"):
                continue
            usage = (obj.get("message") or {}).get("usage")
            if not usage:
                continue
            used = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
            return {"used": used, "at": obj.get("timestamp")}
        if window >= size:
            break
    return None


def find_transcript(session_id):
    if not session_id or not os.path.isdir(PROJECTS):
        return None
    best = None
    for root, _dirs, files in os.walk(PROJECTS):
        name = session_id + ".jsonl"
        if name in files:
            p = os.path.join(root, name)
            if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                best = p
    return best


def git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="replace").rstrip()


def ticket_for(cwd, name=None):
    if cwd and os.path.isdir(cwd):
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if branch:
            m = BRANCH_TICKET_RE.search(branch)
            if m:
                return m.group(1).upper()
            m = TICKET_RE.search(branch)
            if m:
                return m.group(1)
    m = TICKET_RE.search(name or "")
    return m.group(1) if m else None


def add_event(event, **data):
    """Append one JSON line. Always JSON-encoded, so Windows paths survive."""
    os.makedirs(STATE, exist_ok=True)
    row = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "event": event}
    row.update({k: v for k, v in sorted(data.items()) if v not in (None, "")})
    line = json.dumps(row, ensure_ascii=False) + "\n"
    # One write on an O_APPEND descriptor keeps concurrent sessions' lines whole.
    fd = os.open(LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return row


def events(ticket=None, session=None, event=None):
    if not os.path.isfile(LEDGER):
        return []
    rows = []
    with open(LEDGER, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if ticket and row.get("ticket") != ticket:
                continue
            if session and row.get("session") != session:
                continue
            if event and row.get("event") != event:
                continue
            rows.append(row)
    return rows


def short(sid):
    return (sid or "")[:8]


# --- hooks -------------------------------------------------------------------

TRIP_REASON = """CONTEXT CAP REACHED - this session is at {used:,} tokens (cap {cap:,}).

Hand this session over now. Take on no new work and start no new investigation;
finish recording anything you are mid-way through in full.

Run the session-cap:handover skill. It gathers state from disk, writes
the handover document and records it in the ledger. When it finishes, report the handover
document path and the successor command, then stop."""


def cmd_trip(_args):
    """Stop hook. Silent below the cap, then blocks one stop with a handover directive.

    There is deliberately no advance warning: a session told it is close to the
    cap rations itself for the rest of its life and hands over in a worse state
    than it was really in. One trip, at a turn boundary.
    """
    data = read_hook_input()
    cap = get_cap()
    if not data or cap <= 0 or data.get("stop_hook_active"):
        return
    sid = data.get("session_id")
    if not sid:
        return
    marker = os.path.join(TRIPPED, sid)
    if os.path.exists(marker):
        return  # trips once per session
    usage = context_used(data.get("transcript_path"))
    if not usage or usage["used"] < cap:
        return
    os.makedirs(TRIPPED, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(str(usage["used"]))
    cwd = data.get("cwd")
    add_event("at-cap", session=sid, ticket=ticket_for(cwd), cwd=cwd, used=usage["used"], cap=cap)
    emit({"decision": "block", "reason": TRIP_REASON.format(used=usage["used"], cap=cap)})


def cmd_retired(_args):
    """UserPromptSubmit hook. A session that has handed over says so before answering.

    It informs rather than blocks: the owner may still have a reason to work in
    the retired session, but two sessions on one lineage can edit the same files.
    """
    data = read_hook_input()
    if not data or get_cap() <= 0 or not data.get("session_id"):
        return
    handed = events(session=data["session_id"], event="handover")
    if not handed:
        return
    last = handed[-1]
    successor = last.get("successor") or "none named"
    ctx = (
        "This session handed over at {ts} and is retired.\n"
        "  handover document: {file}\n"
        "  successor:         {succ}\n"
        "New work belongs in the successor, which holds the same state at a fraction of the\n"
        "context. Say so before answering; if the user wants it done here anyway, do it and\n"
        "mention that two sessions on one lineage may edit the same files."
    ).format(ts=last.get("ts"), file=last.get("file"), succ=successor)
    emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})


def cmd_sync(_args):
    """SessionStart hook. Copies the scripts to stable paths under ~/.claude/session-cap.

    The plugin's own directory changes with every update. The status line
    setting points at the copy of statusline.py, and plugins built on this one
    import the copy of session_cap.py, so both stay identical to the installed
    version.
    """
    read_hook_input()
    for name in ("statusline.py", "session_cap.py"):
        src = os.path.join(HERE, name)
        dst = os.path.join(STATE, name)
        try:
            with open(src, "rb") as fh:
                new = fh.read()
            if os.path.isfile(dst):
                with open(dst, "rb") as fh:
                    if fh.read() == new:
                        continue
            os.makedirs(STATE, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError:
            continue


# --- commands ----------------------------------------------------------------

def _section(title, body):
    out = ["### " + title, ""]
    out += ["```", body, "```"] if body else ["_(none)_"]
    out.append("")
    return out


def _mr_lines(cwd, branch):
    # Best-effort: an expired CLI token must not take the handover down with it.
    for tool, argv in (("glab", ["glab", "mr", "list", "--source-branch", branch]),
                       ("gh", ["gh", "pr", "list", "--head", branch])):
        if not shutil.which(tool):
            continue
        try:
            out = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return "%s lookup failed" % tool
        if out.returncode != 0:
            return "%s exited %d (check its login)" % (tool, out.returncode)
        return out.stdout.decode("utf-8", errors="replace").strip() or "none"
    return None


def cmd_gather(args):
    """Print the factual half of a handover document, read from disk.

    Everything here comes from git, the transcript and the ledger rather than
    from the session's recollection, so a session at its cap produces the same
    document quality as a fresh one.
    """
    cwd = args.cwd or os.getcwd()
    ticket = args.ticket or ticket_for(cwd, args.name)
    cap = get_cap()
    usage = context_used(args.transcript or find_transcript(args.session_id))

    os.makedirs(HANDOVERS, exist_ok=True)
    slug = ticket or "session"
    sid8 = short(args.session_id) or "nosessid"
    # The session id is part of the name because several sessions can work one
    # ticket at once; numbering by ticket alone would read as one chain.
    mine = sorted(f for f in os.listdir(HANDOVERS) if f.startswith("%s-%s-" % (slug, sid8)) and f.endswith(".md"))
    target = os.path.join(HANDOVERS, "%s-%s-%02d.md" % (slug, sid8, len(mine) + 1))
    others = sorted(f for f in os.listdir(HANDOVERS)
                    if f.startswith(slug + "-") and f.endswith(".md") and f not in mine)

    out = ["## Gathered facts", ""]
    out.append("- ticket: %s" % (ticket or "(none detected)"))
    out.append("- session: %s%s" % (args.session_id or "(unknown)", "  (%s)" % args.name if args.name else ""))
    out.append("- cwd: %s" % cwd)
    out.append("- context: %s" % ("{:,} / {:,}".format(usage["used"], cap) if usage else "unknown"))
    out.append("- handover target: %s" % target)
    out.append("- earlier handovers by this session: %s" % (", ".join(mine) or "none"))
    if others:
        out.append("- other handover documents for %s (earlier sessions or concurrent ones):" % slug)
        out += ["    " + f for f in others]
    out.append("")

    if git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        out += ["### Git", "", "_cwd is not a git work tree_", ""]
    else:
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        upstream = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        bits = ["branch:   %s" % branch, "upstream: %s" % (upstream or "(none - never pushed)")]
        if upstream:
            lr = (git(cwd, "rev-list", "--left-right", "--count", "@{u}...HEAD") or "").split()
            if len(lr) == 2:
                bits += ["behind:   %s" % lr[0], "ahead:    %s" % lr[1]]
        bits.append("worktree: %s" % git(cwd, "rev-parse", "--show-toplevel"))
        out += _section("Branch", "\n".join(bits))
        out += _section("Working tree", git(cwd, "status", "--porcelain=v1", "--branch"))
        out += _section("Uncommitted diff stat", git(cwd, "diff", "--stat", "HEAD"))
        out += _section("Recent commits", git(cwd, "log", "--oneline", "--decorate", "-15"))
        if upstream:
            out += _section("Unpushed commits", git(cwd, "log", "--oneline", "@{u}..HEAD"))
        out += _section("Stashes", git(cwd, "stash", "list"))
        out += _section("Worktrees", git(cwd, "worktree", "list"))
        if branch and git(cwd, "remote"):
            mr = _mr_lines(cwd, branch)
            if mr is not None:
                out += _section("Merge requests for this branch", mr)

    rows = events(ticket=ticket)[-25:] if ticket else []
    lines = []
    for r in rows:
        extra = " | ".join(str(r[k]) for k in ("successor", "file", "note") if r.get(k))
        lines.append("%s  %-8s  %s  %s" % (r.get("ts", "")[:16], r.get("event"), short(r.get("session")), extra))
    out += _section("Ledger for this ticket", "\n".join(lines))

    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))


def cmd_save(args):
    """Write the document from stdin.

    Claude Code asks before its own file tools write under ~/.claude, which
    stalls a background session at exactly the moment it is retiring; a script
    the session runs writes there without that prompt.
    """
    target = os.path.abspath(os.path.expanduser(args.file))
    if os.path.dirname(target) != os.path.abspath(HANDOVERS):
        sys.exit("save writes only into %s" % HANDOVERS)
    body = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not body.strip():
        sys.exit("empty document on stdin")
    os.makedirs(HANDOVERS, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body if body.endswith("\n") else body + "\n")
    print(target)


def cmd_log(args):
    if args.event == "handover" and not (args.file and os.path.isfile(os.path.expanduser(args.file))):
        # The ledger is what successors and the retired note trust, so it only
        # ever points at a document that exists.
        sys.exit("handover document not found: %s - save it first" % args.file)
    row = add_event(args.event, ticket=args.ticket, session=args.session, name=args.name,
                    file=args.file, successor=args.successor, successorSession=args.successor_session,
                    cwd=args.cwd, note=args.note)
    print(json.dumps(row, ensure_ascii=False))


def cmd_ctx(args):
    """Context used by every session whose transcript changed in the last few hours."""
    cap = args.cap if args.cap is not None else get_cap()
    cutoff = datetime.datetime.now().timestamp() - args.hours * 3600
    rows = []
    if os.path.isdir(PROJECTS):
        for root, _dirs, files in os.walk(PROJECTS):
            if os.path.basename(root) == "subagents":
                continue
            for f in files:
                if not f.endswith(".jsonl"):
                    continue
                p = os.path.join(root, f)
                if os.path.getmtime(p) < cutoff:
                    continue
                usage = context_used(p)
                if not usage:
                    continue
                sid = f[:-len(".jsonl")]
                rows.append({
                    "session": sid,
                    "used": usage["used"],
                    "ratio": round(usage["used"] / float(cap), 2) if cap > 0 else None,
                    "over": cap > 0 and usage["used"] >= cap,
                    "tripped": os.path.exists(os.path.join(TRIPPED, sid)),
                    "handedOver": bool(events(session=sid, event="handover")),
                    "project": os.path.basename(root),
                    "lastAt": usage["at"],
                })
    rows.sort(key=lambda r: r["used"], reverse=True)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no session active in the last %d h" % args.hours)
        return
    print("%7s  %6s  %-15s  %-8s  %s" % ("ctx", "cap", "state", "id", "project"))
    for r in rows:
        # Past the cap a fill percentage stops reading as a fill, so say how far past.
        pct = "%.1fx" % r["ratio"] if r["over"] else "%d%%" % round(100 * r["ratio"]) if r["ratio"] is not None else "?"
        state = "handed over" if r["handedOver"] else "HANDOVER DUE" if r["tripped"] else "over" if r["over"] else ""
        print("%6dk  %6s  %-15s  %-8s  %s" % (round(r["used"] / 1000.0), pct, state, short(r["session"]), r["project"]))
    print("cap {:,} - {} session(s), {} over".format(cap, len(rows), sum(1 for r in rows if r["over"])))


def main(argv=None):
    p = argparse.ArgumentParser(prog="session_cap.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("trip").set_defaults(fn=cmd_trip)
    sub.add_parser("retired").set_defaults(fn=cmd_retired)
    sub.add_parser("sync").set_defaults(fn=cmd_sync)

    g = sub.add_parser("gather")
    g.add_argument("--session-id")
    g.add_argument("--transcript", help="transcript path, when the session id cannot be resolved")
    g.add_argument("--cwd")
    g.add_argument("--name")
    g.add_argument("--ticket")
    g.set_defaults(fn=cmd_gather)

    sv = sub.add_parser("save")
    sv.add_argument("--file", required=True, help="the handover target printed by gather")
    sv.set_defaults(fn=cmd_save)

    lg = sub.add_parser("log")
    lg.add_argument("event", choices=["at-cap", "handover", "note"])
    for opt in ("--ticket", "--session", "--name", "--file", "--successor", "--cwd", "--note"):
        lg.add_argument(opt)
    lg.add_argument("--successor-session", help="the successor's session id, when it was started for you")
    lg.set_defaults(fn=cmd_log)

    c = sub.add_parser("ctx")
    c.add_argument("--hours", type=int, default=12)
    c.add_argument("--cap", type=int)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_ctx)

    args = p.parse_args(argv)
    if args.cmd in ("trip", "retired", "sync"):
        try:
            args.fn(args)
        except Exception:
            pass
        return 0
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
