"""
Nova — Local Voice Assistant (Windows MVP) — v1.1 (diagnostics)
File: mainv1.py

What’s in this build
- --list-devices to find your microphone index
- Robust mic start (tries 16000, 44100, 48000 Hz)
- --debug prints partial/final transcripts
- Wake phrase variants: "hey nova", "hello nova", common mishears like "hey noah"
- --bypass-wake to test intents without wake phrase
- Fuzzy matching for app/site names (e.g., "grown" -> "chrome")
"""

import os
import re
import sys
import json
import queue
import threading
import subprocess
import webbrowser
import argparse
from datetime import datetime
from difflib import get_close_matches

# === Third-party deps ===
# pip install vosk sounddevice pyttsx3 pywin32 pyautogui
from vosk import Model, KaldiRecognizer
import sounddevice as sd
import pyttsx3

try:
    import win32api  # from pywin32
except Exception:
    win32api = None

try:
    import pyautogui
except Exception:
    pyautogui = None

# ---------------- Config ---------------- #
VOSK_MODEL_DIR = os.environ.get("VOSK_MODEL_DIR", r"vosk-model-small-en-us-0.15")

# Wake patterns (catch common mis-hearings)
WAKE_WORDS = [
    r"\bhey\s+nova\b",
    r"\bhello\s+nova\b",
    r"\bhey\s+noah\b",
    r"\bhey\s+novaa\b",
]

# Map friendly names to absolute paths (adjust to your system)
APP_PATHS = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "brave": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "vscode": r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    "notepad": r"notepad.exe",
    "spotify": r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
    "calculator": r"calc.exe",
}
# Common mis-hears -> canonical app key
APP_ALIASES = {
    "grown": "chrome",
    "goal": "chrome",
    "rome": "chrome",
    "google": "chrome",  # saying "open google" often means browser
    "code": "vscode",
    "visual studio code": "vscode",
}

WEBSITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "chatgpt": "https://chat.openai.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
}

# -------------- TTS helper -------------- #
class Speaker:
    def __init__(self):
        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", int(self.engine.getProperty("rate") * 0.95))
        self.lock = threading.Lock()

    def say(self, text: str):
        with self.lock:
            self.engine.say(text)
            self.engine.runAndWait()

speaker = Speaker()

# -------------- Helpers -------------- #
def fuzzy_wake(text: str) -> bool:
    text = text.lower()
    return any(re.search(p, text) for p in WAKE_WORDS)

def normalize(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())

def resolve_name_fuzzy(name: str, choices: list[str], extra_alias: dict[str, str] | None = None) -> str | None:
    n = name.strip().lower()
    if extra_alias and n in extra_alias:
        return extra_alias[n]
    if n in choices:
        return n
    # try closest match
    matches = get_close_matches(n, choices, n=1, cutoff=0.7)
    return matches[0] if matches else None

# -------------- STT stream -------------- #
class Listener:
    def __init__(self, model_dir: str, device_idx: int | None, debug: bool):
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"Vosk model not found at: {model_dir}")
        self.model = Model(model_dir)
        self.recognizer: KaldiRecognizer | None = None
        self.stream: sd.RawInputStream | None = None
        self.q: queue.Queue[bytes] = queue.Queue()
        self.stop_flag = threading.Event()
        self.debug = debug
        self.device_idx = device_idx
        self.active_rate = None

    def _callback(self, indata, frames, time_info, status):
        if status and self.debug:
            print("[sd status]", status, flush=True)
        self.q.put(bytes(indata))
        return None

    def start(self):
        self.stop_flag.clear()
        for rate in (16000, 44100, 48000):
            try:
                if self.debug:
                    print(f"[audio] trying rate {rate} @ device {self.device_idx}")
                self.recognizer = KaldiRecognizer(self.model, rate)
                self.stream = sd.RawInputStream(
                    samplerate=rate,
                    blocksize=8000,
                    dtype="int16",
                    channels=1,
                    device=self.device_idx,
                    callback=self._callback,
                )
                self.stream.start()
                self.active_rate = rate
                if self.debug:
                    print(f"[audio] using rate {rate}")
                return
            except Exception as e:
                if self.debug:
                    print(f"[audio] failed at {rate}: {e}")
                try:
                    if self.stream:
                        self.stream.close()
                except Exception:
                    pass
                self.stream = None
                self.recognizer = None
        raise RuntimeError("Could not start microphone stream at 16k/44.1k/48k. Try another input device (--device).")

    def stop(self):
        self.stop_flag.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def listen_forever(self, on_partial, on_final):
        if not self.recognizer:
            raise RuntimeError("Recognizer not initialised.")
        try:
            while not self.stop_flag.is_set():
                data = self.q.get()
                if self.recognizer.AcceptWaveform(data):
                    res = json.loads(self.recognizer.Result())
                    if txt := res.get("text"):
                        if self.debug:
                            print("final:", txt)
                        on_final(txt)
                else:
                    part = json.loads(self.recognizer.PartialResult()).get("partial", "")
                    if part:
                        if self.debug:
                            print("partial:", part)
                        on_partial(part)
        except KeyboardInterrupt:
            return

