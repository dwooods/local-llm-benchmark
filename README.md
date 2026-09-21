# Local LLM Benchmark

Benchmarking open-weight LLMs running **entirely locally** (no cloud calls) across two very
different pieces of hardware — a high-end Windows gaming PC and a Raspberry Pi 5 — to find, for
each machine, the best model + settings combination across five workloads: coding assistance,
agentic/tool use, structured data extraction from text, structured data extraction from images
(OCR/vision), and general chat/Q&A.

This repo holds the actual [promptfoo](https://www.promptfoo.dev/) configs used to score every
model, the receipt images used for the vision/OCR suite, a working local voice-assistant
(Whisper → Ollama → Piper) built as part of the project, a long-context ("needle in a haystack")
degradation test, and a full write-up of what was found — including several real bugs in the test
harness itself, not just model results. See [`FINDINGS.md`](FINDINGS.md) for the results and the
story; this file is about replicating the runs yourself.

Everything here runs against [Ollama](https://ollama.com/) on each machine — no API keys, no
network calls to score a model (the only exception is one deliberate LAN call described below).

## Repo layout

```
benchmarks/                  promptfoo suites — run these to reproduce a scored suite
  promptfoo-*.yaml           PC configs (chat, coding, extraction, agentic, vision)
  promptfoo-*-pi.yaml        same four workloads, tuned for the Pi's 8GB RAM / CPU-only inference
  assertions/check-fields.js deterministic scorer used by the vision suite (no LLM judge)
  images/                    the 6 receipt/document photos used by the vision/OCR suite
  monitor.sh                 RAM/swap logger for the Pi runs (see "Pi setup" below)
  needle_test.py             long-context ("needle in a haystack") degradation test — see
                             "Long-context test" below
  needle_results.csv         measured output of needle_test.py, appended to as it runs
scripts/
  encode_images.py           turns images/*.jpg|png into the base64 .txt files the vision yamls
                              expect — see "Vision suite setup"
voice-assistant/
  voice_assistant.py         push-to-talk local voice assistant: faster-whisper (STT) → Ollama
                             (streamed, with real TTFT) → piper-tts (TTS), with per-stage timing
```

Three small isolated repro configs used along the way to debug promptfoo config bugs (the `tools`-
nesting bug, the hand-injected `tool_calls` string-vs-object bug, and a cloud-provider tool-call-
shape check) aren't committed here — the bugs themselves, and what each repro proved, are written
up in full in [`FINDINGS.md`](FINDINGS.md).

## Hardware this was run on

| | Windows PC ("DavidPC") | Raspberry Pi 5 |
|---|---|---|
| CPU | Intel Core i7-13700K (16C/24T) | Pi 5 SoC (CPU-only, no GPU offload) |
| GPU | AMD Radeon RX 6700 XT, 12GB VRAM | — |
| RAM | 128GB DDR | 8GB (7.87GB usable) |
| Storage | NVMe SSD | NVMe HAT (confirmed — not a MicroSD card) |
| OS | Windows 11 | Debian 13 ("trixie"), 64-bit |
| Runtime | Ollama v0.33.2 | Ollama v0.34.1 |

The PC is the primary inference engine (12GB VRAM is the binding constraint — see FINDINGS.md for
the "12GB cliff" data); the Pi is CPU-only and memory-bandwidth-bound, representing the
"embedded/offline" end of the spectrum. The two runtimes have drifted a minor version apart — not
confirmed to matter, but worth reconciling before trusting a cross-machine comparison that assumes
identical behavior (see FINDINGS.md open items).

## Prerequisites (both machines)

1. Install [Ollama](https://ollama.com/download).
2. Install [Node.js](https://nodejs.org/) (needed for `npx promptfoo`).
3. `git clone` this repo, then `cd local-llm-benchmark/benchmarks`.

## PC setup

The RX 6700 XT needs these forced on for Ollama to use GPU acceleration instead of silently
falling back to CPU (confirmed regression path: a Windows/driver update can reset these — always
re-check before a benchmark session):

```powershell
[System.Environment]::SetEnvironmentVariable('HSA_OVERRIDE_GFX_VERSION', '10.3.0', 'User')
[System.Environment]::SetEnvironmentVariable('OLLAMA_NUM_PARALLEL', '1', 'User')
[System.Environment]::SetEnvironmentVariable('OLLAMA_ORIGINS', '*', 'User')
[System.Environment]::SetEnvironmentVariable('OLLAMA_MAX_LOADED_MODELS', '1', 'User')
```

Restart Ollama after setting these, then sanity-check GPU acceleration is actually active:

```powershell
ollama run qwen2.5:3b --verbose "hi"
```

Expect roughly 140-150 tok/s eval rate. If you see ~50 tok/s or ~7 tok/s instead, the GPU isn't
being used — recheck the env vars and the AMD driver (`Adrenalin Edition → Check for Updates`).

**Also confirm these are NOT set:** `OLLAMA_FLASH_ATTENTION` and `OLLAMA_KV_CACHE_TYPE`. Both were
tested here as VRAM-saving levers and instead dropped `qwen2.5:3b`'s eval rate from ~150 tok/s to
~52 tok/s on this AMD/Vulkan card — see FINDINGS.md. Unset either one if a driver update or a
previous session left it set.

In Ollama's own desktop Settings, turn OFF "Enable cloud models and web search" and "Expose Ollama
to the network" before benchmarking (the latter needs to be temporarily re-enabled only for the
Pi's remote-judge pattern below), and turn off auto-updates for the duration of a benchmark
session so a version bump can't land mid-comparison.

Pull the models used in the PC suites:

```powershell
ollama pull qwen2.5:3b; ollama pull qwen2.5:latest; ollama pull qwen3.5:9b
ollama pull llama3:latest; ollama pull deepseek-r1:14b; ollama pull phi4:14b
ollama pull gpt-oss:20b; ollama pull qwen2.5-coder:7b; ollama pull qwen2.5-coder:14b
ollama pull deepseek-coder-v2:16b; ollama pull gemma4:12b
ollama pull glm-ocr; ollama pull qwen3-vl:8b
```

`deepseek-r1:14b` is used as the fixed LLM-judge (`llm-rubric`) across every suite that needs one
— it's deliberately kept separate from every model under test so nothing grades its own output.

Run a suite (one at a time — concurrent suites just contend for the same Ollama instance):

```powershell
npx promptfoo@latest eval -c promptfoo-chat.yaml -j 1 --no-cache
npx promptfoo@latest eval -c promptfoo-coding.yaml -j 1 --no-cache
npx promptfoo@latest eval -c promptfoo-extraction.yaml -j 1 --no-cache
npx promptfoo@latest eval -c promptfoo-agentic.yaml -j 1 --no-cache
npx promptfoo@latest view
```

`--no-cache` matters if you ever re-run the same suite to check reproducibility — promptfoo
caches responses to disk across separate CLI invocations, and a "rerun" without it can silently
replay a cached result instead of actually querying the model again. Note that `--no-cache` only
suppresses cache *reads*: a run launched with it still writes its results to the cache as normal,
so a later invocation that omits the flag can replay what that run generated (see FINDINGS.md).

## Pi setup

**Read this before running anything sustained on the Pi.** Six confirmed hard, silent reboots
happened during this project under sustained CPU-bound inference — with the official Active Cooler
physically installed and its fan confirmed running. Active cooling reduces the risk but does
**not** eliminate it; there is no known setting or config change that closes it out. Don't run an
unattended multi-hour suite on the Pi assuming a clean finish — checkpoint intermediate results
(e.g. `--output` to a file per provider, or rely on promptfoo's own cache) so a crash mid-suite
doesn't lose already-completed test cases. Full details, including what's already been ruled out
(power supply) and what hasn't (the actual crash trigger), are in FINDINGS.md.

A kill-and-cool thermal watchdog (poll temperature, kill the running inference process, let the
board cool before it reaches the crash zone) was built and used ad hoc during this project's
testing — see FINDINGS.md for what it caught and where it fell short (a kill window tuned too
aggressively for the vision suite killed normal-length cases before they could finish, and an
early PID-based kill was defeated by process orphaning until fixed to pattern-match and
`pkill -f` on a distinguishing config filename instead). It is **not** included in this repo as a
reusable script — it was built and tuned once, for one specific run, not as a general-purpose
tool. Treat the watchdog write-up in FINDINGS.md as a documented mitigation *pattern* to
reimplement and tune for your own workload, not something you can run directly from this repo.

Ollama on a fresh install does **not** set `OLLAMA_MAX_LOADED_MODELS=1` on its own, and its
absence causes silent concurrent-model CPU thrashing on a multi-provider matrix run rather than an
obvious error. Set it via a systemd drop-in (the `[Service]` header is required — omitting it is
silently ignored with no error at all):

```bash
sudo systemctl edit ollama.service
```

```ini
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
sudo systemctl cat ollama   # confirm the drop-in actually took
```

Pull the Pi-scale models:

```bash
ollama pull llama3.2:1b; ollama pull llama3.2:3b
ollama pull qwen2.5:1.5b; ollama pull qwen2.5:3b
ollama pull gemma2:2b; ollama pull phi3.5:3.8b
ollama pull minicpm-v4.6; ollama pull qwen3-vl:2b
```

Run each suite serially and with `-j 1` (not optional on the Pi — the CPU-bound, single-model
setup needs it to avoid resource contention):

```bash
npx promptfoo@latest eval -c promptfoo-extraction-pi.yaml -j 1 --no-cache
npx promptfoo@latest eval -c promptfoo-vision-pi.yaml -j 1 --no-cache
```

**Judge-graded suites on the Pi (chat, coding, agentic tests 3-4):** the Pi can't fit
`deepseek-r1:14b` (~9GB) in 7.87GB usable RAM, so those suites route only the *grading* call
across the LAN to the PC's Ollama instance, while generation stays entirely on the Pi. This is the
"Option 4" pattern used throughout `promptfoo-chat-pi.yaml`, `promptfoo-coding-pi.yaml`, and the
later tests of `promptfoo-agentic-pi.yaml` — look for `openai:chat:deepseek-r1:14b` providers
pointed at `http://<PC-LAN-IP>:11434/v1` with a dummy `apiKey`. To use it:

1. On the PC, temporarily turn ON "Expose Ollama to the network" in Ollama's Settings.
2. Edit the `apiBaseUrl` in `promptfoo-chat-pi.yaml` / `promptfoo-coding-pi.yaml` /
   `promptfoo-agentic-pi.yaml` to your PC's actual LAN IP.
3. Run the suite from the Pi as above.
4. Turn the PC's network-exposure toggle back OFF when you're done — it should not be left on by
   default.

Optionally watch RAM/swap pressure during a multi-model matrix run (this is what first caught the
empty-output-under-memory-pressure bug documented in FINDINGS.md):

```bash
bash monitor.sh &
```

**Not used for any scored result in this repo:** a native ARM64 build of Jan Desktop, with its own
llama.cpp backend, was also gotten running on this Pi during the project — compiled from source,
not the prebuilt x86_64 binary. It needed `libayatana-appindicator3-dev` for Tauri's tray-icon
feature, a manual `vendor/llama.cpp` checkout to work around a submodule that wouldn't init
cleanly, `-mno-outline-atomics`/`-latomic`/`-lgcc` compile flags to work around the default GCC
toolchain's ARM v8.1+ LSE-atomics handling, and a manual copy of the compiled `libggml*.so` files
into `resources/bin/` (they aren't copied there automatically). Everything actually scored in this
repo runs against Ollama; the llama.cpp-vs-Ollama head-to-head speed comparison that build would
enable was never run — see FINDINGS.md open items.

## Vision suite setup (both machines)

The vision configs (`promptfoo-vision-pc.yaml`, `promptfoo-vision-pi.yaml`) reference
`images/<name>_b64.txt` files that aren't committed to this repo (committing both an image and its
~33%-larger base64 text duplicate doubles storage for no benefit). Generate them once, from inside
`benchmarks/`:

```bash
python scripts/encode_images.py
```

Then run the suite as usual:

```bash
npx promptfoo@latest eval -c promptfoo-vision-pc.yaml -j 1 --no-cache   # PC
npx promptfoo@latest eval -c promptfoo-vision-pi.yaml -j 1 --no-cache  # Pi
```

**Both vision configs set `num_predict` explicitly (4096 on the Pi) — do not remove it.** A vision
model without a reliable EOS token can otherwise run all the way to the `num_ctx` ceiling before
stopping, turning one stuck test case into a many-minutes-long hang at Pi CPU speeds. This isn't
hypothetical — it's what happened during this project, and it's also the reason the Pi's thermal
watchdog (see "Pi setup" above) needs its kill window tuned longer than a model's normal per-case
time, not shorter.

Two of the six receipts are worth knowing about before you eyeball results: the "La Cabaña"
receipt prints two totals (card $57.71 / cash $55.49 — ground truth here uses the card total), and
the "Costa" receipt is priced in EUR and has a pre-tax subtotal printed alongside the VAT-inclusive
total (see FINDINGS.md for what every model tested actually did with that ambiguity, and for a
separate, unreconciled non-termination anomaly specific to this receipt on the Pi).

## Voice assistant

A working push-to-talk local voice pipeline built on the PC to get a real, measured
mic-to-spoken-answer latency number instead of an estimate — faster-whisper for speech-to-text,
Ollama's streaming API for the LLM (captures real time-to-first-token), and piper-tts for
text-to-speech.

```powershell
pip install faster-whisper piper-tts sounddevice numpy
python -m piper.download_voices en_US-lessac-medium
python voice-assistant/voice_assistant.py
```

Edit the constants at the top of `voice_assistant.py` (`OLLAMA_MODEL`, `THINK`, `STRIP_MARKDOWN`,
`INPUT_DEVICE`, `OUTPUT_DEVICE`, `VOICE_PATH`) to match your own hardware — run
`python -c "import sounddevice as sd; print(sd.query_devices())"` first to find your mic/speaker
device indices. `SYSTEM_PROMPT` and the `ABBREVIATIONS`/`strip_markdown()` regex list just below
it are also plain top-of-file constants if you want the model to speak differently (e.g. keep
bullet points, add a persona) — set `STRIP_MARKDOWN = False` to hear the model's raw output
un-cleaned and see how much of the leakage `SYSTEM_PROMPT` alone actually stops.

**Three request-payload settings this script relies on, and why** (full numbers in FINDINGS.md —
these aren't optional tuning, the script misbehaves without them):

- `"options": {"num_ctx": 8192}` — at the default 4096, a hybrid-reasoning model's invisible
  chain-of-thought can consume the entire context budget on a non-trivial question, leaving zero
  tokens for the actual answer. The script crashes writing an empty WAV file when this happens.
- `"keep_alive": -1` — keeps the model resident between turns. Ollama's default 5-minute
  `keep_alive` evicts an idle model, so without this a cold-reload penalty gets silently counted
  as TTFT on the next turn (worse the *second* time, not better).
- `THINK` (top of file, `True`/`False`) — the single highest-leverage latency lever found in this
  project **for this model**. For a hybrid-reasoning model like the default `qwen3.5:9b`, the full
  reasoning trace generates silently before any visible token streams out, and Ollama's
  `/api/chat` doesn't expose that phase as separate timing — from the outside it just looks like a
  broken, catastrophically slow model. Measured TTFT was 14.4s-83.6s with reasoning on; setting
  `THINK = False` suppresses the trace entirely and collapsed measured TTFT to 2.39s-2.47s, with
  no visible quality regression on the questions tried. It's a no-op for non-reasoning models
  (e.g. `qwen2.5:3b`), so it's safe to leave set either way when you swap `OLLAMA_MODEL` — but it
  is not universal for every hybrid-reasoning model either: the Pi vision suite found one
  (`qwen3-vl:2b`) where the flag reaches Ollama but has no effect at all. Confirm it actually does
  something for whatever model you swap in before relying on it as a latency lever.

## Long-context test ("needle in a haystack")

`needle_test.py` measures context-length degradation on `qwen3.5:9b`: it inserts one exact-match
fact into a haystack of unrelated filler text at a controlled depth, asks the model to recall it,
and sweeps both haystack length (1,024 → 32,768 tokens) and needle depth (0% → 100%). Recall is
scored with a plain substring match — deliberately not an LLM judge, since this project already
found one confirmed false-negative judge grade elsewhere (see FINDINGS.md).

```powershell
pip install requests
python benchmarks/needle_test.py
```

It reuses the same request pattern as `voice_assistant.py` (`keep_alive: -1`, `think: false`) so
reasoning latency doesn't contaminate what's meant to be a pure recall/throughput measurement, and
calls `ollama stop` between context-size blocks to force a reload with the new `num_ctx` — that
setting is fixed at model load time, not per request, so skipping this would silently keep testing
at whatever `num_ctx` the model happened to load with first. Results append to
`needle_results.csv` after every cell, so an interrupted run still leaves usable partial data. Edit
`CONTEXT_TARGETS` and `DEPTH_FRACTIONS` at the top of the file to change the sweep — the larger
context sizes take meaningfully longer per cell (see FINDINGS.md for the actual prefill-speed
numbers before assuming a full sweep is quick).

## Results

See [`FINDINGS.md`](FINDINGS.md) for the actual per-model scores, the 12GB VRAM cliff data, the
Pi's speed-vs-quality tradeoff, the Pi's thermal-crash reliability findings, the long-context
(needle-in-a-haystack) results, real bugs found in the promptfoo configs themselves, and a
local-vs-cloud cost/speed/TTFT comparison.

## License

MIT — see [`LICENSE`](LICENSE).
