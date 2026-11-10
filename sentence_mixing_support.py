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


def complete_selected_phonemes(combo, videos):
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
    return selected
