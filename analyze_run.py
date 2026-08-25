#!/usr/bin/env python3
"""analyze_run — answer, from one run's artifacts, the four questions the
framework owner asked about PR #617, plus the one nobody has an answer to.

    python3 analyze_run.py [<cascade-id>] [--log /tmp/mono.log]

Reads the saved session transcripts and the daemon log. Run `unload.py sub`
and `unload.py con` first, or the transcripts are stale — a session writes
its history on SAVE (§7.15).

Deliberately post-hoc rather than live: a send_to_sibling receipt is
readable only in the sending session's saved history, and the RECEIVING
side's merged batch is visible only as that session's turn input. Neither
is on the cascade event stream.
"""
import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(REPO, ".jaato", "sessions")

#: The daemon-side marker for a drained batch that exceeded the bound.
BOUND_WARN = re.compile(r"still had (\d+) queued after 100 passes")

#: Per-message drain marker: one line per message actually drained, with the
#: tier and the sender. `jaato_session.py:1598`. Written by `_trace`, which
#: goes to the PROVIDER TRACE file (not the daemon log) and is off unless
#: JAATO_PROVIDER_TRACE names a path.
DRAIN_ONE = re.compile(
    r"DRAIN_(?P<tier>\w+)_MESSAGE: agent_id=(?P<agent>[^,]*), "
    r"source_type=(?P<src>[^,]*), source_id=(?P<sid>[^,]*)")

#: #618. Daemon log, greppable with nothing configured — deliberately, so
#: the diagnostic is readable during the run it is needed for rather than
#: after it.
SIBLING_DELIVERY = re.compile(
    r"SIBLING_DELIVERY: from=(?P<frm>\S+) to=(?P<to>\S+) "
    r"target_session=(?P<sess>\S+) busy=(?P<busy>\S+) "
    r"thread_alive=(?P<alive>\S+) outcome=(?P<outcome>\S+)")

#: #618. The half neither instrument could see: whether a drain pass ran at
#: all, and what it found.
DRAIN_SUMMARY = re.compile(
    r"DRAIN_SUMMARY: agent_id=(?P<agent>\S+) queue_at_entry=(?P<entry>\d+) "
    r"drained=(?P<drained>\d+) passes=(?P<passes>\d+) "
    r"queue_at_exit=(?P<exit>\d+)")

#: Batch size, emitted once per drain. `jaato_session.py:1611`.
DRAIN_TOTAL = re.compile(r"DRAIN_MESSAGES: Processed (?P<n>\d+) messages total")

#: Counts SIBLING messages in a turn input — NOT batch size. Only
#: `deliver_sibling_message` wraps; `inject_prompt_to_session` and
#: `send_to_named_session` do not, deliberately (an authenticated operator
#: is not attacker-authored, and wrapping their words would teach the model
#: to discount a boundary that exists for hostile text). So a cross-tier
#: batch of {sibling message + watchdog nudge} shows ONE wrapper and reads
#: as unmerged — which is exactly the overlap most likely to happen, and
#: the reason this is a secondary signal rather than the detector.
WRAPPER = "⟦UNTRUSTED-EXTERNAL-CONTENT"


def _docs(cascade):
    out = []
    for path in sorted(glob.glob(os.path.join(SESSIONS, "*.json"))):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("cascade_driver_id"):
            out.append((os.path.getmtime(path), d))
    if cascade:
        return [d for _, d in out if d.get("cascade_driver_id") == cascade]
    if not out:
        return []
    newest = max(out, key=lambda t: t[0])[1].get("cascade_driver_id")
    return [d for _, d in out if d.get("cascade_driver_id") == newest]


def _turn_inputs(doc):
    """Each user-role turn input, in order."""
    for m in doc.get("history") or []:
        if m.get("role") == "user":
            yield "".join(p.get("text") or "" for p in m.get("parts") or [])


