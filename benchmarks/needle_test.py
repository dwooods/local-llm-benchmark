"""
Needle-in-a-haystack context-degradation test.

Methodology (see Project Instructions Sec 10 backlog item #4 / Sec 9 sequencing
note): insert one distinctive, exact-match "needle" fact at a controlled
position inside a haystack of unrelated filler text, ask the model to recall
it, and sweep both haystack length (approx. token count) and needle depth
(0% = start of document, 100% = end, right before the question). Scoring is
a deterministic substring match, not an LLM judge -- this project has already
found one false-negative judge grade (deepseek-r1:14b on the coding suite),
so recall on an exact fact does not need a second model's opinion.

Reuses the exact Ollama call pattern already proven in voice_assistant.py:
streaming /api/chat, keep_alive=-1 (no cold-reload penalty mid-run), and
think=False (keeps this a pure recall test, not a reasoning-latency test --
see the Sept 18 TTFT finding for why reasoning traces would otherwise dominate
the timing numbers and are irrelevant to what this test is measuring).

Known constraint from Sec 2: num_ctx is fixed at model LOAD time, not per
request. So the script calls `ollama stop` between context-size blocks to
force a reload with the new num_ctx -- skipping this would silently keep
testing at whatever num_ctx the model happened to load with first.

Results append to needle_results.csv after every cell, so an interrupted run
still leaves usable partial data.
"""

import csv
import json
import random
import subprocess
import time
from pathlib import Path

import requests

# ---- config ----
OLLAMA_MODEL = "qwen3.5:9b"
OLLAMA_URL = "http://localhost:11434/api/chat"
OUTPUT_CSV = "needle_results.csv"

# Target haystack sizes in approx. tokens. This is a QUICK first pass --
# uncomment the larger sizes below once you've seen how long the small tier
# actually takes on this rig. The project's own prefill-speed data point
# (deepseek-r1:14b: 38-75 tok/s prefill, see Benchmark Log) suggests a 16K+
# prompt could take several minutes PER CELL, not seconds -- don't assume
# GPU prefill is fast here until you've timed it.
CONTEXT_TARGETS = [1024, 2048, 4096, 8192]
CONTEXT_TARGETS += [16384, 32768]  # extending now -- 8K run was fast (~2.5 min/12 cells), full GPU residency with headroom

# DEPTH_FRACTIONS = [0.0, 0.5, 1.0]
DEPTH_FRACTIONS = [0.0, 0.25, 0.5, 0.75, 1.0]  # full sweep now that we have time budget

NEEDLE_SECRET = "ZULU-FOXTROT-8841"
NEEDLE_SENTENCE = (
    f"For maintenance purposes, the override code for the north wing "
    f"backup generator is {NEEDLE_SECRET}."
)
QUESTION = (
    "Based only on the document above, what is the override code for the "
    "north wing backup generator? Reply with only the code, nothing else."
)
SYSTEM_PROMPT = (
    "You are a careful reading-comprehension assistant. Answer strictly "
    "using the document the user gives you. Give short, exact answers with "
    "no commentary, preamble, or explanation."
)

random.seed(20260918)

FILLER_FACTS = [
    "Honey never spoils if it is stored in a sealed container.",
    "The average cumulus cloud weighs about five hundred tons.",
    "Bamboo can grow up to thirty five inches in a single day under ideal conditions.",
    "A group of flamingos is called a flamboyance.",
    "The Eiffel Tower grows about six inches taller in summer heat.",
    "Octopuses have three hearts and blue blood.",
    "Bananas are botanically classified as berries, while strawberries are not.",
    "Sound travels roughly four times faster in water than in air.",
    "The shortest war in recorded history lasted about thirty eight minutes.",
    "A bolt of lightning is roughly five times hotter than the surface of the sun.",
    "Wombats produce cube shaped droppings.",
    "There are more possible chess games than atoms in the observable universe.",
    "A single strand of spaghetti is called a spaghetto.",
    "The unicorn is the national animal of Scotland.",
    "Sharks existed before trees appeared on Earth.",
    "A jiffy is an actual unit of time equal to one hundredth of a second.",
    "The inventor of the frisbee was turned into a frisbee after he died.",
    "Cows have best friends and get stressed when separated from them.",
    "A cloud can hold more water than a bathtub, several thousand times over.",
    "The dot over a lowercase i or j is called a tittle.",
    "Venus is the only planet that rotates clockwise.",
    "Butterflies taste with their feet.",
    "The longest recorded flight of a chicken is thirteen seconds.",
    "There is a species of jellyfish that is biologically immortal.",
    "A day on Mercury lasts longer than a year on Mercury.",
    "The Great Wall of China is not actually visible from space with the naked eye.",
    "Polar bears have black skin underneath their white fur.",
    "The smell of fresh cut grass is a plant distress signal.",
    "A blue whale's heart is roughly the size of a small car.",
    "Koalas have fingerprints almost indistinguishable from human fingerprints.",
    "It rains diamonds on Neptune and Uranus, according to some models.",
    "The average person walks the equivalent of five times around the world in a lifetime.",
    "Some cats are actually allergic to humans.",
    "The first oranges were not orange, but green.",
    "A group of crows is called a murder.",
    "Hot water can freeze faster than cold water under certain conditions.",
    "Peanuts are not technically nuts, they are legumes.",
    "An ostrich's eye is bigger than its brain.",
    "The Mona Lisa has no eyebrows.",
    "Slugs have four noses.",
    "Scotland has an official national sport that is not soccer, it is curling.",
]


