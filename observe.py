#!/usr/bin/env python3
"""observe — watch the monologue without touching it.

    ./observe.py

Registers on the CASCADE-CLIENT REGISTRY, not by session attachment. An
observer calls no attach_session, holds nothing open, and sees BOTH halves
— where an attached client's events() sees only its own session. Reading is
never a reason to stay attached.

role="observer" is read-only and many are allowed; the driver holds the
single role="owner" per cid (lifecycle authority).
"""
import asyncio
import contextlib
import json
import os
import time

from jaato_sdk import ClientType, EventType, IPCClient

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


async def main():
    with open(os.path.join(CONFIG_ROOT, "monologue.json")) as fh:
        cid = json.load(fh)["cid"]

    client = IPCClient(SOCKET, client_type=ClientType.API, auto_start=False,
                       env_file=ENV_FILE, workspace_path=REPO,
                       config_root=CONFIG_ROOT)
    if not await client.connect(timeout=120.0):
        raise SystemExit("no daemon — is monologue.py running?")

    print(f"observing cascade {cid} — ctrl-c to leave\n", flush=True)
    try:
        async with contextlib.aclosing(
                client.cascade_events(cid, event_types=None,
                                      role="observer")) as stream:
            async for ev in stream:
                if getattr(ev, "type", None) != EventType.AGENT_OUTPUT:
                    continue
                text = (getattr(ev, "text", "") or "").strip()
                if text:
                    who = getattr(ev, "agent_id", None) or "?"
                    print(f"[{time.strftime('%H:%M:%S')}] {who}: {text}\n",
                          flush=True)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
