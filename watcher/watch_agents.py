"""
watch_agents.py - live observability for multi-agent runs.

Reads Claude Code / Workflow JSONL transcripts, derives per-agent state
(context fill, token spend, tool-call feed), and serves it to dashboard.html
on localhost. No API calls, no network, stdlib only.

Usage:
    py -3 watch_agents.py                      # auto-detect newest transcripts
    py -3 watch_agents.py --source <dir>       # watch a directory of *.jsonl
    py -3 watch_agents.py --source <file.jsonl>
    py -3 watch_agents.py --port 8780 --limit 16

Options:
    --source PATH     a .jsonl file or a directory of them
    --port N          listen port (default 8770)
    --limit N         max transcripts to watch (default 16). Going over the cap
                      hides agents, so leave room for mid-run spawns.
    --recent MIN      only transcripts touched in the last N minutes
                      (default 90; 0 disables the filter)
    --exclude LIST    comma-separated model substrings to hide (default: fable)
    --all-sessions    include agents from earlier sessions, not just this one
    --once            print the state as JSON and exit, no server

Then open http://localhost:<port>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent

# Context window per model. Used only to express fill as a % of the window.
CONTEXT_WINDOW = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
DEFAULT_WINDOW = 1_000_000

# Published rates, $ per 1M tokens. Used for the "priced at API rates" label
# only. Plan usage is not billed this way -- the dashboard says so on screen.
RATES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# A lane is considered live if its file changed within this many seconds.
LIVE_WINDOW_S = 20.0


# The Task tool reports the agent it created as `agentId: <hex>`, and that hex
# is the subagent's filename. It is the parent-child link, stated in the data.
# Alphanumeric, not hex: today's ids happen to be hex, but the pattern is the
# only thing standing between an exact tree and a guessed one, and narrowing it
# to [a-f0-9] means one non-hex character silently drops the link and falls back
# to timestamp matching. The `agentId` label is what makes this specific.
_AGENT_ID_RE = re.compile(r"agentId[\"':\s]+([A-Za-z0-9_-]{8,})")


def _model_family(model: str | None) -> str:
    """Longest declared family the model id starts with.

    Transcripts record dated ids like `claude-haiku-4-5-20251001`, while the
    tables above are keyed by family. A plain dict lookup misses every dated id
    and silently falls through to the default, so every Haiku agent was drawn
    against a 1M window instead of its real 200k — a fifth of the true fill.
    """
    m = model or ""
    if m in CONTEXT_WINDOW or m in RATES:
        return m
    keys = set(CONTEXT_WINDOW) | set(RATES)
    hits = [k for k in keys if m.startswith(k)]
    return max(hits, key=len) if hits else m


def _iter_records(path: Path):
    """Yield (parsed object, raw line) from a .jsonl file, skipping bad lines.

    The raw line comes along because a couple of things are far cheaper to find
    as text than by walking every possible record shape. Task notifications, for
    one, arrive on four different record types.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line), line
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _parse_ts(value):
    """Claude Code writes ISO-8601 with a trailing Z."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _workflow_label(path: Path) -> str | None:
    """A Workflow writes agent-<id>.jsonl beside a journal.jsonl that names
    each agent. Prefer that label over anything we could infer."""
    if not path.stem.startswith("agent-"):
        return None
    agent_id = path.stem[len("agent-"):]
    journal = path.parent / "journal.jsonl"
    if not journal.is_file():
        return None
    for rec, _raw in _iter_records(journal):
        ident = str(rec.get("agentId") or rec.get("id") or "")
        if agent_id and agent_id in ident:
            label = rec.get("label") or rec.get("phase") or rec.get("description")
            if isinstance(label, str) and label.strip():
                return label.strip()[:34]
    return None


# Nouns a role name ends on. Title-case prompts do not shout and do not say the
# word "agent", so without this an office of six desks shows up as six
# truncated copies of "You are the ...".
_ROLE_NOUN = (r"Desk|Team|Office|Committee|Analyst|Researcher|Research|Agent|"
              r"Officer|Strategist|Economist|Specialist|Lead|Reviewer|Auditor")


def _role_from_prompt(text: str) -> str | None:
    """Subagent prompts open with 'You are the X'. Pull X.

    Three shapes, tried in order of how much they tell us:

      1. SHOUTED     'You are the ORCHESTRATOR of a small swarm'  -> Orchestrator
      2. Title case  'You are the Capital Markets Desk'           -> Capital Markets Desk
      3. Loose       'You are the pricing agent'                  -> Pricing

    Shape 2 was missing and it is the one real prompts use. Every desk in the
    endowment runs came back unnamed because of it, so the lanes showed prompt
    fragments instead of who was working.
    """
    # 1. Shouted. Stop at a bracket so 'SYSTEMATIC (QUANT) DESK' does not
    #    truncate to 'Systematic'.
    m = re.search(r"You are the ([A-Z][A-Z0-9&/-]*(?:[ ]+\(?[A-Z0-9&/-]+\)?){0,3})\b",
                  text)
    if m:
        role = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(role) > 1:
            return role.title().replace("'S", "'s")[:34]

    # 2. Title case, ending on a role noun. Handles the possessive in
    #    "You are the Systematic Desk's literature analyst".
    m = re.search(
        rf"You are (?:the|a|an) ((?:[A-Z][\w&/-]*[ ]+){{0,3}}(?:{_ROLE_NOUN})\b)",
        text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:34]

    # 3. Loose, the original form.
    m = re.search(r"You are the ([A-Za-z0-9 &/-]{2,28}?) agent", text)
    if m:
        return m.group(1).strip().title()

    # 4. Nothing self-identified. Some workers open on the task instead
    #    ("I'm compiling current CMAs..."), so look for a named desk anywhere
    #    in the opening rather than giving up and printing the first six words.
    m = re.search(rf"\b((?:[A-Z][\w&/-]*[ ]+){{1,3}}(?:{_ROLE_NOUN}))\b", text[:1200])
    return re.sub(r"\s+", " ", m.group(1)).strip()[:34] if m else None


def _title_from_prompt(text: str, words: int = 5, chars: int = 34) -> str:
    """Turn an opening prompt into a short lane title."""
    # Drop harness-injected wrappers such as <ide_opened_file>...</...>.
    cleaned = re.sub(r"<[^>]+>.*?</[^>]+>", " ", text, flags=re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Workflow prompts open on a markdown heading rather than a sentence, so
    # the first "words" were "## Adversarial Claim Verifier (voter" with the
    # hashes included. Take the heading's own text and stop at its newline:
    # the line after it is a fresh sentence, and running the two together read
    # as "## Source Extractor Research question:".
    head = re.match(r"\s*#{1,6}\s+(.+)", cleaned)
    if head:
        cleaned = head.group(1)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    picked = " ".join(cleaned.split(" ")[:words]).strip(" ,.;:!?-")
    return picked[:chars]


def _tool_summary(block: dict) -> str:
    """One readable line describing a tool call."""
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "pattern", "file_path", "url", "query",
                "description", "prompt", "skill", "path"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            first = val.strip().splitlines()[0]
            # Agents prefix nearly every Bash call with a long `cd "..." &&`.
            # That path is noise; the command after it is the actual action.
            first = re.sub(r'^\s*cd\s+"[^"]*"\s*&&\s*', "", first)
            first = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", first)
            return first[:110]
    if inp:
        return ", ".join(list(inp)[:4])[:110]
    return ""


# How long a transcript can sit untouched before it stops counting as busy.
IDLE_WINDOW_S = 180.0


def _state(age: float, is_root: bool, outcome: str | None = None) -> str:
    """A readable status, which "live"/"done" is too coarse to give.

    For an agent this is now a stated fact rather than a guess. Claude Code
    writes the outcome in two places, and between them they cover every agent
    measured here: a <task-notification> in the parent transcript names the
    result of a background agent, and `stoppedByUser` in the agent's own
    .meta.json covers the rest. Falling back to how long ago the file was last
    written is kept for anything neither source names, which on a 36-agent
    session was nothing at all.

    A root has no such record, so it stays inferred. The same silence means
    different things either side of the tree, so the two sides get different
    words. A root that stops writing is waiting on you and may well speak
    again. A subagent that stops writing is finished and nothing will wake it.
    """
    if outcome and not is_root:
        return outcome
    if age < LIVE_WINDOW_S:
        return "active" if is_root else "running"
    if is_root:
        return "your turn" if age < IDLE_WINDOW_S else "left open"
    return "returned"


# A <task-notification> pairs a task id with the outcome Claude Code recorded
# for it. Matched over the raw line: these blocks are embedded in escaped JSON
# strings across four different record types, and the shapes keep changing.
_NOTIF_RE = re.compile(
    r"<task-id>\\?\s*([A-Za-z0-9_-]+?)\s*\\?</task-id>.*?"
    r"<status>\\?\s*([a-z_]+?)\s*\\?</status>",
    re.S)

# What Claude Code calls an outcome, and what the roster shows for it.
_OUTCOME_WORDS = {
    "completed": "returned",
    "failed": "failed",
    "killed": "stopped",
    "cancelled": "stopped",
}


def _agent_outcome(path: Path) -> str | None:
    """The outcome an agent's own metadata states, if it states one.

    Claude Code writes agent-<id>.meta.json beside every agent transcript. Its
    `stoppedByUser` flag is the only completion signal that survives for agents
    the parent never got a notification for.
    """
    meta = path.with_name(path.stem + ".meta.json")
    try:
        with meta.open(encoding="utf-8") as f:
            j = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(j, dict) or "stoppedByUser" not in j:
        return None
    return "stopped" if j.get("stoppedByUser") else "returned"


# Parsed lanes, keyed by path and invalidated on size or mtime. A finished
# subagent's transcript never changes again, so reparsing it on every one-second
# poll is pure waste — and with a 24 MB root transcript in the mix, the waste is
# what pushes a poll past its own interval and lets requests pile up.
_LANE_CACHE: dict[str, tuple[int, int, dict | None]] = {}


def read_lane(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _LANE_CACHE.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        lane = hit[2]
        if lane is None:
            return None
        # Ages are relative to now, so they have to be recomputed even when the
        # file itself has not moved.
        age = time.time() - st.st_mtime
        lane = dict(lane)
        lane["updated_s_ago"] = round(age, 1)
        lane["status"] = "live" if age < LIVE_WINDOW_S else "done"
        lane["state"] = _state(age, lane["is_root"])
        return lane

    lane = _read_lane_uncached(path)
    _LANE_CACHE[key] = (st.st_size, st.st_mtime_ns, lane)
    if len(_LANE_CACHE) > 400:
        _LANE_CACHE.clear()
    return lane


def _read_lane_uncached(path: Path) -> dict | None:
    """Collapse one transcript file into a single agent lane."""
    turns = 0
    output_tokens = 0
    input_tokens = 0
    cache_write = 0
    cache_read = 0
    context_now = 0
    context_peak = 0
    # message id -> the largest usage figures already counted for it.
    seen_usage: dict[str, dict[str, int]] = {}
    compactions: list[dict] = []
    cache_ttl_s = 0        # longest prompt-cache tier this lane has written to
    cache_written_at = None
    entrypoint = None
    # task id -> [stated outcome, when it was stated]. Roots only.
    outcomes: dict[str, list[str]] = {}
    model = None
    tools: list[dict] = []
    says: list[dict] = []
    msgs: list[dict] = []
    # Task call id -> what it asked for, and then agent id -> the same, once the
    # result comes back naming the agent that call actually produced.
    pending: dict[str, dict] = {}
    spawned: dict[str, dict] = {}
    prompts: list[str] = []   # when the user spoke; roots only
    first_ts = None
    last_ts = None
    effort = None
    cwd = None
    opening = None
    brief = None
    subagent = None
    role = None
    is_root = False

    for rec, raw_line in _iter_records(path):
        # Claude Code injects its own housekeeping as user records flagged isMeta:
        # skill preambles, image placeholders, session boot text. It is not
        # something anybody said, and reading it as a prompt puts "Base directory
        # for this skill: C:\\Users\\..." in the assignment column.
        if rec.get("isMeta"):
            continue

        ts = _parse_ts(rec.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        # Claude Code writes down what the context held immediately before it
        # compacted. Without this the run looks like it never went near its
        # limit: this session twice reached 1,000,080 tokens and then reported
        # 25,548, with nothing on screen to say two million tokens were dropped.
        if rec.get("type") == "system" and rec.get("subtype") in (
                "compact_boundary", "microcompact_boundary"):
            cm = (rec.get("compactMetadata") or rec.get("microcompactMetadata")
                  or {})
            compactions.append({
                "at": ts.isoformat() if ts else None,
                "micro": rec.get("subtype") == "microcompact_boundary",
                "trigger": cm.get("trigger"),
                "pre": cm.get("preTokens") or 0,
                "post": cm.get("postTokens") or 0,
                "dropped_total": cm.get("cumulativeDroppedTokens") or 0,
            })
        if rec.get("effort"):
            effort = rec["effort"]
        if rec.get("cwd") and not cwd:
            cwd = rec["cwd"]
        if rec.get("entrypoint") and not entrypoint:
            entrypoint = rec["entrypoint"]

        # A background agent's outcome comes back to its parent as a
        # <task-notification>, which is the only place a completed/failed/killed
        # verdict is written down for it. Scanned off the raw line because these
        # arrive on several record types (queue-operation, attachment, user and
        # assistant all carry them) and chasing each shape misses some.
        if "<task-notification>" in (raw_line or ""):
            # The timestamp comes along so a later resume can be told from a
            # stale verdict. Without it, an agent restarted after being killed
            # would read "stopped" for the rest of the run.
            for tid, st in _NOTIF_RE.findall(raw_line):
                outcomes[tid] = [_OUTCOME_WORDS.get(st) or st,
                                 ts.isoformat() if ts else ""]
        if rec.get("isSidechain") and not subagent:
            subagent = rec.get("subagent_type") or "subagent"
        # Only a real interactive session writes last-prompt records. Subagent
        # transcripts never do, so this is how we tell a root from a worker.
        if rec.get("type") == "last-prompt":
            is_root = True
            # Each of these is one message from the user, and they are the only
            # boundary in the transcript between "what I asked for just now"
            # and "what I asked for before". They carry no timestamp of their
            # own, but they are written in order, so the last message before
            # one dates it well enough to compare against a spawn.
            if last_ts:
                prompts.append(last_ts.isoformat())
        if rec.get("type") == "last-prompt" and not opening:
            raw = rec.get("lastPrompt")
            if isinstance(raw, str):
                opening = _title_from_prompt(raw) or opening
                brief = _title_from_prompt(raw, words=40, chars=260) or brief

        # Subagent transcripts carry no last-prompt record; their opening user
        # message is the spawn prompt, which names the role.
        if role is None and rec.get("type") == "user":
            m = rec.get("message")
            c = m.get("content") if isinstance(m, dict) else None
            text = c if isinstance(c, str) else ""
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text = b.get("text") or ""
                        break
            if text:
                role = _role_from_prompt(text)
                # Agents the lead spawns get prompts we did not author, so fall
                # back to the opening words: that is what it asked them to do.
                if role is None and not opening:
                    opening = _title_from_prompt(text, words=6, chars=38)
                # `not brief` rather than `is None`: a last-prompt that strips to
                # nothing (all tags, or a slash command) would otherwise pin the
                # brief at empty and block every later source.
                if not brief:
                    brief = _title_from_prompt(text, words=40, chars=260)

        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue

        # "<synthetic>" is Claude Code's placeholder for a message it generated
        # itself rather than one the API returned. Letting it win would print
        # "<synthetic>" as the agent's model purely because it happened to land
        # last, and it resolves to no context window and no rates.
        if msg.get("model") and msg["model"] != "<synthetic>":
            model = msg["model"]

        # Only assistant records carry real usage. Reading it from any record
        # that happens to have the field is how a future transcript type quietly
        # doubles every total.
        usage = msg.get("usage") if rec.get("type") == "assistant" else None
        if isinstance(usage, dict):
            # One API message is written as several records, one per content
            # block: the thinking, the prose, and one for every tool call it
            # made. All of them repeat the SAME usage object. Adding it once per
            # record counted this session's output at 2.4M when the true figure
            # was 1.16M, because 663 of its 1,440 messages are written more than
            # once, some five times over.
            #
            # The two transcript writers do not even agree on how they repeat
            # it. A root session stamps every copy with the message's final
            # usage. A subagent stamps the early copies with a partial count and
            # completes it on the last. So neither "take the first" nor "take
            # the last" is safe: adding only what each field GAINED handles both,
            # since a repeat of the final figure gains nothing.
            # Keyed on the message id AND the request id. They agree on every
            # transcript measured here (0 of 1,524 message ids spanned two
            # requests), but a retried request reusing an id would be counted
            # once under the looser key, and losing a whole call is worse than
            # the cost of one more string.
            mid = (f"{msg.get('id')}:{rec.get('requestId')}"
                   if msg.get("id") else f"_anon{len(seen_usage)}")
            prev = seen_usage.get(mid)
            if prev is None:
                turns += 1
                prev = {"input_tokens": 0, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": 0}
                seen_usage[mid] = prev
            for field in prev:
                now = usage.get(field) or 0
                if now > prev[field]:
                    gain = now - prev[field]
                    prev[field] = now
                    if field == "output_tokens":
                        output_tokens += gain
                    elif field == "input_tokens":
                        input_tokens += gain
                    elif field == "cache_creation_input_tokens":
                        cache_write += gain
                    else:
                        cache_read += gain
            # Real prompt size is the sum of all three, not input_tokens alone.
            # A call that carried no context at all says nothing about the
            # working set, so it must not become the reading. Claude Code's
            # own synthetic messages are exactly that, and one landing last
            # made a session holding 360k tokens report "context 0 / 1M".
            here = (prev["input_tokens"]
                    + prev["cache_creation_input_tokens"]
                    + prev["cache_read_input_tokens"])
            if here:
                context_now = here
            context_peak = max(context_peak, here + prev["output_tokens"])

            # Prompt cache: reads bill at a tenth of fresh input, so the first
            # call after the cache expires costs roughly ten times the one
            # before it. The TTL is stated per tier, and the clock restarts
            # every time anything is written to the cache.
            tier = usage.get("cache_creation")
            if isinstance(tier, dict) and ts:
                if tier.get("ephemeral_1h_input_tokens"):
                    cache_ttl_s, cache_written_at = 3600, ts
                elif tier.get("ephemeral_5m_input_tokens"):
                    cache_ttl_s, cache_written_at = 300, ts

        content = msg.get("content")
        if isinstance(content, list):
            # Assistant prose between tool calls is the agent narrating what it
            # is doing. That is the live activity, not the static brief.
            if msg.get("role") == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        txt = (block.get("text") or "").strip()
                        if txt:
                            says.append({
                                "text": " ".join(txt.split())[:220],
                                "at": ts.isoformat() if ts else None,
                            })
            # The result of a Task call names the agent it actually created.
            # That is the parent-child link, stated outright, so nothing here
            # has to be inferred from timestamps.
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call = pending.get(str(block.get("tool_use_id") or ""))
                if not call:
                    continue
                found = _AGENT_ID_RE.search(json.dumps(block.get("content"))[:4000])
                if found:
                    spawned[found.group(1)] = call

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or "?"
                # Agent-to-agent traffic is a different kind of event from an
                # agent thinking out loud, so keep it in its own stream.
                raw_in = block.get("input")
                inp: dict = raw_in if isinstance(raw_in, dict) else {}
                if name in ("Task", "Agent"):
                    # Record the call id so the tool_result carrying the real
                    # agent id can be matched to it below.
                    pending[str(block.get("id") or "")] = {
                        "description": str(inp.get("description") or "")[:34],
                        "at": ts.isoformat() if ts else None,
                        # Which standing capability was deployed. Absent means
                        # the default was used, not that no type was involved.
                        "type": str(inp.get("subagent_type")
                                    or inp.get("agentType") or "general-purpose"),
                    }
                if name == "SendMessage":
                    msgs.append({
                        "kind": "send",
                        "to": str(inp.get("to") or "")[:34],
                        # Long enough that expanding a balloon actually shows
                        # you something. At 160 the client offered to reveal
                        # more and there was no more to reveal.
                        "text": " ".join(str(inp.get("summary")
                                             or inp.get("message") or "").split())[:600],
                        "at": ts.isoformat() if ts else None,
                    })
                elif name in ("Task", "Agent"):
                    # A Task call normally carries both, and the prompt is the
                    # interesting half. When it does not, the description is
                    # still something; an empty balloon on the board says the
                    # spawn happened and refuses to say what it asked for.
                    msgs.append({
                        "kind": "spawn",
                        "to": str(inp.get("description") or "")[:34],
                        "text": " ".join(str(inp.get("prompt")
                                             or inp.get("description")
                                             or "").split())[:600],
                        "at": ts.isoformat() if ts else None,
                    })
                tools.append({
                    "name": name,
                    "detail": _tool_summary(block),
                    "at": ts.isoformat() if ts else None,
                })

    if turns == 0 and not tools:
        return None

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    age = time.time() - mtime
    # An agent's own metadata is the fallback source for its outcome; the
    # parent's notification is the primary one and is applied in build_state,
    # where the parent is in scope.
    outcome = None if is_root else _agent_outcome(path)
    family = _model_family(model)
    window = CONTEXT_WINDOW.get(family, DEFAULT_WINDOW)
    rate_in, rate_out = RATES.get(family, (0.0, 0.0))

    # Cache reads bill at ~0.1x input, cache writes at ~1.25x.
    priced = (
        (input_tokens * rate_in)
        + (cache_write * rate_in * 1.25)
        + (cache_read * rate_in * 0.10)
        + (output_tokens * rate_out)
    ) / 1_000_000

    elapsed = None
    if first_ts and last_ts:
        elapsed = max(0.0, (last_ts - first_ts).total_seconds())

    # Name priority: Workflow's own label > subagent type > opening prompt >
    # project folder > uuid prefix. The first case is what the real pipeline
    # produces; the rest keep arbitrary session files readable.
    # The interactive session is always the orchestrator of whatever it spawns,
    # so name it for its role rather than for the folder it happens to be in.
    if is_root:
        name = "Orchestrator"
    else:
        name = (
            _workflow_label(path)
            or role
            or opening
            or subagent
            or (Path(cwd).name if cwd else None)
            or path.stem[:8]
        )

    return {
        "id": path.stem,
        "name": name,
        # True when the name came from a "You are the X agent" spawn prompt,
        # i.e. this lane is a worker the lead created.
        "role_derived": role is not None,
        "is_root": is_root,
        "parent": None,      # set by _link_hierarchy
        "depth": 0,          # set by _link_hierarchy
        "orphaned": False,   # set by build_state
        "started": first_ts.isoformat() if first_ts else None,
        # The last record the lane wrote, as the transcript dates it. Compared
        # against a notification's timestamp this tells a resumed agent from a
        # stale verdict, and unlike the file's mtime it survives the file being
        # copied, restored or regenerated.
        "last_seen": last_ts.isoformat() if last_ts else None,
        "ref": path.stem[:8],
        "brief": brief or "",
        "project": Path(cwd).name if cwd else None,
        "cwd": cwd,   # the bench looks here for .claude/agents
        "label": path.stem[:8],
        "model": model or "unknown",
        "effort": effort,
        # "live"/"done" is the two-state flag the tree colours itself with.
        # "state" is the finer reading shown in the roster.
        "status": "live" if age < LIVE_WINDOW_S else "done",
        "state": _state(age, is_root, outcome),
        # What the transcript says happened, as opposed to what its mtime
        # suggests. build_state overrides this with the parent's notification
        # when there is one, since that names failed and killed as well.
        "outcome": outcome,
        # Task notifications this lane received, keyed by the agent they name.
        "outcomes": outcomes,
        "turns": turns,
        # Every time the window filled and Claude Code threw context away. The
        # board otherwise shows a session that quietly lost 2M tokens as though
        # it had simply been small all along.
        "compactions": compactions,
        "dropped_tokens": (compactions[-1]["dropped_total"] if compactions else 0),
        # Seconds until the prompt cache expires, negative once it has. Reads
        # bill at a tenth of fresh input, so this is the difference between a
        # cheap next message and an expensive one.
        "cache_expires_in_s": (
            round(cache_ttl_s - (time.time() - cache_written_at.timestamp()))
            if cache_written_at else None),
        "cache_ttl_s": cache_ttl_s or None,
        # How this session was started. Two windows open at once look identical
        # on the board otherwise.
        "entrypoint": entrypoint,
        "context_now": context_now,
        # The largest single call this lane ever made. A session that filled its
        # window and compacted reads small now, and the peak is the only place
        # that still shows it happened.
        "context_peak": context_peak,
        "context_pct": round(100.0 * context_now / window, 2) if window else 0.0,
        "window": window,
        "input_tokens": input_tokens,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "output_tokens": output_tokens,
        # Cumulative across turns: cache reads recur on every call, so this is
        # what the meter sees, NOT the size of anything held in memory.
        "billed_tokens": input_tokens + cache_write + cache_read + output_tokens,
        # Exactly the agents this lane created, keyed by their transcript id.
        # Counting Task calls instead over-counted: a call that errored or was
        # skipped never produced an agent, but still incremented the tally.
        "spawned": spawned,
        "spawns": len(spawned),
        # Only the root records these; build_state uses them to date every lane.
        # Not truncated: keeping the last 40 collapsed everything older than 40
        # messages onto one boundary, so a whole session read as a single round
        # of delegation and nothing was ever old enough to hide. A long session
        # is a few hundred short strings, which is a rounding error next to a
        # 24 MB transcript.
        "prompts": prompts,
        "priced_usd": round(priced, 4),
        "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
        "updated_s_ago": round(age, 1),
        "tools": tools[-40:],
        "tool_count": len(tools),
        "says": says[-12:],
        "saying": says[-1]["text"] if says else "",
        "msgs": msgs[-6:],
        "msg_count": len(msgs),
    }








def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _peek(path: Path, keys: tuple[str, ...], limit: int = 40) -> dict:
    """First value seen for each key, reading only the head of the file."""
    found: dict[str, str] = {}
    for n, (rec, _raw) in enumerate(_iter_records(path)):
        for k in keys:
            if k not in found and isinstance(rec.get(k), str) and rec[k].strip():
                found[k] = rec[k]
        if len(found) == len(keys) or n >= limit:
            break
    return found


def _is_within(path: Path, root: Path) -> bool:
    """True when `path` sits anywhere under `root`.

    Path.is_relative_to landed in 3.9 and the watcher targets 3.9+, but it
    raises on a mismatched drive on Windows rather than returning False, which
    happens the moment a transcript and the projects root live on different
    disks. Hence the try.
    """
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


def _session_agents(session: Path) -> list[Path]:
    """Subagent transcripts live in <session-id>/subagents/ beside the file.

    Recursive, because a Workflow run nests its agents one level deeper, in
    subagents/workflows/<runId>/, beside the journal.jsonl that indexes them.
    A flat glob returned [] for those, and it failed silently: a session with
    ten workflow agents on disk rendered a single lane and reported "1 of 1",
    because agents_on_disk is computed from this same call. Their transcripts
    are the same format as any other subagent's, so nothing downstream needs
    to know the difference.
    """
    d = session.with_suffix("") / "subagents"
    if not d.is_dir():
        return []
    try:
        return sorted(d.rglob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    except OSError:
        return []


# A Workflow run writes two things in two places, and the interesting half is
# the one that is easy to miss:
#
#   <session>/workflows/<runId>.json              the run record
#   <session>/subagents/workflows/<runId>/        agent transcripts + journal
#
# journal.jsonl is deliberately minimal, {type, key, agentId, result} and no
# timestamp, so it cannot name or time anything. Everything worth showing is in
# the run record's `workflowProgress` array: per-agent label, phaseIndex,
# phaseTitle, startedAt, durationMs, tokens and toolCalls. That array is the
# only place an agent is mapped to the phase that spawned it.
_WF_CACHE: dict[str, tuple[int, int, dict]] = {}


def _workflow_runs(session: Path) -> dict:
    """runId -> a normalised run record for every Workflow run in a session.

    Returns {} when the session never ran one, which is the common case.
    """
    d = session.with_suffix("") / "workflows"
    if not d.is_dir():
        return {}

    runs = {}
    for meta in sorted(d.glob("wf_*.json")):
        try:
            st = meta.stat()
            key = str(meta)
            hit = _WF_CACHE.get(key)
            if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                runs[meta.stem] = hit[2]
                continue
            rec = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        prog = rec.get("workflowProgress") or []
        agents = [p for p in prog if p.get("type") == "workflow_agent"]
        phases = [p for p in prog if p.get("type") == "workflow_phase"]

        # An agent that never started has no agentId, so it cannot be joined to
        # a transcript. Both killed runs are full of these: 64 of the 424
        # agents on this machine are state="start" with nothing behind them.
        by_agent = {a["agentId"]: a for a in agents if a.get("agentId")}

        run = {
            "run_id": rec.get("runId") or meta.stem,
            "name": rec.get("workflowName") or meta.stem,
            # completed | killed | failed. Nothing else has ever been observed,
            # and no in-flight status is ever written, so a run that is still
            # going has no record here at all until it ends.
            "status": rec.get("status") or "unknown",
            "summary": rec.get("summary") or "",
            "started_ms": rec.get("startTime"),
            "ended_iso": rec.get("timestamp"),
            "duration_ms": rec.get("durationMs") or 0,
            "agent_count": rec.get("agentCount"),
            "total_tokens": rec.get("totalTokens") or 0,
            "total_tools": rec.get("totalToolCalls") or 0,
            "model": rec.get("defaultModel"),
            # Present only on killed/failed runs, a JS stack trace as a string.
            "error": rec.get("error") or None,
            "logs": [str(x) for x in (rec.get("logs") or [])],
            "phases": [{"index": p.get("index"), "title": p.get("title") or "",
                        "detail": p.get("detail") or ""} for p in phases]
                      or [{"index": i + 1, "title": p.get("title") or "",
                           "detail": p.get("detail") or ""}
                          for i, p in enumerate(rec.get("phases") or [])],
            "agents": [_wf_agent(a) for a in agents],
            "by_agent_id": by_agent,
            # result is null exactly when status != completed, and is a dict in
            # almost every case but a bare markdown string in a few, so callers
            # must type-check before reaching into it.
            "result": rec.get("result"),
            "has_result": rec.get("result") is not None,
            "script": rec.get("script") or "",
        }
        _WF_CACHE[str(meta)] = (st.st_size, st.st_mtime_ns, run)
        runs[run["run_id"]] = run
    return runs


# Scanning every project for run records costs a directory walk, and the tab
# polls once a second. Runs appear on a human timescale, so a short TTL is
# plenty; the per-file _WF_CACHE underneath makes repeat scans nearly free.
_WF_SCAN: dict = {}


def _workflow_runs_scoped(scope: str, paths: list[Path]) -> dict:
    """Run records for a scope wider than the session in view.

    The agent tree is deliberately scoped to one session: it answers what is
    happening now. Workflow runs are a log, and the run you want to look at is
    usually not the one this session produced, so the tab scopes separately.

      session  the sessions currently in view (the default, and the old behaviour)
      project  every session in those sessions' project folders
      all      every project on disk
    """
    if scope not in ("project", "all"):
        runs = {}
        for p in paths:
            if "subagents" not in p.parts:
                runs.update(_workflow_runs(p))
        return runs

    now = time.time()
    hit = _WF_SCAN.get(scope)
    if hit and now - hit[0] < 10:
        return hit[1]

    if scope == "all":
        roots = [d for d in _projects_root().iterdir() if d.is_dir()]
    else:
        roots = sorted({p.parent for p in paths if "subagents" not in p.parts})

    runs = {}
    for root in roots:
        try:
            for meta in root.glob("*/workflows/wf_*.json"):
                # <project>/<session>/workflows/x.json -> the session transcript
                runs.update(_workflow_runs(meta.parent.parent.with_suffix(".jsonl")))
        except OSError:
            continue
    _WF_SCAN[scope] = (now, runs)
    return runs


def _wf_agent(a: dict) -> dict:
    """One workflowProgress agent entry, flattened to what the UI needs."""
    started = a.get("startedAt")
    dur = a.get("durationMs")
    return {
        "index": a.get("index"),
        "agent_id": a.get("agentId"),
        # The label the script gave it ("search:Metric recipes"). Far better
        # than anything derivable from the transcript, whose prompts open on a
        # markdown heading rather than "You are the X agent".
        "label": a.get("label") or "",
        "phase_index": a.get("phaseIndex"),
        "phase": a.get("phaseTitle") or "",
        "model": a.get("model") or "",
        # done | progress | start. "start" means queued and never reported.
        "state": a.get("state") or "",
        "attempt": a.get("attempt"),
        "queued_ms": a.get("queuedAt"),
        "started_ms": started,
        "duration_ms": dur,
        # Derived rather than stored. startedAt + durationMs overshoots the
        # transcript's last line by up to 8s, so this is good enough to draw a
        # bar with and not good enough to quote as an end time.
        "ended_ms": (started + dur) if (started and dur) else None,
        "tokens": a.get("tokens") or 0,
        "tool_calls": a.get("toolCalls") or 0,
        "last_tool": a.get("lastToolName") or "",
        "last_tool_detail": a.get("lastToolSummary") or "",
        "prompt_preview": a.get("promptPreview") or "",
        "result_preview": a.get("resultPreview") or "",
        "agent_type": a.get("agentType") or "",
    }


def build_index(max_sessions: int = 5) -> dict:
    """Enumerate every Claude Code project and its sessions.

    The on-disk slug (`c--Users-you-my-project`) is ambiguous — a dash
    stands for both a path separator and an underscore — so the readable label
    comes from the `cwd` recorded inside a transcript instead of by decoding it.
    """
    root = _projects_root()
    projects = []
    if not root.is_dir():
        return {"projects": [], "root": str(root)}

    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                           reverse=True)
        except OSError:
            continue
        if not files:
            continue

        sessions = []
        for f in files[:max_sessions]:
            head = _peek(f, ("cwd", "lastPrompt"))
            st = f.stat()
            workers = len(_session_agents(f))
            sessions.append({
                "id": f.stem,
                "path": str(f),
                "title": _title_from_prompt(head.get("lastPrompt", ""), words=9,
                                            chars=64) or "untitled session",
                "cwd": head.get("cwd", ""),
                "updated": st.st_mtime,
                "size": st.st_size,
                # Every session has an orchestrator: itself. Counting only the
                # subagent transcripts made sessions that never delegated read
                # as "0 agents", which is not what happened in them.
                "agents": workers + 1,
                "workers": workers,
            })

        cwd = next((s["cwd"] for s in sessions if s["cwd"]), "")
        projects.append({
            "slug": d.name,
            "label": Path(cwd).name if cwd else d.name,
            "cwd": cwd,
            "sessions": sessions,
            "updated": max((s["updated"] for s in sessions), default=0),
            "agents": sum(s["agents"] for s in sessions),
        })

    projects.sort(key=lambda p: -p["updated"])
    return {"projects": projects, "root": str(root)}


def scope_paths(scope: dict, limit: int) -> list[Path] | None:
    """Files for an explicit session or project selection.

    Returns None when nothing was selected, so the caller falls back to
    auto-detection.
    """
    root = _projects_root()

    session_id = scope.get("session")
    if session_id:
        for d in root.iterdir() if root.is_dir() else []:
            f = d / f"{session_id}.jsonl"
            if f.is_file():
                return ([f] + _session_agents(f))[:limit]
        return []

    slugs = [s for s in scope.get("projects", []) if s]
    if slugs:
        picked: list[Path] = []
        for slug in slugs:
            d = root / slug
            if not d.is_dir():
                continue
            try:
                files = sorted(d.glob("*.jsonl"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                continue
            # One session per project, plus its workers. Showing every session
            # of every project at once is noise, not a view.
            for f in files[:1]:
                picked.append(f)
                picked.extend(_session_agents(f))
        return picked[:limit]

    return None


def discover(source: Path | None, limit: int) -> list[Path]:
    """Pick the transcript files to watch."""
    if source and source.is_file():
        return [source]
    if source and source.is_dir():
        files = sorted(source.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        # Truncating by mtime alone drops the root session first, because the
        # session that started the run is by definition the least recently
        # touched file in it. That deletes the one lane the whole tree hangs
        # off, so carry it through the cut the way the no-source branch does.
        roots = [p for p in files if not p.name.startswith("agent-")]
        workers = [p for p in files if p.name.startswith("agent-")]
        # Mock corpora keep the agent files flat beside the session. A real
        # project directory does not: it puts them in <session-id>/subagents/.
        # Globbing only the top level meant --source pointed at anything real
        # drew the session on its own and silently no agents at all.
        if roots:
            workers = workers + [a for r in roots[:1] for a in _session_agents(r)]
            return (roots[:1] + workers)[:limit]
        return files[:limit]

    # No source given: prefer Workflow per-agent transcripts, else the newest
    # Claude Code session transcripts for this project.
    roots = []
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        roots.append(Path(tmp) / "claude")
    home = Path.home() / ".claude" / "projects"
    if home.is_dir():
        roots.append(home)

    workers: list[Path] = []
    sessions: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*.jsonl"):
                (workers if p.name.startswith("agent-") else sessions).append(p)
        except OSError:
            pass

    by_mtime = lambda p: p.stat().st_mtime  # noqa: E731
    workers.sort(key=by_mtime, reverse=True)
    sessions.sort(key=by_mtime, reverse=True)

    if sessions:
        # Prefer the session whose AGENTS moved most recently, not the one whose
        # own file did. Ranking by the session file alone means whichever
        # conversation is busiest wins — a session running tools every few
        # seconds permanently outranks one that just spawned a swarm, so you end
        # up watching the wrong run without any sign that you are.
        def freshest(p: Path) -> float:
            own = _session_agents(p)
            if not own:
                return 0.0
            try:
                return max(a.stat().st_mtime for a in own)
            except OSError:
                return 0.0

        busy = [p for p in sessions if time.time() - freshest(p) < LIVE_WINDOW_S * 3]
        if busy:
            busy.sort(key=freshest, reverse=True)
            sessions = busy + [p for p in sessions if p not in busy]
        newest = sessions[0]
        # A session's own workers sit in <session-id>/subagents/ beside it, so
        # the right set can be named exactly. Taking the most recently touched
        # agent files instead leaked across sessions: start a new conversation
        # and the previous one's agents were still on the board — and because
        # they filled the limit, the new session's own agents never showed up.
        own = _session_agents(newest)
        if own:
            return ([newest] + own)[:limit]
        # No subagents folder yet: either nothing has been spawned, or these
        # are Workflow transcripts, which sit flat next to a journal.jsonl.
        # Relative-to rather than an equality on .parent, because a workflow's
        # agents live under <session>/subagents/workflows/<runId>/ and an
        # equality test on the immediate parent discards every one of them.
        stem = newest.with_suffix("")
        beside = [w for w in workers
                  if w.parent == newest.parent or _is_within(w, stem)]
        return ([newest] + beside)[:limit]

    # Workflow-only run: agent transcripts with no interactive session at all.
    return workers[:limit]


def _link_hierarchy(lanes: list[dict]) -> None:
    """Attach every spawned lane to the agent that created it.

    Each Task call's result names the agent it produced, and that name is the
    subagent's own filename, so the tree is read straight out of the data.

    This used to pair children with spawns by timestamp — nearest unclaimed
    call at or before the child's first message. That guess broke whenever two
    agents were spawned in the same second, when a spawn errored and produced
    no agent at all, or when a child's first write landed before its parent
    flushed the call. Timestamps are only consulted now as a fallback for
    Workflow transcripts, which record no agent id.
    """
    by_id = {l["id"]: l for l in lanes}

    claimed: set[str] = set()
    for lane in lanes:
        for agent_id, call in (lane.get("spawned") or {}).items():
            child = by_id.get(f"agent-{agent_id}") or by_id.get(agent_id)
            if child is None or child is lane or child["is_root"]:
                continue
            child["parent"] = lane["id"]
            claimed.add(child["id"])
            # A lane that named itself in its own prompt keeps that name; the
            # rest take the label their parent gave the task.
            if not child["role_derived"] and call.get("description"):
                child["name"] = call["description"][:34]

    # Fallback for transcripts with no recorded agent id (Workflow runs). Same
    # nearest-earlier-spawn rule as before, but now only for lanes the exact
    # pass could not place.
    orphans = sorted((l for l in lanes
                      if not l["is_root"] and l["started"] and l["id"] not in claimed),
                     key=lambda l: l["started"])
    if orphans:
        spawns = sorted(
            (t["at"], t["detail"], l["id"])
            for l in lanes for t in l["tools"]
            if t["name"] in ("Task", "Agent") and t.get("detail") and t.get("at"))
        used: set[int] = set()
        for child in orphans:
            best = None
            for i, (at, _d, owner) in enumerate(spawns):
                if i in used or at > child["started"] or owner == child["id"]:
                    continue
                if best is None or at > spawns[best][0]:
                    best = i
            if best is not None:
                used.add(best)
                child["parent"] = spawns[best][2]
                if not child["role_derived"]:
                    child["name"] = spawns[best][1][:34]

    # The fallback matches on timestamps, and timestamps do not know about
    # ancestry: it happily made A the parent of B while B was already an
    # ancestor of A. The depth walk survived that only because of its own guard,
    # and the tree it drew was nonsense. Break any link that closes a loop.
    for lane in lanes:
        seen = {lane["id"]}
        cur = lane
        while cur.get("parent"):
            if cur["parent"] in seen:
                cur["parent"] = None
                break
            seen.add(cur["parent"])
            nxt = by_id.get(cur["parent"])
            if nxt is None:
                break
            cur = nxt

    # Roots sit at depth 0; walk the parent chain for everyone else. The guard
    # stops a malformed cycle from hanging the server.
    for lane in lanes:
        depth, cur, seen = 0, lane, set()
        while cur.get("parent") and cur["id"] not in seen and depth < 12:
            seen.add(cur["id"])
            nxt = by_id.get(cur["parent"])
            if not nxt:
                break
            cur, depth = nxt, depth + 1
        lane["depth"] = 0 if lane["is_root"] else max(depth, 1)


def _ui_mtime() -> float | None:
    """Modification time of dashboard.html, for the page's own hot reload."""
    try:
        return round((Path(__file__).resolve().parent / "dashboard.html").stat().st_mtime, 3)
    except OSError:
        return None