# -------------- Intent parsing -------------- #
OPEN_APP_PAT   = re.compile(r"^(open|launch|start)\s+([\w\s\.\+\-]+)$")
SEARCH_PAT     = re.compile(r"^(search for|google|lookup)\s+(.+)$")
OPEN_SITE_PAT  = re.compile(r"^(open)\s+(\w+)(?:\.com)?$")
TIME_PAT       = re.compile(r"^(what\s+time\s+is\s+it|time)$")
VOLUME_UP_PAT  = re.compile(r"^(volume up|increase volume)$")
VOLUME_DOWN_PAT= re.compile(r"^(volume down|decrease volume)$")
MUTE_PAT       = re.compile(r"^(mute|mute volume)$")
SCREENSHOT_PAT = re.compile(r"^(take a screenshot|screenshot)$")

class Executor:
    def open_app(self, name: str):
        # alias then fuzzy
        key = APP_ALIASES.get(name.strip().lower(), name.strip().lower())
        choice = resolve_name_fuzzy(key, list(APP_PATHS.keys()), APP_ALIASES)
        if not choice:
            speaker.say(f"I couldn't find an app like {name}.")
            return
        path = APP_PATHS.get(choice)
        try:
            subprocess.Popen(os.path.expandvars(path))
            speaker.say(f"Opening {choice}.")
        except Exception:
            speaker.say(f"Couldn't open {choice}.")

    def web_search(self, query: str):
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        webbrowser.open(url)
        speaker.say(f"Searching for {query}.")

    def open_site(self, key: str):
        choice = resolve_name_fuzzy(key, list(WEBSITES.keys()))
        if not choice:
            speaker.say(f"I don't know the site {key}.")
            return
        webbrowser.open(WEBSITES[choice])
        speaker.say(f"Opening {choice}.")

    def tell_time(self):
        now = datetime.now().strftime("%I:%M %p")
        speaker.say(f"It's {now}.")

    def volume_up(self):
        if win32api:
            for _ in range(10):
                win32api.keybd_event(0xAF, 0, 0, 0)  # VK_VOLUME_UP
        speaker.say("Volume up.")

    def volume_down(self):
        if win32api:
            for _ in range(10):
                win32api.keybd_event(0xAE, 0, 0, 0)  # VK_VOLUME_DOWN
        speaker.say("Volume down.")

    def mute(self):
        if win32api:
            win32api.keybd_event(0xAD, 0, 0, 0)  # VK_VOLUME_MUTE
        speaker.say("Muted.")

    def screenshot(self):
        if pyautogui:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = os.path.join(os.path.expanduser("~"), f"Pictures\\Nova_Screenshot_{ts}.png")
            os.makedirs(os.path.dirname(fname), exist_ok=True)
            img = pyautogui.screenshot()
            img.save(fname)
            speaker.say("Screenshot saved in Pictures.")
        else:
            speaker.say("Screenshot tool not available.")

executor = Executor()

# -------------- Orchestrator -------------- #
class Nova:
    def __init__(self, device_idx: int | None, debug: bool, bypass_wake: bool):
        self.listener = Listener(VOSK_MODEL_DIR, device_idx, debug)
        self.awake = bypass_wake
        self.debug = debug
        self.bypass_wake = bypass_wake

    def on_partial(self, text: str):
        if not self.awake and fuzzy_wake(text):
            self.awake = True
            speaker.say("Listening")

    def on_final(self, text: str):
        text = normalize(text)
        if not text:
            return
        if not self.awake:
            if fuzzy_wake(text):
                self.awake = True
                speaker.say("Ready")
            return

        # strip wake words if they appear again
        for pat in WAKE_WORDS:
            text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
        if not text:
            return

        print(f"Command: {text}")

        # Parse intents (order matters)
        if m := SEARCH_PAT.match(text):
            executor.web_search(m.group(2))
        elif m := OPEN_SITE_PAT.match(text):
            executor.open_site(m.group(2))
        elif m := OPEN_APP_PAT.match(text):
            executor.open_app(m.group(2))
        elif TIME_PAT.match(text):
            executor.tell_time()
        elif VOLUME_UP_PAT.match(text):
            executor.volume_up()
        elif VOLUME_DOWN_PAT.match(text):
            executor.volume_down()
        elif MUTE_PAT.match(text):
            executor.mute()
        elif SCREENSHOT_PAT.match(text):
            executor.screenshot()
        elif text in {"stop", "go to sleep", "sleep"}:
            speaker.say("Going to sleep")
        else:
            speaker.say("I didn't catch a supported command.")

        # After handling a command, go back to sleep unless bypassing
        self.awake = self.bypass_wake

    def run(self):
        print("Nova running. Say 'hey nova' (or 'hello nova') to wake.")
        speaker.say("Nova online")
        self.listener.start()
        try:
            self.listener.listen_forever(self.on_partial, self.on_final)
        finally:
            self.listener.stop()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--device", type=int, default=None, help="Input device index to use")
    parser.add_argument("--debug", action="store_true", help="Print partial/final transcripts and audio logs")
    parser.add_argument("--bypass-wake", action="store_true", help="Start already awake (for testing)")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    try:
        Nova(device_idx=args.device, debug=args.debug, bypass_wake=args.bypass_wake).run()
    except RuntimeError as e:
        print(f"Error: {e}")
        print("Make sure VOSK_MODEL_DIR points to a valid model folder or pass --device to select the right mic.")

if __name__ == "__main__":
    main()
