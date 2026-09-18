import json
import time
import wave

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel
from piper import PiperVoice

# ---- config ----
INPUT_DEVICE = 1   # Microphone (Webcam(X1))
OUTPUT_DEVICE = 4  # Speakers (USB2.0 Device)
SAMPLE_RATE = 16000
OLLAMA_MODEL = "qwen3.5:9b"  # swap to "qwen2.5:3b" for the fast-vs-quality comparison run
THINK = False  # hybrid-reasoning models (e.g. qwen3.5:9b) generate their whole reasoning trace
               # silently before the first visible token — leaving this True reproduces the
               # 14.4-83.6s TTFT measured in FINDINGS.md; False collapses it to ~2.4s with no
               # visible quality loss on the questions tried. Ignored by non-reasoning models
               # (e.g. qwen2.5:3b), so it's safe to leave set either way when you swap models.
VOICE_PATH = "en_US-lessac-medium.onnx"
OLLAMA_URL = "http://localhost:11434/api/chat"

print(f"Loading STT + TTS models (one-time cost, not counted per-turn)... model={OLLAMA_MODEL}")
stt_model = WhisperModel("base.en", device="cpu", compute_type="int8")
tts_voice = PiperVoice.load(VOICE_PATH)
print("Ready.\n")

_chunks = []

def _callback(indata, frames, time_info, status):
    _chunks.append(indata.copy())

def record_until_enter():
    global _chunks
    _chunks = []
    input("Press Enter to START recording...")
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                             device=INPUT_DEVICE, callback=_callback)
    stream.start()
    input("Recording... press Enter to STOP.")
    stream.stop()
    stream.close()
    return np.concatenate(_chunks, axis=0)

def run_turn():
    audio = record_until_enter()
    t_record_end = time.time()

    sf.write("turn_input.wav", audio, SAMPLE_RATE)

    segments, _ = stt_model.transcribe("turn_input.wav")
    transcript = "".join(s.text for s in segments).strip()
    t_stt_done = time.time()
    print(f"You said: {transcript}")

    if not transcript:
        print("(heard nothing, try again)\n")
        return

    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": transcript}],
            "stream": True,
            "options": {"num_ctx": 8192},
            "keep_alive": -1,
            "think": THINK,
        },
        stream=True,
    )
    full_text = ""
    t_first_token = None
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        piece = chunk.get("message", {}).get("content", "")
        if piece and t_first_token is None:
            t_first_token = time.time()
        full_text += piece
        if chunk.get("done"):
            break
    t_llm_done = time.time()
    print(f"Assistant: {full_text}")

    with wave.open("turn_output.wav", "wb") as wav_file:
        tts_voice.synthesize_wav(full_text, wav_file)
    t_tts_done = time.time()

    data, sr = sf.read("turn_output.wav", dtype="float32")
    t_playback_start = time.time()
    sd.play(data, sr, device=OUTPUT_DEVICE)
    sd.wait()

    ttft = (t_first_token - t_stt_done) if t_first_token else float("nan")
    print("\n--- Latency breakdown ---")
    print(f"STT:                {t_stt_done - t_record_end:.2f}s")
    print(f"LLM TTFT:           {ttft:.2f}s")
    print(f"LLM total gen:      {t_llm_done - t_stt_done:.2f}s")
    print(f"TTS synth:          {t_tts_done - t_llm_done:.2f}s")
    print(f"TOTAL mic->speech:  {t_playback_start - t_record_end:.2f}s")
    print("-------------------------\n")

if __name__ == "__main__":
    print("Ctrl+C to quit.\n")
    while True:
        try:
            run_turn()
        except KeyboardInterrupt:
            break