def _sends(doc):
    """(args, receipt) for every send_to_sibling this session made."""
    pending, out = None, []
    for m in doc.get("history") or []:
        for p in m.get("parts") or []:
            if p.get("name") != "send_to_sibling":
                continue
            if m.get("role") == "model":
                pending = p.get("args") or {}
            elif m.get("role") == "tool":
                out.append((pending, p.get("result")))
                pending = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cascade", nargs="?")
    ap.add_argument("--log", default="/tmp/mono.log")
    ap.add_argument("--trace", default="/tmp/mono-trace",
                    help="provider trace base path (JAATO_PROVIDER_TRACE); "
                         "per-agent variants are globbed")
    args = ap.parse_args()

    docs = _docs(args.cascade)
    if not docs:
        print("no cascade sessions found — did you run unload.py?")
        return 1
    cid = docs[0].get("cascade_driver_id")
    print(f"cascade {cid[:12]}  ({len(docs)} sessions)\n")

    # ---- 0. #618: the send, and whether a drain ever ran ----------------
    deliveries, drains = [], []
    if os.path.isfile(args.log):
        with open(args.log, errors="replace") as fh:
            for ln in fh:
                if "RPC_DIAG" in ln:
                    continue
                ts = ln[:23]          # "2026-08-25 23:03:46,123"
                d = SIBLING_DELIVERY.search(ln)
                if d:
                    deliveries.append(dict(d.groupdict(), ts=ts))
                k = DRAIN_SUMMARY.search(ln)
                if k:
                    drains.append(dict(k.groupdict(), ts=ts))

    print("== 0. delivery + drain (#618) ==")
    if not deliveries and not drains:
        print("  neither token present — pre-#618 daemon, or no sibling "
              "traffic. NOT evidence of anything.")
    for d in deliveries:
        # busy=True alive=True  -> peer genuinely mid-turn (a sleeping
        #                          evaluator counts, and is honest)
        # busy=True alive=False -> the flag outlived its thread
        note = ""
        if d["busy"] == "True":
            note = ("  peer mid-turn (honest)" if d["alive"] == "True"
                    else "  <-- STALE FLAG: busy with no live thread")
        print(f"  send {d['frm']}->{d['to']}: outcome={d['outcome']} "
              f"busy={d['busy']} thread_alive={d['alive']}{note}")
    for k in drains:
        entry, drained = int(k["entry"]), int(k["drained"])
        if entry == 0:
            verdict = "  <-- ran, queue EMPTY: message is not on the queue " \
                      "the drain reads (or never arrived)"
        elif drained == 0:
            verdict = "  <-- ran, SAW it, did not take it"
        else:
            verdict = "  collected"
        print(f"  drain {k['agent']}: entry={entry} drained={drained} "
              f"passes={k['passes']} exit={k['exit']}{verdict}")
    if deliveries and not drains:
        print("  !! a send was recorded but NO drain summary — the drain did "
              "not run on that turn")

    # ---- THE PAIRING: the queued send, and the FIRST drain after it ------
    #
    # There will be several DRAIN_SUMMARY lines. Only one answers the
    # question: the first drain on the TARGET session whose timestamp is
    # later than the `queued` receipt. Everything else is context. If that
    # drain reports queue_at_entry=0, the message is not on the queue the
    # drain reads (or never arrived); if entry>0 and drained=0, it saw it
    # and left it.
    print("\n  -- pairing (the queued send -> the next drain on its target) --")
    queued = [d for d in deliveries if d["outcome"] == "queued"]
    if not queued:
        print("  no `queued` outcome recorded — the queue branch was not "
              "exercised this run; the pairing has nothing to answer")
    for q in queued:
        later = [k for k in drains
                 if k["ts"] > q["ts"] and k["agent"] == q["to"]]
        print(f"  {q['ts']}  {q['frm']} -> {q['to']}  QUEUED "
              f"(busy={q['busy']} thread_alive={q['alive']})")
        if not later:
            print("     -> NO drain ran on the target after this. The turn "
                  "it was waiting on ended without one.")
            continue
        k = later[0]
        entry, drained = int(k["entry"]), int(k["drained"])
        verdict = ("MESSAGE NOT ON THE QUEUE THE DRAIN READS (or never arrived)"
                   if entry == 0 else
                   "SAW IT AND DID NOT TAKE IT" if drained == 0 else
                   "COLLECTED — the strand is gone")
        print(f"     -> {k['ts']}  drain entry={entry} drained={drained} "
              f"passes={k['passes']} exit={k['exit']}")
        print(f"        {verdict}")
    print()

    # ---- 2. turns per message, and 3. accepted-with-no-turn -------------
    sent_to, turns_of = {}, {}
    for d in docs:
        who = d.get("sibling_name")
        turns_of[who] = len(list(_turn_inputs(d)))
        for a, r in _sends(d):
            sent_to.setdefault((a or {}).get("sibling_name"), []).append(r)

    print("== 2. turns vs messages (should track 1:1) ==")
    for who in sorted(turns_of):
        got = len(sent_to.get(who, []))
        turns = turns_of[who]
        flag = "" if turns <= max(got, 1) else "  <-- TURNS OUTPACE MESSAGES"
        print(f"  {who:14} received {got:3}  turn inputs {turns:3}{flag}")

    print("\n== 3. receipts (accepted with NO turn is the bug) ==")
    for who, receipts in sorted(sent_to.items()):
        kinds = {}
        for r in receipts:
            st = r.get("status") if isinstance(r, dict) else "?"
            kinds[st] = kinds.get(st, 0) + 1
        print(f"  -> {who:14} {kinds}")
        if kinds.get("accepted", 0) > turns_of.get(who, 0):
            print(f"     !! {kinds['accepted']} accepted but only "
                  f"{turns_of.get(who, 0)} turns — a drive failed silently")

    # ---- 1. batch merging, and whether the reply addresses both ---------
    #
    # Counted from the drain trace, not from the transcript: the batch is
    # joined with a blank line and carries no sender boundary, so the text
    # alone cannot tell you how many messages made it — and a wrapper count
    # is blind to cross-tier merges (see WRAPPER above).
    print("\n== 1. merged batches (does the reply address BOTH?) ==")
    trace_paths = sorted(glob.glob(args.trace + "*"))
    if not trace_paths:
        print(f"  NO TRACE at {args.trace}* — set JAATO_PROVIDER_TRACE in "
              f".env before the run, or this question cannot be answered "
              f"at all. A wrapper count would understate it.")
    else:
        batches, pending = [], []
        for tp in trace_paths:
            with open(tp, errors="replace") as fh:
                for line in fh:
                    m = DRAIN_ONE.search(line)
                    if m:
                        pending.append((m.group("tier"), m.group("src"),
                                        m.group("sid")))
                        continue
                    t = DRAIN_TOTAL.search(line)
                    if t:
                        n = int(t.group("n"))
                        if n > 1:
                            batches.append((n, pending[-n:]))
                        pending = []
        if not batches:
            print(f"  none — every drain carried a single message "
                  f"({len(trace_paths)} trace file(s) read)")
        for n, msgs in batches:
            tiers = {t for t, _, _ in msgs}
            cross = "  CROSS-TIER" if len(tiers) > 1 else ""
            print(f"  batch of {n}{cross}: "
                  + ", ".join(f"{t}/{src}" for t, src, _ in msgs))
            print("     -> read this turn's reply and confirm it answers ALL "
                  "of them; a dropped one is invisible to any queue metric")

    # ---- 4. the 100-pass bound ------------------------------------------
    print("\n== 4. drain bound ==")
    hits = []
    if os.path.isfile(args.log):
        with open(args.log, errors="replace") as fh:
            hits = [ln.strip() for ln in fh if BOUND_WARN.search(ln)]
    print("  " + ("\n  ".join(hits) if hits
                  else f"clean (no bound warning in {args.log})"))

    # ---- 4b. how the run ENDED -----------------------------------------
    #
    # Without this a clean budget stop and a stall look identical in the
    # transcripts — the exact confusion that cost two runs today.
    print("\n== 4b. terminal condition ==")
    marks = []
    if os.path.isfile(args.log):
        with open(args.log, errors="replace") as fh:
            for ln in fh:
                # NOT a bare "budget_exhausted" substring: that also
                # matches the `session.get_budget_exhausted` RPC, which is
                # a POLL and says nothing about whether the ceiling fired.
                # Matching it reported "the budget did its job" for a run
                # that had been killed by hand — a confident wrong answer
                # from the instrument, which is worse than no answer.
                if "RPC_DIAG" in ln:
                    continue
                if ("cascade_budget_exhausted" in ln
                        or "CascadeExhaustedError" in ln
                        or "stopped at its budget ceiling" in ln
                        or 'reason="budget_exhausted"' in ln):
                    marks.append(ln.strip()[:160])
    if marks:
        print("  ceiling stopped it (budget did its job):")
        for m in marks[-3:]:
            print(f"    {m}")
    else:
        print("  no budget-exhaustion marker — the run ended some other way "
              "(stall, kill, or still running). NOT a clean stop.")

    # ---- 5. the genuine unknown: slow asymmetry over rounds -------------
    print("\n== 5. asymmetry over rounds (nobody has seen past round 1) ==")
    for d in docs:
        who = d.get("sibling_name")
        lens = [len((a or {}).get("message", "")) for a, _ in _sends(d)]
        if lens:
            trend = " ".join(str(x) for x in lens)
            print(f"  {who:14} message lengths by round: {trend}")
    print("  (one side growing while the other flattens is the asymmetry "
          "a single exchange cannot show)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
