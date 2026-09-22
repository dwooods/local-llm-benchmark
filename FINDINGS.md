# Findings

Everything below is from real measured runs on the two machines described in
[`README.md`](README.md) — a Windows PC with a 12GB AMD RX 6700 XT, and a Raspberry Pi 5 (8GB,
CPU-only, running off an NVMe HAT rather than a MicroSD card) — using
[promptfoo](https://www.promptfoo.dev/) against local Ollama models. Where a number turned out to
be wrong on a first pass (a bad test config, a judge that graded something incorrectly, a bug in
how a test was scored), that's noted explicitly rather than quietly corrected — a couple of those
turned out to be more interesting than the model scores themselves.

## How "best" is defined

A model+settings combo is "best" for a machine and workload when it wins on this priority order:
first, it clears a minimum quality bar for the task (a fast wrong answer is worse than a slower
right one); then fastest generation speed among combos that clear the bar; then lowest
time-to-first-token; then fits comfortably in available VRAM/RAM; then reliability of structured
output (valid JSON, correctly-formed tool calls).

## PC: the 12GB VRAM cliff

Models that fit fully in the RX 6700 XT's 12GB run at native VRAM bandwidth and are dramatically
faster than CPU-only inference: `qwen2.5:3b` hit **138.9 tok/s** (~20x the ~6.9 tok/s CPU
baseline), `qwen3.5:9b` hit **18.0 tok/s**, `deepseek-r1:14b` (a reasoning model, slower per-token
by design) still hit **8.3 tok/s** fully offloaded at ~8.9GB.

Once a model's footprint exceeds roughly 11.2GB, Ollama splits layers across the PCIe bus into
system RAM — and this isn't just "slower," it can be **worse than CPU-only**:

| Model | Split-mode speed | vs. CPU baseline (6.9 tok/s) |
|---|---|---|
| `gpt-oss:20b` | 11.3 tok/s | beats CPU |
| `gemma4:31b` | ~5.5 tok/s | **worse than CPU** |
| `qwen2.5-coder:32b` | 4.25 tok/s | **worse than CPU, worst result logged** |

The practical rule that came out of this: treat ~13B params at Q4_K_M (7-9GB) as the comfortable,
reliably-fast ceiling on a 12GB card. Above that, don't assume a "stretch tier" model will be
usably slow — test each one, because some split-mode results are fine and others are worse than
not using the GPU at all, and the pattern so far is that bigger overshoots trend toward worse
split penalties, not better.

Two AMD/Vulkan-specific settings were tested as VRAM-saving levers and made things dramatically
**worse**, not better: `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0` together dropped
`qwen2.5:3b`'s eval rate from ~150 tok/s to ~52 tok/s. Both are likely far more mature on
NVIDIA/CUDA than on AMD's Vulkan backend here — don't set either on an AMD card without retesting.

**Which backend, and which env var actually fixed the CPU fallback:** the captured Ollama server
log from this machine shows both GPUs enumerated under Vulkan, not ROCm/HIP —
`library=Vulkan ... name=Vulkan0 description="AMD Radeon RX 6700 XT" ... available="11.2 GiB"`
(the Intel iGPU shows up as `Vulkan1` and is dropped as integrated). That means
`HSA_OVERRIDE_GFX_VERSION`, a ROCm/HSA runtime variable, has no defined effect on this backend and
the original explanation for why it "fixed" GPU detection (from the Gemini session that predates
this project) was wrong. The three env vars in `README.md` were set together with a restart and
verified only by the before/after tok/s jump; there is no "before" server log and no per-variable
isolation. Best hypothesis, not confirmed: `OLLAMA_NUM_PARALLEL=1`, since Ollama's scheduler makes
GPU/CPU placement decisions from predicted memory need and fewer parallel slots shrink that
prediction. `OLLAMA_ORIGINS` is CORS-only and cannot be it. Listed under open items.

The CPU baseline above was measured by disabling GPU acceleration at the env-var level, not by
using Ollama's own `num_gpu: 0` request parameter, which forces genuine full-CPU inference without
touching any environment variables — see open items below; a confirmatory run with `num_gpu: 0` on
an already-measured split-mode model would cleanly separate "CPU baseline" from "GPU split-mode
overhead" instead of inferring it indirectly the way the numbers above currently do.

## PC: quality suite results

| Workload | Result |
|---|---|
| **Chat/Q&A** | `qwen3.5:9b`, `phi4:14b`, `gpt-oss:20b` scored **10/12** (corrected — see bug #2 below). `phi4:14b` reliably failed both Raspberry-Pi-5-knowledge questions, insisting the Pi 5 "hasn't been officially released" — a real, concrete instance of local models' training-cutoff risk on anything about recent hardware/products. |
| **Structured extraction** | `qwen2.5:latest`, `qwen3.5:9b`, `gemma4:12b` scored **8/9**. The one failure (`qwen2.5:latest`) was a genuine instruction-adherence gap — hallucinated trailing text after otherwise-correct JSON broke a strict JSON-only contract, not a data-accuracy problem. |
| **Agentic/tool use** | `qwen3.5:9b` and `qwen2.5:latest` both went a clean **4/4**. `llama3:latest` went **0/4** — complete empty-output failures on every test, not a transport issue. `qwen2.5-coder:14b` — this project's original "top pick for agentic coding" per general guidance — scored only **1/4**; a debug run found the model doesn't reliably populate Ollama's native `tool_calls` API field at all, it just prints a JSON-lookalike blob as plain text. That's a deeper problem than a formatting quirk: a real downstream tool-calling integration would never see it as an executable call. |
| **Coding** | `qwen2.5-coder:7b`, `qwen2.5-coder:14b`, `deepseek-coder-v2:16b`, `gemma4:12b` scored a corrected **16/16 (100%)** — see bug #3 below for why the first pass showed a failure that wasn't real. |
| **Vision/OCR** | See its own section below — this workload had the most methodology surprises of the five, and is the only one run on both machines. |

Across every real (non-placeholder) test in every suite it entered, `qwen3.5:9b` came out as the
single most consistently reliable model on this rig — which also happens to match its role as the
actual daily-driver model in use before this project started scoring it formally.

**The empirically-supported picks are `qwen3.5:9b` and `qwen2.5:latest`, not `qwen2.5-coder:14b`**,
despite the latter being the generic "best local coding model" recommendation going in. Don't take
a name's reputation as a substitute for testing it against your own harness.

## Three real bugs found in the test harness (not the models)

Worth knowing before trusting or writing any promptfoo config with per-test overrides or
hand-built multi-turn `messages` arrays — these produced misleadingly uniform "failure" scores that
looked like real model weaknesses until traced back to the config:

1. **`tools` nested under the wrong key silently produces a false "model can't call tools"
   result.** A first agentic-suite run scored a uniform 0/16 because `tools` had been nested under
   each test's `options.provider.config` — but in a multi-provider matrix eval, `options.provider`
   only overrides the *grading* provider, never the actual generation call. Fix: put `tools`
   directly on each provider's own top-level `config` block under `providers:`.

2. **An `llm-rubric` assertion with no explicit judge `provider:` can silently fall through to an
   unpredictable — and possibly cloud — default.** One chat-suite test had no rubric provider
   override and defaulted to a deprecated cloud model in this environment, which hard-errored
   instead of scoring anything (and, separately, would have been a real problem for a benchmark
   whose whole point is local-only inference). Fix: set `defaultTest.options.provider` to the
   local judge in every config that uses `llm-rubric`, so a forgotten override still stays local.

3. **A hand-injected assistant `tool_calls` turn needs `function.arguments` as a plain JSON
   object, not a JSON-encoded string — even though the OpenAI API spec documents it as a string.**
   The spec-correct string form produced a suspicious uniform 0/4 on a tool-error-recovery test,
   including for two models with otherwise-perfect records. An isolated debug run (not committed
   to this repo — see README.md) showed the string form makes Ollama return an empty completion
   when replaying that turn, while the object form works correctly. This only affects turns you
   hand-build for a test, not a model's own freshly-generated tool_calls.

A fourth, non-config bug worth flagging separately: **the fixed judge model (`deepseek-r1:14b`)
produced a confirmed false-negative grade** on the coding suite — it marked a correct
`parse_duration` function as failing. Hand-verifying the generated code against the spec showed it
was actually right; corrected score is 16/16, not 15/16. This is a harder class of error to catch
than the three above, because it sits quietly inside an otherwise-clean run instead of producing an
obviously-broken uniform result — worth spot-checking judge-graded runs, not just trusting the
aggregate score.

A fifth, separate gotcha: **promptfoo caches responses to disk across separate CLI invocations,
not just within one `eval` run.** Re-running the exact same command with no config changes does
NOT re-query the model — it silently replays the cached response, even minutes later in a totally
separate process. The tell is a suspiciously fast run (~1 second instead of several minutes) and
output that matches a previous run character-for-character. Always append `--no-cache` when the
point of a rerun is to check reproducibility.

**Update on the caching gotcha:** `--no-cache` only suppresses cache *reads*, not cache *writes* —
a run launched with `--no-cache` still writes its successful responses to the cache as normal, so a
later invocation that omits the flag can silently replay a result that was originally generated
under a `--no-cache` run. Separately, error responses (a 4xx, or a 200 with an embedded error
payload) are never cached regardless of flag state — a failed test's retry always re-queries live,
so a suspiciously-fast rerun after a real fix is a legitimate signal, not a cache artifact to
distrust.

**A sixth gotcha, found while building the vision suite's custom scorer
(`assertions/check-fields.js`):** a hand-written JSON extractor that just takes "first `{` to last
`}`" breaks the moment a model reasons out loud before answering, or re-emits its complete answer
instead of stopping (see `glm-ocr`'s non-termination defect below) — either one puts more than one
JSON object in the output and the naive slice grabs garbage. Fix: scan for every complete,
brace-balanced top-level `{...}` object in the output, then try parsing from the *last* one found
backward until one succeeds. Separately, normalize vendor-name strings with Unicode NFD
decomposition and strip combining marks before doing a substring match — an OCR'd accent drop
(e.g. "La Cabaña" → "La Cabana") otherwise scores as a wrong vendor, and this was seen
independently on two different models on two different platforms for the same receipt.

**A seventh, infrastructure-adjacent gotcha:** a naive PID-based kill/watchdog script can be
defeated by process orphaning. `npx`-launched processes (e.g. `promptfoo`) can spawn children
(e.g. `llama-server`) that outlive a `kill` sent to the captured launcher PID. If you're building
a watchdog for a long-running suite — the Pi's thermal risk below is exactly the situation that
calls for one — track and kill by pattern-matching the invocation (`pgrep -f`/`pkill -f` on a
distinguishing config filename) instead of trusting a single captured PID.

## Vision/OCR suite — PC: `glm-ocr` vs. `qwen3-vl:8b`

This suite deliberately uses a deterministic `javascript` assertion instead of an LLM judge —
receipt ground truth is objective, so there's no reason to introduce judge-model error into
scoring something a human can just read off the image.

**Context-window starvation, and the fix:** at `num_ctx: 4096`, `qwen3-vl:8b` hit the exact 4096
token ceiling on one test with a genuinely empty captured output (images alone consume
700-1,650 prompt tokens before generation even starts, on top of the usual text-suite budget).
Bumping to `num_ctx: 8192` fixed it — the same test terminated naturally at 4423 tokens with fully
correct content. **This is a deliberate, documented deviation from the project's usual
`num_ctx: 4096`** used on the four text suites — the two aren't meant to be the same value, they're
each tuned for what their workload actually needs.

**`glm-ocr`'s genuine non-termination defect:** independent of context size, `glm-ocr` hit the
exact token ceiling on all 6/6 test cases at both 4096 and 8192 tokens — always by re-emitting its
own already-complete JSON answer instead of stopping. Doubling the context didn't fix it, it just
let the repetition loop run twice as long (proportionally similar completion-token counts at both
settings). Confirmed stable across three separate runs — this is a real reliability finding about
the model, not a one-off or a config bug — a downstream integration using `glm-ocr` needs either a
stop-sequence workaround or a different model.

**Final scores, `num_ctx: 8192` (the trustworthy run — scoring corrected after an initial pass
mis-weighted the non-termination cases; the corrected numbers below supersede an earlier
3/6-raw / 4.5-of-6-weighted result for `glm-ocr`):**

| Model | Raw score | Weighted score | Notes |
|---|---|---|---|
| `qwen3-vl:8b` | 5/6 (83%) | 5.5/6 (92%) | Best result on the vision suite |
| `glm-ocr` | 4/6 (67%) | 5/6 (83%) | Non-termination defect above (confirmed stable across three runs) plus a Home Depot vendor-mis-extraction; closer to `qwen3-vl:8b` after the scoring correction than the original run suggested, but still behind it — and the non-termination defect is a real integration blocker independent of raw accuracy |

**The VAT/net-vs-gross ambiguity (a durable, non-fixable-by-prompt-wording finding):** the Costa
coffee receipt has both a pre-tax subtotal (€4.24) and a VAT-inclusive grand total (€5.00). Both
models, at both context sizes — four independent observations — extracted €4.24, even after the
schema's `total` field was explicitly reworded to specify "the final amount actually paid or due,
including any tax, VAT, or service charge — not a pre-tax subtotal." Consistent enough across two
models and two runs to log as a genuine small-local-vision-model limitation on multi-total
receipts, not something worth another prompt-wording iteration.

## Vision/OCR suite — Pi: `qwen3-vl:2b` vs. `minicpm-v4.6`

Run and closed separately from the PC suite above, same receipts/ground truth, restricted to the
two Pi-scale vision models in the shortlist (`minicpm-v4.6` and `qwen3-vl:2b` — see `README.md`).

| Model | Raw score | Notes |
|---|---|---|
| `minicpm-v4.6` | 11/12 (92%) | Best Pi vision result — an interrupted partial run's number, accepted as final rather than re-run clean |
| `qwen3-vl:2b` | 4/6 (67%) | Costa and Home Depot receipts failed, the other four passed |

Two things specific to this suite, not the PC one. First, `qwen3-vl:2b`'s `think: false` setting
reaches Ollama correctly but has **no effect** — the model writes its reasoning into the visible
answer regardless of the flag. Don't assume the think-flag TTFT lever documented in the
local-vs-cloud section below applies universally; it's confirmed model-specific. Second, running
this suite is what produced the sixth Pi hard crash logged in the thermal section below: a
kill-and-cool watchdog was built for it, but its kill window turned out shorter than the model's
normal per-case generation time, so the remaining `qwen3-vl:2b` cases were run manually instead,
via promptfoo's cache to avoid losing already-completed cases — see the thermal section for what
happened next. It's also the same pairing — `minicpm-v4.6` followed by `qwen3-vl:2b` in the same
suite — that a later instrumented reproduction used to catch a crash with full telemetry; see the
thermal section below.

**An unreconciled anomaly on the Costa receipt case specifically:** one run of this exact
case/model/config non-terminated for roughly 18.86 minutes before being killed, while two other
runs of the identical configuration completed cleanly in 1-2 minutes. This doesn't match either of
the two known non-termination causes documented elsewhere in this doc (`glm-ocr`'s confirmed
structural non-termination defect above, or a missing `num_predict` — it was set correctly on all
three runs here). Still open and unreconciled as of this writing — not blocking, but worth a
targeted repeat before treating this specific result as fully settled.

## Pi: thermal reliability under sustained load

Six confirmed hard, silent reboots during this project, all under sustained CPU-bound inference
(both multi-model and single-model workloads), all with the official Raspberry Pi Active Cooler
physically installed and its fan confirmed running. **Active cooling is necessary but not
sufficient** — this is the single biggest caveat in this repo for anyone planning to run these Pi
suites unattended, or to ship a product on this hardware.

Two crash signatures were observed: fast uncaught temperature spikes (~1 minute from onset to
reset) and a slower throttle-engage/recover cycle before an eventual reset. The power supply was
ruled out as a cause via `vcgencmd pmic_read_adc` — every voltage rail read stable through every
crash. The board also survived one run that peaked at 80.7°C without crashing, so crash risk in
the 79–86°C range initially looked probabilistic, not a fixed trip point — and
`vcgencmd get_throttled` reading `0x0` is **not** reliable evidence nothing is wrong: several
crashes here read `0x0` right up to the reset, because the temperature climb outran the firmware's
own reaction time.

**Update — the mechanism, from a 1Hz instrumented reproduction (`benchmarks/pi-crash-capture/`):**
built a logger (`pi_thermal_log.py`) that samples `vcgencmd measure_temp`, `get_throttled`, every
PMIC rail, CPU clock, and fan RPM once a second and `fsync`s every row to disk — necessary because
Raspberry Pi OS keeps its journal in RAM by default, so nothing about a crash moment survives a
hard reset otherwise (`journalctl -b -1` after a reset here returned "no persistent journal was
found"). Reran the Pi vision suite and reproduced a crash on the first attempt, with the full 29
seconds before the reset captured (`thermal.csv`, `boot_id b8f8132e`).

The result: this isn't probabilistic. `minicpm-v4.6` ran first and held steady indefinitely — about
6.8A into the SoC (~67°C, fan at 6,400 RPM) for a minute and a half, no issue. When the suite
swapped to `qwen3-vl:2b`, the SoC went from 60°C to 80°C in 6 seconds and settled at about 11.5A
(~12W), with the fan pinned at its 8,774 RPM ceiling and the firmware's soft-temperature-limit flag
flapping on and off for 11 of the last 24 seconds. The board reset 29 seconds after the model swap,
peak 84.5°C. Rails stayed flat throughout (5V input 4.97-5.32V against a 4.63V under-voltage trip;
VDD_CORE about 1.0V) — including through a *higher* current peak, 15.2A, from `minicpm-v4.6`'s own
load spike earlier in the run, which the board survived without issue. So it isn't a current spike
and it isn't the power supply: it's `qwen3-vl:2b` holding roughly 12W sustained for half a minute,
against a cooler that this data shows holds about 7W indefinitely and does not hold about 12W.
Which of two same-tier models is the 12W one is not something the model card tells you — it has to
be measured, the way this logger measured it.

Two items this reproduction leaves open rather than resolves: the kernel reported CPU clock at a
flat 3000MHz through every second the soft limit was set, which is either the throttle
reaction-time gap made visible as one number, or a stale `scaling_cur_freq` read that doesn't
reflect what the firmware actually did — sampling `vcgencmd measure_clock arm` directly is the next
test. And this specific reproduction is a single instrumented run, not a sweep across many
models/loads, so treat "the cooler's real ceiling sits between ~7W and ~12W" as a bracket from one
data point, not a precisely measured threshold.

**Practical rule (updated):** treat every sustained CPU-pegged Pi run as being at real risk of an
unannounced hard crash whenever the model's *sustained* power draw is unknown — it is no longer
accurate to call this purely probabilistic, but per-model power draw isn't published anywhere and
has to be measured per model before trusting a multi-hour unattended run. A kill-and-cool thermal
watchdog is a real, working mitigation for triggering an early shutdown before the crash zone — but
its kill window needs to be tuned per workload; too short, and it kills a normal case before it can
finish (see the Pi vision suite above), which is its own kind of lost data. A more aggressive or
replacement physical cooler was considered and declined as a fix — the standing plan for any real
product on this hardware is an application-level watchdog/auto-restart designed around per-model
measured power draw, not a cooling upgrade meant to eliminate the risk generically.

## Long-context degradation (needle-in-a-haystack)

`needle_test.py` inserts one exact-match "needle" fact (an override code) at a controlled depth
inside a haystack of unrelated filler sentences, asks `qwen3.5:9b` to recall it, and sweeps both
haystack length and needle depth. Scoring is a deterministic substring match, not an LLM judge —
this project already has one confirmed false-negative judge grade (see the fourth bug above), and
exact-string recall doesn't need a second model's opinion. Results are in `needle_results.csv`:
42 rows across two runs — a first pass of four sizes (1,024–8,192) at three depths, then a full sweep
of six sizes (1,024–32,768) at five depths — covering 30 unique size/depth cells.

**Recall itself never degraded:** the needle was found in every single cell tested — every
context size from 1,024 to 32,768 tokens, at every depth fraction from 0% to 100%, across both
runs. For this model, on this task, "lost in the middle" simply didn't happen inside the range
tested.

**But speed degrades sharply and predictably as context grows**, independent of recall quality —
this is a second, distinct TTFT driver from the `think`-flag finding in the local-vs-cloud section
below, and it doesn't require a hybrid-reasoning model to show up:

| Context (tokens) | Prefill speed | Generation speed | TTFT |
|---|---|---|---|
| 1,024 | ~655 tok/s | ~61-62 tok/s | ~3.7-3.9s |
| 2,048 | ~654-658 tok/s | ~61-62 tok/s | ~5.1-5.2s |
| 4,096 | ~625-630 tok/s | ~60 tok/s | ~8.4-8.6s |
| 8,192 | ~400-470 tok/s | ~33-40 tok/s | ~18.6-23.4s |
| 16,384 | ~276-277 tok/s | ~17.1-17.9 tok/s | ~57.7-62.0s |
| 32,768 | ~203-204 tok/s | ~11.5-11.8 tok/s | ~152-158s |

VRAM stayed comfortable throughout (5.4GB at 1,024 tokens up to 6.6GB at 32,768, all `100% GPU`
per `ollama ps`) — this isn't a VRAM-cliff story, it's pure prefill-cost scaling. The practical
implication: a conversational product that dutifully sets `think: false` and keeps growing
conversation history can still see multi-second-to-multi-minute TTFT purely from accumulated
context length, with no reasoning trace involved at all. The two TTFT levers (reasoning on/off,
and total context size) are separate and both need budgeting for, not just the one this project
found first.

## Pi: speed — every vendor estimate was optimistic

All measurements below assume the model is already loaded into memory. Separately, this Pi runs
off an NVMe HAT (confirmed, not a MicroSD card) — that matters for per-request model-load latency
in a real product, but not for the generation-speed figures below, which measure inference only.

All six shortlist models were measured directly with `ollama run --verbose` for the first time
after months of relying on vendor-published estimates. Every single estimate turned out
optimistic — actual speeds landed at roughly 37-81% of the published number, depending on model:

| Model | File size | Estimated | **Measured** | % of estimate |
|---|---|---|---|---|
| `qwen2.5:1.5b` | 0.99 GB | ~15-17 tok/s | **12.08 tok/s** | 71-80% (fastest measured) |
| `llama3.2:1b` | 1.3 GB | ~20-22 tok/s | **8.24 tok/s** | 37-41% (biggest miss) |
| `gemma2:2b` | 1.6 GB | 5-10 tok/s | **6.66 tok/s** | within its (wide) band |
| `llama3.2:3b` | 2.0 GB | ~8-9 tok/s | **5.55 tok/s** | 62-69% |
| `qwen2.5:3b` | 1.9 GB | ~8-9 tok/s | **5.49 tok/s** | 61-69% |
| `phi3.5:3.8b` | 2.4 GB | ~6-7 tok/s | **4.87 tok/s** | 70-81% (slowest measured) |

The methodology finding that came out of this: **on-disk GGUF file size, not nominal parameter
count, predicts Pi 5 CPU speed almost exactly.** Across all six models, tok/s × file-size(GB)
lands in a tight 10.4-11.9 range; dividing by the Pi's ~17 GB/s memory-bandwidth ceiling shows
every model achieved ~61-70% of theoretical bandwidth-limited throughput. That's why
`qwen2.5:1.5b` (smallest file) beat every other model including both "3B"-labeled ones, and why
`llama3.2:1b` beat `gemma2:2b` despite the "1B" tag sounding smaller than "2B."

7-9B models (mistral:7b, llama3.1:8b, gemma2:9b, deepseek-r1:8b, llava:7b) all landed at ~2.0-2.9
tok/s in an earlier round — well below the shortlist above, confirming that tier is only viable
for patient/batch use on the base Pi, not live interactive chat.

## Pi: the speed leader is the weakest model on quality

This is the single most important finding on the Pi platform, and it inverted the original
recommendation. Across all four completed quality suites (extraction, agentic, chat, coding):

| Model | Extraction | Agentic | Chat | Coding |
|---|---|---|---|---|
| `llama3.2:3b` | 3/3 | **4/4** | — | — |
| `qwen2.5:3b` | 3/3 | **4/4** | — | — |
| `llama3.2:1b` | 2/3 | 2/4 | — | — |
| `gemma2:2b` | 3/3 | 0/4* | — | — |
| `phi3.5:3.8b` | 0/3* | 0/4* | — | — |
| `qwen2.5:1.5b` (speed leader) | 1/3 | 2/4 | 2/4 | 1/4 |

\* driven by a memory-pressure bug (below), not a content weakness.

Chat and coding were scored the same way, across all six models, as extraction and agentic — the
per-model breakdown isn't reproduced above for space, but the suite totals are: **chat 13/24**
(4 tests × 6 models) and **coding 15/24**. `qwen2.5:1.5b`'s row is filled in for chat/coding above
because it's the model this section is specifically about (the fastest model finishing weakest on
every workload it was tested on); the full 6×4 grid for all four suites lives in the project's own
run log, not reproduced here.

`qwen2.5:1.5b` — the outright fastest model on the shortlist by a wide margin — finishes as the
**weakest model on quality across all four completed suites**. `llama3.2:3b` and `qwen2.5:3b` are
the only two models with a perfect record on every judge-free test given to them. Per the "quality
bar first, speed second" priority order, **the Pi recommendation is `llama3.2:3b` or `qwen2.5:3b`,
not the fastest model** — a case where optimizing for the obvious number (tok/s) would have
produced the wrong pick.

## Pi: RAM pressure is this platform's version of the PC's VRAM cliff

A 6-provider promptfoo matrix is a different regime from a single model loaded alone: the six
models' combined file size (~10.2GB) exceeds the Pi's 7.87GB usable RAM, forcing repeated
load/evict cycles. `monitor.sh` caught real memory pressure on two separate runs — the extraction
suite pushed available RAM down to 403MB and swap up to 1.4GB; a shorter agentic-suite run (82
seconds vs. ~3 minutes) still pushed available RAM to 555MB and swap to 515MB, ruling out "only
happens on long runs."

**Confirmed downstream effect: a model can return completely empty output (0 completion tokens)
purely from this pressure, even though it works perfectly standalone.** First seen with
`phi3.5:3.8b` (reproduced twice), then also hit `gemma2:2b` — a smaller model, earlier in provider
order — ruling out "largest file + last in order" as the mechanism. This looks like a
run-dependent reliability failure tied to memory pressure at the moment of a specific call, not a
fixed weakness of any one model's size or position. Practical implication: a Pi product that
serves multiple different local models from shared RAM carries a real, twice-demonstrated risk of
silent empty-output failures unrelated to any individual model's actual capability. A
single-fixed-model deployment (the realistic product pattern) doesn't hit this specific failure
mode, but the underlying constraint — Pi RAM, not VRAM — is still the thing to design around.

**Update — a possible mechanism:** Ollama's scheduler evicts a loaded model once a new model's
predicted VRAM/RAM need crosses a `requireFull`-triggered ~80% of available memory. That threshold
crossing, not pure timing, may be what determines whether a given request lands on an evicted
model and comes back empty — worth checking against the specific cases logged here before treating
this mechanism as fully confirmed. Still listed as an open item below.

Separately, `OLLAMA_MAX_LOADED_MODELS=1` turned out to be a **required Pi setting**, not just a PC
one. A fresh Ollama reinstall doesn't set it automatically, and its absence caused a genuine hard
stall (two models loaded simultaneously, 2-3 `llama-server` processes pinned near 99% CPU across
all 4 cores, `ollama ps` showing concurrent residency) rather than an obvious error — see
`README.md`'s Pi setup section for the fix.

## Local vs. cloud: speed, cost, and TTFT

One concrete comparison point against closed cloud models (Artificial Analysis snapshot, paired
against this project's own measured local numbers):

- **Speed:** `qwen2.5:3b` (138.9 tok/s) beats every cloud model checked except Gemini 2.5 Flash
  (192 tok/s) — but every local model past the 3B tier (i.e., every local model with real
  capability) sits below the entire cloud field tested (Claude 4.5 Sonnet 37.9 tok/s, GPT-5 high
  84.7, `qwen3.5:9b` only 18.0, `deepseek-r1:14b` only 8.3).
- **Marginal cost:** local electricity-only cost per 1M output tokens (~320W system draw,
  $0.16/kWh) runs $0.10 (`qwen2.5:3b`) to $1.71 (`deepseek-r1:14b`) vs. $2-15 for cloud API list
  price — 10-100x cheaper on marginal cost alone, before the hardware's sunk cost even enters it.
- **TTFT:** Claude 4.5 Sonnet answers in 1.43s; GPT-5 and GPT-5 mini in "high" reasoning mode take
  64-110s to first token. The comparable *measured* local number, from the voice-assistant build
  in this repo: `qwen3.5:9b` with `think: false` hits 2.39s-2.47s TTFT — competitive with Claude's
  cloud number and nowhere near GPT-5's high-reasoning tail. With reasoning left on (Ollama's
  default), the same model measures 14.4s-83.6s — worse than every cloud model checked here,
  including GPT-5's high-reasoning mode at the low end of its own range. The `think` flag is not a
  minor tuning knob for a hybrid-reasoning model on this hardware; it's the difference between
  beating Claude's TTFT and having the worst number in the whole comparison. It's also not a
  universal lever — see the Pi vision suite above, where the same flag has no effect on a
  different model. Separately, the needle-in-haystack section above shows a second, independent
  TTFT driver (raw context size) that applies even with `think: false` set correctly.
- **Quality:** a rigorous version of this (running this project's own promptfoo suites against
  real cloud APIs) was scoped and then explicitly declined over API cost — permanently out of
  scope, not a pending item. The comparison above is speed/cost/TTFT only; there is still no
  apples-to-apples local-vs-cloud *quality* data anywhere in this repo, only the local-only scores
  in the sections above.

## Open items

- The empty-output-under-memory-pressure bug's exact mechanism isn't fully isolated — see the
  scheduler-eviction lead noted in the Pi RAM-pressure section above; a deliberate
  provider-reordering test around that ~80% threshold would help confirm or rule it out.
- The PC's CPU baseline (6.9 tok/s) has only been measured by disabling GPU acceleration at the
  env-var level, never confirmed with Ollama's own `num_gpu: 0` request parameter — a cleaner
  isolation lever worth a quick confirmatory run on an already-measured split-mode model (e.g.
  `gemma4:31b`) to separate "CPU baseline" from "GPU split-mode overhead" without relying on
  environment-variable side effects.
- The llama.cpp-vs-Ollama speed comparison on the Pi was never run, despite a native ARM64
  llama.cpp build (via a from-source Jan Desktop build) being completed during this project — see
  `README.md`'s Pi setup section for what that build took.
- Ollama version: the PC was updated from v0.33.2 to v0.34.1 partway through the project; the Pi
  has been on v0.34.1 throughout. Most PC numbers are on v0.34.1, but which specific early PC runs
  predate the update isn't logged, and none were re-run after it — not confirmed to matter.
- Which of the three PC GPU env vars (or the restart) actually fixed the silent CPU fallback is not
  isolated — see the VRAM-cliff section above. A clean test is: unset all three, confirm CPU
  fallback in the server log, then set them one at a time with a restart between each.
- Pi thermal reliability under sustained load: the root mechanism is now identified (sustained
  per-model power draw against the cooler's real ceiling — see the thermal section above), but
  that's confirmed from one instrumented reproduction, not a sweep across models/loads; treat the
  ~7W/~12W bracket as a single data point, not a calibrated threshold. Two items that reproduction
  left open: whether the flat 3000MHz clock reading during the firmware's soft-temperature
  throttle reflects real reaction-time lag or a stale `scaling_cur_freq` read (next test: sample
  `vcgencmd measure_clock arm` directly), and making the Pi's journal persistent
  (`sudo mkdir -p /var/log/journal && sudo systemd-tmpfiles --create --prefix /var/log/journal`)
  before any future crash-hunting, so `journalctl` isn't blind to the reset the way it was here. An
  application-level watchdog/auto-restart design is still the standing plan for any real product on
  this hardware; the existing watchdog's kill mechanism still needs per-workload timing tuning (too
  aggressive, and it kills normal cases — see the Pi vision suite above).
- The Costa-receipt non-termination anomaly on the Pi vision suite (one ~18.86-minute non-
  terminating run vs. two clean 1-2 minute completions at the identical config) is unreconciled —
  see the Pi vision section above.
- Phone platform: dropped from scope entirely before any device was picked or any data was
  collected. Not a gap in this repo's coverage — a deliberate decision. If phone benchmarking is
  ever revisited, it should start fresh rather than picking this back up.