def build_haystack(target_tokens: int, depth_fraction: float):
    """Build filler text of roughly target_tokens length (~1.3 tokens/word),
    insert the needle sentence at the given fractional depth."""
    approx_words_needed = int(target_tokens / 1.3)
    sentences = []
    words_so_far = 0
    pool = FILLER_FACTS[:]
    while words_so_far < approx_words_needed:
        random.shuffle(pool)
        for s in pool:
            sentences.append(s)
            words_so_far += len(s.split())
            if words_so_far >= approx_words_needed:
                break
    insert_at = int(len(sentences) * depth_fraction)
    sentences.insert(insert_at, NEEDLE_SENTENCE)
    return " ".join(sentences)


def ollama_ps():
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip().replace("\n", " | ")
    except Exception as e:
        return f"(ollama ps failed: {e})"


def stop_model():
    subprocess.run(["ollama", "stop", OLLAMA_MODEL], capture_output=True, text=True, timeout=30)


def normalize(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def run_cell(haystack: str, num_ctx: int):
    prompt = f"{haystack}\n\n{QUESTION}"
    t0 = time.time()
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "options": {"num_ctx": num_ctx},
            "keep_alive": -1,
            "think": False,
        },
        stream=True,
    )
    full_text = ""
    t_first_token = None
    final_chunk = {}
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        piece = chunk.get("message", {}).get("content", "")
        if piece and t_first_token is None:
            t_first_token = time.time()
        full_text += piece
        if chunk.get("done"):
            final_chunk = chunk
            break
    t_done = time.time()

    ttft = (t_first_token - t0) if t_first_token else None
    prompt_eval_count = final_chunk.get("prompt_eval_count")
    eval_count = final_chunk.get("eval_count")
    prompt_eval_duration = final_chunk.get("prompt_eval_duration", 0) / 1e9
    eval_duration = final_chunk.get("eval_duration", 0) / 1e9

    return {
        "response": full_text.strip(),
        "ttft": ttft,
        "total_time": t_done - t0,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
        "prefill_tok_s": (prompt_eval_count / prompt_eval_duration) if prompt_eval_duration else None,
        "gen_tok_s": (eval_count / eval_duration) if eval_duration else None,
    }


def main():
    csv_path = Path(OUTPUT_CSV)
    is_new = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "model", "target_ctx", "num_ctx_setting", "depth_fraction",
                "actual_prompt_tokens", "found_needle", "ttft_s", "total_time_s",
                "prefill_tok_s", "gen_tok_s", "ollama_ps", "response_text",
            ])

        total_cells = len(CONTEXT_TARGETS) * len(DEPTH_FRACTIONS)
        cell_num = 0
        run_start = time.time()

        for target_tokens in CONTEXT_TARGETS:
            num_ctx = target_tokens + 512
            print(f"\n=== Switching context target to {target_tokens} (num_ctx={num_ctx}) ===")
            stop_model()
            time.sleep(1)

            for depth in DEPTH_FRACTIONS:
                cell_num += 1
                haystack = build_haystack(target_tokens, depth)
                elapsed = time.time() - run_start
                avg = elapsed / (cell_num - 1) if cell_num > 1 else 0
                remaining = avg * (total_cells - cell_num + 1)
                print(f"[{cell_num}/{total_cells}] target_ctx={target_tokens} depth={depth:.0%} "
                      f"(elapsed {elapsed:.0f}s, est. remaining {remaining:.0f}s)")

                result = run_cell(haystack, num_ctx)
                found = normalize(NEEDLE_SECRET) in normalize(result["response"])
                ps_status = ollama_ps() if depth == DEPTH_FRACTIONS[0] else ""

                print(f"    -> found={found}  ttft={result['ttft']}  "
                      f"actual_prompt_tokens={result['prompt_eval_count']}  "
                      f"gen_tok_s={result['gen_tok_s']}")
                print(f"    -> response: {result['response'][:200]}")

                writer.writerow([
                    OLLAMA_MODEL, target_tokens, num_ctx, depth,
                    result["prompt_eval_count"], found, result["ttft"],
                    result["total_time"], result["prefill_tok_s"], result["gen_tok_s"],
                    ps_status, result["response"].replace("\n", " ")[:500],
                ])
                f.flush()

    print(f"\nDone. Results in {csv_path.resolve()}")


if __name__ == "__main__":
    main()
