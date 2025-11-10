"""
Nova — Local Voice Assistant — v3 (Whisper STT, offline)
--------------------------------------------------------
• Replaces Vosk with Whisper (faster-whisper) for much higher accuracy.
• Still fully offline. Downloads the model once on first run.
• Works with your existing intents, aliases, and debug flags.

Install (CPU ok; GPU faster):
    pip install faster-whisper sounddevice pyttsx3 pywin32 pyautogui numpy

Run examples:
    python mainv3_whisper.py --list-devices
    python mainv3_whisper.py --device 1 --debug --bypass-wake
    # Choose model size: tiny, base, small, medium, large-v3 (accuracy↑, speed↓)
    python mainv3_whisper.py --device 1 --model small --compute int8 --debug

Notes:
- If you have a GPU (NVIDIA), try: --compute float16 (needs CUDA); CPU: use int8/int8_float16
- We chunk the mic every ~1.5s and run Whisper with vad_filter=True to ignore silence/noise.
"""

import os
import re
import json
import time
import queue
import threading
import subprocess
import webbrowser
import argparse
from datetime import datetime
from difflib import get_close_matches

import numpy as np
import sounddevice as sd
import pyttsx3
from faster_whisper import WhisperModel

try:
    import win32api
except Exception:
    win32api = None

try:
    import pyautogui
except Exception:
    pyautogui = None


WAKE_WORDS = [r"\bhey\s+nova\b", r"\bhello\s+nova\b", r"\bhey\s+noah\b", r"\bhey\s+novaa\b"]
INTENT_VERBS = {"open", "search", "google", "lookup", "close", "time", "what", "volume", "mute", "screenshot"}
SAMPLE_RATE = 16000
CHUNK_SECONDS = 1.5  # adjust with your chunks per second

APP_PATHS = {
    "chrome": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "edge": r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "brave": r"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
    "vscode": r"C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
    "notepad": r"notepad.exe",
    "spotify": r"C:\\Users\\%USERNAME%\\AppData\\Roaming\\Spotify\\Spotify.exe",
    "calculator": r"calc.exe",
}

WEBSITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "chatgpt": "https://chat.openai.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
}

# aliases 
CONFUSIONS = {
    "grow": "chrome",
    "goal": "chrome",
    "rome": "chrome",
    "googly": "google",
    "googling": "google",
    "blows": "close",
}

APP_ALIASES = {
    "google": "chrome",
    "visual studio code": "vscode",
    "code": "vscode",
    "bad": "notepad",      # your custom alias
}

SITE_ALIASES = {
    "yt": "youtube",
    "you": "youtube",      # your custom alias
    "you tube": "youtube",
    "g mail": "gmail",
    "mail": "gmail",
    "drive": "google drive",
    "good": "github",      # your custom alias
}


class Speaker:
    def __init__(self):
        self.engine = pyttsx3.init()
        self.engine.setProperty('rate', int(self.engine.getProperty('rate') * 0.95))
        self.lock = threading.Lock()
    def say(self, text: str):
        with self.lock:
            self.engine.say(text)
            self.engine.runAndWait()

speaker = Speaker()


def fuzzy_wake(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in WAKE_WORDS)

def normalize(t: str) -> str:
    t = t.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t

def apply_confusions(t: str) -> str:
    keys = sorted(CONFUSIONS.keys(), key=len, reverse=True)
    for k in keys:
        t = re.sub(rf"\b{re.escape(k)}\b", CONFUSIONS[k], t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def resolve_name_fuzzy(name: str, choices: list[str], extra_alias: dict[str, str] | None = None) -> str | None:
    n = name.strip().lower()
    if extra_alias and n in extra_alias:
        n = extra_alias[n]
    if n in choices:
        return n
    matches = get_close_matches(n, choices, n=1, cutoff=0.7)
    return matches[0] if matches else None

class WhisperListener:
    def __init__(self, device_idx: int | None, model_size: str, compute_type: str, debug: bool):
        self.device_idx = device_idx
        self.debug = debug
        self.model = WhisperModel(model_size, device="auto", compute_type=compute_type)
        self.stream = None
        self.buffer = np.zeros(0, dtype=np.float32)
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.frames_per_chunk = int(SAMPLE_RATE * CHUNK_SECONDS)

    def _callback(self, indata, frames, time_info, status):
        pcm = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float32) / 32768.0
        with self.lock:
            self.buffer = np.concatenate([self.buffer, pcm])

    def start(self):
        self.stop_flag.clear()
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=0,
            dtype='int16',
            channels=1,
            device=self.device_idx,
            callback=self._callback,
        )
        self.stream.start()
        if self.debug:
            print(f"[audio] using Whisper @ {SAMPLE_RATE} Hz, device {self.device_idx}")

    def stop(self):
        self.stop_flag.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def listen_loop(self, on_text):
        try:
            while not self.stop_flag.is_set():
                time.sleep(0.2)
                with self.lock:
                    if self.buffer.shape[0] >= self.frames_per_chunk:
                        chunk = self.buffer[: self.frames_per_chunk]
                        self.buffer = self.buffer[self.frames_per_chunk :]
                    else:
                        chunk = None
                if chunk is None:
                    continue
                # Transcribe this chunk
                segments, info = self.model.transcribe(
                    chunk,
                    language="en",
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=300),
                    beam_size=1,
                    condition_on_previous_text=False,
                )
                text = "".join(seg.text for seg in segments).strip()
                if text:
                    if self.debug:
                        print("final:", text)
                    on_text(text)
        except KeyboardInterrupt:
            return

