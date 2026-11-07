import json
import os
import re
import sys
import threading
from pathlib import Path

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
    def __init__(self, assets_dir):
        configured = os.environ.get("MARIOASSISTANT_VOSK_MODEL")
        self.model_path = (
            Path(configured).expanduser() if configured else assets_dir / "vosk"
        )
        self.model = None
        self.vosk_unavailable = False
        self.last_engine = None
        self.lock = threading.Lock()

    def transcribe(self, samples):
        if not samples:
            raise ValueError("No speech was captured")
        self.last_engine = None
        if not self.vosk_unavailable:
            try:
                import vosk

                with self.lock:
                    if self.model is None:
                        vosk.SetLogLevel(-1)
                        self.model = vosk.Model(str(self.model_path))
                    recognizer = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
                recognizer.AcceptWaveform(samples)
                transcript = json.loads(recognizer.FinalResult()).get("text", "").strip()
                if not transcript:
                    raise ValueError("Vosk did not recognize speech")
                self.last_engine = "vosk"
                return transcript
            except Exception as exc:
                self.vosk_unavailable = True
                print(
                    f"MarioAssistant voice: Vosk failed using {self.model_path}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        return self.transcribe_google(samples)

    def transcribe_google(self, samples):
        try:
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            recognizer.operation_timeout = 10
            audio = sr.AudioData(samples, SAMPLE_RATE, 2)
            print(
                "MarioAssistant voice: trying Google SpeechRecognition fallback",
                file=sys.stderr,
                flush=True,
            )
            transcript = recognizer.recognize_google(audio, language="en-US").strip()
            if not transcript:
                raise ValueError("Google recognition returned no speech")
            self.last_engine = "google"
            return transcript
        except Exception as exc:
            print(
                f"MarioAssistant voice: Google SpeechRecognition failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            raise


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
