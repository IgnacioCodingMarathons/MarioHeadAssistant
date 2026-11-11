import math


def ensure_complete_search():
    import sentence_mixing.logic.analyze as analyze
    from sentence_mixing.model.choice import Choice
    from sentence_mixing.model.exceptions import PhonemError

    if getattr(analyze.compute_children, 'mario_complete_search', False):
        return
    original = analyze.compute_children

    def complete_children(target_phonem, nodes_left, choice, randomizer):
        try:
            children = original(target_phonem, nodes_left, choice, randomizer)
        except (PhonemError, ZeroDivisionError):
            children = []
        if children:
            return children
        candidates = analyze.audio_data.get_candidates(target_phonem, randomizer)
        if not candidates:
            raise PhonemError(target_phonem)
        ranked = []
        for association in candidates:
            rates = choice.compute_child_step_3_score(association) if choice is not None else (0, 0, 0)
            rates = tuple(float(value) if math.isfinite(value) else 0 for value in rates)
            score = association.get_total_score() + sum(rates)
            ranked.append((score if math.isfinite(score) else float('-inf'), association, rates))
        _, association, rates = max(ranked, key=lambda candidate: candidate[0])
        return [Choice(choice, association, max(2.0, nodes_left), *rates, randomizer)]

    complete_children.mario_complete_search = True
    analyze.compute_children = complete_children


def complete_selected_phonemes(combo, videos, prefer_recorded=True):
    from sentence_mixing.logic.global_audio_data import audioDataFactory
    from sentence_mixing.logic.randomizer import Randomizer
    from sentence_mixing.sentence_mixer import get_global_randomizer_from_videos

    choices = combo.get_choices()
    target = choices[0].association.target_phonem.word.sentence
    associations = {target.get_phonem_index(choice.association.target_phonem): choice.association
                    for choice in choices}
    audio_data = audioDataFactory(videos)
    randomizer = Randomizer(get_global_randomizer_from_videos(videos))
    selected = []
    for word in target.words:
        for phone in word.phonems:
            association = associations.get(target.get_phonem_index(phone))
            if association is None:
                candidates = audio_data.get_candidates(phone, randomizer)
                if not candidates:
                    from sentence_mixing.model.exceptions import PhonemError
                    raise PhonemError(phone)
                association = max(candidates, key=lambda candidate: candidate.get_total_score())
            selected.append(association.audio_phonem)
    return prefer_recorded_words(target, selected, videos) if prefer_recorded else selected


def prefer_recorded_words(target, selected, videos):
    def key(word):
        return word.token, tuple(phone.transcription for phone in word.phonems)

    lookup = {}
    for video in videos:
        for subtitle in video.subtitles:
            for word in subtitle.words:
                if word.token != '<BLANK>':
                    lookup.setdefault(key(word), []).append(word)
    pieces = []
    offset = 0
    for word in target.words:
        size = len(word.phonems)
        pieces.append(selected[offset:offset + size])
        offset += size
    result = []
    index = 0
    while index < len(target.words):
        word = target.words[index]
        best = None
        for candidate in lookup.get(key(word), []):
            cursor = candidate
            chain = []
            count = 0
            gap_total = 0
            matches = 0
            while index + count < len(target.words) and cursor is not None:
                wanted = target.words[index + count]
                if wanted.token == '<BLANK>' or key(cursor) != key(wanted):
                    break
                chain.extend(cursor.phonems)
                matches += sum(first is second for first, second in zip(pieces[index + count], cursor.phonems))
                count += 1
                following = cursor.next_in_seq()
                if following is not None:
                    gap = following.start - cursor.end
                    if gap < -1 / 16000 or gap > 0.08:
                        break
                    gap_total += gap
                cursor = following
            rank = (count, matches, -gap_total)
            if best is None or rank > best[0]:
                best = (rank, chain, count)
        if best is None:
            result.extend(pieces[index])
            index += 1
        else:
            result.extend(best[1])
            index += best[2]
    return result
