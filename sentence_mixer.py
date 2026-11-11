import io
import os
import random
import re
import tempfile
import threading
import subprocess
import math
import uuid
import sys
from pathlib import Path

from phoneme_dict import load_pronunciations, guess_pronunciation
from sentence_mixer_sources import archive_needs_update, AUDIO_EXTENSIONS
from sentence_mixer_binary import MODEL_NAME, VERSION, read_model
from sentence_mixer_silence import original_video_time
from sentence_mixer_passages import RecordedPassages, format_subtitle_text, tokenize_variant_text


PHONE_GROUPS = (
    ('IY', 'IH', 'EH', 'AE'), ('AA', 'AH', 'AO'), ('UH', 'UW'),
    ('EY', 'AY', 'OY', 'AW', 'OW', 'ER'), ('P', 'B'), ('T', 'D'),
    ('K', 'G'), ('F', 'V'), ('TH', 'DH'), ('S', 'Z'), ('SH', 'ZH'),
    ('CH', 'JH'), ('M', 'N', 'NG'), ('L', 'R'), ('W', 'Y', 'HH'),
)
VOWEL_PHONES = frozenset(phone for group in PHONE_GROUPS[:4] for phone in group)


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

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if self.enabled and not self.started:
            self.started = True
            threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
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
            self.available = audioDataFactory(self.videos).get_transcription_dict_audio_phonem()
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
        frames = [None] * count
        run_index = 0
        for index in range(count):
            timestamp = min(index / fps, max(0, duration - 0.001))
            while run_index + 1 < len(timeline) and timestamp >= timeline[run_index + 1][0]:
                run_index += 1
            begin, source, start, end = timeline[run_index]
            if source in self.audio_only_sources or source.suffix.lower() in AUDIO_EXTENSIONS:
                frames[index] = self.visual_random.choice(self._get_audio_filler_frames())
                continue
            source_time = min(end - 0.001, start + max(0, timestamp - begin))
            source_time = original_video_time(self.source_time_maps.get(source, []), source_time)
            frame_index = max(0, int(source_time * fps))
            requested.setdefault(source, {}).setdefault(frame_index, []).append(index)
        for source, wanted in requested.items():
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
                        for destination in wanted[index]:
                            frames[destination] = last_frame
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

    def mix(self, text, with_video=False):
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
                for token_index, token in enumerate(speech_tokens):
                    if not token.isalpha():
                        continue
                    token_key = token.upper()
                    if token_key == 'SP':
                        dictionary[token_key] = 'sp'
                        self.runtime_pronunciations[token_key] = ['sp']
                        continue
                    options = word_options.get(token_index, {})
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
                for part, passage in self.recorded_passages.plan(
                    speech_tokens, controls, skip_recorded=skip_recorded, pauses=pauses
                ):
                    if part and part[0].startswith('\x00pause:'):
                        pause_duration = max(0, int(part[0].split(':', 1)[1])) / 1000
                        runs.append((None, pause_duration, pause_duration, None))
                        continue
                    if passage is not None:
                        runs.append((*passage, None))
                        token_cursor += len(part)
                        continue
                    combos = sm.process_sm(' '.join(part), self.videos)
                    if not combos:
                        raise RuntimeError('sentence-mixing returned no combinations')
                    from sentence_mixing_support import complete_selected_phonemes
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
                        adjusted_rate = max(1, round(rate * len(chunk) / (desired_length * 1000)))
                        chunk = chunk._spawn(chunk.raw_data, overrides={'frame_rate': adjusted_rate}).set_frame_rate(rate)
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
            print(f'MarioAssistant TTS: sentence-mixing failed: {type(exc).__name__}: {exc}', flush=True)
            return None
