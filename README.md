# Local LLM Benchmark

Benchmarking open-weight LLMs running **entirely locally** (no cloud calls) across two very
different pieces of hardware — a high-end Windows gaming PC and a Raspberry Pi 5 — to find, for
each machine, the best model + settings combination across five workloads: coding assistance,
agentic/tool use, structured data extraction (text and image/OCR), and general chat/Q&A.

This repo holds the actual [promptfoo](https://www.promptfoo.dev/) configs used to score every
model, the receipt images used for the vision/OCR suite, a working local voice-assistant
(Whisper → Ollama → Piper) built as part of the project, and a full write-up of what was found —
including several real bugs in the test harness itself, not just model results. See
[`FINDINGS.md`](FINDINGS.md) for the results and the story; this file is about replicating the
runs yourself.

Everything here runs against [Ollama](https://ollama.com/) on each machine — no API keys, no
network calls to score a model (the only exception is one deliberate LAN call described below).

## Repo layout

```
benchmarks/                  promptfoo suites — run these to reproduce a scored suite
  promptfoo-*.yaml           PC configs (chat, coding, extraction, agentic, vision)
  promptfoo-*-pi.yaml        same four workloads, tuned for the Pi's 8GB RAM / CPU-only inference
  promptfoo-*-debug.yaml     small isolated repros for the three promptfoo bugs found along the
                             way (see FINDINGS.md) — kept because the bugs are the actual finding
  assertions/check-fields.js deterministic scorer used by the vision suite (no LLM judge)
  images/                    the 6 receipt/document photos used by the vision/OCR suite
  monitor.sh                 RAM/swap logger for the Pi runs (see "Pi setup" below)
scripts/
  encode_images.py           turns images/*.jpg|png into the base64 .txt files the vision yamls
                              expect — see "Vision suite setup"
voice-assistant/
  voice_assistant.py         push-to-talk local voice assistant: faster-whisper (STT) → Ollama
                             (streamed, with real TTFT) → piper-tts (TTS), with per-stage timing
```

## Hardware this was run on

| | Windows PC ("DavidPC") | Raspberry Pi 5 |
|---|---|---|
| CPU | Intel Core i7-13700K (16C/24T) | Pi 5 SoC (CPU-only, no GPU offload) |
| GPU | AMD Radeon RX 6700 XT, 12GB VRAM | — |
| RAM | 128GB DDR | 8GB (7.87GB usable) |
| OS | Windows 11 | Debian 13 ("trixie"), 64-bit |
| Runtime | Ollama v0.33.2 | Ollama v0.34.1 |

The PC is the primary inference engine (12GB VRAM is the binding constraint — see FINDINGS.md for
the "12GB cliff" data); the Pi is CPU-only and memory-bandwidth-bound, representing the
"embedded/offline" end of the spectrum.

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
replay a cached result instead of actually querying the model again (see FINDINGS.md).

## Pi setup

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

Two of the six receipts are worth knowing about before you eyeball results: the "La Cabaña"
receipt prints two totals (card $57.71 / cash $55.49 — ground truth here uses the card total), and
the "Costa" receipt is priced in EUR and has a pre-tax subtotal printed alongside the VAT-inclusive
total (see FINDINGS.md for what every model tested actually did with that ambiguity).

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
  project. For a hybrid-reasoning model like the default `qwen3.5:9b`, the full reasoning trace
  generates silently before any visible token streams out, and Ollama's `/api/chat` doesn't expose
  that phase as separate timing — from the outside it just looks like a broken, catastrophically
  slow model. Measured TTFT was 14.4s-83.6s with reasoning on; setting `THINK = False` suppresses
  the trace entirely and collapsed measured TTFT to 2.39s-2.47s, with no visible quality
  regression on the questions tried. It's a no-op for non-reasoning models (e.g. `qwen2.5:3b`), so
  it's safe to leave set either way when you swap `OLLAMA_MODEL`.

## Results

See [`FINDINGS.md`](FINDINGS.md) for the actual per-model scores, the 12GB VRAM cliff data, the
Pi's speed-vs-quality tradeoff, three real bugs found in the promptfoo configs themselves, and a
local-vs-cloud cost/speed/TTFT comparison.

## License

MIT — see [`LICENSE`](LICENSE).
