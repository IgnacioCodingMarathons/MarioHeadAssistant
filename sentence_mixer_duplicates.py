def remove_duplicate_sources(sources, audio):
    import numpy as np

    retained = []
    duplicates = {}
    spectra = {}
    waves = {}
    window = 1280

    def wave(source):
        name = source['name']
        if name not in waves:
            waves[name] = np.frombuffer(
                audio[source['offset']:source['offset'] + source['length']],
                dtype='<i2',
            ).astype(np.float64)
        return waves[name]

    def match(short, long):
        if len(short) < 3200 or len(short) > len(long):
            return None
        size = 1 << (len(long) + window - 2).bit_length()
        key = id(long)
        if key not in spectra:
            sums = np.r_[0, np.cumsum(long)]
            squares = np.r_[0, np.cumsum(long * long)]
            energy = squares[window:] - squares[:-window]
            energy -= (sums[window:] - sums[:-window]) ** 2 / window
            spectra[key] = (np.fft.rfft(long, size), np.maximum(energy, 1), size)
        spectrum, energy, size = spectra[key]
        starts = np.unique(np.linspace(0, len(short) - window,
                                      min(32, max(3, len(short) // 640))).astype(int))
        checks = list(dict.fromkeys([int(starts[0]), int(starts[len(starts) // 2]),
                                     int(starts[-1]), *map(int, starts)]))
        anchors = []
        for start in checks:
            probe = short[start:start + window]
            probe = probe - probe.mean()
            power = np.dot(probe, probe)
            if power < window * 100:
                continue
            if anchors:
                anchor, original = anchors[0]
                expected = original - window // 2 + start - (anchor - window // 2)
                left = max(0, expected - 1600)
                right = min(len(energy), expected + 1601)
                if right <= left:
                    return None
                correlation = np.correlate(long[left:right + window - 1], probe, 'valid')
                score = correlation / np.sqrt(energy[left:right] * power)
            else:
                left = 0
                correlation = np.fft.irfft(
                    spectrum * np.fft.rfft(probe[::-1], size), size
                )[window - 1:len(long)]
                score = correlation / np.sqrt(energy * power)
            best = int(np.argmax(score))
            position = left + best
            if score[best] < 0.96:
                return None
            anchors.append((start + window // 2, position + window // 2))
        if len(anchors) < 3:
            return None
        anchors.sort()
        for (left, original_left), (right, original_right) in zip(anchors, anchors[1:]):
            if original_right <= original_left or abs(
                (original_right - original_left) - (right - left)
            ) > 1600:
                return None
        return anchors

    def merge_words(source, parent, anchors):
        points = np.array(anchors)

        def mapped(seconds):
            sample = round(seconds * 16000)
            if sample < points[0, 0]:
                result = sample + points[0, 1] - points[0, 0]
            elif sample > points[-1, 0]:
                result = sample + points[-1, 1] - points[-1, 0]
            else:
                result = np.interp(sample, points[:, 0], points[:, 1])
            return max(0, min(parent['length'] // 2, round(result))) / 16000

        for word in source['words']:
            start, end = mapped(word['start']), mapped(word['end'])
            if end <= start or any(existing['text'] == word['text']
                                   and abs(existing['start'] - start) < 0.1
                                   for existing in parent['words']):
                continue
            phones = [(phone, mapped(left), mapped(right))
                      for phone, left, right in word['phones']]
            if phones and all(start <= left < right <= end for _, left, right in phones):
                parent['words'].append(dict(word, start=start, end=end, phones=phones))
        parent['words'].sort(key=lambda word: word['start'])

    for original in sorted(sources, key=lambda source: (-source['length'], source['name'])):
        source = dict(original, words=list(original['words']))
        short = wave(source)
        for parent in retained:
            long = wave(parent)
            if np.array_equal(short, long):
                anchors = [(0, 0), (len(short), len(short))]
            else:
                anchors = match(short, long)
            if anchors is not None:
                duplicates[source['name']] = parent['name']
                merge_words(source, parent, anchors)
                print(f'Duplicate {source["name"]}: already contained in {parent["name"]}', flush=True)
                break
        else:
            retained.append(source)
    output = bytearray()
    for source in sorted(retained, key=lambda source: source['name']):
        start, length = source['offset'], source['length']
        source['offset'] = len(output)
        output.extend(audio[start:start + length])
    return sorted(retained, key=lambda source: source['name']), output, duplicates
