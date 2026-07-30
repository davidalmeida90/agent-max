# Changelog

## [0.2.1]

- The status bar item stays visible instead of hiding when nothing is running,
  so a fresh install has one entry point that is simply there. It shows a live
  count during a run and a plain label the rest of the time.

## [0.2.0]

- Status bar item with the live agent count. It appears only while the watcher
  is running and hides itself when nothing is going on, so it does not sit in
  the status bar for people who are not using it. Click it to open the exhibit.
  Turn it off with `agentMax.statusBar`.

## [0.1.1]

- Buttons on the Agents panel title bar: open beside the editor, and restart
  the watcher. Opening it to the side previously required the command palette,
  which nobody finds on a freshly installed extension.

## [0.1.0]

First release.

- Live tree of Claude Code agents and subagents, to any depth
- Per-agent context in play, turns, tool calls and output tokens
- What each agent last said and the last tool it ran
- Dialogue, log and trace views
- Filters by session, depth, model and whether the agent did real work
- Opens beside the editor, in the bottom panel, or in a browser
