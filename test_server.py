"""Self-check over real MCP stdio on a private tmux server:  python3 test_server.py [server.py]

Runs on its own socket, so the human's real tmux sessions are never touched.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

S = "tmux-mcp-test"
here = os.path.dirname(os.path.abspath(__file__))
server_py = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "server.py")
failed = []
sock = os.path.join(tempfile.mkdtemp(prefix="tmux-mcp-test-"), "sock")
env = {**os.environ, "TMUX": f"{sock},0,0"}  # every tmux client, ours and the server's, talks to this socket


def sh(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, env=env).stdout.strip()


def check(name, ok, out=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got: {out!r}"))
    if not ok:
        failed.append(name)


async def main():
    # a pane standing in for the one the agent runs in, and a pane with a tiny history-limit
    subprocess.run(["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "-s", S + "-self"])
    self_pane = sh("display", "-p", "-t", f"={S}-self:", "#{pane_id}")
    sh("set", "-g", "history-limit", "50")
    hl_pane = sh("new-window", "-P", "-F", "#{pane_id}", "-t", f"={S}-self:")
    server = StdioServerParameters(command=sys.executable, args=[server_py],
        env={**env, "TMUX_MCP_SESSION": S, "TMUX_PANE": self_pane})

    async with stdio_client(server) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def call(tool, **args):
            res = await s.call_tool(tool, args)
            return res.isError, res.content[0].text

        tools = {t.name: t for t in (await s.list_tools()).tools}
        check("five tools", set(tools) == {"tmux_send", "tmux_read", "tmux_list_panes", "tmux_new", "tmux_kill"}, tools)
        check("annotations", tools["tmux_read"].annotations.readOnlyHint and not tools["tmux_send"].annotations.readOnlyHint)

        err, out = await call("tmux_read")
        check("read never creates the session", err and "tmux_send creates it" in out, out)

        err, out = await call("tmux_send", text="echo hi-$((40+2))")
        check("basic send", not err and "hi-42" in out and "[at shell prompt]" in out, out)

        err, out = await call("tmux_list_panes")
        check("list panes, own pane and agent session flagged",
              f"{S}:" in out and "[this agent" in out and f"[opened by agent: session {S}]" in out, out)

        sh("copy-mode", "-t", f"={S}:")
        in_mode = sh("display", "-p", "-t", f"={S}:", "#{pane_in_mode}") == "1"
        err, out = await call("tmux_send", text="echo cm-$((1+1))")
        check("copy-mode left before typing", in_mode and "cm-2" in out, out)

        # a silent long command returns early and is flagged; a parallel read is not blocked by it
        slow = asyncio.create_task(call("tmux_send", text="sleep 3; echo done-$((1+1))", idle=1))
        await asyncio.sleep(0.5)
        err, out = await call("tmux_read", lines=5)
        check("read not blocked by a running send", not err and not slow.done(), out)
        err, out = await slow
        check("silent command flagged as running", "[foreground: sleep]" in out, out)
        err, out = await call("tmux_read", wait_for="(?m)^done-2", timeout=10)
        check("wait_for sees new output", "timeout" not in out and "done-2" in out, out)

        # wait_for must not match output that was already on screen before the last send
        await call("tmux_send", text="echo stale-$((1+1))")
        await call("tmux_send", text="sleep 2; echo stale-$((1+1))", idle=0.5)
        t0 = time.monotonic()
        err, out = await call("tmux_read", wait_for="(?m)^stale-2", timeout=10)
        check("wait_for ignores stale output", time.monotonic() - t0 > 1 and "timeout" not in out, out)

        await call("tmux_send", text="sleep 2; echo wd-$((1+1))", idle=0.5)
        err, out = await call("tmux_read", wait_done=True, timeout=10)
        check("wait_done waits for the shell", not err and "wd-2" in out and "[at shell prompt]" in out, out)

        # text tmux would otherwise mangle: leading '-' parsed as a flag, trailing ';' eaten
        await call("tmux_send", text="cat", idle=1)
        err, out = await call("tmux_send", text="-h", idle=1)
        check("text starting with -", not err and out.count("-h") >= 2, out)
        err, out = await call("tmux_send", text="SELECT 1;", idle=1)
        check("trailing ; kept", out.count("SELECT 1;") >= 2, out)
        err, out = await call("tmux_send", text=r"find . -exec rm {} \;", idle=1)
        check(r"trailing \; kept", out.count(r"{} \;") >= 2, out)
        block = "-h\nSELECT 1;\nline three"
        err, out = await call("tmux_send", text=block, paste=True, idle=1)
        check("paste keeps every line verbatim", not err and all(out.count(l) >= 2 for l in block.split("\n")), out)
        err, out = await call("tmux_send", keys=["C-d"], enter=False)
        check("C-d key", "[at shell prompt]" in out, out)

        err, out = await call("tmux_send", keys=["Ctrl+C"], enter=False)
        check("bad key name rejected", err and "Unknown key" in out, out)

        # a REPL that stays silent longer than 0.5s must not count as done
        await call("tmux_send", text="python3 -q")
        err, out = await call("tmux_send", text="print(6*7)")
        check("REPL stays open", "42" in out and "python" in out, out)
        # typed key by key, the REPL's auto-indent would turn `else:` into a SyntaxError
        block = "if 1:\n    print('br-' + str(1+1))\nelse:\n    print('no')\n"
        err, out = await call("tmux_send", text=block, paste=True)
        check("paste into REPL survives auto-indent", "br-2" in out and "SyntaxError" not in out, out)
        err, out = await call("tmux_send", text="__import__('time').sleep(1.5) or print('slept')")
        check("REPL silent work waited for", "slept" in out, out)
        err, out = await call("tmux_send", keys=["C-d"], enter=False)
        check("exit REPL", "[at shell prompt]" in out, out)

        await call("tmux_send", text="seq 1 200", target_pane=hl_pane)
        err, out = await call("tmux_send", text="seq 1001 1030", target_pane=hl_pane)
        limit = sh("display", "-p", "-t", hl_pane, "#{history_limit}")
        check("history-limit trim keeps whole output", limit == "50" and "1001\n" in out and "1030" in out, (limit, out))

        err, out = await call("tmux_send", text="echo nope", target_pane=self_pane)
        check("refuses to type into own pane", err and "agent runs in" in out, out)

        # tmux_new: a named session the human can attach to, a window and a pane inside it
        target = lambda out: re.search(r'target_pane="([^"]+)"', out).group(1)
        err, out = await call("tmux_new", name=f"{S}-ming", cwd="/tmp")
        t = target(out) if not err else ""
        check("new session is attachable by name and starts in cwd",
              not err and subprocess.run(["tmux", "has-session", "-t", f"={S}-ming"], env=env).returncode == 0 and
              sh("display", "-p", "-t", t, "#{pane_current_path}") == "/tmp", out)
        err, out = await call("tmux_send", text="echo nw-$((1+1))", target_pane=t)
        check("returned target works with tmux_send", "nw-2" in out and "[at shell prompt]" in out, out)
        err, out = await call("tmux_new", name=f"{S}-ming")
        check("duplicate session refused", err and "already exists" in out, out)
        err, out = await call("tmux_new", name="w1", kind="window", session=f"{S}-ming")
        check("new window named", not err and sh("display", "-p", "-t", target(out), "#{window_name}") == "w1", out)
        err, out = await call("tmux_new", name="p1", kind="pane", session=f"{S}-ming")
        check("new pane splits the session's window",
              not err and sh("display", "-p", "-t", f"={S}-ming:", "#{window_panes}") == "2", out)
        p1 = target(out) if not err else ""
        err, out = await call("tmux_new", name="p2", kind="pane", session=f"{S}-ming")
        p2 = target(out) if not err else ""
        sh("rename-session", "-t", f"={S}-ming", f"{S}-renamed")
        err, out = await call("tmux_send", text="echo in-$((1+1))", target_pane=p1)
        check("pane target survives a later split and a session rename", not err and
              "in-2" in sh("capture-pane", "-p", "-t", p1) and "in-2" not in sh("capture-pane", "-p", "-t", p2), out)

        # tmux_kill: only what tmux_new opened, found by the name it was given
        err, out = await call("tmux_list_panes")
        check("list marks what the agent opened", "[opened by agent: pane p1]" in out and "[opened by agent: window w1]" in out, out)
        panes = lambda: sh("list-panes", "-a", "-F", "#{pane_id}").split()
        err, out = await call("tmux_kill", name="p2")
        check("kill pane by name", not err and p2 not in panes() and p1 in panes(), out)
        err, out = await call("tmux_kill", name="w1")
        check("kill window by name", not err and "w1" not in sh("list-windows", "-a", "-F", "#{window_name}").split(), out)
        err, out = await call("tmux_kill", name=f"{S}-ming")  # renamed since: the name the agent chose still finds it
        check("kill session by the name it was opened with",
              not err and subprocess.run(["tmux", "has-session", "-t", f"={S}-renamed"], env=env).returncode, out)
        dups = [target((await call("tmux_new", name="dup", kind="window"))[1]) for _ in range(2)]
        err, out = await call("tmux_kill", name="dup")
        check("ambiguous name refused", err and "pass one pane id" in out, out)
        err, out = await call("tmux_kill", name=dups[0])
        check("kill by pane id", not err and dups[0] not in panes() and dups[1] in panes(), out)
        err, out = await call("tmux_kill", name=hl_pane)
        check("kill refuses a pane the agent did not open", err and "Nothing tmux_new opened" in out, out)
        sh("set-option", "-p", "-t", self_pane, "@tmux_mcp", "pane fake")  # as if the human ran the agent inside one
        err, out = await call("tmux_kill", name="fake")
        check("kill refuses the agent's own pane", err and "kill the agent" in out and self_pane in panes(), out)
        for bad in ({"name": "a:b"}, {"name": "a b"}, {"name": "x", "cwd": "/no/such/dir"}, {"name": "x", "kind": "window", "session": "nope"}):
            err, out = await call("tmux_new", **bad)
            check(f"tmux_new rejects {bad}", err, out)

        err, out = await call("tmux_read", wait_for="never-appears", timeout=1)
        check("wait_for timeout", "[timeout:" in out, out)
        err, out = await call("tmux_read", wait_for="(unclosed")
        check("bad regex", err and "not a valid Python regex" in out, out)
        err, out = await call("tmux_send", text="x", target_pane="nope:9.9")
        check("bad target", err and "tmux_list_panes" in out, out)
        err, out = await call("tmux_send", idle=0)
        check("Field(gt=0) validation", err, out)


try:
    asyncio.run(main())
finally:
    sh("kill-server")
    shutil.rmtree(os.path.dirname(sock), ignore_errors=True)
print("ok" if not failed else f"{len(failed)} failed: {failed}")
sys.exit(1 if failed else 0)
