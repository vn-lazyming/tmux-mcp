# tmux-mcp

A small MCP server that lets an AI agent type into a live tmux pane that you can watch and type in.

It is not limited to one session: the agent can list every tmux session, window and pane on the machine, create sessions with any name, and send keys to or read from any pane, including your own.

## Tools

- `tmux_send`: type text or keys into any pane
- `tmux_read`: read what's on any pane
- `tmux_list_panes`: list all sessions, windows and panes
- `tmux_new`: open a session, window or pane with a name you choose
- `tmux_kill`: close one that the agent opened

## Setup

```sh
pip install mcp
claude mcp add tmux -- python3 /path/to/server.py
```

Watch what the agent does:

```sh
tmux attach -t agent
```

The session name defaults to `agent`. Change it with the `TMUX_MCP_SESSION` env var.

## License

MIT
