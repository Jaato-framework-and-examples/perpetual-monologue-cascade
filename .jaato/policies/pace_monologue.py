"""Paces a perpetual sibling monologue at the permission boundary.

WHY HERE, and not in the driver. Four shipped properties make the
permission evaluator the right place for the throttle, and only the first
is load-bearing for the idea:

1. It runs RUNNER-SIDE. ``permission/runner_rpc_channel.py`` states the
   permission plugin runs inside the per-session confined runner, with only
   ASK decisions relaying to the daemon. So ``time.sleep()`` below blocks
   *this session's* tool-worker thread — not the daemon's event loop, not
   the sibling (a separate subprocess), and with parallel tools only one of
   eight workers. Were evaluators daemon-side, this idea would stall every
   session on the host.
   *** UNVERIFIED. Read from source, never observed. Nobody has run a slow
   evaluator and watched a sibling keep going. See README §5.4. ***
2. Evaluators run even for PRE-APPROVED tools (docs/permission-evaluators.md).
   Whitelisting ``send_to_sibling`` for headless autonomy does not bypass
   this script, so autonomy and pacing stop being in tension.
3. Module-level state persists per session — the script is loaded once and
   ``evaluate`` cached — so this is a real governor with a counter, not a
   blind sleep.
4. No watchdog. ``permission/evaluator.py`` contains no timeout, signal,
   alarm, or asyncio. Nothing will kill a sleeping evaluator. Nothing will
   save you from a ``sleep(3600)`` typo either.

NOT A SAFETY MECHANISM. Evaluators are fail-safe by design: any exception
is logged and becomes FALLBACK, and the profile whitelist then allows the
call. So a bug in this file means no delay and full-speed burn. The
cascade ceiling is the actual backstop (README §7.7). Treat this as an
optimiser.
"""
import os
import time

from shared.plugins.permission.evaluator import PolicyDecision, EvalResult

#: Seconds between this session's sends, and the thought ceiling, both read
#: from the profile's ``env:`` block. Per-profile because the two halves are
#: paced differently and an evaluator script takes no parameters — there is
#: nowhere else to put a per-half number.
_INTERVAL_VAR = "MONOLOGUE_INTERVAL_SECONDS"
_CEILING_VAR = "MONOLOGUE_THOUGHT_CEILING"

_last = 0.0
_count = 0


def _misconfigured(detail):
    """Fail LOUD rather than silently unpaced.

    The tempting alternative is a default interval, and it is wrong here: a
    missing env var would then be invisible, the loop would run at whatever
    the default happened to be, and the first symptom would be the bill.
    Denying stops the loop, surfaces the reason in the model's tool result,
    and trips the driver's watchdog. A misconfigured governor should not be
    indistinguishable from a working one.
    """
    return EvalResult(
        PolicyDecision.DENY_WITH_COMMENT,
        comment=(f"pace_monologue is misconfigured and the monologue cannot "
                 f"be paced: {detail}. This is an operator error, not "
                 f"something to work around — stop sending and report it."),
    )


def evaluate(tool_name, args, context):
    global _last, _count

    raw_interval = os.environ.get(_INTERVAL_VAR)
    raw_ceiling = os.environ.get(_CEILING_VAR)
    if raw_interval is None or raw_ceiling is None:
        return _misconfigured(
            f"{_INTERVAL_VAR} and {_CEILING_VAR} must both be set in the "
            f"profile's env: block")
    try:
        interval = float(raw_interval)
        ceiling = int(raw_ceiling)
    except ValueError:
        return _misconfigured(
            f"{_INTERVAL_VAR}={raw_interval!r} / {_CEILING_VAR}={raw_ceiling!r} "
            f"are not a float and an int")

    _count += 1

    if _count > ceiling:
        # Soft wind-down, in words the model can act on. The HARD stop is
        # the cascade ceiling; this exists so the mind gets to choose how to
        # spend its last thoughts rather than being cut off mid-sentence.
        return EvalResult(
            PolicyDecision.DENY_WITH_COMMENT,
            comment=(f"Thought budget spent ({ceiling}). Stop sending and let "
                     f"the stream settle; record anything worth keeping with "
                     f"the memory tool first."),
        )

    wait = interval - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)          # blocks this session only — see property 1
    _last = time.monotonic()

    # ALLOW_WITH_COMMENT gives the loop awareness of its own mortality: the
    # comment lands in the tool result the model reads, so the governor
    # reports remaining budget back into the stream without the driver being
    # in the loop. The mind can then spend its last hundred thoughts
    # consolidating rather than wandering.
    return EvalResult(
        PolicyDecision.ALLOW_WITH_COMMENT,
        comment=f"thought {_count}/{ceiling}",
    )
