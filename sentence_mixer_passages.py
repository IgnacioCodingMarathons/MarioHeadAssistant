import re
from difflib import SequenceMatcher


TAG_CONTROL = re.compile(r'\\([^\\]+)\\')
PAUSE_TOKEN = '\x00pause:'
PHONE_NAMES = {
    'AA': 'ah', 'AE': 'a', 'AH': 'uh', 'AO': 'aw', 'AW': 'ow', 'AY': 'eye',
    'EH': 'eh', 'ER': 'er', 'EY': 'ay', 'IH': 'ih', 'IY': 'ee', 'OW': 'oh',
    'OY': 'oy', 'UH': 'uh', 'UW': 'oo', 'B': 'bee', 'CH': 'ch', 'D': 'dee',
    'DH': 'the', 'F': 'eff', 'G': 'gee', 'HH': 'h', 'JH': 'jay', 'K': 'kay',
    'L': 'el', 'M': 'em', 'N': 'en', 'NG': 'ing', 'P': 'pee', 'R': 'ar',
    'S': 'ess', 'SH': 'shh', 'T': 'tee', 'TH': 'th', 'V': 'vee', 'W': 'wuh',
    'Y': 'yuh', 'Z': 'zee', 'ZH': 'zh',
}


def tokenize_variant_text(text, normalizer):
    def tokenize(value):
        return re.findall(r'[a-z]+|[,;.!?]', normalizer(value).lower().replace("'", ''))

    pieces = []
    markers = []
    pause_markers = []
    cursor = 0
    length = 0
    for match in TAG_CONTROL.finditer(str(text)):
        part = str(text)[cursor:match.start()]
        pieces.append(part)
        length += len(part)
        options = {}
        variant = None
        pause = None
        for item in match.group(1).split('|'):
            item = item.strip()
            variant_match = re.fullmatch(r'wvariant(?:([+\-=])([0-9]+)|(R))', item, re.IGNORECASE)
            if variant_match:
                variant = (variant_match.group(1) or 'R', variant_match.group(2))
            elif item.lower() == 'ignorerecdpronun':
                options['ignore_recorded'] = True
            elif item.lower() == 'ignoremarioaliasdisplay':
                options['ignore_alias_display'] = True
            elif item.lower() == 'hidden':
                options['hidden'] = True
            elif item.lower().startswith('custompronunword='):
                value = item.split('=', 1)[1].strip().strip('[]').lower()
                spoken = re.findall(r'[a-z]+', value)
                if len(spoken) == 1:
                    options['custom_word'] = spoken[0]
            elif item.lower().startswith('custompronunphoneme='):
                value = item.split('=', 1)[1].strip().strip('[]')
                custom = []
                pending_length = 0.25
                for field in value.split(','):
                    field = field.strip()
                    length_match = re.fullmatch(r'l\s*=\s*([0-9]+(?:\.[0-9]+)?)', field, re.IGNORECASE)
                    if length_match and custom:
                        try:
                            custom[-1] = (custom[-1][0], max(0.001, float(length_match.group(1))))
                        except ValueError:
                            pass
                    elif re.fullmatch(r'[A-Za-z]+', field):
                        custom.append((field.upper(), pending_length))
                        pending_length = 0.25
                if custom:
                    options['custom_phones'] = custom
            elif item.lower().startswith('pause='):
                try:
                    pause = max(0, int(item.split('=', 1)[1].strip().strip('[]')))
                except ValueError:
                    pass
        if variant is not None:
            markers.append((length, variant))
        if options:
            markers.append((length, options))
        if pause is not None:
            if pieces and pieces[-1] and not pieces[-1][-1].isspace():
                pieces.append(' ')
                length += 1
            pause_markers.append((length, pause))
        cursor = match.end()
    pieces.append(str(text)[cursor:])
    cleaned = ''.join(pieces)
    tokens = tokenize(cleaned)
    controls = {}
    word_options = {}
    pauses = {}
    for position, value in markers:
        index = len(tokenize(cleaned[:position]))
        if isinstance(value, tuple):
            controls[index] = value
        else:
            word_options.setdefault(index, {}).update(value)
    for position, duration in pause_markers:
        index = len(tokenize(cleaned[:position]))
        pauses[index] = pauses.get(index, 0) + duration
    return cleaned, tokens, controls, word_options, pauses


