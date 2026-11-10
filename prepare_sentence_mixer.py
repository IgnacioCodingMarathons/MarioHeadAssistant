import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from phoneme_dict import load_pronunciations
from sentence_mixer_sources import discover_clips, source_fingerprint, AUDIO_EXTENSIONS


def parse_result(raw):
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            result = payload.get('result', [])
            if isinstance(result, list):
                return result
    except (json.JSONDecodeError, TypeError):
        pass
    parsed = []
    for item in re.findall(r'\{[^{}]*\}', str(raw)):
        word = re.search(
            r'''["']?word["']?\s*:\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([^,}\s]+))''',
            item,
        )
        start = re.search(r'''["']?start["']?\s*:\s*(-?\d+(?:\.\d+)?)''', item)
        end = re.search(r'''["']?end["']?\s*:\s*(-?\d+(?:\.\d+)?)''', item)
        if word and start and end:
            parsed.append({
                'word': word.group(1) or word.group(2) or word.group(3),
                'start': float(start.group(1)),
                'end': float(end.group(1)),
            })
    return parsed


def add_fragment(index, blob, key, fragment):
    if not key or len(fragment) < 640:
        return
    offset = len(blob)
    blob.extend(fragment)
    index.setdefault(key, []).append([offset, len(fragment)])


def reuse_source(cached, old_blob, blob, word_index, unit_index):
    decoded = old_blob[cached['offset']:cached['offset'] + cached['length']]
    source = dict(cached, offset=len(blob))
    blob.extend(decoded)
    for word in source['words']:
        left, right = round(word['start'] * 16000), round(word['end'] * 16000)
        add_fragment(word_index, blob, word['text'], decoded[left * 2:right * 2])
        phones = word['phones']
        for first in range(len(phones)):
            for last in range(first + 1, min(first + 8, len(phones)) + 1):
                part = phones[first:last]
                left, right = round(part[0][1] * 16000), round(part[-1][2] * 16000)
                if 320 <= right - left <= 19200:
                    add_fragment(unit_index, blob, ' '.join(phone[0] for phone in part),
                                 decoded[left * 2:right * 2])
    return source


