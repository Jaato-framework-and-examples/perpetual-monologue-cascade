#!/usr/bin/env python3
"""unload — save one half's transcript, without stopping the daemon.

A send_to_sibling RECEIPT is readable in exactly one place: the sending
session's saved history. No cascade event carries it, and a session writes
its history on SAVE, which happens on unload — so a cascade you are still
diagnosing has no receipts on disk yet.

Stopping the daemon would also save them, and is the wrong tool: it discards
the warm runner-pool slots and interrupts every other workspace the daemon
serves. Attaching away is the surgical version.

ONE session per invocation, deliberately. Chaining two attaches in one
client hung in practice — the second never returned while the first unload
was still in flight — and it is not needed: DISCONNECT unloads whatever the
client is attached to, so attach-then-disconnect saves exactly one session
with no second round trip to wait on.

    python3 unload.py sub
    python3 unload.py con
"""
import asyncio
import json
import os
import sys

from jaato_sdk import ClientType, IPCClient

REPO = os.path.dirname(os.path.abspath(__file__))
CONFIG_ROOT = os.path.join(REPO, ".jaato")
ENV_FILE = os.path.join(REPO, ".env")


def _load_env_file(path):
    if not os.path.isfile(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.split("   #")[0].strip()


_load_env_file(ENV_FILE)
SOCKET = os.environ.get("JAATO_IPC_SOCKET", "/tmp/mono.sock")


async def main():
    with open(os.path.join(CONFIG_ROOT, "monologue.json")) as fh:
        state = json.load(fh)
    which = sys.argv[1] if len(sys.argv) > 1 else "sub"
    target = state["subconscient_session_id" if which == "sub"
                   else "conscient_session_id"]

    client = IPCClient(SOCKET, client_type=ClientType.API, auto_start=False,
                       env_file=ENV_FILE, workspace_path=REPO,
                       config_root=CONFIG_ROOT)
    if not await client.connect(timeout=60.0):
        raise SystemExit("no daemon")
    try:
        await client.attach_session(target)
        print(f"attached {which}={target}; disconnecting to save it")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
