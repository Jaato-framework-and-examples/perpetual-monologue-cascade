#!/usr/bin/env python3
"""switch_model — change a live half's model, and watch what that breaks.

    ./switch_model.py subconscient anthropic/claude-sonnet-5
    ./switch_model.py conscient --list

WHY THIS EXISTS, AND IT IS NOT FOR CHANGING MODELS. `/model select`
INVALIDATES `_cached_context_limit` (core.py:5692) — one of the four
documented entrances to the trap #633 fixed. In a healthy session
`initialize()` fills that cache before any progress event fires
(core.py:2596), so the miss path at core.py:3297 NEVER RUNS and a whole
clean run says nothing about it. This is the only cheap way to make it run.

Expect, on the next progress events after a switch:
    at most ONE reading with percent_used=0 AND context_limit=0 together
    (honest-unknown, #541), then a healed non-zero.
A PERSISTENT zero, or a ~10s stall around the switch, means the fix
regressed. Watch the driver's own `?? ... percent_used=0` lines for it —
this tool changes the state; monologue.py is what observes the result.

THE RESULT ARRIVES AS AN EVENT, NOT A RETURN VALUE. `execute_command`
returns None whatever happens (jaato-tui/backend.py:455 does the same),
so a caller that does not subscribe cannot tell a completed switch from a
rejected one. Not academic: the first version of this tool sent
`args=[model]`, was refused with "Unknown subcommand" — the valid ones are
`list` and `select` (jaato_session.py:4074) — and the run that followed
produced a clean-looking null about a cache that was never invalidated.
An unverified stimulus makes a null worthless, so this prints what the
daemon actually said and exits non-zero if the switch did not take.
"""
import argparse
import asyncio
import json
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
CONFIG_ROOT = os.path.join(REPO, ".jaato")
ENV_FILE = os.path.join(REPO, ".env")


def _load_env_file(path):
    """Read .env before any module constant reads it — same as whisper.py."""
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
SOCKET = os.environ.get("JAATO_IPC_SOCKET", "/tmp/monologue.sock")

from jaato_sdk import ClientType, IPCClient   # noqa: E402  (after .env)

#: Which of the three the caller may address. The curator is included: it
#: is not a sibling, but it is a session with a context cache like any other.
HALVES = ("conscient", "subconscient", "curator")


async def switch(half, model, listing):
    state = json.load(open(os.path.join(CONFIG_ROOT, "monologue.json")))
    target = state[f"{half}_session_id"]

    client = IPCClient(SOCKET, client_type=ClientType.API, auto_start=False,
                       env_file=ENV_FILE, workspace_path=REPO,
                       config_root=CONFIG_ROOT)
    if not await client.connect(timeout=120.0):
        raise SystemExit("no daemon — is monologue.py running?")

    said = []
    client.subscribe_all(
        lambda ev: said.append((str(getattr(ev, "type", "?")),
                                getattr(ev, "message", "")
                                or getattr(ev, "text", "") or "")))
    try:
        await client.attach_session(target)
        await client.execute_command(
            "model", args=["list"] if listing else ["select", model])
        # The command is answered asynchronously; nothing here can await it.
        await asyncio.sleep(6.0)
    finally:
        # NEVER end_session() — it terminates the ATTACHED session and takes
        # no argument. Disconnecting is safe only because the DRIVER still
        # holds this half, so `_maybe_unload_session` returns early (§7.10).
        await client.disconnect()

    spoken = [t for k, t in said if "SYSTEM_MESSAGE" in k and t.strip()]
    for line in spoken:
        print(line, flush=True)
    if listing:
        return 0
    if any("Unknown subcommand" in t or "error" in t.lower() for t in spoken):
        print("\nthe switch DID NOT TAKE — any reading after this is void.",
              file=sys.stderr)
        return 1
    if not any("Switched from" in t or "Model changed" in t for t in spoken):
        print("\nno confirmation arrived; treat the switch as UNVERIFIED "
              "and do not score what follows.", file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("half", choices=HALVES)
    ap.add_argument("model", nargs="?", help="e.g. anthropic/claude-sonnet-5")
    ap.add_argument("--list", action="store_true", dest="listing",
                    help="show the models this half can switch to")
    args = ap.parse_args()
    if not args.listing and not args.model:
        raise SystemExit("give a model, or --list to see them")
    return asyncio.run(switch(args.half, args.model, args.listing))


if __name__ == "__main__":
    sys.exit(main())
