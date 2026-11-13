import io
import json
import base64
import os
import random
import re
import tempfile
import threading
import subprocess
import math
import heapq
import uuid
import sys
import atexit
import traceback
from pathlib import Path

from phoneme_dict import load_pronunciations, guess_pronunciation
from sentence_mixer_sources import archive_needs_update, AUDIO_EXTENSIONS
from sentence_mixer_binary import MODEL_NAME, VERSION, read_model
from sentence_mixer_silence import original_video_time
from sentence_mixer_passages import ALL_VARIANTS, SIMULTANEOUS_TOKEN, RecordedPassages, SILENT_TOKEN, format_subtitle_text, tokenize_variant_text


PHONE_GROUPS = (
    ('IY', 'IH', 'EH', 'AE'), ('AA', 'AH', 'AO'), ('UH', 'UW'),
    ('EY', 'AY', 'OY', 'AW', 'OW', 'ER'), ('P', 'B'), ('T', 'D'),
    ('K', 'G'), ('F', 'V'), ('TH', 'DH'), ('S', 'Z'), ('SH', 'ZH'),
    ('CH', 'JH'), ('M', 'N', 'NG'), ('L', 'R'), ('W', 'Y', 'HH'),
)
VOWEL_PHONES = frozenset(phone for group in PHONE_GROUPS[:4] for phone in group)


def adjust_speed_preserving_pitch(audio, target_ms, ffmpeg):
    if target_ms <= 0 or len(audio) == 0:
        return audio
    speed = len(audio) / target_ms
    filters = []
    while speed > 2:
        filters.append('atempo=2')
        speed /= 2
    while speed < 0.5:
        filters.append('atempo=0.5')
        speed /= 0.5
    if abs(speed - 1) > 0.01:
        filters.append(f'atempo={speed:.6f}')
    if not filters:
        return audio
    source = io.BytesIO()
    audio.export(source, format='wav')
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
         '-filter:a', ','.join(filters), '-f', 'wav', 'pipe:1'],
        input=source.getvalue(), capture_output=True, check=True, timeout=8,
    )
    return audio.__class__.from_file(io.BytesIO(result.stdout), format='wav')


def phone_distance(first, second):
    if first == second:
        return 0
    if any(first in group and second in group for group in PHONE_GROUPS):
        return 1
    vowels = VOWEL_PHONES
    if first in vowels and second in vowels:
        return 3
    return 6 if (first in vowels) == (second in vowels) else 12


def normalize_numbers(text):
    from num2words import num2words

    def replace_time(match):
        hour = int(match.group(1))
        minute = int(match.group(2))
        parts = [num2words(hour)]
        if minute == 0:
            parts.append("o'clock")
        elif minute < 10:
            parts.extend(("oh", num2words(minute)))
        else:
            parts.append(num2words(minute))
        return ' '.join(parts)

    text = re.sub(r'\b(\d{1,2}):(\d{2})\b', replace_time, text)
    text = re.sub(r'\b([ap])\.?m\.?\b', r'\1 m', text, flags=re.IGNORECASE)

    def replace_ordinal(match):
        return num2words(int(match.group(1)), to='ordinal')

    text = re.sub(r'\b(\d+)(st|nd|rd|th)\b', replace_ordinal, text, flags=re.IGNORECASE)

    def replace_number(match):
        value = match.group(0).replace(',', '')
        if '.' in value:
            whole, decimal = value.split('.', 1)
            integer = 'minus zero' if value.startswith('-') and int(whole) == 0 else num2words(int(whole))
            return integer + ' point ' + ' '.join(
                num2words(int(digit)) for digit in decimal
            )
        return num2words(int(value))

    return re.sub(r'(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?\b', replace_number, text)


