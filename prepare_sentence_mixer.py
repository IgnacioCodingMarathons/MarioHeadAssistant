import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from phoneme_dict import load_pronunciations
from sentence_mixer_sources import discover_clips, source_fingerprint, AUDIO_EXTENSIONS
from sentence_mixer_binary import MODEL_NAME, VERSION, read_model, write_model
from sentence_mixer_silence import remove_silence, remap_words
from sentence_mixer_duplicates import remove_duplicate_sources


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


def reuse_source(cached, old_blob, blob):
    decoded = old_blob[cached['offset']:cached['offset'] + cached['length']]
    source = dict(cached, offset=len(blob))
    blob.extend(decoded)
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


def align_recognized_words(decoded, recognized, pronunciations):
    from pocketsphinx import Decoder

    samples_total = len(decoded) // 2
    probe = Decoder(lm=None, loglevel='ERROR')
    items = []
    for item in recognized:
        label = str(item.get('word', '')).lower()
        key = re.sub(r'[^a-z]', '', label)
        if key in pronunciations and probe.lookup_word(label):
            items.append(dict(item, label=label, key=key))
    if not items:
        return []

    def align(decoder, text, audio, origin=0, bounds=None):
        decoder.set_align_text(text)
        decoder.start_utt()
        decoder.process_raw(audio, full_utt=True)
        decoder.end_utt()
        if not decoder.hyp() or not decoder.hyp().hypstr:
            raise RuntimeError('No forced-alignment hypothesis')
        decoder.set_alignment()
        decoder.start_utt()
        decoder.process_raw(audio, full_utt=True)
        decoder.end_utt()
        alignment = decoder.get_alignment()
        records = []
        for word in alignment:
            if word.name.startswith('<'):
                continue
            phones = []
            for phone in word:
                if phone.name == 'SIL':
                    continue
                left = max(0, origin + phone.start * 160)
                right = min(samples_total, origin + (phone.start + phone.duration) * 160)
                if bounds is not None:
                    left, right = max(left, bounds[0]), min(right, bounds[1])
                if right > left:
                    phones.append((phone.name, left / 16000, right / 16000))
            records.append(phones)
        return records

    try:
        records = align(probe, ' '.join(item['label'] for item in items), decoded)
        if len(records) != len(items) or not any(records):
            raise RuntimeError('Forced alignment did not cover the transcript')
    except RuntimeError as exc:
        print(f'Joint alignment failed; using bounded word alignment: {exc}', flush=True)
        records = []
        for index, item in enumerate(items):
            left = max(0, round(float(item['start']) * 16000))
            right = min(samples_total, round(float(item['end']) * 16000))
            if index:
                boundary = round((float(items[index - 1]['end']) + float(item['start'])) * 8000)
                left = max(left - 160, boundary)
            if index + 1 < len(items):
                boundary = round((float(item['end']) + float(items[index + 1]['start'])) * 8000)
                right = min(right + 160, boundary)
            try:
                if right <= left:
                    records.append([])
                    continue
                result = align(Decoder(lm=None, loglevel='ERROR'), item['label'],
                               decoded[left * 2:right * 2], left, (left, right))
                records.append(result[0] if result else [])
            except RuntimeError:
                records.append([])
    words = []
    for item, phones in zip(items, records):
        if phones and float(item.get('conf', 1)) >= 0.5:
            words.append({'text': item['key'], 'start': phones[0][1],
                          'end': phones[-1][2], 'phones': phones})
    return words


