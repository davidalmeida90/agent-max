<div align="center">

<img src="docs/logo.png" alt="Agent Max" width="110">

# Agent Max

**See what your Claude Code agents are actually doing, while they do it.**

[![Version](https://img.shields.io/visual-studio-marketplace/v/davidarias011.agent-max?color=0C1E48&label=marketplace)](https://marketplace.visualstudio.com/items?itemName=davidarias011.agent-max)
[![Installs](https://img.shields.io/visual-studio-marketplace/i/davidarias011.agent-max?color=0C1E48)](https://marketplace.visualstudio.com/items?itemName=davidarias011.agent-max)
[![Rating](https://img.shields.io/visual-studio-marketplace/r/davidarias011.agent-max?color=0C1E48)](https://marketplace.visualstudio.com/items?itemName=davidarias011.agent-max)
[![License](https://img.shields.io/github/license/davidalmeida90/agent-max?color=0C1E48)](LICENSE)

</div>

When a session delegates, the work disappears. You see *"Agent launched
successfully"* and then nothing until it returns. Agent Max puts the whole tree
on screen while it runs: every agent, what it is working on, how much context it
is holding, and what it has spent.

![Agents appearing and working during a run](docs/live.gif)

## Install

**From VS Code:** open the Extensions view, search `Agent Max`, click Install.

**From the command line:**

```
code --install-extension davidarias011.agent-max
```

## Opening it

Three ways, depending on where you want it. The watcher starts on first open,
whichever you pick.

**Beside your editor**, which is what most people want. Press
<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd> and run **Agent Max: Open Beside
Editor**.

It opens as a normal editor tab in the column to the right, so you can split it,
move it or maximise it like any other tab.

**In the bottom panel:** there is an **Agents** tab next to Terminal, Problems
and Output. To move it to the side, right-click the panel and choose *Move Panel
Right*.

**In a browser:** **Agent Max: Open in Browser**, useful on a second monitor.

No default keyboard shortcut ships with the extension, deliberately, because a
single combo would collide with whatever you already have bound. To add your
own: <kbd>Ctrl</kbd>+<kbd>K</kbd> <kbd>Ctrl</kbd>+<kbd>S</kbd>, search
`Agent Max`, click the `+` next to the command you want.

> **Requires Python 3.9 or later** on your PATH. The watcher is a single
> standard-library script with no pip dependencies, bundled with the extension.
> It is found automatically (`py -3` on Windows, then `python3`, then `python`);
> if yours lives somewhere unusual, set `agentMax.pythonPath`.

## Features

### The tree, to any depth

Agents that spawn their own agents nest under them. A run three levels deep
looks three levels deep, and each card carries context against the window,
turns, tool calls and output tokens.

![The full agent tree](docs/tree.jpg)

### What each agent is doing, right now

Agents appear the moment they are created, with the brief they were given.

![A second agent spawning](docs/spawn.jpg)

**Last said** is the most recent line from that agent. **Last action** is the
last tool call it made, with a timestamp. Between them you can tell a stuck
agent from a slow one without opening anything.

### Session totals, kept apart

![The dashboard header and agent table](docs/dashboard.jpg)

| | |
|---|---|
| **Context in play** | size of the current prompt. A snapshot, not a running total |
| **Written, all turns** | output tokens the session actually produced |
| **Biggest single call** | the largest one, useful for spotting a context blow-up |
| **Billed, all turns** | the cumulative cost, high because every turn re-sends its context |

Filter the agent list by session, depth, model, or down to agents that did real
work, which is the fastest way to hide the ones that spawned and returned
immediately.

### Dialogue, log and trace

![The log view](docs/log.jpg)

Open any agent and read it back. Everything it said with timestamps, every tool
call in order, and the raw trace underneath. You can follow a number in the
output back to the call that produced it.

## Commands

| Command | Description |
|---|---|
| `Agent Max: Open Beside Editor` | Open as an editor tab to the right |
| `Agent Max: Show in Bottom Panel` | Focus the panel view |
| `Agent Max: Restart Watcher` | Stop and restart the local process |
| `Agent Max: Open in Browser` | Open the same page outside VS Code |
| `Agent Max: Show Logs` | Output channel, when something is wrong |

## Settings

| Setting | Default | Description |
|---|---|---|
| `agentMax.pythonPath` | `""` | Python 3.9+ interpreter. Empty means auto-detect |
| `agentMax.port` | `8780` | Localhost port the watcher listens on |
| `agentMax.limit` | `16` | Maximum transcripts watched at once |
| `agentMax.recentMinutes` | `90` | Hide agents idle longer than this. `0` shows all |
| `agentMax.autoStart` | `true` | Start the watcher when the view opens |
| `agentMax.watcherDir` | `""` | Advanced. Point at your own copy of the watcher |

`agentMax.limit` is worth knowing about. Agents past the cap are hidden without
warning, so if a run fans out wider than you expected, raise it.

## Privacy

**Everything is local. The extension makes no network requests.**

The watcher reads Claude Code's own transcript files under `~/.claude/projects/`,
which is where your sessions already live, and serves a page on `127.0.0.1`.
Nothing is uploaded, no API keys are read, no telemetry is collected, and no
data leaves the machine. The port is bound to the loopback address only, so it
is not reachable from your network.

The watcher is a single readable Python file:
[`watcher/watch_agents.py`](watcher/watch_agents.py).

## Known behaviour

- If the port is already serving, the extension **reuses it** rather than
  starting a second watcher. Two processes on one port means the second binds
  nothing and exits quietly, which looks identical to the extension being broken.
- Closing the last VS Code window stops the watcher it started.

## Contributing

Issues and pull requests welcome at
[github.com/davidalmeida90/agent-max](https://github.com/davidalmeida90/agent-max/issues).

To run from source: clone the repo, open it in VS Code, press <kbd>F5</kbd> to
launch an Extension Development Host. To build a `.vsix`, run `npm run package`.

## Not affiliated with Anthropic

An independent tool that reads local Claude Code transcripts. Not built,
endorsed or supported by Anthropic.

## License

[MIT](LICENSE) © David Arias
