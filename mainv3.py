"""
Nova — Local Voice Assistant — v2 (accuracy & alias build)
----------------------------------------------------------
Upgrades
- Intent gate: only act on clear verbs (open/search/close/time/volume/screenshot)
- Text cleanup: normalize + replace common ASR confusions (from your logs)
- Fuzzy matching for apps & sites with large alias map
- Site & app extraction supports multi‑word targets ("open google drive")
- "close <app/site>" via Alt+F4 (simple, foreground window)
- Debug remains: --list-devices / --device / --debug / --bypass-wake

Run examples
  python mainv2.py --list-devices
  python mainv2.py --device 1 --debug
  python mainv2.py --device 1 --debug --bypass-wake

Deps
  pip install vosk sounddevice pyttsx3 pywin32 pyautogui
Set VOSK_MODEL_DIR to your vosk model or keep the default folder name.
"""

import os
import re
import json
import queue
import threading
import subprocess
import webbrowser
import argparse
from datetime import datetime
from difflib import get_close_matches

from vosk import Model, KaldiRecognizer
import sounddevice as sd
import pyttsx3

try:
    import win32api
except Exception:
    win32api = None

try:
    import pyautogui
except Exception:
    pyautogui = None

# -------- Config -------- #
VOSK_MODEL_DIR = os.environ.get("VOSK_MODEL_DIR", r"vosk-model-small-en-us-0.15")
WAKE_WORDS = [r"\bhey\s+nova\b", r"\bhello\s+nova\b", r"\bhey\s+noah\b", r"\bhey\s+novaa\b"]
INTENT_VERBS = {"open", "search", "google", "lookup", "close", "time", "what time is it", "volume", "mute", "screenshot"}

# App paths (adjust if needed)
APP_PATHS = {
    "chrome": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "edge": r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "brave": r"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
    "vscode": r"C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
    "notepad": r"notepad.exe",
    "spotify": r"C:\\Users\\%USERNAME%\\AppData\\Roaming\\Spotify\\Spotify.exe",
    "calculator": r"calc.exe",
}

# Sites
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

# ASR confusion replacements (from your logs) → canonical tokens
CONFUSIONS = {
    # chrome / google
    "grown": "chrome",
    "goal": "chrome",
    "rome": "chrome",
    "googly": "google",
    "googling": "google",
    "googling.": "google",
    "goole": "google",
    "orban": "open",
    "old when": "open",
    "oh been": "open",
    "been": "open",
    "or been": "open",
    "or but": "open",
    "opening": "open",
    "open will": "open",
    "open book": "open",
    "open building": "open",
    "open braille": "open chrome",
    "open ravi": "open chrome",
    "open robbie": "open chrome",
    "open bully": "open",
    "blows": "close",
    # misc fillers
    "i think": "",
    "that": "",
    "it's": "",
    "video": "",
    "while you are": "",
}

# Extra aliases for apps/sites
APP_ALIASES = {
    "google": "chrome",
    "visual studio code": "vscode",
    "code": "vscode",
    "bad": "notepad"
}
SITE_ALIASES = {
    "yt": "youtube",
    "you": "youtube",
    "you tube": "youtube",
    "g mail": "gmail",
    "mail": "gmail",
    "drive": "google drive",
    "good": "github"
}

# -------- TTS -------- #
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

# -------- Utils -------- #
def fuzzy_wake(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in WAKE_WORDS)

def normalize(t: str) -> str:
    t = t.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t

def apply_confusions(t: str) -> str:
    # replace longer keys first
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

# -------- Listener -------- #
class Listener:
    def __init__(self, model_dir: str, device_idx: int | None, debug: bool):
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"Vosk model not found at: {model_dir}")
        self.model = Model(model_dir)
        self.rec: KaldiRecognizer | None = None
        self.stream: sd.RawInputStream | None = None
        self.q: queue.Queue[bytes] = queue.Queue()
        self.stop_flag = threading.Event()
        self.debug = debug
        self.device_idx = device_idx

    def _callback(self, indata, frames, time_info, status):
        if status and self.debug:
            print("[sd status]", status, flush=True)
        self.q.put(bytes(indata))

    def start(self):
        for rate in (16000, 44100, 48000):
            try:
                if self.debug:
                    print(f"[audio] trying rate {rate} @ device {self.device_idx}")
                self.rec = KaldiRecognizer(self.model, rate)
                self.stream = sd.RawInputStream(
                    samplerate=rate,
                    blocksize=8000,
                    dtype='int16',
                    channels=1,
                    device=self.device_idx,
                    callback=self._callback,
                )
                self.stream.start()
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
                self.rec = None
        raise RuntimeError("Could not start microphone stream at 16k/44.1k/48k. Try --device.")

    def stop(self):
        self.stop_flag.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def listen_forever(self, on_partial, on_final):
        if not self.rec:
            raise RuntimeError("Recognizer not initialised.")
        try:
            while not self.stop_flag.is_set():
                data = self.q.get()
                if self.rec.AcceptWaveform(data):
                    res = json.loads(self.rec.Result())
                    if txt := res.get("text"):
                        if self.debug:
                            print("final:", txt)
                        on_final(txt)
                else:
                    part = json.loads(self.rec.PartialResult()).get("partial", "")
                    if part and self.debug:
                        print("partial:", part)
                    if part:
                        on_partial(part)
        except KeyboardInterrupt:
            return

# -------- Intents -------- #
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
        # take first 1-2 tokens as candidate app name
        target = phrase.split(" for ")[0].split(" on ")[0]
        target = target.strip()
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
        target = phrase.strip()
        target = SITE_ALIASES.get(target, target)
        choice = resolve_name_fuzzy(target, list(WEBSITES.keys()), SITE_ALIASES)
        if not choice:
            # if user said open google <query>, treat as search
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

# -------- Orchestrator -------- #
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

    def _gate_intent(self, text: str) -> bool:
        # act only if starts with a known verb to avoid random sentences
        first = text.split(" ")[0]
        return first in {"open", "search", "google", "lookup", "close", "time", "what"}

    def on_final(self, text: str):
        text = normalize(text)
        if not text:
            return
        if not self.awake:
            if fuzzy_wake(text):
                self.awake = True
                speaker.say("Ready")
            return

        # Clean and map confusions, then strip wake words if present
        clean = apply_confusions(text)
        for pat in WAKE_WORDS:
            clean = re.sub(pat, "", clean, flags=re.IGNORECASE).strip()
        if not clean:
            return

        if self.debug:
            print("Command(clean):", clean)

        if not self._gate_intent(clean):
            # ignore chatter
            return

        # Route intents (order matters)
        if m := SEARCH_PAT.match(clean):
            executor.web_search(m.group(2))
        elif m := CLOSE_PAT.match(clean):
            target = m.group(2)
            # special case: "close google/chrome" just alt+f4
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
            # fallback: if contains the word google <query>
            if clean.startswith("google "):
                executor.web_search(clean[len("google "):])
            # otherwise ignore silently to reduce noise

        # go back to sleep unless bypassing
        self.awake = self.bypass_wake

    def run(self):
        print("Nova running. Say 'hey nova' or use --bypass-wake.")
        speaker.say("Nova online")
        self.listener.start()
        try:
            self.listener.listen_forever(self.on_partial, self.on_final)
        finally:
            self.listener.stop()

# -------- main -------- #

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
        print("Check VOSK_MODEL_DIR and audio device index with --list-devices.")

if __name__ == "__main__":
    main()
