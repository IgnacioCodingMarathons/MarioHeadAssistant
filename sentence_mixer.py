import io
import json
import os
import random
import re
import tempfile
import threading
import subprocess
import math
import uuid
import sys
import zipfile
from pathlib import Path

from phoneme_dict import load_pronunciations, guess_pronunciation
from sentence_mixer_sources import archive_needs_update, AUDIO_EXTENSIONS


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
        self.archive_path = assets_dir / 'mario_sentence_mixer.zip'
        self.ffmpeg = ffmpeg
        self.enabled = False
        self.started = False
        self.ready = False
        self.load_done = threading.Event()
        self.lock = threading.Lock()
        self.videos = []
        self.audio_only_sources = set()
        self.audio_filler_frames = None
        self.visual_random = random.Random()
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
                with zipfile.ZipFile(self.archive_path) as archive:
                    needs_update = archive_needs_update(json.loads(archive.read('index.json')), self.assets_dir)
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
            from sentence_mixing.model.video import Video
            from sentence_mixing.model.audio import SubtitleLine, AudioWord, AudioPhonem
            from sentence_mixing.logic.randomizer import Randomizer
            from sentence_mixing.logic.global_audio_data import audioDataFactory
            from sentence_mixing.logic import parameters
            from sentence_mixing_support import ensure_complete_search

            ensure_complete_search()
            parameters.MINIMAL_PHONEM_LENGTH = 0.015
            parameters.MAXIMAL_MINIMAL_PHONEM_LENGH_MALUS = 100

            with zipfile.ZipFile(self.archive_path) as archive:
                index = json.loads(archive.read('index.json'))
                blob = archive.read('fragments.pcm')
            if index.get('version') != 5:
                raise ValueError('Run prepare_sentence_mixer.py to update the prepared recordings')
            self.pronunciations = load_pronunciations()
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
                if source.get('audio_only', video.mario_path.suffix.lower() in AUDIO_EXTENSIONS):
                    self.audio_only_sources.add(video.mario_path)
                video.get_audio_wave = lambda data=stereo: (16000, data)
                subtitle = SubtitleLine(' '.join(word['text'] for word in source['words']),
                                        0, len(mono) / 16000, video)
                video.add_subtitle(subtitle)
                previous_end = 0
                for record in sorted(source['words'], key=lambda word: word['start']):
                    if record['start'] - previous_end >= 0.02:
                        gap = AudioWord(subtitle, '<BLANK>', ' ', previous_end, record['start'])
                        subtitle.add_word(gap)
                        gap.add_phonem(AudioPhonem(gap, 'sp', gap.start, gap.end, randomizer))
                    word = AudioWord(subtitle, record['text'].upper(), record['text'],
                                     record['start'], record['end'])
                    subtitle.add_word(word)
                    for phone, start, end in record['phones']:
                        word.add_phonem(AudioPhonem(word, phone, start, end, randomizer))
                    previous_end = record['end']
                if subtitle.words:
                    self.videos.append(video)
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

            sentence = normalize_numbers(str(text)).lower().replace("'", '')
            tokens = re.findall(r'[a-z]+|[,;.!?]', sentence)
            with self.lock:
                dictionary = parser.get_dict()
                for token in tokens:
                    if not token.isalpha():
                        continue
                    entries = self.pronunciations.get(token) or [guess_pronunciation(token)]
                    target_phones = entries[0]
                    mapped = [phone if phone in self.available else min(
                        (candidate for candidate in self.available if candidate != 'sp'),
                        key=lambda candidate: phone_distance(phone, candidate)
                    ) for phone in target_phones]
                    dictionary[token.upper()] = ' '.join(mapped)
                combos = sm.process_sm(' '.join(tokens), self.videos)
                if not combos:
                    raise RuntimeError('sentence-mixing returned no combinations')
                from sentence_mixing_support import complete_selected_phonemes
                selected = complete_selected_phonemes(combos[0], self.videos)
                runs = []
                for phone in selected:
                    video = phone.word.sentence.video
                    if runs and runs[-1][0] is video and abs(runs[-1][2] - phone.start) <= 0.015:
                        runs[-1] = (video, runs[-1][1], phone.end)
                    else:
                        runs.append((video, phone.start, phone.end))
                output = AudioSegment.silent(duration=0, frame_rate=16000)
                timeline = []
                for video, start, end in runs:
                    rate, data = video.get_audio_wave()
                    samples = data[round(start * rate):round(end * rate)]
                    if not len(samples):
                        continue
                    chunk = AudioSegment(samples.tobytes(), sample_width=2, frame_rate=rate, channels=2)
                    overlap = min(3, len(output), len(chunk))
                    timeline.append((max(0, (len(output) - overlap) / 1000),
                                     video.mario_path, start, end))
                    output = output.append(chunk, crossfade=overlap) if overlap else output + chunk
                result = io.BytesIO()
                normalize(output).set_frame_rate(44100).export(result, format='wav')
                if with_video:
                    return {'audio': result.getvalue(), 'video': self._render_video(timeline, len(output) / 1000),
                            'text': str(text)}
                return result.getvalue()
        except Exception as exc:
            print(f'MarioAssistant TTS: sentence-mixing failed: {type(exc).__name__}: {exc}', flush=True)
            return None
