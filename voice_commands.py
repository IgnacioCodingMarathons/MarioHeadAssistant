import json
import os
import re
import sys
import threading
import time
from pathlib import Path
import urllib.request

from pygame._sdl2 import AUDIO_S16, AudioDevice, get_audio_device_names


SAMPLE_RATE = 16000
MAX_RECORDING_BYTES = SAMPLE_RATE * 2 * 30


def create_beep_sound(pygame):
    from array import array
    import math

    samples = array(
        "h",
        (
            int(32767 * 0.5 * math.sin(2 * math.pi * 1000 * index / 44100))
            for index in range(11025)
            for _ in range(2)
        ),
    )
    return pygame.mixer.Sound(buffer=samples.tobytes())


class MicrophoneCapture:
    def __init__(self):
        devices = get_audio_device_names(iscapture=True)
        if not devices:
            raise RuntimeError("No microphone input device is available")
        self.samples = bytearray()
        self.lock = threading.Lock()
        self.accepting = True
        self.device = AudioDevice(
            devicename=devices[0],
            iscapture=True,
            frequency=SAMPLE_RATE,
            audioformat=AUDIO_S16,
            numchannels=1,
            chunksize=1600,
            allowed_changes=0,
            callback=self._capture,
        )
        self.device.pause(0)

    def _capture(self, device, audio):
        if not self.accepting:
            return
        chunk = bytes(audio)
        with self.lock:
            remaining = MAX_RECORDING_BYTES - len(self.samples)
            if remaining > 0:
                self.samples.extend(chunk[:remaining])

    def stop(self):
        self.accepting = False
        self.device.pause(1)
        self.device.close()
        with self.lock:
            return bytes(self.samples)


class SpeechTranscriber:
    def __init__(self, assets_dir, preferences):
        configured = os.environ.get("MARIOASSISTANT_VOSK_MODEL")
        self.assets_dir = assets_dir
        self.preferences = preferences
        self.model_paths = {
            "vosk_quick": Path(configured).expanduser() if configured else assets_dir / "vosk",
            "vosk_smart": assets_dir / "vosksmart",
        }
        self.models = {}
        self.failed_models = set()
        self.last_engine = None
        self.lock = threading.Lock()
        self.google_checked_at = 0.0
        self.google_is_online = False

    def _method_enabled(self, method):
        if method == "vosk_quick":
            return (
                self.preferences.get("speech_enable_vosk", True)
                and self.model_paths[method].is_dir()
            )
        if method == "vosk_smart":
            return (
                self.preferences.get("speech_enable_vosk", True)
                and self.preferences.get("speech_enable_smart_vosk", False)
                and self.model_paths[method].is_dir()
            )
        if method == "google":
            return (
                self.preferences.get("speech_enable_google", True)
                and self._google_online()
            )
        return False

    def _google_online(self):
        now = time.monotonic()
        if now - self.google_checked_at < 15:
            return self.google_is_online
        self.google_checked_at = now
        try:
            request = urllib.request.Request(
                "https://www.google.com/generate_204",
                headers={"User-Agent": "MarioAssistant"},
            )
            with urllib.request.urlopen(request, timeout=1.5) as response:
                self.google_is_online = response.status in (200, 204)
        except Exception:
            self.google_is_online = False
        return self.google_is_online

    def _transcribe_vosk(self, method, samples):
        import vosk

        if method in self.failed_models:
            raise RuntimeError("This Vosk model failed earlier in this session")
        with self.lock:
            model = self.models.get(method)
            if model is None:
                vosk.SetLogLevel(-1)
                model = vosk.Model(str(self.model_paths[method]))
                self.models[method] = model
        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        recognizer.AcceptWaveform(samples)
        transcript = json.loads(recognizer.FinalResult()).get("text", "").strip()
        if not transcript:
            raise ValueError("Vosk did not recognize speech")
        return transcript

    def transcribe_google(self, samples):
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        recognizer.operation_timeout = 10
        audio = sr.AudioData(samples, SAMPLE_RATE, 2)
        transcript = recognizer.recognize_google(audio, language="en-US").strip()
        if not transcript:
            raise ValueError("Google recognition returned no speech")
        return transcript

    def transcribe(self, samples):
        if not samples:
            raise ValueError("No speech was captured")
        if self.preferences.get("speech_use_one_method_only", False):
            methods = [self.preferences.get("speech_preferred_method", "google")]
        else:
            methods = self.preferences.get(
                "speech_method_order", ["vosk_quick", "google", "vosk_smart"]
            )
        last_transcript = None
        last_engine = None
        errors = []
        for method in methods:
            if method not in self.model_paths and method != "google":
                continue
            engine = "google" if method == "google" else method
            if not self._method_enabled(method):
                detail = "disabled, offline, or its model folder is unavailable"
                errors.append(f"{method}: {detail}")
                print(
                    f"MarioAssistant voice: {method} skipped because it is {detail}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            try:
                transcript = (
                    self.transcribe_google(samples)
                    if method == "google"
                    else self._transcribe_vosk(method, samples)
                )
            except Exception as exc:
                if method != "google":
                    self.failed_models.add(method)
                errors.append(f"{method}: {type(exc).__name__}: {exc}")
                print(
                    f"MarioAssistant voice: {method} failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            action = interpret_voice_command(transcript)
            label = {
                "vosk_quick": "Vosk (quick)",
                "vosk_smart": "Vosk (smart)",
                "google": "Google",
            }[method]
            if action is None:
                result = "did not match a supported command"
                last_transcript = transcript
                last_engine = method
            else:
                result = f"got it right: command {action!r}"
            print(
                f"MarioAssistant voice: {label} {result}; transcript: {transcript!r}",
                file=sys.stderr,
                flush=True,
            )
            if action is not None:
                self.last_engine = method
                return transcript, method, action
        if last_transcript is not None:
            print(
                "MarioAssistant voice: no enabled recognition method matched a command",
                file=sys.stderr,
                flush=True,
            )
            self.last_engine = last_engine
            return last_transcript, last_engine, None
        raise RuntimeError(
            "No enabled speech recognition method succeeded. " + "; ".join(errors)
        )


def interpret_voice_command(transcript):
    text = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    words = set(text.split())

    def contains(phrase):
        return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None

    if any(contains(phrase) for phrase in (
        "stop", "shut up", "skip", "silence", "shut your mouth", "stop it",
    )):
        return "stop"
    if contains("goodbye"):
        return "goodbye"
    if "speak" in words or contains("say something"):
        return "speak"
    if "voice" in words and ("command" in words or "commands" in words):
        return "commands"
    if "quench" in words:
        if "hunger" in words and "thirst" not in words:
            return "quench_hunger"
        if "thirst" in words and "hunger" not in words:
            return "quench_thirst"
    if "move" in words or contains("out of the way") or contains("out of my way"):
        return "move"
    if (
        "minigames" in words
        and ("open" in words or "interactive" in words or "mario" in words)
    ):
        return "minigames"
    if ("play" in words or "mario" in words) and contains("tunnel of doom"):
        return "tunnel"
    if ("play" in words or "mario" in words) and contains("something in my eye"):
        return "eye"
    if "debug" in words:
        return "superdebug"
    if contains("mario's") or contains("pet care"):
        return "petcare"
    if "settings" in words or "configure" in words:
        return "settings"
    return None