def enable_safe_sentence_search(audio_data):
    import sentence_mixing.logic.analyze as analyze

    if getattr(analyze.get_n_best_combos, 'mario_safe_search', False):
        return
    from sentence_mixing.logic import parameters, utils
    from sentence_mixing.model.exceptions import Interruption
    from sentence_mixing.logic.randomizer import Randomizer
    from sentence_mixing.model.choice import Combo

    def collect_combos(root):
        combos = []
        stack = [root]
        while stack:
            choice = stack.pop()
            choice.children = choice._create_children()
            if choice.children:
                stack.extend(reversed(choice.children))
            else:
                combos.append(Combo(choice))
        return combos

    def safe_get_n_best_combos(sentence, videos, randomizer, n=1,
                               interrupt_callback=None):
        analyze.audio_data = audio_data
        target_phonems = utils.get_phonems(sentence.words)
        roots = analyze.compute_children(target_phonems[0], parameters.NODES, None, randomizer)
        if len(roots) < 2:
            if not roots:
                combos = []
            else:
                if interrupt_callback is not None and interrupt_callback():
                    raise Interruption(interrupt_callback)
                stream = collect_combos(roots[0])
                combos = heapq.nlargest(n, stream, key=lambda combo: combo.get_score())
        else:
            for position, root in enumerate(roots):
                isolated_random = random.Random(position + len(target_phonems))
                root.randomizer = Randomizer(isolated_random)
            best_combo = None
            best_position = None
            combos = []
            for position, root in enumerate(roots):
                if interrupt_callback is not None and interrupt_callback():
                    raise Interruption(interrupt_callback)
                root_combos = collect_combos(root)
                if not root_combos:
                    continue
                combo = max(root_combos, key=lambda candidate: candidate.get_score())
                if n == 1:
                    if (best_combo is None or combo.get_score() > best_combo.get_score()
                            or (combo.get_score() == best_combo.get_score()
                                and position < best_position)):
                        best_combo = combo
                        best_position = position
                else:
                    combos.extend(root_combos)
                    if len(combos) > n * 2:
                        combos = heapq.nlargest(n, combos, key=lambda candidate: candidate.get_score())
            if n == 1:
                return [best_combo] if best_combo is not None else []
            combos.sort(key=lambda combo: combo.get_score(), reverse=True)
            return combos[:n]
        combos.sort(key=lambda combo: combo.get_score(), reverse=True)
        return combos[:n]

    safe_get_n_best_combos.mario_safe_search = True
    analyze.get_n_best_combos = safe_get_n_best_combos


