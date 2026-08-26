# Running it

The design is in [README.md](README.md). This is the operational half.

**Nothing here has been run end to end.** Two framework facts underneath it
are certified on jaato `4138a9a5` (README §4.1, §11 Q2); the shutdown path
is read from source and never observed (§7.15). Expect to debug.

## It does not run yet — and the reason is not in this repo

**Do not expect a working loop.** It completes exactly one round trip and
then strands, on every framework version tested including `e5ef1003` (#617)
and `8d89ffc1` (#618).

The cause, root-caused jointly with the framework owner over runs 8-11: a
message delivered to an IDLE session is queued and never drained, because
`inject_prompt` only starts a turn while a `send_message` RPC is in flight.
The recovery path is the same channel, so the driver's watchdog nudge lands
in the same dead queue — measured, the queue grows 1 → 2 → 3 and is never
popped. See README §7.14.

PR #619 fixes the half that made this UNRECOVERABLE — a nudge to an idle
session will drive a turn, and `inject_prompt` returns a status instead of
silence. It does NOT fix the sibling strand; that is a second change, moving
the busy/idle decision to the runner. So after #619 expect a loop that still
stops after one round trip, but a session you can still reach. Until it lands, this
workspace is useful as a BENCH rather than as a running monologue: the
pacer interval is one line in one profile, and widening it turns a
turn-boundary race from something a request/response cascade dismisses as a
flake into something that reproduces 5/5.

## What you need

- A jaato daemon on `$JAATO_IPC_SOCKET` (default `/tmp/monologue.sock`).
  Keep the path short — AF_UNIX caps it at 108 bytes and fails opaquely.
- An OpenRouter key.

```bash
cp .env.example .env      # then put your key in it
python3 monologue.py      # the driver: opens, observes, nudges, reaps
```

Two optional companions, in other terminals:

```bash
python3 observe.py                        # read both halves, touch nothing
echo "what about the retry loop?" | python3 whisper.py
python3 whisper.py --urgent "stop and answer me"
```

`whisper` offers a thought the mind may ignore; `--urgent` interrupts and
expects an answer. That difference is an authority tier the framework
enforces, not a wording choice — README §8.1.

## The knobs that matter

| Where | Knob | Why you would touch it |
|---|---|---|
| `monologue.py` | `LIMITS` | **The ceiling. Read this before your first run.** `{"usd": 5.00}` is the whole safety mechanism; the pacer is an optimiser and fails open (§7.7). |
| `.jaato/profiles/_base_*.yaml` | `MONOLOGUE_INTERVAL_SECONDS` | 30s deliberate / 8s associative. §11 Q4 records that this asymmetry is a guess and may be backwards. |
| `.jaato/profiles/_base_*.yaml` | `suppress_base_instructions` | **The biggest lever on burn rate.** See below. |
| `.jaato/profiles/openrouter_mixed/*.yaml` | `model` | Per-half binding; the base profiles stay provider-agnostic. |
| `monologue.py` | `STALL_AFTER` | 180s. Below ~90s it will nudge a healthy paced loop. |

### suppress_base_instructions — the one real trade

Shipped as `false`. The coordination example measured ~24,000 tokens of
base instructions per session — 81% of one turn's context — and suppressing
them is what made its cascade budget measurable rather than guessed.

It is left on anyway, because README §6's security property (a sibling
proposes, it cannot command) depends on the model honouring the
`⟦UNTRUSTED-EXTERNAL-CONTENT⟧` boundary its inbound sibling messages are
wrapped in, and the base instructions are where that boundary is explained.
Their `_base_sibling-a.yaml` keeps it on for exactly this reason.

Flipping it to `true` roughly quarters your burn rate and weakens §6 to
whatever the model does with an unexplained marker. That is a real choice
with a real cost on both sides; it is not a default anyone should flip
without deciding.

## Model bindings

| Half | Model | Provenance |
|---|---|---|
| subconscient | `anthropic/claude-haiku-4.5` | proven on OpenRouter by the coordination example |
| conscient | `anthropic/claude-sonnet-5` | **inferred.** Current per the model table; the exact OpenRouter slug is unverified against their catalogue. One line in one file if it 404s. |

## Temperature is deliberately not zero

Every other cascade in this org pins `temperature: 0.0` for byte-identical
output. Here that is the failure mode — a thought process that returns the
same thought forever is §7.9, "a stuck volley is invisible". The monologue
is the one workload that wants variance, and it is the one place this
repo parts company with the determinism budget.

## What is not implemented

- **Continuity across restarts (README §9).** `{{continuity_scope}}` has
  zero occurrences in the framework and the checkout carries no `docs/`
  tree, so the mechanism §9 describes could not be verified, let alone
  wired. Both halves load the `memory` plugin, so they can store — but
  nothing drains raw→curated, and enrichment surfaces curated memories
  only. **The mind wakes with amnesia every run.** Fixing it needs the
  curator §9 calls non-optional.
- **Stuck-volley detection (§7.9, §11 Q1).** Nothing notices two halves
  circling one thought. Every event looks healthy while it happens.
- **Reviving a cold half.** `session.wake` is the mechanism (§11 Q2) and
  the driver does not call it — cold is not reachable by resting, so it
  would be code for a case that should not occur. `render_cold` says so
  loudly if it ever does.

## First-run checklist

1. Set `LIMITS` to something you are willing to lose.
2. Watch the first three volleys in the driver's own output — you should
   see alternating `→ send_to_sibling` lines about 30s apart.
3. If the first send returns `✗`, read the error: `sibling_cold` means
   something attached away from a half (§11 Q2); a permission block means
   the whitelist did not apply, and the most likely cause is
   `defaultPolicy` sitting flat under `permission:` instead of nested
   under `policy:`.
4. If nothing appears at all, the loop never started: the kickoff message
   goes to whichever session the driver is attached to, and that is the
   conscient by construction.