# Fields heavy enough to matter on a one-second poll. A 104-agent run carries
# a promptPreview and a resultPreview per agent; shipping those every second is
# ~40x the bytes of the summary for data nobody is reading until they click.
# They live behind /workflow.json?run=<id> instead.
_WF_LEAN = ("index", "agent_id", "label", "phase", "phase_index", "state",
            "model", "started_ms", "duration_ms", "ended_ms", "tokens",
            "tool_calls", "last_tool")


def _workflow_summaries(runs: dict, lanes: list[dict]) -> list[dict]:
    """The workflow block for /state.json, newest run first.

    Two populations are merged. Runs that ENDED have a record on disk. Runs
    still in flight have no record at all, only live transcripts, so they are
    synthesised from the lanes and marked status="running". Without that, a
    workflow you are watching right now is the one thing the tab cannot show.
    """
    out = []
    for run in runs.values():
        live = [l for l in lanes if l.get("workflow") == run["run_id"]
                and l["status"] == "live"]
        out.append({
            k: run[k] for k in
            ("run_id", "name", "status", "summary", "started_ms", "ended_iso",
             "duration_ms", "agent_count", "total_tokens", "total_tools",
             "model", "error", "has_result", "phases")
        } | {
            "agents": [{k: a.get(k) for k in _WF_LEAN} for a in run["agents"]],
            "live": len(live),
            "on_disk": sum(1 for l in lanes
                           if l.get("workflow") == run["run_id"]),
            "logs": run["logs"][:20],
        })

    # In-flight runs. A run writes no record until it ENDS, so while you are
    # watching one there is no name, no phase list and no timing on disk. All
    # of it has to be rebuilt from the transcripts, which ARE being written
    # live. Without this the one run you actually care about is the one the tab
    # cannot draw.
    known = set(runs)
    in_flight = {str(l["workflow"]) for l in lanes if l.get("workflow")}
    now_ms = time.time() * 1000.0

    def _ms(iso: str | None):
        d = _parse_ts(iso)
        return d.timestamp() * 1000.0 if d else None

    for run_id in sorted(in_flight - known):
        members = [l for l in lanes if l.get("workflow") == run_id]
        starts = [s for s in (_ms(l["started"]) for l in members) if s]
        t0 = min(starts) if starts else None
        agents = []
        for i, l in enumerate(members):
            st = _ms(l["started"])
            live = l["status"] == "live"
            # A live agent has not ended, so its bar runs to NOW and grows on
            # every poll. A finished one stops at its last written record.
            en = now_ms if live else (_ms(l["last_seen"]) or st)
            agents.append({
                "index": i, "agent_id": l["id"][len("agent-"):],
                "label": l["name"], "phase": "", "phase_index": None,
                "state": "progress" if live else "done",
                "model": l["model"], "started_ms": st,
                "duration_ms": int(en - st) if (st and en) else None,
                "ended_ms": en, "tokens": l["output_tokens"],
                "tool_calls": l["tool_count"],
                # What it is doing this second. Only meaningful while live, and
                # it is the difference between "an agent is running" and
                # "an agent is running a WebSearch".
                "last_tool": ((l.get("tools") or [{}])[-1].get("name") or "")
                             if live else "",
            })
        agents.sort(key=lambda a: a["started_ms"] or 0)
        for i, a in enumerate(agents):
            a["index"] = i
        out.append({
            "run_id": run_id, "name": run_id, "status": "running",
            "summary": "", "started_ms": int(t0) if t0 else None,
            "ended_iso": None,
            "duration_ms": int(now_ms - t0) if t0 else 0,
            "agent_count": len(members),
            "total_tokens": sum(l["output_tokens"] for l in members),
            "total_tools": sum(l["tool_count"] for l in members),
            "model": members[0]["model"] if members else "",
            "error": None, "has_result": False, "phases": [],
            "agents": agents,
            "live": sum(1 for l in members if l["status"] == "live"),
            "on_disk": len(members),
            "logs": [],
            # Tells the UI to keep re-scaling against a moving right edge
            # instead of a fixed one.
            "now_ms": int(now_ms),
        })

    # Newest first. started_ms is epoch ms on real runs and absent on in-flight
    # ones, so fall back to the earliest transcript timestamp for those.
    def when(r):
        return r.get("started_ms") or 0
    out.sort(key=when, reverse=True)
    # An in-flight run sorts to 0 under that key, which would bury the one run
    # you are actually watching. Hoist anything still running to the top.
    out.sort(key=lambda r: r["status"] != "running")
    return out


