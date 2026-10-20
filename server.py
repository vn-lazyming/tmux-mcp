#!/usr/bin/env python3
"""tmux_mcp: drive a live tmux pane the human is watching and typing in.

The agent types into the pane exactly like a person would, so interactive
programs (python, gdb, ssh, nc, vim) keep running between calls.
Watch it with:  tmux attach -t agent
"""
import asyncio
import collections
import os
import re
import subprocess
import time
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

SESSION = os.environ.get("TMUX_MCP_SESSION", "agent")
SELF = os.environ.get("TMUX_PANE", "")  # the pane the agent itself runs in, if inside tmux
MAX_OUT = 20000
ANCHOR = 5
KEY_RE = re.compile(r"(?:[CMS]-)*(?:[^\s;]|F(?:[1-9]|1[0-2])|Up|Down|Left|Right|Enter|Escape|Tab|BTab|"
                    r"BSpace|Space|Home|End|IC|DC|PageUp|PageDown|PgUp|PgDn|PPage|NPage)")
TARGET_DOC = ('tmux target such as "0:1.0" (session:window.pane, from tmux_list_panes); '
              f'empty = the agent session "{SESSION}"')
FIELDS = ("history_size", "history_limit", "cursor_y", "alternate_on", "pane_in_mode",
          "pane_current_command", "pane_pid")

mcp = FastMCP("tmux_mcp", instructions=(
    "You are typing into a LIVE tmux pane a human is also watching and typing in "
    f"(they see it via `tmux attach -t {SESSION}`). State persists between calls: "
    "a REPL or ssh you start stays open. tmux_send types, tmux_read looks, "
    "tmux_list_panes finds the human's other panes, tmux_new opens a named session, window or pane, "
    "tmux_kill closes one tmux_new opened."))

last_send = {}  # pane id -> snapshot taken just before the last tmux_send typed there
locks = collections.defaultdict(asyncio.Lock)  # parallel sends to one pane would interleave keys


def tmux(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if r.returncode:
        err = r.stderr.strip() or f"tmux {args[0]} failed"
        if "can't find" in err or "no server running" in err:
            err += ". Call tmux_list_panes to see valid targets, or leave the target empty for the agent session."
        raise RuntimeError(err)
    return r.stdout


def has_session(name):
    return not subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True).returncode


def opened():
    """Panes tmux_new opened: pane id -> (kind, name), read from the tag it set on each."""
    rows = (r.split("\t") for r in tmux("list-panes", "-a", "-F", "#{pane_id}\t#{@tmux_mcp}").splitlines())
    return {p: tag.split(" ", 1) for p, tag in rows if tag}


def info(p):
    d = dict(zip(FIELDS, tmux("display", "-p", "-t", p, "\t".join(f"#{{{f}}}" for f in FIELDS))
                 .rstrip("\n").split("\t")))
    with open(f"/proc/{d['pane_pid']}/comm") as f:
        d["shell"] = f.read().strip()
    return d


def footer(p):
    d = info(p)
    cmd = d["pane_current_command"]
    return "[at shell prompt]" if cmd == d["shell"] else f"[foreground: {cmd}]"


def capture(p, start=None, end=None):
    args = ["capture-pane", "-p", "-J", "-t", p]
    if start is not None:
        args += ["-S", str(start)]
    if end is not None:
        args += ["-E", str(end)]
    return "\n".join(line.rstrip() for line in tmux(*args).split("\n")).rstrip("\n")


def tail(s):
    return s if len(s) <= MAX_OUT else "[...truncated...]\n" + s[-MAX_OUT:]


async def settle(t):
    """Wait for a fresh shell to draw its prompt: keys typed before that get echoed twice."""
    prev, deadline = None, time.monotonic() + 5
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        screen = capture(t)
        if screen.strip() and screen == prev:
            break
        prev = screen


