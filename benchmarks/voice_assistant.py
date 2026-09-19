import json
import re
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
OLLAMA_MODEL = "qwen2.5:3b"  # swap to "qwen2.5:3b" for the fast-vs-quality comparison run
VOICE_PATH = "en_US-lessac-medium.onnx"
OLLAMA_URL = "http://localhost:11434/api/chat"
SYSTEM_PROMPT = (
    "You are a voice assistant. Your responses are converted to speech and read aloud, "
    "so respond in plain spoken sentences only, as if talking out loud to someone. "
    "Never use Markdown formatting: no asterisks, no bold or italics, no bullet points or "
    "numbered lists, no headers, no code blocks, no horizontal rules. "
    "Do not use abbreviations like e.g., i.e., etc., or vs. - spell them out as 'for example', "
    "'that is', 'and so on', 'versus'. Do not use dashes, en dashes, or em dashes to join "
    "clauses - use a full sentence or a comma instead. Do not use symbols such as #, /, or & - "
    "say the words they stand for. Spell out numbers the way you would say them out loud."
)

ABBREVIATIONS = [
    (r"\be\.g\.,?\s*", "for example "),
    (r"\bi\.e\.,?\s*", "that is "),
    (r"\betc\.", "and so on"),
    (r"\bvs\.", "versus"),
]

def strip_markdown(text):
    """Clean model output for TTS: strip Markdown, expand abbreviations, drop stray symbols."""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)   # **bold**
    text = re.sub(r"\*(.*?)\*", r"\1", text)         # *italic*
    text = re.sub(r"__(.*?)__", r"\1", text)          # __bold__
    text = re.sub(r"_(.*?)_", r"\1", text)            # _italic_
    text = re.sub(r"`(.*?)`", r"\1", text)            # `code`
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)  # # headers (line start)
    text = re.sub(r"#{2,}", "", text)                  # stray ## / ### mid-line
    text = re.sub(r"^[\*\-\+]\s+", "", text, flags=re.MULTILINE)  # bullet markers
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)  # numbered list markers
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.MULTILINE)  # --- horizontal rules
    text = re.sub(r"\s+[-\u2013\u2014]{1,2}\s+", ", ", text)  # " - " / " -- " / em-dash as a clause join
    for pattern, replacement in ABBREVIATIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"[/&]", " ", text)                  # stray slashes and ampersands
    text = re.sub(r"[ \t]{2,}", " ", text)             # collapse extra spaces left behind
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


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
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "stream": True,
            "options": {"num_ctx": 8192},
            "keep_alive": -1,
            "think": False,
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

    speech_text = strip_markdown(full_text)

    with wave.open("turn_output.wav", "wb") as wav_file:
        tts_voice.synthesize_wav(speech_text, wav_file)
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