def build_state(paths: list[Path], exclude: tuple[str, ...] = (),
                recent_s: float | None = None,
                session_only: bool = True,
                wf_scope: str = "session") -> dict:
    # Workflow run records, keyed by runId. Read once per build rather than per
    # lane: a 104-agent run would otherwise re-parse the same record 104 times.
    wf_runs: dict = _workflow_runs_scoped(wf_scope, paths)

    lanes = []
    for p in paths:
        lane = read_lane(p)
        if not lane:
            continue
        # Substring match so "fable" filters claude-fable-5 and friends.
        if any(tok.lower() in lane["model"].lower() for tok in exclude if tok):
            continue
        # A workflow agent sits at <session>/subagents/workflows/<runId>/, so
        # its run is named by the directory holding it. Attaching here, where
        # the path is still in scope, avoids threading it through read_lane's
        # cache, which is keyed on the transcript's own size and mtime and
        # would serve a stale label after the run record was rewritten.
        if p.parent.parent.name == "workflows":
            run = wf_runs.get(p.parent.name)
            entry = (run or {}).get("by_agent_id", {}).get(
                lane["id"][len("agent-"):]) or {}
            lane["workflow"] = p.parent.name
            lane["wf_phase"] = entry.get("phaseTitle") or ""
            lane["wf_label"] = entry.get("label") or ""
            # The script's own label beats anything inferred from the prompt.
            # Workflow prompts open on a markdown heading, not "You are the X
            # agent", so the inferred name was the first 38 characters of the
            # brief: "## Adversarial Claim Verifier (voter 2".
            if lane["wf_label"]:
                lane["name"] = lane["wf_label"][:44]
                lane["role_derived"] = True
        else:
            lane["workflow"] = None
            lane["wf_phase"] = ""
            lane["wf_label"] = ""
        # Recency decides which SESSION is worth watching, never which of its
        # agents. discover() now returns a session together with its own
        # subagents, so an old worker is old because the work finished, not
        # because it belongs to somebody else — and dropping it left you
        # looking at an orchestrator with no agents under it, in a session that
        # plainly had fifteen.
        if (recent_s is not None and lane["is_root"]
                and lane["updated_s_ago"] > recent_s):
            continue
        lanes.append(lane)
    # Scope to the current session: a new Claude Code session is a new root, so
    # workers that began before it belong to a previous run and are dropped.
    # This is what makes the exhibit reset cleanly instead of accumulating.
    if session_only:
        root = next((l for l in lanes if l["is_root"]), None)
        if root and root["started"]:
            # Keep anything that cannot be dated. discover() already guarantees
            # these transcripts are the session's own, so an unparseable
            # timestamp is not grounds for hiding an agent — and the old form
            # dropped those lanes silently, because `None and ...` is falsy.
            lanes = [l for l in lanes
                     if l["is_root"] or not l["started"]
                     or l["started"] >= root["started"]]

    # Group agents into rounds of delegation, newest first: wave 0 is the batch
    # spawned most recently, wave 1 the one before it, and so on.
    #
    # Counting the user's messages directly does not work. Most messages spawn
    # nothing, so in a long session every agent sits thirty or forty messages
    # back and the board collapses to a lone orchestrator. What matters is how
    # many times you have delegated since, not how much you have typed.
    root = next((l for l in lanes if l["is_root"]), None)
    marks = sorted((root or {}).get("prompts") or [])
    for lane in lanes:
        lane["wave"] = (0 if lane["is_root"] or not lane["started"]
                        else sum(1 for m in marks if m > lane["started"]))

    rounds = sorted({l["wave"] for l in lanes if not l["is_root"]})
    rank = {w: i for i, w in enumerate(rounds)}
    for lane in lanes:
        lane["wave"] = 0 if lane["is_root"] else rank.get(lane["wave"], 0)

    # Order has to be stable. Sorting by how recently each transcript was
    # touched reshuffles the roster every second, so rows move out from under
    # the cursor as you go to click one. Spawn order never changes, and it is
    # also the order the tree draws them in.
    lanes.sort(key=lambda x: (x["started"] or "", x["ref"], x["id"]))
    _link_hierarchy(lanes)
    lanes.sort(key=lambda x: (x["depth"], x["started"] or "", x["id"]))

    # An agent's outcome is written in its parent's transcript, as the task
    # notification that came back when it finished. That is the only source
    # that distinguishes a clean return from a failure or a kill, so it wins
    # over the agent's own metadata, which only knows whether you stopped it.
    stated: dict[str, tuple[str, str]] = {}
    for lane in lanes:
        for aid, said in (lane.get("outcomes") or {}).items():
            word, when = said if isinstance(said, list) else (said, "")
            prev = stated.get(aid)
            if prev is None or (when or "") >= (prev[1] or ""):
                stated[aid] = (word, when or "")

    for lane in lanes:
        if lane["is_root"]:
            continue
        said = stated.get(lane["id"].removeprefix("agent-"))
        if not said:
            continue
        word, when = said
        # A stated outcome beats the clock. Deciding from how recently the file
        # was touched is an inference; a notification saying the agent was
        # killed is a fact, and for the first seconds after a kill the file is
        # freshly written, so the inference said "running" about an agent that
        # had already been told to stop.
        #
        # The exception is a resume. An agent can be restarted after it stops,
        # and then the old notification is stale. Writing materially later than
        # the notification is what tells them apart.
        notif = _parse_ts(when)
        wrote = _parse_ts(lane.get("last_seen"))
        resumed = (notif is not None and wrote is not None
                   and wrote > notif + timedelta(seconds=60))
        if resumed:
            continue
        lane["outcome"] = word
        lane["state"] = word
        # Keep the live flag consistent with the outcome, or the dot, the
        # colour and the live count all disagree with the word next to them.
        lane["status"] = "done"

    # An orphan is a worker still running, or finished, whose parent stopped
    # first. That is exactly the Haiku coordination failure, and it is worth
    # flagging because nothing else surfaces it.
    by_id = {l["id"]: l for l in lanes}
    for lane in lanes:
        parent = by_id.get(lane.get("parent") or "")
        lane["orphaned"] = bool(
            parent and parent["status"] == "done" and lane["status"] == "live"
        )

    by_model: dict[str, dict] = {}
    for lane in lanes:
        agg = by_model.setdefault(lane["model"], {
            "model": lane["model"], "lanes": 0, "billed_tokens": 0,
            "output_tokens": 0, "priced_usd": 0.0, "tool_count": 0,
            "spawns": 0, "turns": 0, "context_peak": 0,
        })
        agg["lanes"] += 1
        for k in ("billed_tokens", "output_tokens", "tool_count", "spawns", "turns"):
            agg[k] += lane[k]
        agg["context_peak"] = max(agg["context_peak"], lane["context_now"])
        agg["priced_usd"] = round(agg["priced_usd"] + lane["priced_usd"], 4)

    feed = []
    for lane in lanes:
        for t in lane["tools"]:
            feed.append({**t, "lane": lane["name"], "ref": lane["ref"],
                         "model": lane["model"]})
    feed.sort(key=lambda x: x["at"] or "", reverse=True)

    # One thread of everything the agents said to each other. Per-lane the
    # messages are fragments; ordered together they are a conversation, which
    # is the only form in which a delegation actually reads.
    dialogue = []
    named = {l["id"]: l["name"] for l in lanes}
    for lane in lanes:
        for m in lane["msgs"]:
            dialogue.append({
                "from": lane["name"],
                "from_id": lane["id"],
                "to": m.get("to") or "?",
                "kind": m.get("kind") or "send",
                "text": m.get("text") or "",
                "at": m.get("at"),
                "depth": lane["depth"],
            })
        # Only the parent issues Task and SendMessage calls, so a thread built
        # from those alone is one voice talking into the void. A subagent's last
        # words ARE its answer to whoever spawned it, so that is the reply leg —
        # without it the tab was six spawns from one speaker and no dialogue.
        if lane.get("parent") and lane["says"]:
            answer = lane["says"][-1]
            dialogue.append({
                "from": lane["name"],
                "from_id": lane["id"],
                "to": named.get(lane["parent"], "?"),
                "kind": "reply",
                "text": answer.get("text") or "",
                "at": answer.get("at"),
                "depth": lane["depth"],
            })
    dialogue.sort(key=lambda x: x["at"] or "")
    dialogue = dialogue[-120:]

    # What the agents actually said, interleaved across lanes. This is the
    # reasoning between tool calls and it is the point of watching at all.
    chatter = []
    for lane in lanes:
        # The root's "says" are its replies to the user — this conversation,
        # not agent work. Including them drowns the feed in meta-commentary.
        if lane["is_root"]:
            continue
        for s in lane["says"]:
            chatter.append({**s, "lane": lane["name"],
                            "live": lane["status"] == "live"})
    chatter.sort(key=lambda x: x["at"] or "", reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Lets the page reload itself when the dashboard file is edited, so a
        # CSS change lands in an open browser without a manual refresh.
        "ui_mtime": _ui_mtime(),
        "lanes": lanes,
        # Workflow runs belonging to the sessions in view, newest first. A run
        # only gets a record when it ENDS, so an in-flight run is absent here
        # while its agents are already present in `lanes`. The tab has to cope
        # with that rather than assume the two sets agree.
        "workflows": _workflow_summaries(wf_runs, lanes),
        "by_model": sorted(by_model.values(), key=lambda x: -x["billed_tokens"]),
        "totals": {
            "lanes": len(lanes),
            "billed_tokens": sum(l["billed_tokens"] for l in lanes),
            # The largest single call anyone made, over the whole run. This used
            # to read the largest CURRENT context, which is a different number:
            # a session that hit its window and compacted showed 137k for a call
            # that had actually reached 1,000,163.
            "context_peak": max((l["context_peak"] for l in lanes), default=0),
            # Each agent's final context is essentially its whole footprint:
            # system + tools + every turn it accumulated. Summing across agents
            # gives the run's real size, and it matches what the task
            # notifications report per subagent.
            "context_sum": sum(l["context_now"] for l in lanes),
            "output_tokens": sum(l["output_tokens"] for l in lanes),
            "tool_count": sum(l["tool_count"] for l in lanes),
            # Agents actually created, counted from the spawn records the
            # transcripts carry. This used to count lanes that had a parent,
            # which is a different quantity: it silently dropped every agent
            # the display had truncated away and every one whose link was not
            # recovered, and reported 14 for a run that spawned 36.
            "spawns": len({aid for l in lanes for aid in (l.get("spawned") or {})}),
            "priced_usd": round(sum(l["priced_usd"] for l in lanes), 4),
            "live": sum(1 for l in lanes if l["status"] == "live"),
        },
        "feed": feed[:60],
        "chatter": chatter[:40],
        # Every agent-to-agent message in one thread, oldest first, so it reads
        # as a conversation rather than as per-agent fragments.
        "dialogue": dialogue,
    }