def process_clip(clip, assets, ffmpeg, model, pronunciations):
    import vosk
    try:
        is_audio_only = audio_only_source(clip, ffmpeg)
        decoded = subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-threads', '1', '-i', str(clip),
             '-threads', '1', '-filter_threads', '1', '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'],
            capture_output=True,
            timeout=90,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'Skipped {clip.name}: audio extraction failed: {exc}', flush=True)
        return None
    if len(decoded) < 3200:
        print(f'Skipped {clip.name}: no usable audio track', flush=True)
        return None
    recognizer = vosk.KaldiRecognizer(model, 16000)
    recognizer.SetWords(True)
    recognized = []
    for offset in range(0, len(decoded), 64000):
        if recognizer.AcceptWaveform(decoded[offset:offset + 64000]):
            recognized.extend(parse_result(recognizer.Result()))
    recognized.extend(parse_result(recognizer.FinalResult()))
    source = {'name': clip.relative_to(assets).as_posix(), 'offset': 0,
              'length': len(decoded), 'words': align_recognized_words(decoded, recognized, pronunciations),
              'audio_only': is_audio_only}
    original_length = len(decoded)
    decoded, time_map = remove_silence(decoded)
    if len(decoded) < 3200:
        print(f'Skipped {clip.name}: no usable audio after silence removal', flush=True)
        return None
    source['length'] = len(decoded)
    source['time_map'] = time_map
    source['words'] = remap_words(source['words'], time_map)
    print(
        f'Processed {clip.name}: recognized full audio, removed '
        f'{(original_length - len(decoded)) / 32000:.3f}s of silence; '
        f'{len(source["words"])} words and '
        f'{sum(len(word["phones"]) for word in source["words"])} remapped phonemes',
        flush=True,
    )
    return source, decoded


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
    output_path = options.output.resolve() if options.output else assets / MODEL_NAME
    clips = discover_clips(assets)
    if not clips:
        parser.error(f'No audio or video clips found in {assets} or its sentencemixvids folder')
    cached_index = {}
    old_blob = b''
    if output_path.is_file() and not options.rebuild:
        try:
            cached_index, old_blob = read_model(output_path)
        except (OSError, ValueError) as exc:
            print(f'Ignoring invalid sentence-mixing cache: {exc}', flush=True)
    legacy_path = assets / 'mario_sentence_mixer.zip'
    if not cached_index and legacy_path.is_file() and not options.rebuild:
        with zipfile.ZipFile(legacy_path) as archive:
            cached_index = json.loads(archive.read('index.json'))
            if cached_index.get('version') == 5:
                old_blob = archive.read('fragments.pcm')
            else:
                cached_index = {}
    if cached_index.get('version') == VERSION and cached_index.get('source_files') == source_files:
        print('Sentence-mixing clips are already up to date; nothing to process.', flush=True)
        return
    sources = []
    blob = bytearray()
    cached_sources = {source['name']: source for source in cached_index.get('sources', [])}
    old_files = cached_index.get('source_files', {})
    pending = []
    for clip in clips:
        name = clip.relative_to(assets).as_posix()
        unchanged = old_files.get(name) == source_files[name]
        parent = cached_index.get('duplicates', {}).get(name)
        parent_unchanged = parent in source_files and old_files.get(parent) == source_files[parent]
        if cached_index.get('version') == VERSION and name in cached_sources and unchanged:
            sources.append(reuse_source(cached_sources[name], old_blob, blob))
            print(f'Reused {name}', flush=True)
        elif cached_index.get('version') == VERSION and unchanged and (parent is None or parent_unchanged):
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
        except ImportError as exc:
            parser.error(f'vosk and pocketsphinx are required to prepare clips: {exc}')
        vosk.SetLogLevel(-1)
        model = vosk.Model(str(model_path))
        pronunciations = load_pronunciations()
    if pending:
        available = getattr(os, 'process_cpu_count', os.cpu_count)() or 1
        if hasattr(os, 'sched_getaffinity'):
            available = len(os.sched_getaffinity(0)) or available
        workers = min(available, len(pending))
        print(f'Preparing {len(pending)} clips with {workers} workers ({available} available CPU threads).', flush=True)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_clip, clip, assets, ffmpeg, model, pronunciations): clip
                for clip in pending
            }
            for future in as_completed(futures):
                clip = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    raise RuntimeError(f'Failed to process {clip.name}: {type(exc).__name__}: {exc}') from exc
                if result is not None:
                    source, decoded = result
                    source['offset'] = len(blob)
                    blob.extend(decoded)
                    sources.append(source)
    sources, blob, duplicates = remove_duplicate_sources(sources, blob)
    retained_names = {source['name'] for source in sources}
    previous_duplicates = cached_index.get('duplicates', {}) if cached_index.get('version') == VERSION else {}
    for name, parent in previous_duplicates.items():
        if name not in retained_names and name in source_files and old_files.get(name) == source_files[name] and parent in source_files and old_files.get(parent) == source_files[parent]:
            duplicates[name] = duplicates.get(parent, parent)
    words = [word for source in sources for word in source['words']]
    if not words or not any(word['phones'] for word in words):
        raise RuntimeError('No usable word and phonetic audio fragments were indexed')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    index = {
        'version': VERSION,
        'sample_rate': 16000,
        'channels': 1,
        'sources': sources,
        'source_files': source_files,
        'duplicates': duplicates,
    }
    write_model(output_path, index, blob)
    print(
        f'Created {output_path}: {len(words)} words, '
        f'{sum(len(word["phones"]) for word in words)} aligned phonemes, '
        f'{len(blob)} bytes of source audio',
        flush=True,
    )


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Sentence mixer preparation failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
