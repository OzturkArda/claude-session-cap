"""Status line: which session this terminal is, and how much of the context cap it has spent.

Row one: session name, short id, ticket, directory, model.
Row two: a bar against the cap, the share of the real context window, HANDOVER DUE
once the Stop hook has tripped, and session cost.

Python rather than a shell pipeline because the status line re-runs on every
repaint; run it as `python3 -S -E` to keep start-up short. It reads .git/HEAD
directly instead of spawning git for the same reason.

The status line is terminal chrome and never reaches the model, so showing the
owner the headroom does not count as the advance warning the cap design rules out.
"""

import json
import os
import re
import sys

HOME = os.path.expanduser("~")
TRIPPED = os.path.join(HOME, ".claude", "session-cap", "tripped")

# Some consoles default stdout to a legacy code page that cannot encode the bar
# glyphs. Ask for UTF-8; if that is refused, fall back to an ASCII bar rather
# than emitting nothing.
UNICODE_OK = True
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    UNICODE_OK = False

FULL, EMPTY = ("█", "░") if UNICODE_OK else ("#", "-")
DOT = " · " if UNICODE_OK else " | "

RESET, DIM, RED, YELLOW, GREEN, CYAN, BOLD = (
    "\033[0m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[1m",
)
SEP = DIM + DOT + RESET

TICKET_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")
BRANCH_TICKET_RE = re.compile(r"(?:issue|feature|bugfix|hotfix)/([A-Za-z][A-Za-z0-9]+-\d+)", re.I)


def get_cap():
    """Same rule as session_cap.py: CLAUDE_CTX_CAP overrides, 200k by default."""
    raw = os.environ.get("CLAUDE_CTX_CAP")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 200000


def ticket_for(cwd, name):
    """Ticket from the branch, else from the session name."""
    d = cwd
    while d:
        head = os.path.join(d, ".git", "HEAD")
        if os.path.isfile(head):
            try:
                with open(head, encoding="utf-8", errors="replace") as fh:
                    ref = fh.read().strip()
            except OSError:
                break
            m = BRANCH_TICKET_RE.search(ref) or TICKET_RE.search(ref)
            if m:
                return m.group(1).upper()
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    m = TICKET_RE.search(name or "")
    return m.group(1) if m else None


def short_dir(cwd):
    if not cwd:
        return ""
    p = cwd.replace("\\", "/").rstrip("/")
    h = HOME.replace("\\", "/").rstrip("/")
    if p.lower().startswith(h.lower()):
        rest = p[len(h):].strip("/")
        return "~/" + rest if rest else "~"
    return p


def human(n):
    if n >= 1000000:
        return ("%.2f" % (n / 1000000.0)).rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return "%dk" % round(n / 1000.0)
    return str(n)


def bar(frac, width=14):
    filled = max(0, min(width, int(round(frac * width))))
    return FULL * filled + EMPTY * (width - filled)


def identity_line(data):
    sid = data.get("session_id") or ""
    name = data.get("session_name") or ""
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or ""
    model = (data.get("model") or {}).get("display_name") or ""

    bits = []
    if name:
        bits.append(BOLD + name + RESET)
    if sid:
        bits.append(DIM + sid[:8] + RESET)
    t = ticket_for(cwd, name)
    if t:
        bits.append(CYAN + t + RESET)
    d = short_dir(cwd)
    if d:
        bits.append(DIM + d + RESET)
    if model:
        bits.append(DIM + model + RESET)
    return SEP.join(bits)


def context_line(data, cap):
    cw = data.get("context_window") or {}
    # total_input_tokens already includes cache reads and writes, so this sum is
    # the same number the Stop hook compares against the cap.
    used = int(cw.get("total_input_tokens") or 0) + int(cw.get("total_output_tokens") or 0)
    window = cw.get("context_window_size")

    if not used:
        return DIM + "context: waiting for first response" + RESET
    if cap <= 0:
        return DIM + "context: %s (cap disabled)" % human(used) + RESET

    frac = used / float(cap)
    colour = RED if frac >= 1.0 else YELLOW if frac >= 0.7 else GREEN

    seg = [colour + bar(frac) + RESET]
    cap_txt = "%s/%s cap" % (human(used), human(cap))
    if used >= cap:
        # Past the cap a fill percentage stops reading as a fill, so say how far past.
        seg.append("%s%s %.1fx OVER%s" % (RED + BOLD, cap_txt, frac, RESET))
    else:
        seg.append("%s%s %d%%%s" % (colour, cap_txt, round(frac * 100), RESET))

    if window:
        seg.append(DIM + "%d%% of %s window" % (round(100.0 * used / window), human(window)) + RESET)

    sid = data.get("session_id") or ""
    if sid and os.path.exists(os.path.join(TRIPPED, sid)):
        seg.append(RED + BOLD + "HANDOVER DUE" + RESET)

    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost:
        seg.append(DIM + "$%.2f" % cost + RESET)

    return SEP.join(seg)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    try:
        cap = get_cap()
        lines = [identity_line(data), context_line(data, cap)]
        # The write sits inside the guard too: an encoding the terminal refuses
        # would otherwise blank the line after the first row had printed.
        sys.stdout.write("\n".join(l for l in lines if l) + "\n")
    except Exception:
        return


if __name__ == "__main__":
    main()
