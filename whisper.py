#!/usr/bin/env python3
"""whisper — put a thought to the mind, fire and forget.

    echo "what about the retry loop?" | ./whisper.py
    ./whisper.py --urgent "stop and answer me"

TWO CHANNELS, and the difference is enforced by the framework rather than
by wording (shared/message_queue.py:23-26):

    whisper  source_type="child"  IDLE-ONLY   a thought OFFERED. Cannot
                                              interrupt. May be ignored.
    speak    source_type="user"   HIGH PRIO   the operator addressing the
                                              mind. Interrupts; expects an
                                              answer.

"A thought the mind is free to act on or not" is not a matter of phrasing.
It is an idle-only stamp, after which the framework CANNOT let that message
seize a turn.

TRANSIENT BY DESIGN. The driver is already the resident client; a resident
whisper would duplicate its keepalive and add a second reconnect loop to
supervise. Being transient also makes it composable — a pipe, a cron line,
a hotkey.
"""
import argparse
import asyncio
import json
import os
import sys

from jaato_sdk import ClientType, IPCClient

REPO = os.path.dirname(os.path.abspath(__file__))
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
SOCKET = os.environ.get("JAATO_IPC_SOCKET", "/tmp/monologue.sock")

#: The stamp is the mechanical guarantee of non-interruption. It does not
#: guarantee the model READS the thought as optional, and it is not
#: established that source_id reaches the model on the idle-drain path — the
#: one place it is rendered (jaato_session.py:7371) is the mid-turn handler.
#: So the client wraps it. Belt and braces: stamp, envelope, persona line.
ENVELOPE = (
    "A thought from your operator, offered rather than asked:\n\n"
    "  {text}\n\n"
    "Take it up, set it aside, or let it pass. No answer is owed."
)


async def whisper(text, urgent=False):
    with open(os.path.join(CONFIG_ROOT, "monologue.json")) as fh:
        state = json.load(fh)

    client = IPCClient(SOCKET, client_type=ClientType.API, auto_start=False,
                       env_file=ENV_FILE, workspace_path=REPO,
                       config_root=CONFIG_ROOT)
    if not await client.connect(timeout=120.0):
        raise SystemExit("no daemon — is monologue.py running?")
    try:
        # A client attaches to ONE session at a time. This addresses the
        # conscient and lets the subconscient hear about it the normal way.
        await client.attach_session(state["conscient_session_id"])
        await client.inject_prompt(
            text if urgent else ENVELOPE.format(text=text),
            source_type="user" if urgent else "child",   # the authority knob
            source_id="operator",
        )
        print("spoken." if urgent else "whispered.")
    finally:
        # NEVER end_session(). It terminates the CURRENTLY-ATTACHED session
        # and takes no argument (ipc.py:1652) — one stray call kills the
        # conscient from what looks like a read-only window.
        #
        # No settle needed before disconnecting: _send_event awaits
        # _write_message, which does writer.write() then await
        # writer.drain() (ipc.py:1310-1319), so the inject is flushed to the
        # socket before the call returns.
        #
        # This disconnect fires _maybe_unload_session(conscient) on the way
        # out, and that returns early because the DRIVER is still in
        # attached_clients. Which is why whispering is safe only while the
        # driver holds the conscient — see README §7.10.
        await client.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text", nargs="*", help="the thought (or pipe it on stdin)")
    ap.add_argument("--urgent", action="store_true",
                    help="SPEAK instead: interrupts the turn in progress and "
                         "expects an answer. Use when it actually matters — "
                         "an idle-only whisper may wait a long time for an "
                         "opening, and is allowed to be ignored entirely.")
    args = ap.parse_args()

    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        raise SystemExit("nothing to say")
    asyncio.run(whisper(text, urgent=args.urgent))


if __name__ == "__main__":
    main()
