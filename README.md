# Agent Exhibit

A live view of what your Claude Code agents are actually doing.

When a session delegates, the work disappears. You see "Agent launched
successfully" and then nothing until it returns. This puts the whole tree on
screen while it runs: every agent, what it is working on, how much context it
is holding, and what it has spent.

![Agents appearing and working during a run](docs/live.gif)

*A run staffing itself. Agents arrive, start working, and the tree grows while
the session carries on.*

---

## The tree

Agents that spawn their own agents nest under them. A run three levels deep
looks three levels deep, and each card carries context against the window,
turns, tool calls and output tokens.

![The full agent tree](docs/tree.jpg)

The orchestrator sits at the top. Below it, level one. Below that, the
subagents those agents hired for themselves, which is usually the part you did
not know was happening.

## Watching it happen

Agents appear the moment they are created, with what they were told to do.

![A second agent spawning](docs/spawn.jpg)

**Last said** is the most recent line from that agent. **Last action** is the
last tool call it made, with a timestamp. Between them you can tell a stuck
agent from a slow one without opening anything.

## Session totals

![The dashboard header and agent table](docs/dashboard.jpg)

Four numbers people routinely conflate, kept apart:

| | |
|---|---|
| **Context in play** | size of the current prompt. A snapshot, not a running total |
| **Written, all turns** | output tokens the session actually produced |
| **Biggest single call** | the largest one, useful for spotting a context blow-up |
| **Billed, all turns** | the cumulative cost, high because every turn re-sends its context |

Below that, every agent with its level, model, status and assignment. Filter by
session, depth, model, or down to agents that did real work, which is the
fastest way to hide the ones that spawned and returned immediately.

## Dialogue, log and trace

![The log view](docs/log.jpg)

Open any agent and read it back. Everything it said with timestamps, every tool
call in order, and the raw trace underneath when you need it. You can follow a
number in the output back to the call that produced it.

---

## Requirements

**Python 3.9 or later** on your PATH. The watcher is a single standard-library
script with no pip dependencies, bundled with the extension.

It is found automatically: `py -3` on Windows, then `python3`, then `python`.
If yours lives somewhere unusual, set `agentExhibit.pythonPath`.

## Use

Open the **Agents** panel at the bottom, or run **Agent Exhibit: Open Beside
Editor** from the command palette. The watcher starts on first open.

| Command | |
|---|---|
| `Agent Exhibit: Open Beside Editor` | opens it as an editor tab |
| `Agent Exhibit: Show in Bottom Panel` | focuses the panel view |
| `Agent Exhibit: Restart Watcher` | stops and restarts the local process |
| `Agent Exhibit: Open in Browser` | opens the same page outside VS Code |
| `Agent Exhibit: Show Logs` | the output channel, when something is wrong |

## Settings

| Setting | Default | |
|---|---|---|
| `agentExhibit.pythonPath` | auto | Interpreter used to run the watcher |
| `agentExhibit.port` | `8780` | Localhost port |
| `agentExhibit.limit` | `16` | Transcripts watched at once |
| `agentExhibit.recentMinutes` | `90` | Hide agents idle longer than this. `0` shows all |
| `agentExhibit.autoStart` | `true` | Start the watcher when the view opens |
| `agentExhibit.watcherDir` | bundled | Point at your own copy of the watcher |

`limit` is worth knowing about. Agents past the cap are hidden without warning,
so if a run fans out wider than you expected, raise it.

## Privacy

Everything is local and it makes no network requests.

The watcher reads Claude Code's own transcript files under
`~/.claude/projects/`, which is where your sessions already live, and serves a
page on `127.0.0.1`. Nothing is uploaded, no API keys are read, no telemetry is
collected, and no data leaves the machine.

The port is bound to the loopback address only, so it is not reachable from
your network.

## Notes

If the port is already serving, the extension reuses it rather than starting a
second watcher. Two processes on one port means the second binds nothing and
exits quietly, which looks identical to the extension being broken.

Closing the last VS Code window stops the watcher it started.

## Not affiliated with Anthropic

An independent tool that reads local Claude Code transcripts. Not built,
endorsed or supported by Anthropic.

## License

MIT
