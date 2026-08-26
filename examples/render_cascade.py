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
    hist_calls = [p for m in doc.get("history") or []
                  if m.get("role") == "model"
                  for p in (m.get("parts") or []) if p.get("name")]

    # THE ALIGNMENT MUST BE PROVEN PER SESSION, NOT ASSUMED.
    #
    # It held exactly on run 34 (24 against 24, 11 against 11) and I
    # generalised from that without testing a second cascade.  Run 36 says
    # otherwise: conscient 0 accounting records against 9 calls, curator 9
    # against 18, and run 34's own curator 40 against 0.  `turn_accounting`
    # is written on save, so a session saved mid-turn has less than its
    # history — and a session whose history was trimmed has more.
    #
    # Unguarded positional indexing does not merely MISS a timestamp there;
    # it silently attaches the WRONG one, which is worse than none because
    # the reader cannot see it happen.  So: align only over the overlap,
    # and only while the names still agree.  The first disagreement ends
    # the anchored region for that session.
    anchored = 0
    for a, b in zip(ta_calls, hist_calls):
        if a.get("name") != b.get("name"):
            break
        anchored += 1

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
                if idx < anchored:
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
    lsteps, rsteps = _steps(left), _steps(right)
    rows = ([(st["ts"], st["exact"], 0, st) for st in lsteps]
            + [(st["ts"], st["exact"], 1, st) for st in rsteps])
    if not rows:
        return []

    # A COLUMN WITH NO ANCHOR CANNOT BE PLACED AGAINST THE OTHER.
    #
    # Inheriting a time works WITHIN a session, where the steps are already
    # in order and an unanchored step sits between two known neighbours.
    # It does not work when a session has no anchored step at all: there is
    # nothing to inherit from, every row lands at the empty string, and the
    # whole column sorts to the top as though that half thought everything
    # before the other half began.  That is a fabricated chronology, and it
    # looks exactly like a real one — so refuse it and say why.
    lanch = sum(1 for st in lsteps if st["exact"])
    ranch = sum(1 for st in rsteps if st["exact"])

    # A COLUMN WITH NO ANCHOR AT ALL STILL GETS RENDERED — but by SEQUENCE,
    # and labelled as such.
    #
    # Inheriting a time works WITHIN a session, where an unanchored step
    # sits between two known neighbours.  It does not work when a session
    # has no anchored step anywhere: there is nothing to inherit, every row
    # lands on the empty string, and the column sorts to the top as though
    # that half thought everything before the other half began.  That is a
    # fabricated chronology that reads exactly like a real one.
    #
    # Refusing outright was the first fix and it was too blunt — an
    # incomplete view beats no view.  So such a column is SPREAD across the
    # other half's measured span in its own order, its time cell shows a
    # dash rather than a number, and the header says it is placed by
    # sequence.  The order within it is real; the position against the
    # other column is not, and nothing in the output implies otherwise.
    def _spread(steps: List[dict], anchors: List[str]) -> None:
        if not anchors or len(steps) < 1:
            return
        lo, hi = anchors[0], anchors[-1]
        for n, st in enumerate(steps):
            st["ts"] = lo if len(steps) == 1 else (
                lo if n * 2 < len(steps) else hi)
            st["placed"] = True

    lts = sorted(st["ts"] for st in lsteps if st["exact"])
    rts = sorted(st["ts"] for st in rsteps if st["exact"])
    if not lanch and rts:
        _spread(lsteps, rts)
    if not ranch and lts:
        _spread(rsteps, lts)

    rows = ([(st["ts"], st["exact"], 0, st) for st in lsteps]
            + [(st["ts"], st["exact"], 1, st) for st in rsteps])

    rows.sort(key=lambda r: (r[0], r[2]))
    def _cov(name, anch, steps):
        if anch:
            return f"`{name}` {anch}/{len(steps)} steps anchored"
        return (f"`{name}` has NO recoverable timestamp — its {len(steps)} "
                "steps are in their own true order but are placed against "
                "the other column by sequence, not measured")

    out = ["## Side by side", "",
           f"Both halves, `{lname}` left and `{rname}` right. Times come "
           "from `turn_accounting`, matched to history by position over the "
           "region where the two call sequences still agree: "
           f"{_cov(lname, lanch, lsteps)}; {_cov(rname, ranch, rsteps)}. "
           "A time in *italics* was inherited from the step before, because "
           "that step made no call and history carries no clock of its own; "
           "a dash means the step could not be timed at all.", "",
           f"| time | `{lname}` | `{rname}` |", "|---|---|---|"]
    for ts, exact, side, step in rows:
        if step.get("placed") or not ts:
            when = "—"
        else:
            when = ts[11:19] if exact else f"*{ts[11:19]}*"
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
