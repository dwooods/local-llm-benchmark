# Findings

Everything below is from real measured runs on the two machines described in
[`README.md`](README.md) — a Windows PC with a 12GB AMD RX 6700 XT, and a Raspberry Pi 5 (8GB,
CPU-only) — using [promptfoo](https://www.promptfoo.dev/) against local Ollama models. Where a
number turned out to be wrong on a first pass (a bad test config, a judge that graded something
incorrectly, a bug in how a test was scored), that's noted explicitly rather than quietly
corrected — a couple of those turned out to be more interesting than the model scores themselves.

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

## PC: quality suite results

| Workload | Result |
|---|---|
| **Chat/Q&A** | `qwen3.5:9b`, `phi4:14b`, `gpt-oss:20b` scored **10/12** (corrected — see bug #2 below). `phi4:14b` reliably failed both Raspberry-Pi-5-knowledge questions, insisting the Pi 5 "hasn't been officially released" — a real, concrete instance of local models' training-cutoff risk on anything about recent hardware/products. |
| **Structured extraction** | `qwen2.5:latest`, `qwen3.5:9b`, `gemma4:12b` scored **8/9**. The one failure (`qwen2.5:latest`) was a genuine instruction-adherence gap — hallucinated trailing text after otherwise-correct JSON broke a strict JSON-only contract, not a data-accuracy problem. |
| **Agentic/tool use** | `qwen3.5:9b` and `qwen2.5:latest` both went a clean **4/4**. `llama3:latest` went **0/4** — complete empty-output failures on every test, not a transport issue. `qwen2.5-coder:14b` — this project's original "top pick for agentic coding" per general guidance — scored only **1/4**; a debug run found the model doesn't reliably populate Ollama's native `tool_calls` API field at all, it just prints a JSON-lookalike blob as plain text. That's a deeper problem than a formatting quirk: a real downstream tool-calling integration would never see it as an executable call. |
| **Coding** | `qwen2.5-coder:7b`, `qwen2.5-coder:14b`, `deepseek-coder-v2:16b`, `gemma4:12b` scored a corrected **16/16 (100%)** — see bug #3 below for why the first pass showed a failure that wasn't real. |
| **Vision/OCR** | See its own section below — this workload had the most methodology surprises of the five. |

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
   including for two models with otherwise-perfect records. An isolated debug run
   (`promptfoo-agentic-toolerror-debug.yaml`) showed the string form makes Ollama return an empty
   completion when replaying that turn, while the object form works correctly. This only affects
   turns you hand-build for a test, not a model's own freshly-generated tool_calls.

A fourth, non-config bug worth flagging separately: **the fixed judge model (`deepseek-r1:14b`)
produced a confirmed false-negative grade** on the coding suite — it marked a correct
`parse_duration` function as failing. Hand-verifying the generated code against the spec showed it
was actually right; corrected score is 16/16, not 15/16. This is a harder class of error to catch
than the three above, because it sits quietly inside an otherwise-clean run instead of producing an
obviously-broken uniform result — worth spot-checking judge-graded runs, not just trusting the
aggregate score.

**A fifth, separate gotcha: promptfoo caches responses to disk across separate CLI invocations,
not just within one `eval` run.** Re-running the exact same command with no config changes does
NOT re-query the model — it silently replays the cached response, even minutes later in a totally
separate process. The tell is a suspiciously fast run (~1 second instead of several minutes) and
output that matches a previous run character-for-character. Always append `--no-cache` when the
point of a rerun is to check reproducibility.

## Vision/OCR suite: `glm-ocr` vs. `qwen3-vl:8b`

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
settings). This is a real reliability finding about the model, not a config bug — a downstream
integration using `glm-ocr` needs either a stop-sequence workaround or a different model.

**Final scores, `num_ctx: 8192` (the trustworthy run):**

| Model | Raw score | Weighted score | Notes |
|---|---|---|---|
| `qwen3-vl:8b` | 5/6 (83%) | 5.5/6 (92%) | Best result on the vision suite |
| `glm-ocr` | 3/6 (50%) | 4.5/6 (75%) | Non-termination defect above; also the OCR-specialist model, arguably should have won this on data-accuracy alone |

**The VAT/net-vs-gross ambiguity (a durable, non-fixable-by-prompt-wording finding):** the Costa
coffee receipt has both a pre-tax subtotal (€4.24) and a VAT-inclusive grand total (€5.00). Both
models, at both context sizes — four independent observations — extracted €4.24, even after the
schema's `total` field was explicitly reworded to specify "the final amount actually paid or due,
including any tax, VAT, or service charge — not a pre-tax subtotal." Consistent enough across two
models and two runs to log as a genuine small-local-vision-model limitation on multi-total
receipts, not something worth another prompt-wording iteration.

## Pi: speed — every vendor estimate was optimistic

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
- **TTFT:** Claude 4.5 Sonnet answers in 1.43s; GPT-5 and GPT-5 mini in "high" reasoning mode
  take 64-110s to first token — cloud's TTFT advantage is real for some models and inverts hard
  for others. The PC voice assistant in this repo exists specifically to get a *real* measured
  local TTFT number instead of a derived estimate, closing a gap this project had flagged early on.
- **Quality:** deliberately left uncompared here — no cloud model has been run through this
  project's own promptfoo suites, so there's still no apples-to-apples local-vs-cloud quality
  data, only the local-only scores above.

## Open items

- Phone platform: zero benchmark data of any kind as of this write-up — app/model research only.
- The empty-output-under-memory-pressure bug's exact mechanism (timing? specific call content?)
  isn't isolated — a deliberate provider-reordering test would help narrow it down.
- Vision suite has only been run once at the corrected `num_ctx`; a second pass to confirm
  `glm-ocr`'s non-termination behavior is stable (not a one-off) would strengthen that finding.
- Ollama version drift: the Pi is on v0.34.1, the PC on v0.33.2 — not confirmed to matter yet, but
  worth reconciling before trusting any cross-machine comparison that assumes identical runtime
  behavior.
