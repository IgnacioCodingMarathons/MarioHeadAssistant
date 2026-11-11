import struct
from pathlib import Path


MAGIC = b'MARIOSMX'
VERSION = 13
MODEL_NAME = 'mario_sentence_mixer.bin'
VOWELS = frozenset('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())
HEADER = struct.Struct('<8sIIHHIIQ')


def write_model(path, index, audio):
    sample_rate = index['sample_rate']
    sources = index['sources']
    fingerprints = index['source_files']
    metadata = bytearray()

    def pack(layout, *values):
        metadata.extend(struct.pack('<' + layout, *values))

    def text(value):
        encoded = value.encode('utf-8')
        pack('I', len(encoded))
        metadata.extend(encoded)

    duplicates = index.get('duplicates', {})
    pack('I', len(duplicates))
    for name, parent in sorted(duplicates.items()):
        text(name)
        text(parent)
    for name, (size, modified) in sorted(fingerprints.items()):
        text(name)
        pack('QQ', size, modified)
    for source in sources:
        text(source['name'])
        pack('BQQI', bool(source.get('audio_only')), source['offset'],
             source['length'], len(source['words']))
        mapping = source['time_map']
        pack('I', len(mapping))
        for compact, original, length in mapping:
            pack('QQQ', compact, original, length)
        for word in source['words']:
            text(word['text'])
            aliases = word.get('aliases', [])
            pack('I', len(aliases))
            for alias in aliases:
                text(alias)
            pack('QQI', round(word['start'] * sample_rate),
                 round(word['end'] * sample_rate), len(word['phones']))
            for phone, start, end in word['phones']:
                text(phone)
                pack('BQQ', phone in VOWELS, round(start * sample_rate),
                     round(end * sample_rate))
    header = HEADER.pack(MAGIC, VERSION, sample_rate, 1, 2,
                         len(sources), len(fingerprints), len(metadata))
    import zstandard

    payload = bytes(metadata) + audio[0::2] + audio[1::2]
    compressed = zstandard.ZstdCompressor(
        level=zstandard.MAX_COMPRESSION_LEVEL, write_checksum=True
    ).compress(payload)
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with temporary.open('wb') as output:
            output.write(header)
            output.write(compressed)
        read_model(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_model(path):
    with Path(path).open('rb') as source:
        raw_header = source.read(HEADER.size)
        if len(raw_header) != HEADER.size:
            raise ValueError('Truncated sentence-mixing header')
        magic, version, rate, channels, width, count, files, size = HEADER.unpack(raw_header)
        if magic != MAGIC or version not in (6, 7, 8, 9, 10, 11, 12, VERSION):
            raise ValueError('Unsupported sentence-mixing binary format')
        if rate != 16000 or channels != 1 or width != 2:
            raise ValueError('Unsupported sentence-mixing audio format')
        payload = source.read()
        if version >= 10:
            import zstandard

            try:
                payload = zstandard.ZstdDecompressor().decompress(payload, allow_extra_data=False)
            except zstandard.ZstdError as exc:
                raise ValueError(f'Invalid compressed sentence-mixing data: {exc}') from exc
        if size > len(payload):
            raise ValueError('Truncated sentence-mixing metadata')
        metadata = memoryview(payload)[:size]
        audio = payload[size:]
        if version >= 10:
            if len(audio) % 2:
                raise ValueError('Invalid sentence-mixing audio byte count')
            count_samples = len(audio) // 2
            restored = bytearray(len(audio))
            restored[0::2] = audio[:count_samples]
            restored[1::2] = audio[count_samples:]
            audio = bytes(restored)
    cursor = 0

    def unpack(layout):
        nonlocal cursor
        record = struct.Struct('<' + layout)
        if cursor + record.size > len(metadata):
            raise ValueError('Truncated sentence-mixing record')
        values = record.unpack_from(metadata, cursor)
        cursor += record.size
        return values

    def text():
        nonlocal cursor
        length, = unpack('I')
        if cursor + length > len(metadata):
            raise ValueError('Truncated sentence-mixing text')
        value = bytes(metadata[cursor:cursor + length]).decode('utf-8')
        cursor += length
        return value

    duplicates = {}
    if version >= 8:
        duplicate_count, = unpack('I')
        for _ in range(duplicate_count):
            name = text()
            duplicates[name] = text()
    fingerprints = {}
    for _ in range(files):
        name = text()
        fingerprints[name] = list(unpack('QQ'))
    sources = []
    expected_offset = 0
    for _ in range(count):
        name = text()
        audio_only, offset, length, word_count = unpack('BQQI')
        if offset != expected_offset or length % 2 or offset + length > len(audio):
            raise ValueError('Invalid sentence-mixing source audio bounds')
        expected_offset += length
        samples = length // 2
        time_map = []
        if version >= 7:
            map_count, = unpack('I')
            compact_end = 0
            original_end = 0
            for _ in range(map_count):
                compact, original, kept = unpack('QQQ')
                if compact != compact_end or original < original_end or not kept:
                    raise ValueError('Invalid sentence-mixing video time map')
                compact_end = compact + kept
                original_end = original + kept
                time_map.append((compact, original, kept))
            if compact_end != samples:
                raise ValueError('Incomplete sentence-mixing video time map')
        elif samples:
            time_map = [(0, 0, samples)]
        words = []
        for _ in range(word_count):
            word_text = text()
            aliases = []
            if version >= 13:
                alias_count, = unpack('I')
                aliases = [text() for _ in range(alias_count)]
            start, end, phone_count = unpack('QQI')
            if not 0 <= start < end <= samples:
                raise ValueError('Invalid sentence-mixing word timing')
            phones = []
            for _ in range(phone_count):
                phone = text()
                vowel, left, right = unpack('BQQ')
                if not start <= left < right <= end or vowel != (phone in VOWELS):
                    raise ValueError('Invalid sentence-mixing phoneme record')
                phones.append((phone, left / rate, right / rate))
            words.append({'text': word_text, 'aliases': aliases, 'start': start / rate,
                          'end': end / rate, 'phones': phones})
        sources.append({'name': name, 'offset': offset, 'length': length,
                        'audio_only': bool(audio_only), 'words': words,
                        'time_map': time_map})
    padding = metadata[cursor:]
    if version == 9:
        if len(padding) > 7 or any(padding) or (HEADER.size + len(metadata)) % 8:
            raise ValueError('Invalid sentence-mixing alignment padding')
    elif padding:
        raise ValueError('Unexpected trailing sentence-mixing metadata')
    if expected_offset != len(audio):
        raise ValueError('Unexpected trailing sentence-mixing data')
    names = {source['name'] for source in sources}
    if any(name not in fingerprints or name in names or parent not in names
           for name, parent in duplicates.items()):
        raise ValueError('Invalid sentence-mixing duplicate reference')
    return {'version': version, 'sample_rate': rate, 'channels': channels,
            'sources': sources, 'source_files': fingerprints,
            'duplicates': duplicates}, audio