# Intent parsing 
OPEN_APP_PAT   = re.compile(r"^(open|launch|start)\s+(.+)$")
OPEN_SITE_PAT  = re.compile(r"^(open)\s+(.+)$")
SEARCH_PAT     = re.compile(r"^(search for|google|lookup)\s+(.+)$")
CLOSE_PAT      = re.compile(r"^(close)\s+(.+)$")
TIME_PAT       = re.compile(r"^(what\s+time\s+is\s+it|time)$")
VOLUME_UP_PAT  = re.compile(r"^(volume up|increase volume)$")
VOLUME_DOWN_PAT= re.compile(r"^(volume down|decrease volume)$")
MUTE_PAT       = re.compile(r"^(mute|mute volume)$")
SCREENSHOT_PAT = re.compile(r"^(take a screenshot|screenshot)$")

class Executor:
    def open_app(self, phrase: str):
        target = phrase.split(" for ")[0].split(" on ")[0].strip()
        key = APP_ALIASES.get(target, target)
        choice = resolve_name_fuzzy(key, list(APP_PATHS.keys()), APP_ALIASES)
        if not choice:
            speaker.say(f"I couldn't find an app like {target}.")
            return
        try:
            subprocess.Popen(os.path.expandvars(APP_PATHS[choice]))
            speaker.say(f"Opening {choice}.")
        except Exception:
            speaker.say(f"Couldn't open {choice}.")

    def open_site(self, phrase: str):
        target = SITE_ALIASES.get(phrase.strip(), phrase.strip())
        choice = resolve_name_fuzzy(target, list(WEBSITES.keys()), SITE_ALIASES)
        if not choice:
            if target.startswith("google "):
                q = target[len("google "):]
                return self.web_search(q)
            speaker.say(f"I don't know the site {target}.")
            return
        webbrowser.open(WEBSITES[choice])
        speaker.say(f"Opening {choice}.")

    def web_search(self, query: str):
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        webbrowser.open(url)
        speaker.say(f"Searching for {query}.")

    def close_foreground(self):
        if pyautogui:
            pyautogui.hotkey('alt', 'f4')
            speaker.say("Closed.")
        else:
            speaker.say("Close command not available.")

    def tell_time(self):
        now = datetime.now().strftime("%I:%M %p")
        speaker.say(f"It's {now}.")

    def volume_up(self):
        if win32api:
            for _ in range(10):
                win32api.keybd_event(0xAF, 0, 0, 0)
        speaker.say("Volume up.")

    def volume_down(self):
        if win32api:
            for _ in range(10):
                win32api.keybd_event(0xAE, 0, 0, 0)
        speaker.say("Volume down.")

    def mute(self):
        if win32api:
            win32api.keybd_event(0xAD, 0, 0, 0)
        speaker.say("Muted.")

    def screenshot(self):
        if pyautogui:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            p = os.path.join(os.path.expanduser("~"), f"Pictures\\Nova_Screenshot_{ts}.png")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            img = pyautogui.screenshot()
            img.save(p)
            speaker.say("Screenshot saved in Pictures.")
        else:
            speaker.say("Screenshot tool not available.")

executor = Executor()

# Orchestrator 
class Nova:
    def __init__(self, device_idx: int | None, model_size: str, compute_type: str, debug: bool, bypass_wake: bool):
        self.listener = WhisperListener(device_idx, model_size, compute_type, debug)
        self.awake = bypass_wake
        self.debug = debug
        self.bypass_wake = bypass_wake

    def _gate_intent(self, text: str) -> bool:
        first = text.split(" ")[0]
        return first in {"open", "search", "google", "lookup", "close", "time", "what"}

    def on_text(self, text: str):
        raw = normalize(text)
        if self.debug:
            print("heard:", raw)
        if not self.awake:
            if fuzzy_wake(raw):
                self.awake = True
                speaker.say("Ready")
            return

        clean = apply_confusions(raw)
        for pat in WAKE_WORDS:
            clean = re.sub(pat, "", clean, flags=re.IGNORECASE).strip()
        if not clean:
            return
        if self.debug:
            print("Command(clean):", clean)
        if not self._gate_intent(clean):
            return

        # Route intents
        if m := SEARCH_PAT.match(clean):
            executor.web_search(m.group(2))
        elif m := CLOSE_PAT.match(clean):
            executor.close_foreground()
        elif m := OPEN_APP_PAT.match(clean):
            executor.open_app(m.group(2))
        elif m := OPEN_SITE_PAT.match(clean):
            executor.open_site(m.group(2))
        elif TIME_PAT.match(clean):
            executor.tell_time()
        elif VOLUME_UP_PAT.match(clean):
            executor.volume_up()
        elif VOLUME_DOWN_PAT.match(clean):
            executor.volume_down()
        elif MUTE_PAT.match(clean):
            executor.mute()
        elif SCREENSHOT_PAT.match(clean):
            executor.screenshot()
        else:
            if clean.startswith("google "):
                executor.web_search(clean[len("google "):])
        self.awake = self.bypass_wake

    def run(self):
        print("Nova (Whisper) running. Say 'hey nova' or use --bypass-wake.")
        speaker.say("Nova online")
        self.listener.start()
        try:
            self.listener.listen_loop(self.on_text)
        finally:
            self.listener.stop()

# main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--device", type=int, default=None, help="Input device index to use")
    parser.add_argument("--model", type=str, default="small", help="Whisper model size: tiny/base/small/medium/large-v3")
    parser.add_argument("--compute", type=str, default="int8", help="Compute type: int8/int8_float16/float16/float32")
    parser.add_argument("--debug", action="store_true", help="Verbose logs")
    parser.add_argument("--bypass-wake", action="store_true", help="Start already awake (testing)")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    try:
        Nova(device_idx=args.device, model_size=args.model, compute_type=args.compute, debug=args.debug, bypass_wake=args.bypass_wake).run()
    except RuntimeError as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
