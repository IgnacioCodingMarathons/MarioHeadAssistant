import json
import os
import re
import unicodedata
from pathlib import Path


def guess_pronunciation(word):
    word = re.sub(r'[^a-z]', '', word.lower())
    groups = {
        'tion': ['SH', 'AH', 'N'], 'sion': ['ZH', 'AH', 'N'],
        'tch': ['CH'], 'igh': ['AY'], 'ough': ['OW'],
        'ch': ['CH'], 'sh': ['SH'], 'th': ['TH'], 'ph': ['F'],
        'ng': ['NG'], 'qu': ['K', 'W'], 'ck': ['K'], 'wh': ['W'],
        'ee': ['IY'], 'ea': ['IY'], 'oo': ['UW'], 'ou': ['AW'],
        'ow': ['OW'], 'oi': ['OY'], 'oy': ['OY'], 'ai': ['EY'],
        'ay': ['EY'], 'au': ['AO'], 'aw': ['AO'], 'er': ['ER'],
        'ir': ['ER'], 'ur': ['ER'], 'ar': ['AA', 'R'], 'or': ['AO', 'R'],
    }
    letters = {
        'a': ['AE'], 'e': ['EH'], 'i': ['IH'], 'o': ['AA'], 'u': ['AH'],
        'b': ['B'], 'c': ['K'], 'd': ['D'], 'f': ['F'], 'g': ['G'],
        'h': ['HH'], 'j': ['JH'], 'k': ['K'], 'l': ['L'], 'm': ['M'],
        'n': ['N'], 'p': ['P'], 'q': ['K'], 'r': ['R'], 's': ['S'],
        't': ['T'], 'v': ['V'], 'w': ['W'], 'x': ['K', 'S'],
        'y': ['Y'], 'z': ['Z'],
    }
    phones = []
    position = 0
    while position < len(word):
        if position == len(word) - 1 and word[position] == 'e' and len(word) > 3:
            break
        matched = False
        for length in (4, 3, 2):
            part = word[position:position + length]
            if part in groups:
                phones.extend(groups[part])
                position += length
                matched = True
                break
        if matched:
            continue
        letter = word[position]
        if position == len(word) - 3 and word.endswith('e') and letter in 'aeiou':
            phones.append({'a': 'EY', 'e': 'IY', 'i': 'AY', 'o': 'OW', 'u': 'UW'}[letter])
        elif letter in 'cg' and word[position + 1:position + 2] in ('e', 'i', 'y'):
            phones.append('S' if letter == 'c' else 'JH')
        elif letter == 'y' and position == len(word) - 1:
            phones.append('IY')
        elif position and letter == word[position - 1] and letter not in 'aeiou':
            pass
        else:
            phones.extend(letters.get(letter, []))
        position += 1
    return phones


def load_pronunciations():
    exported_path = os.environ.get('MARIOASSISTANT_PRONUNCIATIONS_PATH')
    if exported_path:
        with Path(exported_path).open(encoding='utf-8') as source:
            return json.load(source)
    import pocketsphinx

    dictionary_path = Path(pocketsphinx.get_model_path()) / 'en-us' / 'cmudict-en-us.dict'
    pronunciations = {}
    with dictionary_path.open(encoding='utf-8', errors='replace') as dictionary_file:
        for line in dictionary_file:
            fields = line.split()
            if len(fields) < 2:
                continue
            word = re.sub(r'\(\d+\)$', '', fields[0].lower())
            phones = [re.sub(r'\d', '', phone) for phone in fields[1:]]
            pronunciations.setdefault(word, []).append(phones)
            alias = re.sub(r'[^a-z]', '', word)
            if alias and alias != word:
                pronunciations.setdefault(alias, []).append(phones)
    return pronunciations


def sentence_mix_spanish(text):
    word_matches = list(re.finditer(r"[^\W\d_]+(?:['’][^\W\d_]+)*", text, re.UNICODE))
    output = text
    for match in reversed(word_matches):
        original = match.group(0)
        word = original.lower()
        phones = []
        index = 0
        while index < len(word):
            phone = None
            pair = word[index:index + 2]
            if pair == 'ch':
                phones.append('CH')
                index += 2
            elif pair == 'rr':
                phones.append('R')
                index += 2
            elif pair == 'll':
                phones.append('Y')
                index += 2
            elif pair == 'qu':
                phones.append('K')
                index += 2
            elif pair == 'gu' and index + 2 < len(word) and word[index + 2] in 'eiéí':
                phones.append('G')
                index += 2
            elif word[index] == 'h':
                index += 1
            elif word[index] == 'ñ':
                phones.append('N')
                index += 1
            elif word[index] in 'aeiouáéíóú':
                phones.append({'a': 'AA', 'á': 'AA', 'e': 'EH', 'é': 'EH',
                               'i': 'IY', 'í': 'IY', 'o': 'OW', 'ó': 'OW',
                               'u': 'UW', 'ú': 'UW'}[word[index]])
                index += 1
            else:
                char = word[index]
                next_char = word[index + 1:index + 2]
                phone = {
                    'b': 'B', 'v': 'B', 'c': 'S' if next_char in 'eiéí' else 'K',
                    'k': 'K', 'q': 'K', 'd': 'D', 'f': 'F',
                    'g': 'HH' if next_char in 'eiéí' else 'G',
                    'j': 'HH', 'l': 'L', 'm': 'M', 'n': 'N', 'p': 'P',
                    'r': 'R', 's': 'S', 't': 'T', 'w': 'W', 'x': 'K',
                    'y': 'Y', 'z': 'S',
                }.get(char)
                index += 1
            if phone:
                phones.append(phone)
        if not phones:
            raise RuntimeError(f'Could not convert Spanish pronunciation for {original}')
        ascii_word = unicodedata.normalize('NFKD', original)
        ascii_word = ''.join(char for char in ascii_word if not unicodedata.combining(char))
        ascii_word = re.sub(r'[^A-Za-z]', '', ascii_word)
        options = ','.join(
            f'{phone},l={0.105 if phone in {"AA", "EH", "IY", "OW", "UW"} else 0.065}'
            for phone in phones
        )
        replacement = f'\\custompronunphoneme={options}|showas={original}\\{ascii_word}'
        output = output[:match.start()] + replacement + output[match.end():]
    return output


def is_spanish_text(text):
    if re.search(r'[áéíóúüñ¿¡]', text, re.IGNORECASE):
        return True
    words = set(re.findall(r'[a-z]+', text.casefold()))
    markers = {
        'el', 'ella', 'ellos', 'ellas', 'nosotros', 'ustedes', 'que', 'qué',
        'como', 'cómo', 'para', 'pero', 'porque', 'por', 'una', 'uno', 'unos',
        'unas', 'con', 'del', 'los', 'las', 'soy', 'eres', 'está', 'esta',
        'están', 'tengo', 'tiene', 'hola', 'buenos', 'buenas', 'gracias',
        'noticias', 'comodoro', 'rivadavia',
    }
    return len(words & markers) >= 2
