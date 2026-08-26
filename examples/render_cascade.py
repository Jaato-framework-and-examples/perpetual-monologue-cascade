#!/usr/bin/env python3
"""Render every session of one cascade as one readable document.

    python3 -m examples.render_cascade [<cascade-id>] > cascade.md

Derived from `tools/dump_turns.py` in prime-agents-vs-jaato, which renders
ONE session: prose, every tool call with its arguments, every result.  The
driver's stdout reports outcomes; the transcript shows the reasoning that
produced them, and the two can disagree.

What is added here is the part a cascade needs and a single session cannot
have: **the siblings are rendered together.**  Sessions carry
``cascade_driver_id`` and ``sibling_name``, so they can be grouped and
labelled — and the tool calls in ``turn_accounting`` carry timestamps, so a
genuine cross-session TIMELINE can be built even though history rows
themselves are not timestamped.

That timeline is the whole point.  A writer asking for a snippet and the
coder answering are two events in two different transcripts, and neither
file alone shows the exchange.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

SESSIONS = Path(__file__).resolve().parent.parent / ".jaato" / "sessions"

#: Calls that cross a session boundary.  Listed explicitly rather than
#: inferred, so a new verb has to be added here deliberately.
HANDOFF_TOOLS = ("send_to_sibling", "list_siblings")


def _clean(text: str, workspace: Optional[str]) -> str:
    return text.replace(workspace, "<workspace>") if workspace else text


def _block(body: str, limit: int, workspace: Optional[str]) -> str:
    body = _clean((body or "").strip(), workspace)
    if len(body) > limit:
        body = body[:limit] + "\n… [truncated]"
    return f"```\n{body}\n```\n"


def _load(cascade: Optional[str]) -> List[dict]:
    """Every session of one cascade, oldest first.

    With no id, the most recent cascade that has more than one session —
    a single-session 'cascade' is not what this renderer is for.
    """
    docs = []
    for path in sorted(SESSIONS.glob("*.json")):
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    if cascade:
        return [d for d in docs if d.get("cascade_driver_id") == cascade]
    grouped: Dict[str, List[dict]] = {}
    for d in docs:
        cid = d.get("cascade_driver_id")
        if cid:
            grouped.setdefault(cid, []).append(d)
    if not grouped:
        return []
    # Prefer a cascade that actually RAN.  Ranking by session count alone
    # picks up the empty two-session cascades a failed probe leaves behind,
    # and renders a document whose every section says "0 model steps".
    def _weight(sessions):
        steps = sum(d.get("turn_count") or 0 for d in sessions)
        newest = max((d.get("created_at", "") for d in sessions), default="")
        return (steps, len(sessions), newest)

    return max(grouped.values(), key=_weight)


def _timeline(docs: List[dict]) -> List[str]:
    """Every cross-session call, in real time, across all siblings.

    Built from ``turn_accounting[*].function_calls``, whose records carry
    ``start_time``.  This is the only view in which one sibling's request
    and another's reply appear next to each other.
    """
    events = []
    for doc in docs:
        who = doc.get("sibling_name") or doc.get("session_id")
        for row in doc.get("turn_accounting") or []:
            for call in row.get("function_calls") or []:
                if not isinstance(call, dict):
                    continue
                if call.get("name") in HANDOFF_TOOLS:
                    events.append((call.get("start_time", ""), who,
                                   call.get("name"),
                                   call.get("duration_seconds", 0.0)))
    if not events:
        return ["## Handoffs", "",
                "No cross-session calls in this cascade — every sibling ran "
                "alone.", ""]
    events.sort()
    out = ["## Handoffs", "",
           "Cross-session calls in real time. This is the exchange itself; "
           "neither transcript below shows it alone.", "",
           "| time | sibling | call | seconds |", "|---|---|---|---|"]
    for when, who, name, secs in events:
        out.append(f"| {when[11:23]} | `{who}` | `{name}` | {secs:.1f} |")
    return out + [""]


def _steps(doc: dict) -> List[dict]:
    """One entry per model step, carrying a real timestamp.

    History rows are NOT timestamped — the module docstring says so, and it
    is why the Handoffs table was the only chronological view here. But the
    Nth tool-call part in ``history`` is the Nth record in
    ``turn_accounting[*].function_calls``, and those carry ``start_time``.
    Verified on run 34: conscient 24 call-parts against 24 records, same
    names in the same order; subconscient 11 against 11. So the alignment is
    positional and exact, not a heuristic match on name or content.

    A step whose model message made no call has no timestamp of its own. It
    INHERITS the last one seen in its own session and is flagged, because
    inventing a time would put prose in a place the evidence does not.
    """
    ta_calls = [c for row in doc.get("turn_accounting") or []
                for c in (row.get("function_calls") or [])
                if isinstance(c, dict)]
    steps: List[dict] = []
    idx = 0
    last_ts = ""
    for message in doc.get("history") or []:
        if message.get("role") != "model":
            continue
        prose, calls, ts = [], [], None
        for part in message.get("parts") or []:
            text = (part.get("text") or "").strip()
            if text:
                prose.append(text)
            if part.get("name"):
                when = ""
                if idx < len(ta_calls):
                    when = ta_calls[idx].get("start_time") or ""
                idx += 1
                if when and ts is None:
                    ts = when
                calls.append((part["name"], part.get("args") or {}))
        if not prose and not calls:
            continue
        exact = ts is not None
        if exact:
            last_ts = ts
        steps.append({"ts": ts or last_ts, "exact": exact,
                      "prose": "\n".join(prose), "calls": calls})
    return steps


def _cell(step: dict, workspace: Optional[str], limit: int = 420) -> str:
    """One step as a table cell: prose, then what it did."""
    body = _clean(step["prose"], workspace).strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + " …"
    bits = [body] if body else []
    for name, args in step["calls"]:
        detail = ""
        if name == "send_to_sibling":
            msg = _clean(str(args.get("message", "")), workspace)
            detail = f": {msg[:160]}…" if len(msg) > 160 else f": {msg}"
        elif args:
            detail = f": {_clean(json.dumps(args), workspace)[:90]}"
        bits.append(f"**→ `{name}`**{detail}")
    out = "<br><br>".join(bits).replace("|", "\\|").replace("\n", "<br>")
    return out or "&nbsp;"


def _side_by_side(docs: List[dict]) -> List[str]:
    """The two halves in parallel, in real time.

    The per-session sections below are complete but sequential: to see what
    the other half was doing while this one thought, you have to hold two
    places in one document. This puts them in one column each, merged on the
    timestamps recovered in ``_steps``, so a thought and the thought it
    crossed sit on the same row.
    """
    sibs = [d for d in docs if d.get("sibling_name")]
    if len(sibs) != 2:
        return []
    left, right = sibs[0], sibs[1]
    lname = left["sibling_name"]
    rname = right["sibling_name"]
    rows = ([(st["ts"], st["exact"], 0, st) for st in _steps(left)]
            + [(st["ts"], st["exact"], 1, st) for st in _steps(right)])
    if not rows:
        return []
    rows.sort(key=lambda r: (r[0], r[2]))
    out = ["## Side by side", "",
           f"Both halves in real time, `{lname}` left and `{rname}` right. "
           "Merged on per-step timestamps recovered by position from "
           "`turn_accounting`; a time in *italics* was inherited from the "
           "step before because that step made no call and history carries "
           "no clock of its own.", "",
           f"| time | `{lname}` | `{rname}` |", "|---|---|---|"]
    for ts, exact, side, step in rows:
        when = ts[11:19] if ts else "?"
        when = when if exact else f"*{when}*"
        cell = _cell(step, (left if side == 0 else right).get("workspace_path"))
        out.append(f"| {when} | {cell} |  |" if side == 0
                   else f"| {when} |  | {cell} |")
    return out + [""]


def _session(doc: dict) -> List[str]:
    workspace = doc.get("workspace_path")
    who = doc.get("sibling_name") or "(unaddressed)"
    out = [f"## `{who}` — session `{doc.get('session_id')}`", "",
           f"Profile `{doc.get('profile_name')}`, agent "
           f"`{doc.get('agent_name')}`, {doc.get('turn_count')} model steps.",
           ""]
    turn = 0
    for message in doc.get("history") or []:
        role = message.get("role")
        for part in message.get("parts") or []:
            if role == "user":
                turn += 1
                out += [f"### {who} · turn {turn} — input", "",
                        _block(part.get("text", ""), 1200, workspace)]
            elif role == "model":
                text = (part.get("text") or "").strip()
                if text:
                    out += ["> " + _clean(text, workspace).replace("\n", "\n> "), ""]
                if part.get("name"):
                    args = _clean(json.dumps(part.get("args") or {}), workspace)
                    if len(args) > 500:
                        args = args[:500] + " …"
                    out += [f"**`{part['name']}`**", f"```json\n{args}\n```", ""]
            elif role == "tool":
                result = part.get("result")
                if not isinstance(result, str):
                    result = json.dumps(result)
                flag = " — ERROR" if part.get("is_error") else ""
                out += [f"<sub>→ `{part.get('name')}`{flag}</sub>",
                        _block(result, 700, workspace)]
    return out


def render(cascade: Optional[str] = None) -> str:
    docs = _load(cascade)
    if not docs:
        return "No cascade sessions found under .jaato/sessions/."
    # Order by WHO SPOKE FIRST, not who was created first.
    #
    # A monologue driver creates the receiver before the sender, so that it
    # is addressable in time — which under created_at ordering prints the
    # reply ABOVE the send it answers. The reader then sees a sibling
    # responding to a thought that appears later in the document, which
    # reads as a stall or an out-of-order loop when it is neither.
    #
    # The discriminator is the FIRST INPUT, not a timestamp: a session the
    # driver kicked off opens with plain operator text, while one woken by a
    # sibling opens with an ⟦UNTRUSTED-EXTERNAL-CONTENT⟧ block. Timestamps
    # would be better, but turn_accounting is written on save and a session
    # saved mid-turn has none — the first input is always there.
    def _woken_by_sibling(d):
        for message in d.get("history") or []:
            if message.get("role") != "user":
                continue
            text = "".join(p.get("text") or "" for p in message.get("parts") or [])
            return (1 if "UNTRUSTED-EXTERNAL-CONTENT" in text else 0,
                    d.get("created_at", ""))
        return (2, d.get("created_at", ""))

    docs.sort(key=_woken_by_sibling)
    cid = docs[0].get("cascade_driver_id") or "?"
    roles = ", ".join(f"`{d.get('sibling_name') or '?'}`" for d in docs)
    out = [f"# Cascade `{cid[:12]}`", "",
           f"{len(docs)} session(s): {roles}.", ""]
    out += _timeline(docs)
    out += _side_by_side(docs)
    for doc in docs:
        out += ["---", ""] + _session(doc)
    return "\n".join(out)


def main() -> int:
    print(render(sys.argv[1] if len(sys.argv) > 1 else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