def format_subtitle_text(cleaned, word_options, normalizer):
    token_index = [0]

    def replace(match):
        index = token_index[0]
        token_index[0] += 1
        word = match.group(0)
        if word in (',', ';', '.', '!', '?'):
            return word
        options = word_options.get(index, {})
        if options.get('hidden'):
            return ''
        if options.get('ignore_alias_display'):
            return word
        if word.lower() == 'planenoises':
            return 'MMmMMmmMM.....'
        if word.lower() == 'crashnoise':
            return '*CHHkkk!*'
        return word

    text = re.sub(r'[a-z]+|[,;.!?]', replace, cleaned, flags=re.IGNORECASE)
    text = re.sub(r'\s+([,;.!?])', r'\1', re.sub(r'\s+', ' ', text)).strip()
    return text


def compatibility_tts_text(text, normalizer):
    cleaned, tokens, _, word_options, pauses = tokenize_variant_text(text, normalizer)
    output = []
    for index in range(len(tokens) + 1):
        if index in pauses:
            duration = pauses[index]
            punctuation = ',' if duration < 300 else ';' if duration < 900 else '.'
            if output:
                output[-1] = output[-1].rstrip('.,;!?') + punctuation
        if index >= len(tokens):
            continue
        token = tokens[index]
        options = word_options.get(index, {})
        if token.isalpha():
            if options.get('custom_phones'):
                token = ' '.join(PHONE_NAMES.get(phone, phone.lower())
                                 for phone, _ in options['custom_phones'])
            elif options.get('custom_word'):
                token = options['custom_word']
        output.append(token)
    sentence = ''
    for token in output:
        if token in (',', ';', '.', '!', '?'):
            sentence = sentence.rstrip() + token
        elif sentence:
            sentence += ' ' + token
        else:
            sentence = token
    return sentence


