import re
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
