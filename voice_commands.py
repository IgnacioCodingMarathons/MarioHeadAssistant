import json
import math
import mmap
import os
import queue
import re
import shutil
import sys
import subprocess
import struct
import threading
import tempfile
import time
from collections import deque
from pathlib import Path
import urllib.request

from pygame._sdl2 import AUDIO_S16, AudioDevice, get_audio_device_names


SAMPLE_RATE = 16000
MAX_RECORDING_BYTES = SAMPLE_RATE * 2 * 30
WAKE_PHRASES = (
    "hey mario head", "hey mario had", "hey mario hat",
    "hey marie head", "hey marie had", "hey marie hat",
    "hey mariobritish head", "hey mariobritish had", "hey mariobritish hat",
)
try:
    VOSK_THREADS = max(1, min(8, int(os.environ.get(
        "MARIOASSISTANT_VOSK_THREADS",
        str(min(4, os.cpu_count() or 1)),
    ))))
except ValueError:
    VOSK_THREADS = max(1, min(4, os.cpu_count() or 1))
os.environ.setdefault("OMP_NUM_THREADS", str(VOSK_THREADS))
os.environ.setdefault("OPENBLAS_NUM_THREADS", str(VOSK_THREADS))
os.environ.setdefault("MKL_NUM_THREADS", str(VOSK_THREADS))


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
        self.started_at = time.monotonic()
        self.last_voice_at = self.started_at
        self.speech_started = False
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
        sample_count = len(chunk) // 2
        if sample_count:
            square_sum = sum(
                sample[0] * sample[0]
                for sample in struct.iter_unpack("<h", chunk[:sample_count * 2])
            )
            if math.sqrt(square_sum / sample_count) >= 450:
                with self.lock:
                    self.speech_started = True
                    self.last_voice_at = time.monotonic()
        with self.lock:
            remaining = MAX_RECORDING_BYTES - len(self.samples)
            if remaining > 0:
                self.samples.extend(chunk[:remaining])

    def should_auto_stop(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if len(self.samples) >= MAX_RECORDING_BYTES:
                return True
            if self.speech_started:
                return now - self.last_voice_at >= 1.25
            return now - self.started_at >= 10

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
        self.model_locks = {
            "vosk_quick": threading.Lock(),
            "vosk_smart": threading.Lock(),
        }
        self.last_engine = None
        self.google_checked_at = 0.0
        self.google_is_online = False
        self.gpu_enabled = False

    def initialize_gpu(self):
        if not self.preferences.get("speech_enable_vosk", True):
            return False
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi is None:
            return False
        try:
            device_check = subprocess.run(
                [nvidia_smi, "-L"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
        except Exception:
            return False
        if "GPU 0:" not in device_check.stdout:
            return False
        try:
            import vosk
        except Exception as exc:
            print(
                f"MarioAssistant voice: NVIDIA GPU found, but Vosk could not load: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        library_path = re.search(r"for '([^']+)'", repr(vosk._c))
        cuda_backend = False
        if library_path is not None:
            path = Path(library_path.group(1))
            if sys.platform.startswith("linux") and shutil.which("ldd"):
                try:
                    dependencies = subprocess.run(
                        ["ldd", str(path)],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                    ).stdout.lower()
                    cuda_backend = any(
                        name in dependencies
                        for name in ("libcudart", "libcublas", "libkaldi-cudamatrix")
                    )
                except Exception:
                    cuda_backend = False
            elif sys.platform == "win32":
                try:
                    with path.open("rb") as library_file:
                        with mmap.mmap(library_file.fileno(), 0, access=mmap.ACCESS_READ) as library_data:
                            cuda_backend = (
                                b"cudart64_" in library_data
                                and b"cublas64_" in library_data
                            )
                except (OSError, ValueError):
                    cuda_backend = False
        if not cuda_backend or not hasattr(vosk, "GpuInit"):
            print(
                "MarioAssistant voice: NVIDIA GPU found, but the installed Vosk library has no detected CUDA backend; using CPU recognition",
                file=sys.stderr,
                flush=True,
            )
            return False
        try:
            vosk.GpuInit()
        except Exception as exc:
            print(
                f"MarioAssistant voice: Vosk GPU initialization failed; using CPU recognition: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        self.gpu_enabled = True
        print(
            "MarioAssistant voice: Vosk CUDA GPU acceleration enabled",
            file=sys.stderr,
            flush=True,
        )
        return True

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

        if self.gpu_enabled:
            try:
                vosk.GpuThreadInit()
            except Exception as exc:
                self.gpu_enabled = False
                print(
                    f"MarioAssistant voice: Vosk GPU worker initialization failed; using CPU recognition: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        model = self._load_vosk_model(method, vosk)
        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        chunk_size = SAMPLE_RATE * 2
        for offset in range(0, len(samples), chunk_size):
            recognizer.AcceptWaveform(samples[offset:offset + chunk_size])
        transcript = json.loads(recognizer.FinalResult()).get("text", "").strip()
        if not transcript:
            raise ValueError("Vosk did not recognize speech")
        return transcript

    def _load_vosk_model(self, method, vosk=None):
        if vosk is None:
            import vosk
        with self.model_locks[method]:
            model = self.models.get(method)
            if model is None:
                vosk.SetLogLevel(-1)
                model = vosk.Model(str(self.model_paths[method]))
                self.models[method] = model
        return model

    def _reinitialize_vosk_model(self, method, reason):
        with self.model_locks[method]:
            had_cached_model = self.models.pop(method, None) is not None
        if self.gpu_enabled:
            self.gpu_enabled = False
            print(
                "MarioAssistant voice: Vosk model error under CUDA; retrying with CPU recognition",
                file=sys.stderr,
                flush=True,
            )
        if not had_cached_model:
            return
        try:
            model = self._load_vosk_model(method)
            print(
                f"MarioAssistant voice: reinitialized {method} after {type(reason).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return model
        except Exception as exc:
            print(
                f"MarioAssistant voice: {method} reinitialization failed: {type(exc).__name__}: {exc}; future requests will retry",
                file=sys.stderr,
                flush=True,
            )
            return None

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
                if method != "google" and str(exc) != "Vosk did not recognize speech":
                    self._reinitialize_vosk_model(method, exc)
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


class WakeWordListener:
    def __init__(self, transcriber, events):
        self.transcriber = transcriber
        self.events = events
        self.stop_event = threading.Event()
        self.thread = None
        self.device = None
        self.chunks = queue.Queue(maxsize=64)
        self.failed = False
        self.reported_unavailable = False
        self.failure_count = 0
        self.retry_at = 0.0

    def start(self):
        if (self.thread is not None and self.thread.is_alive()) or time.monotonic() < self.retry_at:
            return False
        if (not self.transcriber.preferences.get("speech_enable_wake_word", True)
                or self.transcriber.preferences.get("wake_word_sensitivity", 5) <= 0):
            return False
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                break
        self.stop_event.clear()
        self.failed = False
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2)

    def _capture(self, device, audio):
        try:
            self.chunks.put_nowait(bytes(audio))
        except queue.Full:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                pass
            try:
                self.chunks.put_nowait(bytes(audio))
            except queue.Full:
                pass

    def _matches_wake_word(self, text):
        normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        return any(phrase in normalized for phrase in WAKE_PHRASES)

    def _rms(self, chunk):
        sample_count = len(chunk) // 2
        if sample_count == 0:
            return 0.0
        square_sum = sum(
            sample[0] * sample[0]
            for sample in struct.iter_unpack("<h", chunk[:sample_count * 2])
        )
        return math.sqrt(square_sum / sample_count)

    def _recognize_wake_segment(self, decoder, chunks):
        raw_audio = b"".join(chunks)
        if len(raw_audio) < SAMPLE_RATE // 2:
            return None
        decoder.start_utt()
        decoder.process_raw(raw_audio, False, True)
        decoder.end_utt()
        hypothesis = decoder.hyp()
        if hypothesis is None:
            return None
        transcript = hypothesis.hypstr
        return transcript if self._matches_wake_word(transcript) else None

    def _create_decoder(self, pocketsphinx, acoustic_dir, dictionary_path, language_model_path, keywords_path):
        config = pocketsphinx.Config()
        config.set_string("-hmm", str(acoustic_dir))
        config.set_string("-dict", str(dictionary_path))
        config.set_string("-lm", str(language_model_path))
        config.set_string("-logfn", os.devnull)
        decoder = pocketsphinx.Decoder(config)
        try:
            decoder.add_word("mariobritish", "M AE R IY OW")
        except Exception:
            pass
        decoder.add_kws("mario_wake", keywords_path)
        decoder.activate_search("mario_wake")
        return decoder

    def _listen(self):
        device = None
        keywords_path = None
        try:
            import speech_recognition
            from pocketsphinx import pocketsphinx

            if self.stop_event.is_set():
                return
            model_dir = Path(speech_recognition.__file__).resolve().parent / "pocketsphinx-data" / "en-US"
            acoustic_dir = model_dir / "acoustic-model"
            dictionary_path = model_dir / "pronounciation-dictionary.dict"
            language_model_path = model_dir / "language-model.lm.bin"
            if (not acoustic_dir.is_dir() or not dictionary_path.is_file()
                    or not language_model_path.is_file()):
                raise RuntimeError(f"PocketSphinx English model files are missing from {model_dir}")
            sensitivity = max(
                1,
                min(10, int(self.transcriber.preferences.get("wake_word_sensitivity", 5))),
            )
            main_threshold = 10 + sensitivity * 5
            alternate_threshold = main_threshold - 5
            rare_variant_threshold = alternate_threshold - 3
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as keyword_file:
                keyword_file.write(f"hey mario head /1e-{main_threshold}/\n")
                keyword_file.write(f"hey mario had /1e-{alternate_threshold}/\n")
                keyword_file.write(f"hey mario hat /1e-{alternate_threshold}/\n")
                keyword_file.write(f"hey marie head /1e-{alternate_threshold}/\n")
                keyword_file.write(f"hey marie had /1e-{rare_variant_threshold}/\n")
                keyword_file.write(f"hey marie hat /1e-{rare_variant_threshold}/\n")
                keyword_file.write(f"hey mariobritish head /1e-{alternate_threshold}/\n")
                keyword_file.write(f"hey mariobritish had /1e-{rare_variant_threshold}/\n")
                keyword_file.write(f"hey mariobritish hat /1e-{rare_variant_threshold}/\n")
                keywords_path = keyword_file.name
            decoder = self._create_decoder(
                pocketsphinx, acoustic_dir, dictionary_path,
                language_model_path, keywords_path,
            )
            devices = get_audio_device_names(iscapture=True)
            if not devices:
                raise RuntimeError("No microphone input device is available")
            if self.stop_event.is_set():
                return
            device = AudioDevice(
                devicename=devices[0],
                iscapture=True,
                frequency=SAMPLE_RATE,
                audioformat=AUDIO_S16,
                numchannels=1,
                chunksize=3200,
                allowed_changes=0,
                callback=self._capture,
            )
            self.device = device
            device.pause(0)
            self.failure_count = 0
            pre_roll = deque(maxlen=3)
            pre_roll_levels = deque(maxlen=3)
            in_speech = False
            quiet_chunks = 0
            speech_chunks = []
            voiced_chunks = 0
            speech_threshold = 350.0
            noise_floor = 180.0
            cooldown_until = 0.0
            while not self.stop_event.is_set():
                try:
                    chunk = self.chunks.get(timeout=0.1)
                except queue.Empty:
                    continue
                if time.monotonic() < cooldown_until:
                    continue
                rms = self._rms(chunk)
                if not in_speech:
                    speech_threshold = max(350.0, noise_floor * 2.2)
                    pre_roll.append(chunk)
                    pre_roll_levels.append(rms)
                    if rms < speech_threshold:
                        noise_floor = noise_floor * 0.95 + rms * 0.05
                        continue
                    in_speech = True
                    quiet_chunks = 0
                    speech_chunks = list(pre_roll)
                    voiced_chunks = sum(
                        level >= speech_threshold for level in pre_roll_levels
                    )
                else:
                    speech_chunks.append(chunk)
                    if rms >= speech_threshold:
                        voiced_chunks += 1
                if rms < speech_threshold:
                    quiet_chunks += 1
                else:
                    quiet_chunks = 0
                if in_speech and (quiet_chunks >= 4 or len(speech_chunks) >= 15):
                    transcript = None
                    if voiced_chunks >= 3:
                        try:
                            transcript = self._recognize_wake_segment(
                                decoder, speech_chunks
                            )
                        except Exception as exc:
                            print(
                                f"MarioAssistant wake word: decoder failed on an audio segment; reinitializing: {type(exc).__name__}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                            decoder = self._create_decoder(
                                pocketsphinx, acoustic_dir, dictionary_path,
                                language_model_path, keywords_path,
                            )
                    if transcript:
                        self.events.put(transcript)
                        return
                    pre_roll.clear()
                    pre_roll_levels.clear()
                    in_speech = False
                    quiet_chunks = 0
                    speech_chunks = []
                    voiced_chunks = 0
                    cooldown_until = time.monotonic() + 0.75
        except Exception as exc:
            self.failed = True
            self.failure_count += 1
            self.retry_at = time.monotonic() + min(
                2 ** min(self.failure_count, 5), 60
            )
            if (isinstance(exc, ImportError) or "model files are missing" in str(exc)) and not self.reported_unavailable:
                print(
                    "MarioAssistant wake word: offline PocketSphinx is unavailable; install the pocketsphinx dependency",
                    file=sys.stderr,
                    flush=True,
                )
                self.reported_unavailable = True
                return
            print(
                f"MarioAssistant wake word: listener failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            self.device = None
            if keywords_path is not None:
                try:
                    os.unlink(keywords_path)
                except OSError:
                    pass
            if device is not None:
                try:
                    device.pause(1)
                    device.close()
                except Exception as exc:
                    print(
                        f"MarioAssistant wake word: microphone close failed: {exc}",
                        file=sys.stderr,
                        flush=True,
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
    if any(contains(phrase) for phrase in (
        "i didn't say your name",
        "i did not say your name",
        "i didnt say your name",
        "i didn t say your name",
        "i didn't call you",
        "i did not call you",
        "i didnt call you",
        "i didn t call you",
    )):
        return "ohsorry"
    volume_commands = (
        ("set volume to ", "set"),
        ("lower volume by ", "lower"),
        ("increase volume by ", "increase"),
    )
    for prefix, operation in volume_commands:
        if text.startswith(prefix):
            amount = parse_voice_number(text[len(prefix):])
            if amount is not None:
                return f"volume_{operation}:{amount}"
    if "speak" in words or contains("say something"):
        return "speak"
    if "voice" in words and ("command" in words or "commands" in words):
        return "commands"
    if words.intersection({"quench", "clinch"}):
        if "hunger" in words and "thirst" not in words:
            return "quench_hunger"
        if "thirst" in words and "hunger" not in words:
            return "quench_thirst"
    if "move" in words or contains("out of the way") or contains("out of my way"):
        return "move"
    has_minigame = (
        "minigame" in words
        or "minigames" in words
        or ("mini" in words and "game" in words)
        or ("mini" in words and "games" in words)
    )
    if "play" in words and "random" in words and has_minigame:
        return "random_minigame"
    if (
        "minigames" in words
        and ("open" in words or "interactive" in words or "mario" in words)
    ):
        return "minigames"
    if (
        ("play" in words or "mario" in words)
        and "tunnel" in words
        and ("doom" in words or "do" in words or "doo" in words)
    ):
        return "tunnel"
    if (
        ("play" in words or "mario" in words)
        and "something" in words
        and "eye" in words
    ):
        return "eye"
    if "debug" in words:
        return "superdebug"
    if "pet" in words and "care" in words:
        return "petcare"
    if "settings" in words or "configure" in words:
        return "settings"
    return None


def parse_voice_number(text):
    tokens = text.strip().split()
    if tokens and tokens[-1] == "percent":
        tokens.pop()
    if not tokens:
        return None
    if len(tokens) == 1:
        try:
            number = float(tokens[0])
        except ValueError:
            number = None
        if number is not None and 0 <= number <= 100:
            return number
    small_numbers = {
        "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3,
        "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
        "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18,
        "nineteen": 19,
    }
    tens = {
        "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    }
    tokens = [token for token in tokens if token != "and"]
    if tokens == ["hundred"] or tokens in (["a", "hundred"], ["one", "hundred"]):
        return 100
    if len(tokens) == 2 and tokens[0] in tens and tokens[1] in small_numbers:
        number = tens[tokens[0]] + small_numbers[tokens[1]]
        return number if number <= 100 else None
    if len(tokens) == 1:
        return small_numbers.get(tokens[0], tens.get(tokens[0]))
    if len(tokens) in (2, 3) and tokens[0] in small_numbers and tokens[1] == "hundred":
        remainder = 0
        if len(tokens) == 3:
            remainder = small_numbers.get(tokens[2], tens.get(tokens[2], -1))
            if remainder < 0:
                return None
        number = small_numbers[tokens[0]] * 100 + remainder
        return number if number <= 100 else None
    return None
