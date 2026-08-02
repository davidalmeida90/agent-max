# Changelog

## [0.3.0]

**Workflow runs get their own tab, beside Agents.**

- A run list with status, agent count and duration, plus a timeline of the
  selected run grouped by the phase that spawned each agent. Phases, labels and
  per-agent timing are read from the run record rather than guessed, so a
  pipeline and a barrier look different on screen.
- **Runs in flight are rebuilt from transcripts.** A workflow writes its record
  only when it ends, so while you are watching one there is nothing on disk
  naming it. Bars now run to now and grow, working agents pulse, and each row
  names the tool it is running this second.
- Click any agent on a timeline to open the same card the tree opens. The
  inspector moved out of the tree section to make that possible; it was
  `display:none` on every other tab.
- Runs are scoped separately from the agent tree: this session, this project,
  or everywhere.

Fixes found while building it:

- **Workflow agents were invisible.** Agent discovery globbed
  `subagents/` without recursing, and a workflow writes to
  `subagents/workflows/<runId>/`. A session with ten workflow agents rendered
  one lane and reported "1 of 1".
- Agent names no longer leak markdown. A workflow prompt opens on a heading, so
  names read `## Adversarial Claim Verifier (voter 2` instead of the role.
- No more horizontal scrolling in a narrow side panel. The session picker and
  the "other session active" badge pushed the whole page sideways below about
  700px, on every tab.
- The tab strip now follows the WAI-ARIA tab pattern: roles, `aria-selected`,
  a roving tabindex, and arrow-key navigation with Home and End. It was five
  unlabelled buttons a screen reader announced as unrelated.

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