class LiveSentenceMixer:
    def __init__(self, assets_dir, ffmpeg):
        self.assets_dir = assets_dir
        self.archive_path = assets_dir / MODEL_NAME
        self.ffmpeg = ffmpeg
        self.enabled = False
        self.started = False
        self.ready = False
        self.load_done = threading.Event()
        self.lock = threading.Lock()
        self.videos = []
        self.audio_only_sources = set()
        self.source_time_maps = {}
        self.audio_filler_frames = None
        self.visual_random = random.Random()
        self.runtime_pronunciations = {}
        self.indexed_pronunciations = {}
        self.fallback_words = set()
        self.temporary = tempfile.TemporaryDirectory(prefix='mario-sentence-mixing-')
        self.worker_process = None
        self.worker_python = None
        atexit.register(self._stop_worker)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if self.enabled and not self.started:
            self.started = True
            threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        worker_python = self._find_free_threaded_python()
        if worker_python is not None:
            try:
                self._prepare_archive_with_parent_python()
                pronunciation_path = self._export_worker_pronunciations()
                self._start_worker(worker_python, pronunciation_path)
                self.ready = True
                print('MarioAssistant TTS: free-threaded sentence-mixing worker ready.', flush=True)
                self.load_done.set()
                return
            except Exception as exc:
                self._stop_worker()
                print(f'MarioAssistant TTS: free-threaded worker unavailable: {type(exc).__name__}: {exc}', flush=True)
        self._load_engine()

    def _prepare_archive_with_parent_python(self):
        needs_update = not self.archive_path.is_file()
        if not needs_update:
            try:
                index, _ = read_model(self.archive_path)
                needs_update = index['version'] != VERSION or archive_needs_update(index, self.assets_dir)
            except (OSError, ValueError):
                needs_update = True
        if needs_update:
            print('MarioAssistant TTS: preparing new or changed sentence-mixing clips before worker startup…', flush=True)
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name('prepare_sentence_mixer.py')),
                 '--assets', str(self.assets_dir), '--ffmpeg', str(self.ffmpeg)],
                check=True,
            )

    def _export_worker_pronunciations(self):
        from phoneme_dict import load_pronunciations

        path = Path(self.temporary.name) / 'pronunciations.json'
        path.write_text(json.dumps(load_pronunciations()), encoding='utf-8')
        return path

    def _find_free_threaded_python(self):
        configured = os.environ.get('MARIOASSISTANT_MIXER_PYTHON')
        candidates = [Path(configured)] if configured else []
        environment = Path(__file__).resolve().parent / '.venv'
        if os.name == 'nt':
            candidates.append(environment / 'Scripts' / 'python.exe')
        else:
            candidates.append(environment / 'bin' / 'python')
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                result = subprocess.run(
                    [str(candidate), '-c',
                     'import sysconfig; raise SystemExit(0 if sysconfig.get_config_var("Py_GIL_DISABLED") == 1 else 1)'],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return candidate
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    def _start_worker(self, python, pronunciation_path):
        worker_script = Path(__file__).with_name('sentence_mixer_worker.py')
        self.worker_python = python
        environment = os.environ.copy()
        environment['MARIOASSISTANT_PRONUNCIATIONS_PATH'] = str(pronunciation_path)
        self.worker_process = subprocess.Popen(
            [str(python), '-u', str(worker_script), '--assets', str(self.assets_dir),
             '--ffmpeg', str(self.ffmpeg)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, encoding='utf-8', bufsize=1, env=environment,
        )
        response = self.worker_process.stdout.readline()
        if not response:
            raise RuntimeError('worker exited before reporting readiness')
        ready = json.loads(response)
        if not ready.get('ready'):
            raise RuntimeError(ready.get('error', 'worker did not become ready'))
        if not ready.get('gil_disabled'):
            raise RuntimeError('worker loaded its dependencies with the GIL enabled')

    def _stop_worker(self):
        process = self.worker_process
        self.worker_process = None
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write('{"command":"stop"}\n')
                process.stdin.flush()
                process.stdin.close()
                process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass

    def _mix_in_worker(self, text, with_video):
        process = self.worker_process
        if process is None or process.poll() is not None:
            raise RuntimeError('free-threaded sentence-mixing worker is not running')
        request = json.dumps({'command': 'mix', 'text': text, 'with_video': with_video})
        process.stdin.write(request + '\n')
        process.stdin.flush()
        response = process.stdout.readline()
        if not response:
            raise RuntimeError('free-threaded sentence-mixing worker exited unexpectedly')
        result = json.loads(response)
        if not result.get('ok'):
            print(f"MarioAssistant TTS: worker mix failed: {result.get('error', 'unknown error')}", flush=True)
            return None
        if not result.get('media'):
            return None
        media = result['media']
        return {'audio': base64.b64decode(media['audio']),
                'video': Path(media['video']) if media.get('video') else None,
                'text': media.get('text', '')} if with_video else base64.b64decode(media['audio'])

    def _load_engine(self):
        try:
            needs_update = not self.archive_path.is_file()
            if not needs_update:
                try:
                    index, _ = read_model(self.archive_path)
                    needs_update = index['version'] != VERSION or archive_needs_update(index, self.assets_dir)
                except (OSError, ValueError):
                    needs_update = True
            if needs_update:
                print('MarioAssistant TTS: preparing new or changed sentence-mixing clips in the background…', flush=True)
                subprocess.run(
                    [sys.executable, str(Path(__file__).with_name('prepare_sentence_mixer.py')),
                     '--assets', str(self.assets_dir), '--ffmpeg', str(self.ffmpeg)],
                    check=True,
                )
            os.environ.setdefault('FFMPEG_BINARY', str(self.ffmpeg))
            import numpy as np
            import sentence_mixing.sentence_mixer as sm
            import sentence_mixing.logic.text_parser as parser
            from sentence_mixing.model.video import Video
            from sentence_mixing.model.audio import SubtitleLine, AudioWord, AudioPhonem
            from sentence_mixing.logic.randomizer import Randomizer
            from sentence_mixing.logic.global_audio_data import audioDataFactory
            from sentence_mixing.logic import parameters
            from sentence_mixing_support import ensure_complete_search

            original_from_token_to_phonem = parser.from_token_to_phonem

            def from_token_to_phonem(token):
                token_key = token.upper()
                if token_key == 'SP':
                    return ['sp']
                override = self.runtime_pronunciations.get(token_key)
                if override:
                    return list(override)
                if token_key.isalpha():
                    existing = parser.get_dict().get(token_key)
                    if existing:
                        return existing.split()
                    indexed = self.indexed_pronunciations.get(token_key.lower())
                    entries = self.pronunciations.get(token_key.lower())
                    target_phones = indexed or (entries[0] if entries else guess_pronunciation(token_key.lower()))
                    if target_phones:
                        candidates = [phone for phone in self.available if phone != 'sp']
                        mapped = [phone if phone in self.available else min(
                            candidates, key=lambda candidate: phone_distance(phone, candidate)
                        ) for phone in target_phones] if candidates else target_phones
                        self.runtime_pronunciations[token_key] = mapped
                        parser.get_dict()[token_key] = ' '.join(mapped)
                        if not indexed and not entries and token_key not in self.fallback_words:
                            self.fallback_words.add(token_key)
                            print(f'MarioAssistant TTS: using an estimated pronunciation for unindexed word {token_key}. Add it in edit_sentence_mixer.py for a recorded pronunciation.', flush=True)
                        return list(mapped)
                return original_from_token_to_phonem(token)

            parser.from_token_to_phonem = from_token_to_phonem

            ensure_complete_search()
            parameters.MINIMAL_PHONEM_LENGTH = 0.015
            parameters.MAXIMAL_MINIMAL_PHONEM_LENGH_MALUS = 100

            index, blob = read_model(self.archive_path)
            self.pronunciations = load_pronunciations()
            self.indexed_pronunciations.clear()
            for source in index['sources']:
                for record in source['words']:
                    labels = [phone for phone, _, _ in record.get('phones', [])]
                    word_tokens = re.findall(r'[a-z]+', record.get('text', '').lower())
                    token = word_tokens[0] if len(word_tokens) == 1 else ''
                    if token and labels:
                        self.indexed_pronunciations.setdefault(token, labels)
            folder = Path(self.temporary.name)
            dictionary_path = folder / 'words.dict'
            dictionary_path.write_text('\n'.join(
                word.upper() + ' ' + ' '.join(entries[0])
                for word, entries in self.pronunciations.items() if entries
            ), encoding='utf-8')
            phone_types = folder / 'phone_types.dict'
            phones = {phone for entries in self.pronunciations.values() for phone in entries[0]}
            vowels = set('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())
            phone_types.write_text(
                'VOWEL ' + ' '.join(sorted(vowels)) + '\nCONSONANT '
                + ' '.join(sorted(phones - vowels)) + '\nSPACE sp\n', encoding='utf-8'
            )
            sm.prepare_sm_config_dict({'lang': 'en', 'dict_path': str(dictionary_path),
                                      'dict_consonant_vowel_path': str(phone_types)})
            sm.SEED = 0
            randomizer = Randomizer(random.Random(0))
            for source in index['sources']:
                offset, length = source['offset'], source['length']
                mono = np.frombuffer(blob[offset:offset + length], dtype='<i2')
                stereo = np.column_stack((mono, mono))
                video = Video('mario://' + source['name'], source['name'], '.vtt')
                video.mario_path = self.assets_dir / source['name']
                self.source_time_maps[video.mario_path] = source['time_map']
                if source.get('audio_only', video.mario_path.suffix.lower() in AUDIO_EXTENSIONS):
                    self.audio_only_sources.add(video.mario_path)
                video.get_audio_wave = lambda data=stereo: (16000, data)
                subtitle = SubtitleLine(' '.join(word['text'] for word in source['words']),
                                        0, len(mono) / 16000, video)
                video.add_subtitle(subtitle)
                for record in sorted(source['words'], key=lambda word: word['start']):
                    word = AudioWord(subtitle, record['text'].upper(), record['text'],
                                     record['start'], record['end'])
                    word.mario_aliases = record.get('aliases', [])
                    subtitle.add_word(word)
                    for phone, start, end in record['phones']:
                        word.add_phonem(AudioPhonem(word, phone, start, end, randomizer))
                if subtitle.words:
                    self.videos.append(video)
            silence_video = Video('mario://silence', 'silence', '.vtt')
            silence_video.mario_path = self.assets_dir / 'greeting.avi'
            silence_video.get_audio_wave = lambda: (16000, np.zeros((1280, 2), dtype='<i2'))
            silence_line = SubtitleLine(' ', 0, 0.08, silence_video)
            silence_video.add_subtitle(silence_line)
            silence_word = AudioWord(silence_line, '<BLANK>', ' ', 0, 0.08)
            silence_line.add_word(silence_word)
            silence_word.add_phonem(AudioPhonem(silence_word, 'sp', 0, 0.08, randomizer))
            self.videos.append(silence_video)
            self.recorded_passages = RecordedPassages(self.videos, self.pronunciations)
            sentence_audio_data = audioDataFactory(self.videos)
            self.available = sentence_audio_data.get_transcription_dict_audio_phonem()
            enable_safe_sentence_search(sentence_audio_data)
            self.ready = bool(self.available)
            print(f'MarioAssistant TTS: sentence-mixing ready with {len(self.videos)} local clips.', flush=True)
        except Exception as exc:
            print(f'MarioAssistant TTS: sentence-mixing load failed: {type(exc).__name__}: {exc}', flush=True)
        finally:
            self.load_done.set()

    def _get_audio_filler_frames(self):
        if self.audio_filler_frames is None:
            raw = subprocess.run(
                [self.ffmpeg, '-v', 'error', '-i', str(self.assets_dir / 'goodjoke.avi'),
                 '-an', '-vf', 'fps=15,scale=400:300', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                capture_output=True, check=True, timeout=30,
            ).stdout
            frame_size = 400 * 300 * 3
            frames = [raw[offset:offset + frame_size]
                      for offset in range(0, len(raw) - frame_size + 1, frame_size)]
            if not frames:
                raise RuntimeError('goodjoke.avi contains no frames for audio-only material')
            self.audio_filler_frames = frames
        return self.audio_filler_frames

    def _render_video(self, timeline, duration):
        width, height, fps = 400, 300, 15
        frame_size = width * height * 3
        count = max(1, math.ceil(duration * fps))
        requested = {}
        frames = [bytearray(frame_size) for _ in range(count)]
        import numpy as np
        from PIL import Image

        def request_frame(source, source_time, destination, panel, panel_count):
            if source in self.audio_only_sources or source.suffix.lower() in AUDIO_EXTENSIONS:
                image = self.visual_random.choice(self._get_audio_filler_frames())
                requested.setdefault(('raw', destination, panel, panel_count), image)
                return
            source_time = original_video_time(self.source_time_maps.get(source, []), source_time)
            frame_index = max(0, int(source_time * fps))
            requested.setdefault(source, {}).setdefault(frame_index, []).append(
                (destination, panel, panel_count)
            )

        for index in range(count):
            timestamp = min(index / fps, max(0, duration - 0.001))
            entry_index = max((position for position, entry in enumerate(timeline)
                               if entry[0] <= timestamp), default=0)
            if not timeline:
                continue
            entry = timeline[entry_index]
            if entry[1] == SIMULTANEOUS_TOKEN:
                group_duration, members = entry[2]
                elapsed = timestamp - entry[0]
                if elapsed < group_duration:
                    for panel, segments in enumerate(members):
                        active = next((segment for segment in segments
                                       if segment[0] <= elapsed < segment[0] + segment[4]), None)
                        if active is None:
                            continue
                        offset, source_video, source_start, source_end, segment_duration = active
                        fraction = min(1.0, max(0.0, (elapsed - offset) / max(0.001, segment_duration)))
                        source_time = source_start + fraction * max(0.0, source_end - source_start)
                        request_frame(source_video.mario_path, source_time, index, panel, len(members))
                continue
            begin, source, start, end = entry
            next_begin = timeline[entry_index + 1][0] if entry_index + 1 < len(timeline) else duration
            if timestamp >= next_begin:
                continue
            source_time = min(end - 0.001, start + max(0, timestamp - begin))
            request_frame(source, source_time, index, 0, 1)
        for source, wanted in requested.items():
            if isinstance(source, tuple) and source[0] == 'raw':
                _, destination, panel, panel_count = source
                raw = wanted
                self._paste_video_panel(frames, destination, panel, panel_count, raw, Image, np, width, height)
                continue
            decoder = subprocess.Popen(
                [self.ffmpeg, '-v', 'error', '-i', str(source), '-an', '-vf',
                 f'fps={fps},scale={width}:{height}', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            last_frame = bytes(frame_size)
            try:
                for index in range(max(wanted) + 1):
                    frame = decoder.stdout.read(frame_size)
                    if len(frame) == frame_size:
                        last_frame = frame
                    if index in wanted:
                        for destination, panel, panel_count in wanted[index]:
                            self._paste_video_panel(frames, destination, panel, panel_count,
                                                    last_frame, Image, np, width, height)
            finally:
                if decoder.poll() is None:
                    decoder.terminate()
                decoder.stdout.close()
                decoder.wait()
        path = Path(self.temporary.name) / f'{uuid.uuid4().hex}.mp4'
        subprocess.run(
            [self.ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
             '-s', f'{width}x{height}', '-r', str(fps), '-i', '-', '-an',
             '-c:v', 'mpeg4', '-q:v', '3', str(path)],
            input=b''.join(frames), capture_output=True, check=True, timeout=30,
        )
        return path

    @staticmethod
    def _paste_video_panel(frames, destination, panel, panel_count, raw, Image, np, width, height):
        panel_width = max(1, width // max(1, panel_count))
        left = panel * panel_width
        right = width if panel == panel_count - 1 else min(width, left + panel_width)
        if right <= left:
            return
        image = Image.frombytes('RGB', (width, height), raw).resize(
            (right - left, height), Image.Resampling.BILINEAR
        )
        canvas = np.frombuffer(frames[destination], dtype=np.uint8).reshape(height, width, 3)
        canvas[:, left:right] = np.asarray(image)

    def mix(self, text, with_video=False):
        if not self.enabled:
            return None
        self.load_done.wait()
        if self.worker_process is not None:
            with self.lock:
                try:
                    return self._mix_in_worker(text, with_video)
                except Exception as exc:
                    print(f'MarioAssistant TTS: free-threaded worker failed: {type(exc).__name__}: {exc}', flush=True)
                    return None
        return self._mix_in_process(text, with_video)

    def _mix_in_process(self, text, with_video=False):
        if not self.enabled:
            return None
        self.load_done.wait()
        if not self.ready:
            return None
        try:
            import sentence_mixing.sentence_mixer as sm
            import sentence_mixing.logic.text_parser as parser
            from pydub import AudioSegment
            from pydub.effects import normalize
            from sentence_mixing_support import complete_selected_phonemes

            cleaned_text, tokens, controls, word_options, pauses = tokenize_variant_text(text, normalize_numbers)
            subtitle_text = format_subtitle_text(cleaned_text, word_options, normalize_numbers)
            speech_tokens = list(tokens)
            for token_index, options in word_options.items():
                if 'custom_word' in options and token_index < len(speech_tokens):
                    speech_tokens[token_index] = options['custom_word']
            with self.lock:
                dictionary = parser.get_dict()
                self.runtime_pronunciations.clear()
                custom_durations = {}
                skip_recorded = set()
                silent_tokens = set()
                for token_index, token in enumerate(speech_tokens):
                    if not token.isalpha():
                        continue
                    token_key = token.upper()
                    options = word_options.get(token_index, {})
                    if options.get('silent'):
                        silent_tokens.add(token_index)
                        continue
                    if token_key == 'SP':
                        dictionary[token_key] = 'sp'
                        self.runtime_pronunciations[token_key] = ['sp']
                        continue
                    indexed = self.indexed_pronunciations.get(token.lower())
                    entries = self.pronunciations.get(token.lower())
                    custom = options.get('custom_phones')
                    if options.get('ignore_recorded') or custom:
                        skip_recorded.add(token_index)
                    target_phones = ([phone for phone, _ in custom] if custom else
                                     entries[0] if options.get('ignore_recorded') and entries else
                                     guess_pronunciation(token) if options.get('ignore_recorded') else
                                     indexed or (entries[0] if entries else guess_pronunciation(token)))
                    if not target_phones:
                        continue
                    candidates = [phone for phone in self.available if phone != 'sp']
                    mapped = [phone if phone in self.available else min(
                        candidates, key=lambda candidate: phone_distance(phone, candidate)
                    ) for phone in target_phones] if candidates else target_phones
                    dictionary[token_key] = ' '.join(mapped)
                    self.runtime_pronunciations[token_key] = mapped
                    if custom:
                        custom_durations[token_index] = [duration for _, duration in custom]
                    if not custom and not indexed and not entries and token_key not in self.fallback_words:
                        self.fallback_words.add(token_key)
                        print(f'MarioAssistant TTS: using an estimated pronunciation for unindexed word {token_key}. Add it in edit_sentence_mixer.py for a recorded pronunciation.', flush=True)
                runs = []
                token_cursor = 0

                def word_runs(position, token):
                    options = word_options.get(position, {})
                    if options.get('silent'):
                        return []
                    local_options = {key: value for key, value in options.items() if key != 'simul'}
                    local_controls = {0: controls[position]} if position in controls else {}
                    local_skip = {0} if position in skip_recorded else set()
                    local_plan = self.recorded_passages.plan(
                        [token], local_controls, local_skip,
                        word_options={0: local_options},
                    )
                    result_runs = []
                    for word_part, word_passage in local_plan:
                        if word_passage is not None:
                            if (isinstance(word_passage, tuple) and len(word_passage) == 2
                                    and word_passage[0] == ALL_VARIANTS):
                                result_runs.extend((*variant, None) for variant in word_passage[1])
                            else:
                                result_runs.append((*word_passage, None))
                            continue
                        combos = sm.process_sm(' '.join(word_part), self.videos)
                        if not combos:
                            raise RuntimeError(f"sentence-mixing returned no combination for {token}")
                        selected = complete_selected_phonemes(
                            combos[0], self.videos,
                            prefer_recorded=not (options.get('ignore_recorded') or options.get('custom_phones')),
                        )
                        custom = options.get('custom_phones')
                        for phone_index, phone in enumerate(selected):
                            desired = custom[phone_index][1] if custom and phone_index < len(custom) else None
                            result_runs.append((phone.word.sentence.video, phone.start, phone.end, desired))
                    return result_runs

                for part, passage in self.recorded_passages.plan(
                    speech_tokens, controls, skip_recorded=skip_recorded, pauses=pauses,
                    silent_tokens=silent_tokens, word_options=word_options
                ):
                    if (isinstance(passage, tuple) and len(passage) == 2
                            and passage[0] == SIMULTANEOUS_TOKEN):
                        member_runs = [word_runs(position, speech_tokens[position])
                                       for position in passage[1]]
                        runs.append((SIMULTANEOUS_TOKEN, member_runs, None, None))
                        token_cursor = passage[1][-1] + 1
                        continue
                    if part and part[0] == SILENT_TOKEN:
                        runs.append((None, 0.25, 0.25, None))
                        token_cursor += 1
                        continue
                    if part and part[0].startswith('\x00pause:'):
                        pause_duration = max(0, int(part[0].split(':', 1)[1])) / 1000
                        runs.append((None, pause_duration, pause_duration, None))
                        continue
                    if (isinstance(passage, tuple) and len(passage) == 2
                            and passage[0] == ALL_VARIANTS):
                        runs.extend((*variant, None) for variant in passage[1])
                        token_cursor += len(part)
                        continue
                    if passage is not None:
                        runs.append((*passage, None))
                        token_cursor += len(part)
                        continue
                    combos = sm.process_sm(' '.join(part), self.videos)
                    if not combos:
                        raise RuntimeError('sentence-mixing returned no combinations')
                    custom_length = custom_durations.get(token_cursor)
                    ignore_recorded = any(
                        word_options.get(position, {}).get('ignore_recorded')
                        or word_options.get(position, {}).get('custom_phones')
                        for position in range(token_cursor, token_cursor + len(part))
                    )
                    selected = complete_selected_phonemes(
                        combos[0], self.videos, prefer_recorded=not ignore_recorded
                    )
                    if custom_length:
                        for phone, desired_length in zip(selected, custom_length):
                            runs.append((phone.word.sentence.video, phone.start, phone.end, desired_length))
                        token_cursor += len(part)
                        continue
                    for phone in selected:
                        video = phone.word.sentence.video
                        if runs and runs[-1][0] is video and abs(runs[-1][2] - phone.start) <= 0.015:
                            runs[-1] = (video, runs[-1][1], phone.end, None)
                        else:
                            runs.append((video, phone.start, phone.end, None))
                    token_cursor += len(part)
                output = AudioSegment.silent(duration=0, frame_rate=16000)
                timeline = []
                previous_was_pause = False
                for video, start, end, desired_length in runs:
                    if video == SIMULTANEOUS_TOKEN:
                        member_audio = []
                        member_timeline = []
                        max_duration = 0
                        for member in start:
                            audio_part = AudioSegment.silent(duration=0, frame_rate=16000)
                            visual_segments = []
                            for source_video, source_start, source_end, desired in member:
                                rate, data = source_video.get_audio_wave()
                                samples = data[round(source_start * rate):round(source_end * rate)]
                                if not len(samples):
                                    continue
                                chunk = AudioSegment(samples.tobytes(), sample_width=2,
                                                     frame_rate=rate, channels=2)
                                if desired is not None and desired > 0:
                                    chunk = adjust_speed_preserving_pitch(
                                        chunk, round(desired * 1000), self.ffmpeg
                                    )
                                visual_segments.append((len(audio_part) / 1000, source_video,
                                                        source_start, source_end, len(chunk) / 1000))
                                audio_part += chunk
                            member_audio.append(audio_part)
                            member_timeline.append(visual_segments)
                            max_duration = max(max_duration, len(audio_part))
                        group_audio = AudioSegment.silent(duration=max_duration, frame_rate=16000)
                        for audio_part in member_audio:
                            group_audio = group_audio.overlay(audio_part)
                        group_start = len(output) / 1000
                        timeline.append((group_start, SIMULTANEOUS_TOKEN,
                                         (max_duration / 1000, member_timeline)))
                        output += group_audio
                        previous_was_pause = False
                        continue
                    if video is None:
                        output += AudioSegment.silent(duration=round(start * 1000), frame_rate=16000)
                        previous_was_pause = True
                        continue
                    rate, data = video.get_audio_wave()
                    samples = data[round(start * rate):round(end * rate)]
                    if not len(samples):
                        continue
                    chunk = AudioSegment(samples.tobytes(), sample_width=2, frame_rate=rate, channels=2)
                    if desired_length is not None and desired_length > 0:
                        chunk = adjust_speed_preserving_pitch(
                            chunk, round(desired_length * 1000), self.ffmpeg
                        )
                    overlap = 0 if previous_was_pause else min(3, len(output), len(chunk))
                    timeline.append((max(0, (len(output) - overlap) / 1000),
                                     video.mario_path, start, end))
                    output = output.append(chunk, crossfade=overlap) if overlap else output + chunk
                    previous_was_pause = False
                result = io.BytesIO()
                normalize(output).set_frame_rate(44100).export(result, format='wav')
                if with_video:
                    return {'audio': result.getvalue(), 'video': self._render_video(timeline, len(output) / 1000),
                            'text': subtitle_text}
                return result.getvalue()
        except Exception as exc:
            print(f'MarioAssistant TTS: sentence-mixing failed: {type(exc).__name__}: {exc}\n'
                  f'{traceback.format_exc()}', flush=True)
            return None