async def resolve(t, create):
    """Pin the target to a pane id once, so a human switching windows mid-call can't redirect us."""
    if not t:
        t = f"={SESSION}:"
        if not has_session(SESSION):
            if not create:
                raise RuntimeError(f'No "{SESSION}" session yet: tmux_send creates it.')
            await tmux_new(SESSION)
    capture(t, 0, 0)  # display -t silently falls back to the current pane on a bad target; this errors
    return tmux("display", "-p", "-t", t, "#{pane_id}").strip()


def snapshot(p):
    d = info(p)
    hist, cy = int(d["history_size"]), int(d["cursor_y"])
    return {
        "hist": hist, "cy": cy, "cmd": d["pane_current_command"],
        "prompt": capture(p, cy, cy),
        # history rows never change, so finding them again tells how many rows tmux trimmed
        "anchor": capture(p, -min(hist, ANCHOR), -1) if hist else None,
    }


def since(p, snap):
    """The pane's text from the row the cursor was on at `snap` down to the bottom."""
    d = info(p)
    if d["alternate_on"] == "1":
        return capture(p)
    hist, hist2 = snap["hist"], int(d["history_size"])
    k, step = min(hist, ANCHOR), max(int(d["history_limit"]) // 10, 1)
    # ponytail: assumes tmux trims full history in history_limit/10 chunks (grid_collect_history);
    # if the anchor is never found (clear-history, resize reflow) we fall back to all of history.
    drop = 0
    while snap["anchor"] is not None:
        top = hist - k - drop - hist2
        if top + k <= -hist2 - 1 or capture(p, top, top + k - 1) == snap["anchor"]:
            break
        drop += step
    return capture(p, max(hist + snap["cy"] - drop - hist2, -hist2))


def finished(p, snap):
    """Back at the shell, or a REPL showing the same prompt it showed before we typed."""
    d = info(p)
    cmd = d["pane_current_command"]
    if cmd == d["shell"]:
        return True
    if snap["cmd"] == d["shell"] or not snap["prompt"]:
        return False
    return norm(capture(p, int(d["cursor_y"]), int(d["cursor_y"]))) == norm(snap["prompt"])


def norm(line):
    return re.sub(r"\d+", "0", line)  # ipython's "In [7]:" or a clock in a prompt still counts as the same prompt


def type_text(p, text):
    body = text.rstrip(";")
    if body:  # "--": text like "-h" would otherwise be parsed as a send-keys flag
        tmux("send-keys", "-t", p, "-l", "--", body)
    if len(body) < len(text):  # tmux eats an argument's trailing ';' as a command separator
        tmux("send-keys", "-t", p, "-H", *["3b"] * (len(text) - len(body)))


def regex(p):
    try:
        return re.compile(p)
    except re.error as e:
        raise ValueError(f"wait_for is not a valid Python regex ({e}).")


@mcp.tool(title="Type into terminal", annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def tmux_send(
    text: Annotated[str, Field(description="Typed literally, e.g. 'ls -la' or 'SELECT 1;'. Empty to send only keys.")] = "",
    keys: Annotated[list[str] | None, Field(description='tmux key names pressed after text: C-c, C-d, C-z, C-], Up, Down, Left, Right, Tab, BTab, Escape, BSpace, Enter, Home, End, PageUp, PageDown, F1-F12, single chars like "q", with C-/M-/S- modifiers')] = None,
    enter: Annotated[bool, Field(description="Press Enter at the end. False for keys like C-c.")] = True,
    idle: Annotated[float, Field(description="Return once the screen is quiet this long (s) even if not finished. Raise for silent slow commands.", gt=0, le=600)] = 3.0,
    timeout: Annotated[float, Field(description="Hard cap on waiting (s).", gt=0, le=600)] = 60.0,
    target_pane: Annotated[str, Field(description=TARGET_DOC)] = "",
) -> str:
    """Type into the live pane like a human, then return the output that appeared.

    Returns once it is finished (shell prompt back, or a REPL/gdb/ssh showing the
    same prompt line as before), or the screen has been quiet `idle` seconds, or
    `timeout` passes. The last line says the state: `[at shell prompt]` = done;
    `[foreground: X]` = X still running or a REPL waiting for input. If it is
    still running, call tmux_read(wait_done=True) to wait for it to finish.
    """
    bad = [k for k in keys or [] if not KEY_RE.fullmatch(k)]
    if bad:
        raise ValueError(f"Unknown key names {bad}. Use tmux names like C-c, Escape, Up, Tab, F5; type text with `text`.")
    p = await resolve(target_pane, create=True)
    if p == SELF:
        raise RuntimeError("That pane is the one this agent runs in: typing there would type into the agent "
                           "itself. Pick another pane from tmux_list_panes.")
    async with locks[p]:
        note = ""
        if info(p)["pane_in_mode"] == "1":  # copy-mode would swallow the keys
            tmux("copy-mode", "-q", "-t", p)
            note = "[left the copy/scroll mode the pane was in]\n"
        snap = last_send[p] = snapshot(p)
        if text:
            type_text(p, text)
        if keys:
            tmux("send-keys", "-t", p, "--", *keys)
        if enter:
            tmux("send-keys", "-t", p, "Enter")

        start = last_change = time.monotonic()
        prev = None
        while True:
            await asyncio.sleep(0.2)
            screen = capture(p)
            now = time.monotonic()
            if screen != prev:
                prev, last_change = screen, now
            quiet = now - last_change
            if (quiet >= 0.5 and finished(p, snap)) or quiet >= idle or now - start >= timeout:
                break
        return f"{note}{tail(since(p, snap))}\n{footer(p)}"


@mcp.tool(title="Read terminal", annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def tmux_read(
    lines: Annotated[int, Field(description="How many trailing lines to return, scrollback included.", ge=1, le=5000)] = 60,
    wait_for: Annotated[str, Field(description="Python regex; first poll until it appears in output since the last tmux_send in this pane, e.g. '(?m)^BUILD OK'. Anchor it with ^ so the typed command line itself doesn't match.")] = "",
    wait_done: Annotated[bool, Field(description="First poll until the last tmux_send's command has finished (shell or REPL prompt back).")] = False,
    timeout: Annotated[float, Field(description="Max seconds to wait.", gt=0, le=600)] = 60.0,
    target_pane: Annotated[str, Field(description=TARGET_DOC)] = "",
) -> str:
    """Return the pane's last `lines` lines (what the human sees, plus scrollback),
    optionally waiting first. Last line: `[at shell prompt]` or `[foreground: X]`,
    plus `[timeout: ...]` if the wait gave up."""
    p = await resolve(target_pane, create=False)
    pat = regex(wait_for) if wait_for else None
    snap = last_send.get(p)
    deadline = time.monotonic() + timeout
    miss = ""
    # a pane we never typed in: "done" can only mean back at the shell
    while (pat and not pat.search(since(p, snap) if snap else capture(p))) or \
            (wait_done and not finished(p, snap or {"cmd": "", "prompt": ""})):
        if time.monotonic() >= deadline:
            miss = f"\n[timeout: still waiting for {'%r' % wait_for if pat else 'it to finish'}]"
            break
        await asyncio.sleep(0.3)
    out = "\n".join(capture(p, -lines).split("\n")[-lines:])
    return f"{tail(out)}\n{footer(p)}{miss}"


@mcp.tool(title="List terminal panes", annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def tmux_list_panes() -> str:
    """List every tmux session, window and pane as `target  pane_id  command  cwd  [flags]`.
    Pass either id to tmux_send/tmux_read to work in the human's own pane.
    `[opened by agent: KIND NAME]` marks what tmux_new opened; tmux_kill(NAME) closes it."""
    try:
        return tmux("list-panes", "-a", "-F",
            "#{session_name}:#{window_index}.#{pane_index}  #{pane_id}  #{pane_current_command}  #{pane_current_path}"
            "#{?pane_active,  [active],}#{?session_attached,  [attached],}"
            "#{?#{@tmux_mcp},  [opened by agent: #{@tmux_mcp}],}"
            f"#{{?#{{==:#{{pane_id}},{SELF or '-'}}},  [this agent: do not type here],}}").rstrip("\n")
    except RuntimeError:
        return f'No tmux server running. tmux_send with empty target_pane creates the "{SESSION}" session.'


@mcp.tool(title="Open terminal", annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def tmux_new(
    name: Annotated[str, Field(description='Name with no spaces, ":" or ".", e.g. "ming". A session is then reachable with `tmux attach -t ming`; tmux_kill(name) closes it.', pattern=r"^[^\s:.]+$")],
    kind: Annotated[Literal["session", "window", "pane"], Field(description='"session" = its own `tmux attach -t NAME`; "window" = a new window in `session`; "pane" = a split of `session`\'s current window')] = "session",
    session: Annotated[str, Field(description=f'For window/pane: the session to add it to; empty = the agent session "{SESSION}"')] = "",
    cwd: Annotated[str, Field(description="Starting directory, relative to home; empty = home.")] = "",
) -> str:
    """Open a new tmux session, window or pane without switching the human's view.

    Returns its pane id (e.g. "%7") for tmux_send/tmux_read: unlike "ming:0.1" it
    stays on this pane when the human renames the session or splits the window."""
    cwd = os.path.normpath(os.path.join(os.path.expanduser("~"), os.path.expanduser(cwd)))
    if not os.path.isdir(cwd):
        raise ValueError(f"cwd {cwd!r} is not a directory.")
    if kind != "session" and not session:
        await resolve("", create=True)  # like tmux_send: the agent session appears on first use
    fmt = ["-P", "-F", "#{pane_id} #{session_name}:#{window_index}.#{pane_index}"]
    if kind == "session":
        if has_session(name):
            raise RuntimeError(f'Session "{name}" already exists: use target_pane "{name}:".')
        out = tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50", *fmt)
    elif kind == "window":
        out = tmux("new-window", "-d", "-t", f"={session or SESSION}:", "-n", name, "-c", cwd, *fmt)
    else:
        out = tmux("split-window", "-d", "-t", f"={session or SESSION}:", "-c", cwd, *fmt)
    t, where = out.strip().split(" ", 1)
    # ponytail: the tag lives on the first pane; if the human closes that pane, tmux_kill can't find the rest
    tmux("set-option", "-p", "-t", t, "@tmux_mcp", f"{kind} {name}")  # pane titles get overwritten by the shell
    await settle(t)
    hint = f" The human can watch it with: tmux attach -t {name}" if kind == "session" else ""
    return f'Opened {kind} {name} at {where} in {cwd}. Use target_pane="{t}".{hint}'


@mcp.tool(title="Close terminal", annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def tmux_kill(
    name: Annotated[str, Field(description='The name given to tmux_new, or the pane id it returned, e.g. "%7".')],
) -> str:
    """Close a session, window or pane that tmux_new opened, killing whatever runs in it.
    Refuses anything else, so the human's own panes are never closed."""
    mine = opened()
    hits = [(p, kind) for p, (kind, n) in mine.items() if name in (p, n)]
    if not hits:
        names = ", ".join(f"{n} ({k} {p})" for p, (k, n) in mine.items()) or "none"
        raise RuntimeError(f'Nothing tmux_new opened is named "{name}". Opened by the agent: {names}.')
    if len(hits) > 1:
        raise RuntimeError(f'{len(hits)} things are named "{name}": pass one pane id: {", ".join(p for p, _ in hits)}.')
    p, kind = hits[0]
    if kind == "pane":
        panes = [p]
    else:  # -s: every pane of the session, not just of one window
        panes = tmux("list-panes", *(["-s"] if kind == "session" else []), "-t", p, "-F", "#{pane_id}").split()
    if SELF in panes:
        raise RuntimeError(f"That {kind} holds the pane this agent runs in: closing it would kill the agent.")
    where = tmux("display", "-p", "-t", p, "#{session_name}:#{window_index}.#{pane_index}").strip()
    tmux(f"kill-{kind}", "-t", p)
    return f"Closed {kind} {name} (was at {where}) and everything running in it."


if __name__ == "__main__":
    mcp.run()
