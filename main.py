"""
Nova — Local Voice Assistant (Windows MVP)
----------------------------------------
Features (MVP)
- Hotword: say "hey nova" to wake.
- Voice → text: offline using Vosk.
- Intent parser: simple regex + keyword rules.
- Actions:
  • Open desktop apps (Chrome, VS Code, Notepad, Spotify, etc.)
  • Web search: "search for <query>" or "google <query>"
  • Open websites: "open youtube", "open gmail"
  • System actions: "volume up/down/mute", "take a screenshot", "what time is it"
- Voice feedback with pyttsx3 (offline TTS).

Setup
1) Install Python 3.10+
2) pip install -r requirements.txt  (or see inline imports below)
   Required: vosk==0.3.45, sounddevice, pyttsx3, pywin32; optional: pynput, pyautogui
3) Download a Vosk English model (e.g., vosk-model-small-en-us-0.15 ~50MB):
   https://alphacephei.com/vosk/models
   Extract and set VOSK_MODEL_DIR below.
4) (Optional) Update APP_PATHS for your machine.

Run
python main.py
Speak: "hey nova" → wait for the "listening" prompt → say your command.

Security note
- Commands are local, no cloud calls.
- Be careful mapping destructive commands; keep allowlist-based intents.
"""

import os
import re
import sys
import time
import json
import queue
import threading
import subprocess
import webbrowser
from datetime import datetime

# === Third‑party deps ===
# pip install vosk sounddevice pyttsx3 pywin32 pynput pyautogui
from vosk import Model, KaldiRecognizer
import sounddevice as sd
import pyttsx3

try:
    import win32gui
    import win32con
    import win32api
except Exception:
    # Non-critical on non-Windows
    win32gui = win32con = win32api = None

try:
    import pyautogui
except Exception:
    pyautogui = None

# ---------------- Config ---------------- #
VOSK_MODEL_DIR = os.environ.get("VOSK_MODEL_DIR", r"vosk-model-small-en-us-0.15")
SAMPLE_RATE = 16000
WAKE_WORD = "hey Nova"

# Map friendly names to absolute paths (adjust to your system)
APP_PATHS = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "vscode": r"C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
    "notepad": r"notepad.exe",
    "spotify": r"C:\\Users\\%USERNAME%\\AppData\\Roaming\\Spotify\\Spotify.exe",
    "calculator": r"calc.exe",
}

WEBSITES = {
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
        # Choose a slightly faster rate than default
        self.engine.setProperty('rate', int(self.engine.getProperty('rate') * 0.95))
        self.lock = threading.Lock()

    def say(self, text: str):
        with self.lock:
            self.engine.say(text)
            self.engine.runAndWait()

speaker = Speaker()

# -------------- STT stream -------------- #
class Listener:
    def __init__(self, model_dir: str, sample_rate: int):
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"Vosk model not found at: {model_dir}")
        self.model = Model(model_dir)
        self.rec = KaldiRecognizer(self.model, sample_rate)
        self.rec.SetWords(True)
        self.sample_rate = sample_rate
        self.q = queue.Queue()
        self.stream = None
        self.stop_flag = threading.Event()
        self.text_buffer = ""

    def _callback(self, indata, frames, time_info, status):
        if status:
            # Over/Underrun warnings
            pass
        self.q.put(bytes(indata))
        return None

    def start(self):
        self.stop_flag.clear()
        self.stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=8000,
            dtype='int16',
            channels=1,
            callback=self._callback,
        )
        self.stream.start()

    def stop(self):
        self.stop_flag.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def listen_forever(self, on_partial, on_final):
        try:
            while not self.stop_flag.is_set():
                data = self.q.get()
                if self.rec.AcceptWaveform(data):
                    res = json.loads(self.rec.Result())
                    if txt := res.get("text"):
                        on_final(txt)
                else:
                    part = json.loads(self.rec.PartialResult()).get("partial", "")
                    if part:
                        on_partial(part)
        except KeyboardInterrupt:
            return

# -------------- Intent parsing -------------- #
OPEN_APP_PAT = re.compile(r"^(open|launch|start)\s+([\w\s\.\+\-]+)$")
SEARCH_PAT = re.compile(r"^(search for|google|lookup)\s+(.+)$")
OPEN_SITE_PAT = re.compile(r"^(open)\s+(\w+)(?:\.com)?$")
TIME_PAT = re.compile(r"^(what\s+time\s+is\s+it|time)$")
VOLUME_UP_PAT = re.compile(r"^(volume up|increase volume)$")
VOLUME_DOWN_PAT = re.compile(r"^(volume down|decrease volume)$")
MUTE_PAT = re.compile(r"^(mute|mute volume)$")
SCREENSHOT_PAT = re.compile(r"^(take a screenshot|screenshot)$")


def normalize(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())


class Executor:
    def __init__(self):
        pass

    def open_app(self, name: str):
        key = name.strip().lower()
        # common aliases
        aliases = {"google": "chrome", "visual studio code": "vscode", "code": "vscode"}
        key = aliases.get(key, key)
        path = APP_PATHS.get(key)
        if not path:
            speaker.say(f"I don't know where {name} is. Update my app paths.")
            return
        try:
            subprocess.Popen(os.path.expandvars(path))
            speaker.say(f"Opening {name}.")
        except Exception as e:
            speaker.say(f"Couldn't open {name}.")

    def web_search(self, query: str):
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        webbrowser.open(url)
        speaker.say(f"Searching for {query}.")

    def open_site(self, key: str):
        url = WEBSITES.get(key.lower())
        if url:
            webbrowser.open(url)
            speaker.say(f"Opening {key}.")
        else:
            speaker.say(f"I don't know the site {key}.")

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
    def __init__(self):
        self.listener = Listener(VOSK_MODEL_DIR, SAMPLE_RATE)
        self.awake = False
        self.last_partial = ""

    def on_partial(self, text: str):
        # Detect wake phrase in streaming partials
        if not self.awake and WAKE_WORD in text.lower():
            self.awake = True
            speaker.say("Listening")
            # Clear last_partial to avoid wake word bleeding
            self.last_partial = ""

    def on_final(self, text: str):
        text = normalize(text)
        if not text:
            return
        if not self.awake:
            # also allow wake word in finals
            if WAKE_WORD in text:
                self.awake = True
                speaker.say("Ready")
            return

        # Remove wake word if user says it again
        text = text.replace(WAKE_WORD, "").strip()
        if not text:
            return
        print(f"Command: {text}")

        # Parse intents in order of specificity
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

        # After handling a command, go back to sleep
        self.awake = False

    def run(self):
        print("Nova running. Say 'hey nova' to wake.")
        speaker.say("Nova online")
        self.listener.start()
        try:
            self.listener.listen_forever(self.on_partial, self.on_final)
        finally:
            self.listener.stop()


if __name__ == "__main__":
    try:
        Nova().run()
    except RuntimeError as e:
        print(f"Error: {e}")
        print("Make sure VOSK_MODEL_DIR points to a valid model folder.")
