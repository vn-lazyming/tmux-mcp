# tmux-mcp

A small MCP server that lets an AI agent type into a live tmux pane that you can watch and type in.

Interactive programs (python, gdb, ssh, nc, vim) keep running between calls.

## Tools

- `tmux_send`: type text or keys into a pane
- `tmux_read`: read what's on the pane
- `tmux_list_panes`: list panes
- `tmux_new`: open a session, window or pane
- `tmux_kill`: close one

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
