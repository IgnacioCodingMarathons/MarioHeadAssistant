import array
import sys


def remove_silence(pcm, sample_rate=16000):
    samples = array.array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    window = max(1, round(sample_rate * 0.005))
    minimum = round(sample_rate * 0.020)
    threshold = (32768 * 10 ** (-50 / 20)) ** 2
    cuts = []
    quiet_start = None
    for start in range(0, len(samples), window):
        block = samples[start:start + window]
        quiet = sum(value * value for value in block) <= threshold * len(block)
        if quiet and quiet_start is None:
            quiet_start = start
        if not quiet and quiet_start is not None:
            if start - quiet_start >= minimum:
                cuts.append((quiet_start, start))
            quiet_start = None
    if quiet_start is not None and len(samples) - quiet_start >= minimum:
        cuts.append((quiet_start, len(samples)))
    output = bytearray()
    time_map = []
    previous = 0
    for start, end in cuts + [(len(samples), len(samples))]:
        if start > previous:
            time_map.append((len(output) // 2, previous, start - previous))
            output.extend(pcm[previous * 2:start * 2])
        previous = end
    return bytes(output), time_map


def original_video_time(time_map, seconds, sample_rate=16000):
    sample = max(0, round(seconds * sample_rate))
    for compact, original, length in time_map:
        if sample < compact + length:
            return (original + max(0, sample - compact)) / sample_rate
    if time_map:
        _, original, length = time_map[-1]
        return (original + length) / sample_rate
    return seconds


def remap_words(words, time_map, sample_rate=16000):
    def remap_interval(start, end):
        left = round(start * sample_rate)
        right = round(end * sample_rate)
        mapped_start = None
        mapped_end = None
        for compact, original, length in time_map:
            overlap_start = max(left, original)
            overlap_end = min(right, original + length)
            if overlap_end > overlap_start:
                if mapped_start is None:
                    mapped_start = compact + overlap_start - original
                mapped_end = compact + overlap_end - original
        if mapped_start is None:
            return None
        return mapped_start / sample_rate, mapped_end / sample_rate

    remapped = []
    for word in words:
        phones = []
        for phone, start, end in word['phones']:
            interval = remap_interval(start, end)
            if interval is not None:
                phones.append((phone, *interval))
        if phones:
            remapped.append(dict(word, start=phones[0][1], end=phones[-1][2], phones=phones))
    return remapped
