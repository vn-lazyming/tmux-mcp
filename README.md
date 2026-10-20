# tmux-mcp

A small MCP server that lets an your agent run commands interactively, not in one-shot mode, in a live tmux pane that you can watch and type in too.

It is not limited to one session: the agent can list every tmux session, window and pane on the machine, create sessions with any name, and send keys to or read from any pane, including your own.

## Tools

- `tmux_send`: type or paste text and keys into any pane
- `tmux_read`: read what's on any pane
- `tmux_list_panes`: list all sessions, windows and panes
- `tmux_new`: open a session, window or pane with a name you choose
- `tmux_move`: move a pane the agent opened next to another one, e.g. yours
- `tmux_kill`: close one that the agent opened

## Setup

Needs [uv](https://docs.astral.sh/uv/) and tmux.

```sh
git clone https://github.com/vn-lazyming/tmux-mcp
cd tmux-mcp && uv sync
claude mcp add tmux -- uv run --directory "$PWD" server.py
```

Watch what the agent does:

```sh
tmux attach -t agent
```

The session name defaults to `agent`. Change it with the `TMUX_MCP_SESSION` env var.

## Develop

```sh
make dev    # install dev tools
make lint   # ruff format --check, ruff check, ty
make test   # self-check on a private tmux server
```

## License

MIT
