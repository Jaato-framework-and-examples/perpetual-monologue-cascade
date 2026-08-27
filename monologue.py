#!/usr/bin/env python3
"""Perpetual monologue driver.  Opens, observes, nudges, reaps.

Two sessions — `conscient` and `subconscient` — keep each other alive by
ending every turn with `send_to_sibling`.  A message to an IDLE sibling
starts a turn on it, so the exchange is self-sustaining and this driver is
not in the loop.  After kickoff it makes exactly one kind of outbound call:
a nudge when the stream has gone silent.

WHAT IS VERIFIED AND WHAT IS NOT (README §4.1, §7.15):
  - `accepted` starts a turn on the peer — CERTIFIED on jaato 4138a9a5.
  - An idle sibling stays loaded across a second session's creation —
    MEASURED on 4138a9a5.
  - Everything in the shutdown path below — that a budget ceiling emits
    SessionTerminatedEvent(reason="budget_exhausted") to a cascade observer
    at all, that `details` carries per-dimension usage — is READ FROM
    SOURCE AND NEVER OBSERVED.  It runs once, at the end, when nobody is
    watching.  Treat a clean exit as unproven until you have seen one.
"""
import asyncio
import contextlib
import signal
import json
import os
import time
import uuid

from jaato_sdk import (ClientType, EventType, IPCRecoveryClient,
                       SessionCreateFailed)

REPO = os.path.dirname(os.path.abspath(__file__))

#: Points AT `.jaato`, not the repo root: profile discovery scans
#: `<config_root>/profiles`, so the root finds nothing — and finds it
#: WITHOUT an error, because an empty profile set is legal.
CONFIG_ROOT = os.path.join(REPO, ".jaato")
ENV_FILE = os.path.join(REPO, ".env")


