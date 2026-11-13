import re
from difflib import SequenceMatcher


TAG_CONTROL = re.compile(r'\\([^\\]+)\\')
PAUSE_TOKEN = '\x00pause:'
SILENT_TOKEN = '\x00silent'
SIMULTANEOUS_TOKEN = '\x00simultaneous'
ALL_VARIANTS = '\x00allvariants'
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
            elif item.lower() == 'silent':
                options['silent'] = True
            elif item.lower() == 'simul':
                options['simul'] = True
            elif item.lower() == 'wallowautovariantinc':
                options['allow_auto_variant_inc'] = True
            elif item.lower() == 'wplayallvariants':
                options['play_all_variants'] = True
            elif item.lower().startswith('wplayvariantsupuntil='):
                try:
                    options['play_variants_until'] = max(
                        0, int(item.split('=', 1)[1].strip().strip('[]'))
                    )
                except ValueError:
                    pass
            elif item.lower().startswith('showas='):
                options['show_as'] = item.split('=', 1)[1].strip()
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
    def replace(match):
        word = match.group(0)
        if word in (',', ';', '.', '!', '?'):
            return word
        prefix = normalizer(cleaned[:match.start()]).lower().replace("'", '')
        index = len(re.findall(r'[a-z]+|[,;.!?]', prefix))
        if word.isdigit():
            return word
        options = word_options.get(index, {})
        if options.get('hidden'):
            return ''
        if 'show_as' in options:
            return options['show_as']
        if options.get('ignore_alias_display'):
            return word
        if word.lower() == 'planenoises':
            return 'MMmMMmmMM.....'
        if word.lower() == 'crashnoise':
            return '*CHHkkk!*'
        return word

    text = re.sub(r'[a-z]+|\d+|[,;.!?]', replace, cleaned, flags=re.IGNORECASE)
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
        if options.get('silent'):
            continue
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
                        self.first_words.setdefault(token, (video, word.start, word.end))
                        self.variants.setdefault(token, []).append((video, word.start, word.end))
                        if word.phonems:
                            self.starts.setdefault(token, []).append((video, words, index))
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
                            self.first_words.setdefault(alias_token, (video, word.start, word.end))
                            self.variants.setdefault(alias_token, []).append(
                                (video, word.start, word.end)
                            )
                            if word.phonems:
                                self.starts.setdefault(alias_token, []).append((video, words, index))

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
                    if any(not word.phonems for word in span):
                        continue
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

    def plan(self, tokens, controls=None, skip_recorded=None, pauses=None, silent_tokens=None,
             word_options=None):
        skip_recorded = set(skip_recorded or ())
        pauses = pauses or {}
        silent_tokens = set(silent_tokens or ())
        counters = {}
        pinned = {}
        all_variants = {}
        auto_increment = set()
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
            options = (word_options or {}).get(index, {})
            if options.get('allow_auto_variant_inc'):
                auto_increment.add(token)
            variants = self.variants.get(token, [])
            if (token in counters or token in auto_increment or options.get('play_all_variants')
                    or 'play_variants_until' in options):
                current = counters.get(token, 0)
                if variants:
                    current %= len(variants)
                    pinned[index] = variants[current]
                    if 'play_variants_until' in options:
                        target = options['play_variants_until'] % len(variants)
                        selected = []
                        variant_index = current
                        for _ in range(len(variants)):
                            selected.append(variants[variant_index])
                            if variant_index == target:
                                break
                            variant_index = (variant_index + 1) % len(variants)
                        all_variants[index] = selected
                    elif options.get('play_all_variants'):
                        all_variants[index] = variants[current:] + variants[:current]
                    if token in auto_increment:
                        counters[token] = (current + 1) % len(variants)
                else:
                    pinned[index] = None
        positions = [index for index, token in enumerate(tokens) if token.isalpha()]
        words = [tokens[index] for index in positions]
        simultaneous_groups = {}
        index = 0
        while index < len(tokens):
            if not (word_options or {}).get(index, {}).get('simul') or not tokens[index].isalpha():
                index += 1
                continue
            group = [index]
            next_index = next((position for position in positions if position > index), None)
            if next_index is not None:
                group.append(next_index)
            while next_index is not None and (word_options or {}).get(next_index, {}).get('simul'):
                next_index = next((position for position in positions if position > next_index), None)
                if next_index is None:
                    break
                group.append(next_index)
            simultaneous_groups[index] = group
            index = group[-1] + 1
        whole = self.match(words) if not pinned and not skip_recorded and not pauses and not silent_tokens and not simultaneous_groups else None
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
            if cursor in simultaneous_groups:
                if pending:
                    result.append((pending, None))
                    pending = []
                group = simultaneous_groups[cursor]
                result.append(([tokens[position] for position in group],
                               (SIMULTANEOUS_TOKEN, group)))
                cursor = group[-1] + 1
                continue
            if cursor in silent_tokens:
                if pending:
                    result.append((pending, None))
                    pending = []
                result.append(([SILENT_TOKEN], None))
                cursor += 1
                continue
            if cursor in all_variants:
                if pending:
                    result.append((pending, None))
                    pending = []
                result.append(([tokens[cursor]], (ALL_VARIANTS, all_variants[cursor])))
                cursor += 1
                continue
            if cursor in skip_recorded:
                if pending:
                    result.append((pending, None))
                    pending = []
                result.append(([tokens[cursor]], None))
                cursor += 1
                continue
            found = None
            if tokens[cursor].isalpha():
                barrier = min((index for index in skip_recorded | set(pauses) | silent_tokens
                               if index > cursor), default=len(tokens))
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