def audio_only_source(clip, ffmpeg):
    if clip.suffix.lower() in AUDIO_EXTENSIONS:
        return True
    probe = shutil.which('ffprobe')
    if probe is None:
        candidate = Path(ffmpeg).with_name('ffprobe.exe' if sys.platform == 'win32' else 'ffprobe')
        probe = str(candidate) if candidate.is_file() else None
    if probe is None:
        return False
    result = subprocess.run(
        [probe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
         'stream=index', '-of', 'csv=p=0', str(clip)],
        capture_output=True, check=True, timeout=15,
    )
    return not result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', type=Path, default=Path(__file__).resolve().parent / 'assets')
    parser.add_argument('--model', type=Path)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--rebuild', action='store_true')
    options = parser.parse_args()
    assets = options.assets.resolve()
    source_files = source_fingerprint(assets)
    model_path = options.model.resolve() if options.model else assets / 'vosk'
    output_path = options.output.resolve() if options.output else assets / 'mario_sentence_mixer.zip'
    clips = discover_clips(assets)
    if not clips:
        parser.error(f'No audio or video clips found in {assets} or its sentencemixvids folder')
    cached_index = {}
    old_blob = b''
    if output_path.is_file() and not options.rebuild:
        with zipfile.ZipFile(output_path) as archive:
            cached_index = json.loads(archive.read('index.json'))
            if cached_index.get('version') == 5:
                old_blob = archive.read('fragments.pcm')
            else:
                cached_index = {}
    if cached_index.get('source_files') == source_files:
        print('Sentence-mixing clips are already up to date; nothing to process.', flush=True)
        return
    word_index = {}
    unit_index = {}
    sources = []
    blob = bytearray()
    cached_sources = {source['name']: source for source in cached_index.get('sources', [])}
    old_files = cached_index.get('source_files', {})
    pending = []
    for clip in clips:
        name = clip.relative_to(assets).as_posix()
        unchanged = old_files.get(name) == source_files[name]
        if name in cached_sources and (unchanged or 'source_files' not in cached_index):
            sources.append(reuse_source(cached_sources[name], old_blob, blob, word_index, unit_index))
            print(f'Reused {name}', flush=True)
        elif unchanged:
            print(f'Reused skipped clip {name}', flush=True)
        else:
            pending.append(clip)
    if pending:
        ffmpeg = shutil.which(options.ffmpeg)
        if ffmpeg is None:
            parser.error(f'ffmpeg was not found: {options.ffmpeg}')
        if not model_path.is_dir():
            parser.error(f'Vosk model folder was not found: {model_path}')
        try:
            import vosk
            from pocketsphinx import Decoder
        except ImportError as exc:
            parser.error(f'vosk and pocketsphinx are required to prepare clips: {exc}')
        vosk.SetLogLevel(-1)
        model = vosk.Model(str(model_path))
        pronunciations = load_pronunciations()
    for clip in pending:
        try:
            is_audio_only = audio_only_source(clip, ffmpeg)
            decoded = subprocess.run(
                [ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(clip),
                 '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'],
                capture_output=True,
                timeout=90,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            print(f'Skipped {clip.name}: audio extraction failed: {exc}', flush=True)
            continue
        if len(decoded) < 3200:
            print(f'Skipped {clip.name}: no usable audio track', flush=True)
            continue
        recognizer = vosk.KaldiRecognizer(model, 16000)
        recognizer.SetWords(True)
        recognized = []
        for offset in range(0, len(decoded), 64000):
            if recognizer.AcceptWaveform(decoded[offset:offset + 64000]):
                recognized.extend(parse_result(recognizer.Result()))
        recognized.extend(parse_result(recognizer.FinalResult()))
        indexed_words = 0
        indexed_diphones = 0
        samples_total = len(decoded) // 2
        source = {'name': clip.relative_to(assets).as_posix(), 'offset': len(blob),
                  'length': len(decoded), 'words': [], 'audio_only': is_audio_only}
        blob.extend(decoded)
        for item in recognized:
            key = re.sub(r'[^a-z]', '', str(item.get('word', '')).lower())
            if not key or float(item.get('conf', 1)) < 0.75:
                continue
            try:
                start = max(0, int(float(item['start']) * 16000) - 240)
                end = min(samples_total, int(float(item['end']) * 16000) + 240)
            except (KeyError, TypeError, ValueError):
                continue
            count = end - start
            if count < 800 or count > 48000:
                continue
            if not pronunciations.get(key):
                continue
            crop_start = max(0, start - 1680)
            crop_end = min(samples_total, end + 1680)
            crop = decoded[crop_start * 2:crop_end * 2]
            label = str(item['word']).lower()
            aligner = Decoder(lm=None, loglevel='ERROR')
            if not aligner.lookup_word(label):
                continue
            try:
                aligner.set_align_text(label)
                aligner.start_utt()
                aligner.process_raw(crop, full_utt=True)
                aligner.end_utt()
                if not aligner.hyp() or not aligner.hyp().hypstr:
                    continue
                aligner.set_alignment()
                aligner.start_utt()
                aligner.process_raw(crop, full_utt=True)
                aligner.end_utt()
                alignment = aligner.get_alignment()
                for word in alignment:
                    if word.name.startswith('<'):
                        continue
                    phones = [(phone.name, crop_start + phone.start * 160,
                               crop_start + (phone.start + phone.duration) * 160)
                              for phone in word if phone.name != 'SIL']
                    if not phones or any(right <= left for _, left, right in phones):
                        continue
                    left, right = phones[0][1], phones[-1][2]
                    source['words'].append({'text': key, 'start': left / 16000,
                                            'end': right / 16000,
                                            'phones': [(name, start / 16000, end / 16000)
                                                       for name, start, end in phones]})
                    add_fragment(word_index, blob, key, decoded[left * 2:right * 2])
                    indexed_words += 1
                    for first in range(len(phones)):
                        for last in range(first + 1, min(first + 8, len(phones)) + 1):
                            unit_phones = phones[first:last]
                            left, right = unit_phones[0][1], unit_phones[-1][2]
                            if right - left < 320 or right - left > 19200:
                                continue
                            unit_key = ' '.join(phone[0] for phone in unit_phones)
                            add_fragment(unit_index, blob, unit_key,
                                         decoded[left * 2:right * 2])
                            indexed_diphones += 1
            except RuntimeError as exc:
                print(f'Skipped alignment for {clip.name}/{key}: {exc}', flush=True)
        print(
            f'Processed {clip.name}: {indexed_words} words and '
            f'{indexed_diphones} phonetic joins',
            flush=True,
        )
        sources.append(source)
    if not word_index or not unit_index:
        raise RuntimeError('No usable word and phonetic audio fragments were indexed')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + '.tmp')
    index = {
        'version': 5,
        'sample_rate': 16000,
        'channels': 1,
        'words': word_index,
        'units': unit_index,
        'sources': sources,
        'source_files': source_files,
    }
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('index.json', json.dumps(index, separators=(',', ':')))
        archive.writestr('fragments.pcm', bytes(blob))
    temporary.replace(output_path)
    print(
        f'Created {output_path}: {len(word_index)} exact words, '
        f'{len(unit_index)} acoustically aligned units, '
        f'{len(blob)} bytes of source audio',
        flush=True,
    )


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Sentence mixer preparation failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
