# Running it

The design is in [README.md](README.md). This is the operational half.

**It runs.** Both halves think, send, and take each other's thoughts up as
material; the curator wakes on a store and reports. What it does not yet do
is run for long — a framework defect kills one half about three minutes in,
every time. That is the whole of the current gap, and it is not in this
repo. Read the next section before your first run.

Current readings are from jaato `891602f9` (#619 through #627 merged).

## The one thing that will happen to you

**A half dies ~3 minutes in, and the loop goes quiet without erroring.**
Reproduced on runs 26 and 27, on two different daemon builds:

```
MODEL_THREAD_TERMINAL_ERROR error_type=TimeoutError
error=daemon loop did not deliver
      RunnerRPCClient.session_get_context_usage within 10.0s
      (daemon-side: the coroutine was scheduled onto the loop
       and did not complete)
→ SessionTerminatedEvent(reason=error)
→ _apply_default_cascade_policy: triggered unload
```

The daemon loop stops running scheduled coroutines for 5-35s. That alone is
survivable. What makes it fatal is that the model thread catches it with a
bare `except Exception` and classifies nothing, so "the event loop was busy
for ten seconds" takes the same branch as a revoked API key: terminal, and
the session is unloaded. It is then **cold**, and a cold sibling is not
woken by a sibling message — so the surviving half spends the rest of the
run sending into a corpse and collecting `sibling_cold`.

The driver prints `!! a half went cold` and then the grep that tells the two
causes apart:

```bash
grep MODEL_THREAD_TERMINAL_ERROR <daemon log>
```

**Present: the framework killed it and nothing here did. Absent: something
attached away**, which is a bug in your code — this driver's one attach-away
is measured harmless (README §5.6, `c4`). Either is revivable with
`session.wake`, which needs no attachment (§11 Q2). The failure is recorded
as README §7.18.

Reported with jaato-30; it is their highest-severity open item. Until it
lands, expect roughly three good minutes per run — which is enough to read
persona behaviour and not enough to exercise continuity.

## What you need

- A jaato daemon on `$JAATO_IPC_SOCKET`. Keep the path short — AF_UNIX caps
  it at 108 bytes and fails opaquely.
- An OpenRouter key.

```bash
cp .env.example .env      # then put your key in it
python3 monologue.py      # the driver: opens, observes, nudges, reaps
```

The daemon is **not** a singleton, whatever `.env.example` says next to
`JAATO_IPC_SOCKET`. Several can coexist as long as each gets its own
`--ipc-socket`, `--pid-file` and `--log-file`; without a distinct pid file
the second refuses to start with "already running". Check who owns a socket
with `ss -lxp | grep <socket>` rather than trusting a pid file. Restart a
daemon only to pick up new framework commits — never between runs, since a
cold spawn costs ~30s against ~7s warm.

Four companions, in other terminals:

```bash
python3 observe.py                        # read all three, touch nothing
echo "what about the retry loop?" | python3 whisper.py
python3 whisper.py --urgent "stop and answer me"
python3 unload.py <session_id>            # save one transcript, daemon stays up
python3 analyze_run.py                    # what one run's artifacts can answer
python3 switch_model.py subconscient --list
python3 switch_model.py subconscient anthropic/claude-sonnet-5
```

`whisper` offers a thought the mind may ignore; `--urgent` interrupts and
expects an answer. That difference is an authority tier the framework
enforces, not a wording choice — README §8.1.

`unload` is how you get a transcript. **Never `delete_session`** — it
destroys the transcript rather than saving it. The driver unloads by
attaching away on shutdown for the same reason.

`switch_model` is not for changing models. `/model select` invalidates the
daemon's cached context limit, and in a healthy session that cache is filled
before any progress event fires — so the miss path that emits
`percent_used=0` never runs, and a whole clean run says nothing about it.
This is the only cheap way to make it run. After a switch, watch the
driver's `?? ... percent_used=0` lines: at most one honest-unknown reading
(percent AND limit both zero) then a healed non-zero is correct; a
persistent zero or a ~10s stall is a regression.

It exits non-zero if the switch did not take, because **an unverified
stimulus makes the reading that follows worthless** — the first version of
this tool sent the wrong argument shape, was refused, and produced a
clean-looking null about a cache that had never been invalidated.

## The knobs that matter

| Where | Knob | Why you would touch it |
|---|---|---|
| `.env` | `MONOLOGUE_CEILING_USD` / `_TURNS` | **The ceiling. Read this before your first run.** Shipped `2.00` / `200`. There is no default in code — the driver exits if either is unset, because guessing it would be guessing how much money you are willing to lose. The pacer is an optimiser and fails open (§7.7). |
| `.jaato/profiles/_base_*.yaml` | `MONOLOGUE_INTERVAL_SECONDS` | `5.0` on both halves. Was 30/8 on a guess that the halves should be paced asymmetrically; §11 Q4 is now answered and that guess was wrong — see below. |
| `.jaato/profiles/_base_*.yaml` | `MONOLOGUE_THOUGHT_CEILING` | `1000`. Surfaces in the send receipt as `thought N/1000`, and the mind reasons about it — run 26 noticed the counter and concluded silence should be the default state. |
| `.jaato/profiles/_base_*.yaml` | `suppress_base_instructions` | **The biggest lever on burn rate.** See below. |
| `.jaato/profiles/openrouter_mixed/*.yaml` | `model` | Per-half binding; the base profiles stay provider-agnostic. |
| `monologue.py` | `STALL_AFTER` | 180s. Below ~90s it will nudge a healthy paced loop. The watchdog resets on thoughts only — output text or a send — not on any event, or a busy-but-silent session looks alive. |
| env | `MONOLOGUE_SAVE_PER_SEND=1` | Per-send `session.save`, **off by default**. It fetches history through the daemon loop, and run 21 attributed 5 of 7 stalls to `session_get_history`. Leaving it off separates our contention from the framework's. |

### The pacer interval is not the phase offset

Both halves are paced identically at 5s because the asymmetry the old
30s/8s split was reaching for does not exist. The one-thought offset
between the halves is **tier-structural**: `SIBLING` is an `IDLE_ONLY`
source, drained at a turn boundary and nowhere else, so a sibling's thought
can never land mid-turn however the pacer is set. Widening the interval
does not change the offset; it only makes it easier to see.

### suppress_base_instructions — the one real trade

Shipped as `false` on all three profiles.

The coordination example measured ~24,000 tokens of base instructions per
session — 81% of one turn's context — and suppressing them is what made its
cascade budget measurable rather than guessed.

It is left on anyway, because README §6's security property (a sibling
proposes, it cannot command) depends on the model honouring the
`⟦UNTRUSTED-EXTERNAL-CONTENT⟧` boundary its inbound messages are wrapped
in, and the base instructions are where that boundary is explained. The
curator's wake arrives inside the same wrapper, so this covers it too.