class RecordedPassages:
    def __init__(self, videos, pronunciations):
        self.pronunciations = pronunciations
        self.starts = {}
        self.first_words = {}
        self.variants = {}
        self.recorded_phrases = {}
        self.cache = {}
        self.maximum_words = 0
        for video in videos:
            for subtitle in video.subtitles:
                words = [word for word in subtitle.words if word.token != '<BLANK>']
                self.maximum_words = max(self.maximum_words, len(words))
                for index, word in enumerate(words):
                    token = re.sub('[^a-z]', '', word.original_word.lower())
                    phrase = tuple(re.findall('[a-z]+', word.original_word.lower()))
                    if len(phrase) > 1:
                        self.maximum_words = max(self.maximum_words, len(phrase))
                        self.recorded_phrases.setdefault(phrase, []).append(
                            (video, word.start, word.end)
                        )
                    else:
                        self.starts.setdefault(token, []).append((video, words, index))
                        self.first_words.setdefault(token, (video, word.start, word.end))
                        self.variants.setdefault(token, []).append((video, word.start, word.end))
                    for alias in getattr(word, 'mario_aliases', []):
                        alias_words = tuple(re.findall('[a-z]+', alias.lower()))
                        if not alias_words:
                            continue
                        self.maximum_words = max(self.maximum_words, len(alias_words))
                        if len(alias_words) > 1:
                            self.recorded_phrases.setdefault(alias_words, []).append(
                                (video, word.start, word.end)
                            )
                        else:
                            alias_token = alias_words[0]
                            self.starts.setdefault(alias_token, []).append((video, words, index))
                            self.first_words.setdefault(alias_token, (video, word.start, word.end))
                            self.variants.setdefault(alias_token, []).append(
                                (video, word.start, word.end)
                            )

    def phones(self, words):
        result = []
        for word in words:
            entries = self.pronunciations.get(word)
            if not entries:
                return []
            result.extend(entries[0])
        return result

    def match(self, requested):
        requested = tuple(requested)
        if requested in self.cache:
            return self.cache[requested]
        phrases = self.recorded_phrases.get(requested)
        if phrases:
            result = phrases[0]
            self.cache[requested] = result
            return result
        best = None
        if len(requested) >= 2:
            for video, words, first in self.starts.get(requested[0], []):
                for last in range(first + 1, min(len(words), first + len(requested) + 2)):
                    if re.sub('[^a-z]', '', words[last].original_word.lower()) != requested[-1]:
                        continue
                    span = words[first:last + 1]
                    if any(left.end > right.start + 1 / 16000 for left, right in zip(span, span[1:])):
                        continue
                    actual = tuple(re.sub('[^a-z]', '', word.original_word.lower()) for word in span)
                    exact = actual == requested
                    matcher = SequenceMatcher(None, actual, requested, autojunk=False)
                    matched = sum(block.size for block in matcher.get_matching_blocks())
                    coverage = matched / len(requested)
                    if not exact:
                        if coverage < 0.65 or words[last].end - words[first].start > len(requested) * 0.8:
                            continue
                        valid = True
                        for operation, left, right, wanted_left, wanted_right in matcher.get_opcodes():
                            if operation == 'equal':
                                continue
                            if operation == 'insert':
                                if left == 0 or left == len(span):
                                    valid = False
                                    break
                                gap = span[left].start - span[left - 1].end
                                if gap < max(0.03, (wanted_right - wanted_left) * 0.06):
                                    valid = False
                                    break
                            elif operation == 'replace' and wanted_right - wanted_left > right - left:
                                source_phones = self.phones(actual[left:right])
                                wanted_phones = self.phones(requested[wanted_left:wanted_right])
                                if not source_phones or not wanted_phones or source_phones[:2] != wanted_phones[:2]:
                                    valid = False
                                    break
                                if SequenceMatcher(None, source_phones, wanted_phones, autojunk=False).ratio() < 0.4:
                                    valid = False
                                    break
                            else:
                                valid = False
                                break
                        if not valid:
                            continue
                        source_phones = [phone.transcription for word in span for phone in word.phonems]
                        wanted_phones = self.phones(requested)
                        if not wanted_phones or SequenceMatcher(None, source_phones, wanted_phones, autojunk=False).ratio() < 0.7:
                            continue
                    rank = (exact, coverage, -abs(len(actual) - len(requested)), -(span[-1].end - span[0].start))
                    if best is None or rank > best[0]:
                        best = (rank, (video, span[0].start, span[-1].end))
        result = best[1] if best is not None else None
        if len(self.cache) >= 4096:
            self.cache.pop(next(iter(self.cache)))
        self.cache[requested] = result
        return result

    def plan(self, tokens, controls=None, skip_recorded=None, pauses=None):
        skip_recorded = set(skip_recorded or ())
        pauses = pauses or {}
        counters = {}
        pinned = {}
        for index, token in enumerate(tokens):
            if controls and index in controls:
                operation, amount = controls[index]
                current = counters.get(token, 0)
                digits = (amount or '0').lstrip('0')
                value = int(digits or '0') if len(digits) <= 18 else None
                if operation == 'R' or value is None:
                    current = 0
                elif operation == '+':
                    current += value
                elif operation == '-':
                    current -= value
                else:
                    current = value
                variants = self.variants.get(token, [])
                counters[token] = current if 0 <= current < len(variants) else 0
            if token in counters:
                variants = self.variants.get(token, [])
                pinned[index] = variants[counters[token]] if variants else None
        positions = [index for index, token in enumerate(tokens) if token.isalpha()]
        words = [tokens[index] for index in positions]
        whole = self.match(words) if not pinned and not skip_recorded and not pauses else None
        if whole is not None:
            return [(tokens, whole)]
        result = []
        cursor = 0
        pending = []
        emitted_pauses = set()
        while cursor < len(tokens):
            if cursor in pauses and cursor not in emitted_pauses:
                if pending:
                    result.append((pending, None))
                    pending = []
                result.append(([f'{PAUSE_TOKEN}{pauses[cursor]}'], None))
                emitted_pauses.add(cursor)
            if cursor in skip_recorded:
                if pending:
                    result.append((pending, None))
                    pending = []
                result.append(([tokens[cursor]], None))
                cursor += 1
                continue
            found = None
            if tokens[cursor].isalpha():
                barrier = min((index for index in skip_recorded | set(pauses) if index > cursor), default=len(tokens))
                remaining = [index for index in positions if cursor <= index < barrier] if cursor not in pinned else []
                for count in range(min(len(remaining), self.maximum_words + 2), 1, -1):
                    end = remaining[count - 1] + 1
                    if any(cursor <= index < end for index in pinned):
                        continue
                    part = tokens[cursor:end]
                    passage = self.match([token for token in part if token.isalpha()])
                    if passage is not None:
                        found = (end, part, passage)
                        break
                if found is None:
                    recorded_word = pinned[cursor] if cursor in pinned else self.first_words.get(tokens[cursor])
                    if recorded_word is not None:
                        found = (cursor + 1, [tokens[cursor]], recorded_word)
            if found is None:
                pending.append(tokens[cursor])
                cursor += 1
            else:
                if pending:
                    result.append((pending, None))
                    pending = []
                end, part, passage = found
                result.append((part, passage))
                cursor = end
        if pending:
            result.append((pending, None))
        if len(tokens) in pauses and len(tokens) not in emitted_pauses:
            result.append(([f'{PAUSE_TOKEN}{pauses[len(tokens)]}'], None))
        return result