# The project/session index, refreshed on a timer rather than per request.
_INDEX_CACHE: dict = {}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, source=None, limit=6, exclude=(), recent_s=None,
                 session_only=True, **kwargs):
        self._source = source
        self._limit = limit
        self._exclude = exclude
        self._recent_s = recent_s
        self._session_only = session_only
        super().__init__(*args, directory=str(HERE), **kwargs)

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        route, _, raw_query = self.path.partition("?")
        query = parse_qs(raw_query)

        # What projects and sessions exist. Walking every project folder costs
        # about a quarter second, so it is cached: sessions appear on disk on a
        # human timescale, not a polling one.
        if route == "/index.json":
            now = time.time()
            if not _INDEX_CACHE or now - _INDEX_CACHE["at"] > 15:
                _INDEX_CACHE["at"] = now
                _INDEX_CACHE["data"] = build_index()
            self._json(_INDEX_CACHE["data"])
            return

        # One run in full: the script it ran, what it returned, and every
        # agent's prompt and result preview. Fetched on click rather than
        # folded into /state.json, because that is polled every second and this
        # payload is roughly forty times the size of the summary.
        if route == "/workflow.json":
            want = (query.get("run") or [""])[0].strip()
            found = None
            for sdir in {p.parent.parent
                         for p in _projects_root().glob("*/*/workflows/wf_*.json")}:
                runs = _workflow_runs(sdir.with_suffix(".jsonl"))
                if want in runs:
                    found = runs[want]
                    break
            if not found:
                self._json({"ok": False, "run": want,
                            "reason": "no run record on disk. A run in flight "
                                      "has not written one yet."})
                return
            self._json({
                "ok": True,
                **{k: found[k] for k in
                   ("run_id", "name", "status", "summary", "started_ms",
                    "ended_iso", "duration_ms", "agent_count", "total_tokens",
                    "total_tools", "model", "error", "logs", "phases")},
                "agents": found["agents"],
                "result": found["result"],
                "script": found["script"][:40000],
                "script_truncated": len(found["script"]) > 40000,
            })
            return

        if route == "/state.json":
            scope = {
                "session": (query.get("session") or [""])[0].strip(),
                "projects": [s for s in
                             (query.get("project") or [""])[0].split(",") if s],
            }
            paths = scope_paths(scope, self._limit)
            if paths is None:                      # nothing picked: auto-detect
                paths = discover(self._source, self._limit)
            # An explicit pick is a request to see that thing, so the
            # "current session only" and recency filters must not veto it.
            explicit = bool(scope["session"] or scope["projects"])
            # Workflow scope is deliberately separate from the agent scope. The
            # tree answers "what is running now" and belongs to one session;
            # the runs list is a log, and the run worth opening is usually not
            # one this session produced.
            wf_scope = (query.get("wfscope") or ["session"])[0].strip()
            state = build_state(
                paths, self._exclude,
                None if explicit else self._recent_s,
                False if explicit else self._session_only,
                wf_scope=wf_scope)
            state["scope"] = scope
            state["wf_scope"] = wf_scope if wf_scope in ("project", "all") else "session"
            # How many agents the session really has, counted on disk rather
            # than inferred from what got rendered. The limit truncates the
            # file list, so a board showing 15 of 36 used to call itself
            # "16 of 16": the total was computed after the cut.
            # "subagents" not in parts, rather than a test on the immediate
            # parent: a workflow agent's parent is its wf_<id> directory, so
            # the old form treated every one of them as a session and called
            # _session_agents on it, which is how a ten-agent session reported
            # itself as "1 of 1".
            state["agents_on_disk"] = sum(
                len(_session_agents(p)) for p in paths
                if "subagents" not in p.parts)
            self._json(state)
            return

        if route == "/":
            self.path = "/dashboard.html"
        return super().do_GET()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """Silence per-request logging; the poll loop would flood the console."""
        return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=None,
                    help="a .jsonl file or a directory of them")
    ap.add_argument("--port", type=int, default=8770)
    # A long session spawns dozens: this one reached 36, and at the old default
    # of 16 the board hid 21 of them. Agent transcripts are small and cached, so
    # the cap is only there to stop something pathological, not to trim.
    ap.add_argument("--limit", type=int, default=200,
                    help="max transcripts to watch (default: 200)")
    ap.add_argument("--once", action="store_true",
                    help="print state as JSON and exit (no server)")
    ap.add_argument("--exclude", default="fable",
                    help="comma-separated model substrings to hide "
                         "(default: fable)")
    ap.add_argument("--recent", type=float, default=90.0,
                    help="only show transcripts touched in the last N minutes "
                         "(default: 90; 0 disables)")
    ap.add_argument("--all-sessions", action="store_true",
                    help="show agents from earlier sessions too. By default "
                         "the view resets per session: only the current root "
                         "and the workers it spawned are shown.")
    args = ap.parse_args()
    exclude = tuple(t.strip() for t in args.exclude.split(",") if t.strip())
    recent_s = args.recent * 60 if args.recent else None

    paths = discover(args.source, args.limit)
    if not paths:
        print("No .jsonl transcripts found. Pass --source explicitly.",
              file=sys.stderr)
        return 1

    if args.once:
        print(json.dumps(build_state(paths, exclude, recent_s,
                                     not args.all_sessions), indent=2))
        return 0

    print(f"watching {len(paths)} transcript(s):")
    for p in paths:
        print(f"  {p}")
    print(f"\n  http://localhost:{args.port}\n")

    handler = partial(Handler, source=args.source, limit=args.limit,
                      exclude=exclude, recent_s=recent_s,
                      session_only=not args.all_sessions)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