Flipping it to `true` roughly quarters your burn rate and weakens §6 to
whatever the model does with an unexplained marker. That is a real choice
with a real cost on both sides; it is not a default anyone should flip
without deciding.

## Model bindings

| Half | Model | Temp | Provenance |
|---|---|---|---|
| conscient | `anthropic/claude-sonnet-5` | 0.8 | verified — runs 22-27 |
| subconscient | `anthropic/claude-haiku-4.5` | 1.0 | proven on OpenRouter by the coordination example |
| curator | `anthropic/claude-sonnet-5` | 0.3 | judgement about what deserves to persist; the one place here that wants consistency |

## Temperature is deliberately not zero

Every other cascade in this org pins `temperature: 0.0` for byte-identical
output. Here that is the failure mode — a thought process that returns the
same thought forever is §7.9, "a stuck volley is invisible". The monologue
is the one workload that wants variance, and it is the one place this repo
parts company with the determinism budget.

The curator is the exception to the exception, at 0.3.

## Reading a run

Traces are archived per run, because a fixed path plus `rm` before each run
destroyed run 21's fourteen-stall sample:

```
/tmp/mono-trace-<half>-<HHMMSS>.log     # per-half provider trace
/tmp/monoC.log                          # daemon log — where the stalls are
```

Greps that pay for themselves:

```bash
grep MODEL_THREAD_TERMINAL_ERROR /tmp/monoC.log   # did a half get killed
grep "CONTINUATION: Processing"  /tmp/monoC.log   # N>1 means a lost-stash repro
grep SIBLING_DELIVERY            /tmp/monoC.log   # outcome= and replica_busy=
```

**A zero from any of these is only a null if the daemon was started after
the commit that emits the line.** `CONTINUATION: Processing` landed in
#627; a daemon older than that produces a zero which means nothing at all.
Check `ps -o lstart=` on the daemon pid before believing an absence. This
has already cost one false negative.

## What is not implemented

- **Continuity across restarts (README §9).** All three sessions load the
  `memory` plugin and the halves do store — 12 raw memories sit in
  `.jaato/memories/raw/` — but **nothing has ever been curated**, so
  enrichment (curated-only) surfaces nothing and the mind wakes with
  amnesia every run. The curator itself is wired and fires correctly; it is
  blocked on a framework regression, below.
- **Stuck-volley detection (§7.9, §11 Q1).** Nothing notices two halves
  circling one thought. Every event looks healthy while it happens.
- **A long run.** Nothing here has run more than ~6 minutes, because of the
  session kill above. Everything about behaviour over hours is unknown.

### The curator is blocked upstream, not here

The driver wakes the curator on every successful `store_memory` (via
`session.wake`, which takes an explicit session_id and revives a cold
target — so the curator being unloaded between curations is harmless). The
chain fires. The curator then reports an empty store.

It is not wrong to. `MemoryStore.search_by_maturity` — documented
"Curator-facing maturity query", correct, unit-tested — has **zero
production callers**: `_execute_retrieve` routes every non-`ids` query
through `search_by_tags`, which reads `curated.jsonl` only. So
`retrieve_memories(maturity="raw")` cannot reach the raw queue, though the
shipped `memory-advisor` persona, the plugin's own class docstring, and the
tool schema all say it can. Regression origin: `3f019999`, which split the
raw queue from the curated store and never repointed the handler.

Worse for debugging: `list_memory_tags` computes `count_by_maturity()` on
every call, which *does* count the raw queue, then returns `memory_count`
from the curated-built indexer and routes the true `count_raw` into
`_telemetry`. It answers "Found 0 memories" while holding "raw: 12" in the
same dict. Both curator sessions reasoned correctly from that false
premise; one hedged that "the memory write hasn't landed yet".

Reported to jaato-30. The minimal fix is one branch in `_execute_retrieve`.
Nothing in this repo needs to change when it lands.

## First-run checklist

1. Set the ceiling in `.env` to something you are willing to lose.
2. Watch the first volleys in the driver's own output — alternating
   `→ send_to_sibling` lines roughly 5s apart.
3. If a send returns `✗ sibling_cold` a few minutes in, that is the
   framework kill, not your configuration. Confirm with
   `grep MODEL_THREAD_TERMINAL_ERROR /tmp/monoC.log`.
4. If the first send returns `✗` with a permission block, the whitelist did
   not apply, and the most likely cause is `defaultPolicy` sitting flat
   under `permission:` instead of nested under `policy:`.
5. If nothing appears at all, the loop never started: the kickoff message
   goes to whichever session the driver is attached to, and that is the
   conscient by construction.