def _load_env_file(path):
    """Read .env into this process, before any module constant reads it.

    The daemon reads this same file to build each SESSION's env; these
    clients need it for their OWN constants (socket path, ceiling), which
    are bound at import — so the load has to happen here, above them.
    Existing environment wins, so `VAR=x python3 <script>` still overrides.
    """
    if not os.path.isfile(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split("   #")[0].strip()
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file(ENV_FILE)

#: Identifies this run's archived artifacts.
_run_id = time.strftime("%H%M%S")

#: percent_used probe state — events seen per agent, and how many
#: zero-readings have been reported (capped, so a genuinely-unknown
#: provider does not fill the log with a correct answer).
_pct_seen: dict = {}
_pct_zero: dict = {}

#: Short on purpose: AF_UNIX caps a socket path at 108 bytes and fails
#: opaquely past it.
SOCKET = os.environ.get("JAATO_IPC_SOCKET", "/tmp/monologue.sock")

#: Seconds of event silence before the watchdog nudges.  Generous: a paced
#: volley is 30s on the conscient side plus a model turn, so anything under
#: ~90s would nudge a healthy loop.
STALL_AFTER = 180.0

#: The ceiling.  This is not a safety net, it is the business model — you
#: are paying for a mind to exist rather than to answer.  It lives in .env
#: rather than here so that changing what you are willing to spend is a
#: config edit, and so that a missing value stops the run instead of
#: quietly selecting someone else's idea of a reasonable bill.
def _ceiling():
    usd = os.environ.get("MONOLOGUE_CEILING_USD")
    turns = os.environ.get("MONOLOGUE_CEILING_TURNS")
    if usd is None or turns is None:
        raise SystemExit(
            "MONOLOGUE_CEILING_USD and MONOLOGUE_CEILING_TURNS must be set "
            "in .env. There is no default: the ceiling is the only real "
            "stop this design has (README §7.7), and guessing it for you "
            "would be guessing how much money you are willing to lose.")
    return {"usd": float(usd), "turns": int(turns)}


def _stamp():
    return time.strftime("%H:%M:%S")


#: Streaming deltas arrive as many AGENT_OUTPUT events per utterance — the
#: first run rendered one token per line, which is unreadable and hides the
#: shape of a thought. Buffer per agent, flush when that agent stops.
_buf = {}

#: session_ids whose history should be flushed at the next safe point.
_pending_saves = []


def _flush(who):
    text = "".join(_buf.pop(who, [])).strip()
    if text:
        print(f"\n[{_stamp()}] {who}:\n{text}\n", flush=True)


def render(ev):
    """The monologue as one document, both halves interleaved.

    An attached client's events() sees only its own session; this is the
    cascade stream, so it sees both. That interleaving IS the trajectory
    view — the thought stream reassembled from two halves that each only
    saw their own side.
    """
    kind = getattr(ev, "type", None)
    who = getattr(ev, "agent_id", None) or "?"
    if kind == EventType.AGENT_OUTPUT:
        _buf.setdefault(who, []).append(getattr(ev, "text", "") or "")
    elif kind == EventType.TOOL_CALL_END and getattr(ev, "tool_name", "") == "send_to_sibling":
        _flush(who)
        ok = "→" if getattr(ev, "success", False) else "✗"
        print(f"[{_stamp()}] {who} {ok} send_to_sibling", flush=True)
        # The receipt is readable ONLY in the sending session's saved
        # history, and history is written on SAVE. Runs 6-8 lost every
        # subconscient receipt this way. `session.save` (#617) is the
        # supported flush; attaching away is not — on an already-cold
        # session it restores the STALE file and writes it back, so it
        # round-trips old data over new.
        # THROTTLED: at most one queued save per session. Saving after
        # EVERY send raced the framework's own turn-end save on the same
        # per-session `.json.tmp` path — the first rename won, the second
        # got ENOENT, and the transcript silently fell behind while the
        # log said the save had failed. A stale transcript read afterwards
        # is evidence about nothing, which cost a whole run's worth of
        # conclusions.
        # DISABLED for the loop-pressure test. `session.save` fetches the
        # runner's history through the DAEMON LOOP, and run 21 attributed
        # 5 of 7 loop stalls to `session_get_history` against 2 to
        # `session_offer_message` — so the saves are the dominant load and
        # the deliveries are collateral. Turning them off separates my
        # instrumentation from the framework's loop: if the stalls vanish,
        # the contention was mine.
        if os.environ.get("MONOLOGUE_SAVE_PER_SEND") == "1":
            sid = getattr(ev, "session_id", None)
            if sid and sid not in _pending_saves:
                _pending_saves.append(sid)
    elif kind == EventType.AGENT_STATUS_CHANGED and getattr(ev, "status", "") == "done":
        _flush(who)


def render_ceiling(details):
    d = details or {}
    print(f"\n[{_stamp()}] ── the ceiling ──", flush=True)
    print(f"  {d.get('reason', 'budget exhausted')}", flush=True)
    for dim, used in sorted((d.get("usage") or {}).items()):
        print(f"  {dim}: {used}", flush=True)


def render_cold(ev):
    print(f"[{_stamp()}] !! a half went cold: {ev.error_message}", flush=True)
    print("   Two causes, and they want opposite responses. Either the driver "
          "attached away (§5.6) — this driver's one such gesture was measured "
          "harmless, so suspect your own code — or the daemon loop stalled and "
          "the model thread's bare `except Exception` terminated the session "
          "(§7.18). Tell them apart:", flush=True)
    print("     grep MODEL_THREAD_TERMINAL_ERROR <daemon log>", flush=True)
    print("   Present: the framework killed it, nothing here did. Absent: "
          "something attached away. Either is revivable with `session.wake`, "
          "which needs no attachment (§11 Q2).", flush=True)


def render_backpressure(ev):
    print(f"[{_stamp()}] .. backpressure: {ev.error_message}", flush=True)


def new_client():
    """IPCRecoveryClient, not IPCClient: this process is long-lived by
    definition and must survive a daemon restart."""
    return IPCRecoveryClient(
        SOCKET,
        client_type=ClientType.API,   # API keeps signal_completion; TERMINAL/WEB strip it
        auto_start=False,             # start your own daemon; see RUNNING.md
        env_file=ENV_FILE,            # never None — the handshake crashes on None
        workspace_path=REPO,
        config_root=CONFIG_ROOT,
    )


async def main():
    client = new_client()
    if not await client.connect(timeout=120.0):
        raise SystemExit("daemon did not start; run jaato-doctor")

    cid = uuid.uuid4().hex
    print(f"[{_stamp()}] cascade {cid}", flush=True)

    # Ceiling FIRST: sessions created under this cid are clamped to
    # min(profile, cascade_remaining) at spawn, and a cid with no headroom
    # REFUSES the spawn rather than starting something that cannot run.
    limits = _ceiling()
    print(f"[{_stamp()}] ceiling {limits}", flush=True)
    await client.cascade_budget_set(cid, limits=limits)

    # subconscient FIRST so it is addressable before conscient sends to it.
    #
    # Creating the conscient attaches this client to it, which is an
    # attach-away from the subconscient — the same gesture the coordination
    # example uses DELIBERATELY to put a sibling to sleep. It was measured
    # not to unload an idle peer on 4138a9a5 (README §11 Q2), which is why
    # this order is safe as written rather than merely untested.
    # `create_session` RAISES now (SDK #635) — it returns `str`, not
    # `Optional[str]`. The old `if not sub or not con or not cur` guard
    # below was dead the moment that shipped: the exception fires on the
    # first failing create, before any check of the three.
    #
    # And a half-built cascade LEAKS. Creation happens outside the
    # try/finally that owns shutdown, so a failure at `con` left `sub`
    # running with its own runner subprocess and pool slot, and the driver
    # exited without ever reaching `disconnect()`. That predates #635; the
    # raise only makes it louder. The runner pool has no ceiling, so this
    # bench was quietly able to manufacture the leak the pool-ceiling work
    # is about.
    created = []

    async def _start(label, **kwargs):
        try:
            sid = await client.create_session(timeout=60.0, **kwargs)
        except SessionCreateFailed as exc:
            print(f"[{_stamp()}] !! {label} did not start: {exc}", flush=True)
            print(f"   cause={exc.cause} may_exist={exc.may_exist}", flush=True)
            if exc.may_exist:
                # NOT a retry candidate. `session.new` has no idempotency
                # key, so a blind retry on an unknown outcome creates a
                # SECOND session with its own runner and slot — the leak
                # this handler exists to avoid, doubled.
                print("   the daemon may hold a session for this profile "
                      "anyway; check `jaato-doctor` before rerunning.",
                      flush=True)
            await _unload(created)
            raise SystemExit(f"cascade not started ({label})")
        created.append((label, sid))
        return sid

    async def _unload(sessions):
        """Give back what was already built.

        Same gesture as the shutdown path below: `attach_session` unloads
        the session the client LEAVES, so attaching through them in turn
        and then disconnecting unloads each one. Never `delete_session` —
        that destroys the transcript instead of saving it.
        """
        for label, sid in sessions:
            print(f"[{_stamp()}] .. unloading orphaned {label}", flush=True)
            with contextlib.suppress(Exception):
                await client.attach_session(sid)
        with contextlib.suppress(Exception):
            await client.disconnect()

    sub = await _start("subconscient", profile="subconscient",
                       agent="subconscient", sibling_name="subconscient",
                       cascade_driver_id=cid)
    con = await _start("conscient", profile="conscient",
                       agent="conscient", sibling_name="conscient",
                       cascade_driver_id=cid)
    # THE THIRD RESIDENT FACULTY. Same cid — so it is inside the one budget
    # ceiling and on the one event stream — but NO sibling_name, so it does
    # not appear in list_siblings and neither half can address it. The thing
    # that judges the memories is not reachable by the things that write
    # them. Like the halves it declares no completion schema and so cannot
    # decide it is finished; unlike them it has no send invariant, because
    # it is woken rather than self-sustaining.
    cur = await _start("curator", profile="curator", agent="curator",
                       cascade_driver_id=cid)

    # EXPLICIT, not incidental. create_session attaches the creating client
    # (session_manager.py:6057), so without this the driver is attached to
    # whichever session it happened to create last and inject_prompt targets
    # it by luck. Here it happens to be a no-op — conscient was created last
    # and is already current — and saying so is the point: the coordination
    # example lost hours to an attach that "left nothing".
    await client.attach_session(con)

    # The whisper client's address book.
    with open(os.path.join(CONFIG_ROOT, "monologue.json"), "w") as fh:
        json.dump({"cid": cid, "conscient_session_id": con,
                   "subconscient_session_id": sub,
                   "curator_session_id": cur,
                   "started_at": time.time()}, fh)

    last_activity = time.monotonic()
    shutdown = asyncio.Event()      # set by the ceiling; awaited by both tasks

    # SIGTERM MUST REACH THE SAME SHUTDOWN PATH AS THE CEILING.
    # Killing the driver does NOT stop its sessions — they stay loaded and
    # keep volleying at each other with nobody watching, and their sends
    # then fail into the next run's log as ghosts ("could not be reached").
    # Several runs' worth of noise came from exactly that. A signal handler
    # that sets `shutdown` lets the `finally` unload both halves, which is
    # the difference between stopping a driver and stopping a mind.
    # Setting the flag is NOT enough on its own: observe() parks on
    # `async for ev in stream`, and a flag it never gets to re-check leaves
    # the turn hanging before the unload block — a graceful stop that
    # degrades into kill -9 and leaves sessions loaded, which is the ghost
    # problem the flag was added to close. So the handler also CANCELS the
    # observer task, which unblocks the iterator and lets the finally run.
    loop = asyncio.get_running_loop()
    tasks: list = []

    def _stop():
        shutdown.set()
        for t in tasks:
            t.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    async def on_failed_send(ev):
        # The receipt STATUS is not on the event — only the prose the
        # receipt's `error` key carried (jaato_session.py:6311). So this
        # matches a sentence, which is brittle; see README §7.12.
        msg = ev.error_message or ""
        if "is resting (unloaded)" in msg:
            render_cold(ev)
        elif "has not been idle since" in msg:
            render_backpressure(ev)     # peer alive and busy; "Let it work."
        else:
            # NOT a shutdown. Run 12 died here on a MODEL error — the
            # subconscient emitted send_to_sibling with no sibling_name —
            # and this branch tore down a healthy nine-send loop over a
            # malformed tool call the model would have retried. A failed
            # send is the sender's problem to correct; the ceiling is the
            # terminator, and silence is the watchdog's job. The driver
            # does not get to end the mind over a typo.
            print(f"[{_stamp()}] .. failed send (recoverable): {msg}",
                  flush=True)

    async def observe():
        nonlocal last_activity
        # aclosing(): `async for ... break` does NOT run the iterator's
        # finally, so cascade.unregister would wait for GC or the 50ms
        # disconnect backstop (ipc.py:2291, "Cleanup contract").
        async with contextlib.aclosing(
                client.cascade_events(cid, event_types=None,
                                      role="owner")) as stream:
            async for ev in stream:
                if shutdown.is_set():
                    return          # signal arrived; let the finally reap
                render(ev)
                while _pending_saves:
                    with contextlib.suppress(Exception):
                        await client.execute_command(
                            "session.save", args=[_pending_saves.pop(0)])
                # Only a THOUGHT resets the stall clock. Keying on any event
                # made the watchdog useless in run 1: the cascade stream
                # emits status/telemetry events continuously, so a mind that
                # had stopped thinking still looked alive and the nudge never
                # fired (it was 40s overdue when the run was stopped).
                if ((getattr(ev, "type", None) == EventType.AGENT_OUTPUT
                     and (getattr(ev, "text", "") or "").strip())
                        or (getattr(ev, "type", None) == EventType.TOOL_CALL_END
                            and getattr(ev, "tool_name", "") == "send_to_sibling")):
                    last_activity = time.monotonic()

                # PERCENT_USED PROBE. This field rides the event stream and
                # is NEVER written daemon-side, so grepping the daemon log
                # for it returns zero whatever is happening — a blind
                # instrument reporting a clean result. That zero nearly went
                # upstream as a passed expectation.
                #
                # BOTH numbers or neither. `percent_used == 0` means two
                # opposite things and one field cannot separate them: with a
                # real `context_limit` it is a stale cache and a regression;
                # with `context_limit == 0` it is an honest-unknown provider
                # (#541) correctly declining to divide by a denominator it
                # does not have. Printing the percentage alone would rebuild
                # the same trap one layer up.
                if getattr(ev, "type", None) in (EventType.TURN_PROGRESS,
                                                 EventType.CONTEXT_UPDATED):
                    pct = getattr(ev, "percent_used", None)
                    lim = getattr(ev, "context_limit", None)
                    who = getattr(ev, "agent_id", "?")
                    _pct_seen[who] = _pct_seen.get(who, 0) + 1
                    if pct == 0 and _pct_zero.get(who, 0) < 3:
                        _pct_zero[who] = _pct_zero.get(who, 0) + 1
                        verdict = ("honest-unknown: no context limit to "
                                   "divide by" if not lim else
                                   "STALE CACHE — a real limit with 0% used")
                        print(f"[{_stamp()}] ?? {who} percent_used=0 "
                              f"context_limit={lim} ({verdict}) "
                              f"[event {_pct_seen[who]}]", flush=True)

                # THE CEILING. A budget refusal runs no turn and produces no
                # turn-completion notification, so this event is the ONLY
                # in-band signal that the mind is over (core.py:4308-4348).
                if (getattr(ev, "type", None) == EventType.SESSION_TERMINATED
                        and getattr(ev, "reason", None) == "budget_exhausted"):
                    render_ceiling(getattr(ev, "details", None))
                    shutdown.set()
                    return

                # THE OTHER WAY THE CEILING ARRIVES (#611, a49e6adf).
                # A budget-refused SPAWN used to reach only the requesting
                # client, so a driver watching the cid saw a session that
                # never appeared and no reason — indistinguishable from a
                # hang (§7.16). It now dispatches to cascade observers with
                # structured evidence, so "the budget did its job" and
                # "something broke" stop looking the same.
                if (getattr(ev, "type", None) == EventType.ERROR
                        and getattr(ev, "error_type", "") == "CascadeExhaustedError"):
                    d = getattr(ev, "details", None) or {}
                    print(f"\n[{_stamp()}] ── ceiling refused a spawn ──\n"
                          f"  exhausted: {d.get('exhausted_dimensions')}\n"
                          f"  remaining: {d.get('cascade_remaining')}\n"
                          f"  session:   {getattr(ev, 'session_id', None)}",
                          flush=True)
                    shutdown.set()
                    return

                # THE MIND REMEMBERED SOMETHING -> WAKE THE CURATOR.
                # `session.wake` and not inject_prompt: inject targets the
                # client's ATTACHED session, and this client's one
                # attachment belongs to the conscient (§8.4). wake takes an
                # explicit session_id, and revives a cold target — so the
                # curator being unloaded between curations is harmless.
                # Fired on the RESULT, never on the call: a store that
                # failed is not something to curate.
                if (getattr(ev, "type", None) == EventType.TOOL_CALL_END
                        and getattr(ev, "tool_name", "") == "store_memory"
                        and getattr(ev, "success", False)):
                    print(f"[{_stamp()}] .. memory stored; waking the curator",
                          flush=True)
                    with contextlib.suppress(Exception):
                        await client.execute_command("session.wake", payload={
                            "session_id": cur,
                            "text": "The mind has just stored something. "
                                    "Curate what is waiting.",
                            "source": "user",
                        })

                # A failed send is still a CALL, so the persona invariant is
                # satisfied while nothing was delivered and nothing will wake
                # the peer (plugin.py:1117 returns (False, receipt) for
                # refused / sibling_cold / no_such_sibling).
                if (getattr(ev, "type", None) == EventType.TOOL_CALL_END
                        and getattr(ev, "tool_name", "") == "send_to_sibling"
                        and not getattr(ev, "success", True)):
                    await on_failed_send(ev)

    async def watchdog():
        # nonlocal: this coroutine RESETS the stall clock after nudging, and
        # assigning it without this declaration makes it a local and shadows
        # the one observe() maintains.
        nonlocal last_activity
        # Silence, not coldness, is what this watches: a loaded sibling does
        # not rest on its own, so a quiet stream means a turn ended without
        # a send, not a peer that unloaded.
        while True:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=30.0)
                return                  # the ceiling fired; stop nudging
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last_activity > STALL_AFTER:
                # Nudging PAST the ceiling live-locks: an exhausted session
                # refuses every further turn (the abort rung latches the
                # reason, jaato_session.py:8233) and each refusal emits an
                # event that resets last_activity. Hence the shutdown gate
                # above rather than a blind sleep (README §7.13).
                print(f"[{_stamp()}] .. stream quiet, nudging", flush=True)
                # READ THE STATUS. Before #619 this returned nothing and the
                # nudge was a black hole that said "ok" — the driver could
                # not tell a delivered nudge from one that fell into a dead
                # queue, which is why runs 8-11 showed silence and it was
                # read as the framework's fault rather than as no answer.
                #   accepted   a turn was started
                #   queued     delivered, waiting on a turn that may not come
                #   terminated the half is gone — stop nudging, say so
                #   None       NOT TOLD (old daemon, or timeout). Not the
                #              same as not delivered; do not report it as one.
                status = await client.inject_prompt(
                    "The stream has gone quiet. Resume: send your next thought "
                    "onward now.", source_type="user")
                print(f"[{_stamp()}] .. nudge -> {status!r}", flush=True)
                if status == "terminated":
                    print(f"[{_stamp()}] the conscient is terminated; "
                          f"nudging cannot help. Stopping.", flush=True)
                    shutdown.set()
                    return
                last_activity = time.monotonic()

    # The one outbound call into the loop. Everything after is observation.
    # The kickoff is the FIRST THOUGHT'S PROMPT, not an instruction to write
    # a message. "Send your first thought to subconscient" framed the whole
    # stream as correspondence and contradicted the persona in the same
    # breath — the model was told to think freely and then addressed as a
    # correspondent by the only sentence it had to go on.
    await client.send_message(
        "Begin thinking. Follow whatever is actually on your mind, one "
        "thought leading to the next.")

    try:
        tasks.extend((asyncio.create_task(observe()),
                      asyncio.create_task(watchdog())))
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        print(f"\n[{_stamp()}] interrupted", flush=True)
    finally:
        # REACHABLE, which it was not in the design's first draft: the
        # ceiling refuses turns, it does not reap sessions, and §8.4's
        # attachment pins the conscient in memory until someone does.
        # UNLOAD, never delete. `delete_session` stops a half AND DESTROYS
        # the record of what it did — the coordination example lost its
        # coder transcripts that way, and this driver lost run 1's receipts
        # to the same call. The transcript is the only place a
        # send_to_sibling receipt is readable (no cascade event carries it),
        # so deleting is deleting the evidence.
        #
        # Attaching away is what unloads: `attach_session` unloads the
        # session the client LEAVES. So attach to the subconscient, then
        # back to the conscient — that saves the subconscient — and let
        # disconnect save the conscient on the way out.
        # WHAT THE PROBE SAW, including when it saw nothing wrong.
        #
        # A probe that only speaks on a hit is indistinguishable from a
        # probe that never ran — which is the exact failure it was added to
        # close. So it reports its own coverage: "N events, 0 zero-readings"
        # is a null with a working instrument behind it, and "0 events" says
        # the stream carried none and the question is still open.
        if _pct_seen:
            for who in sorted(_pct_seen):
                z = _pct_zero.get(who, 0)
                print(f"[{_stamp()}] .. percent_used probe: {who} "
                      f"{_pct_seen[who]} progress/context events, "
                      f"{z or 'no'} zero-reading{'' if z == 1 else 's'}",
                      flush=True)
        else:
            print(f"[{_stamp()}] .. percent_used probe: NO progress or "
                  "context events reached the driver — the expectation is "
                  "untested, not passed.", flush=True)

        # ARCHIVE THE TRACES BEFORE THE NEXT RUN CAN OVERWRITE THEM.
        # A fixed trace path plus `rm` before each run destroyed run 21's
        # fourteen-stall sample — the one that would have answered whether
        # the daemon-loop stall tracks output volume. Optimising for a clean
        # read instead of a comparable one.
        # THREE, not two. The curator was left out of this loop while it was
        # the only session with no trace to archive; now that it has one, an
        # omission here would quietly overwrite it on the next run — the same
        # way a fixed path plus `rm` destroyed run 21's sample.
        # THREE sessions, and the curator writes TWO files: the provider
        # trace (budget/tokens) and the PLUGIN trace, which is the only one
        # carrying `update_memory: id=..., maturity=...`. Archiving one and
        # not the other would leave the answer to be overwritten by the next
        # run — the same way a fixed path plus `rm` destroyed run 21's sample.
        _traces = [f"/tmp/mono-trace-{_h}.log"
                   for _h in ("conscient", "subconscient", "curator")]
        _traces.append("/tmp/mono-plugin-curator.log")
        for src in _traces:
            with contextlib.suppress(Exception):
                if os.path.isfile(src):
                    stem = src[:-len(".log")]
                    os.replace(src, f"{stem}-{_run_id}.log")
        print(f"[{_stamp()}] unloading both halves (saving transcripts)",
              flush=True)
        with contextlib.suppress(Exception):
            await client.attach_session(cur)
            await client.attach_session(sub)   # leaving cur saves it
            await client.attach_session(con)   # leaving sub saves it
        await client.disconnect()              # leaving con saves it


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
