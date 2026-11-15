import random
from functools import lru_cache

from board_game_window import create_board_game_window
from game_difficulty import try_random_move


BAR = 24
OFF = 25


def roll_dice():
    first, second = random.randint(1, 6), random.randint(1, 6)
    return (first,) * 4 if first == second else tuple(sorted((first, second)))


def initial():
    points = [0] * 24
    for point, count in ((23, 2), (12, 5), (7, 3), (5, 5)):
        points[point] = count
        points[23 - point] = -count
    return tuple(points), (0, 0), (0, 0), roll_dice()


def steps(points, bar, off, player, die):
    side = 1 if player == 0 else -1
    own = [index for index, count in enumerate(points) if count * side > 0]
    distance = lambda point: point + 1 if player == 0 else 24 - point
    home = not bar[player] and all(distance(point) <= 6 for point in own)
    for origin in (BAR,) if bar[player] else own:
        target = (24 if player == 0 else -1) - side * die if origin == BAR else origin - side * die
        if not 0 <= target < 24:
            if not home or (die != distance(origin) and any(distance(point) > distance(origin) for point in own)):
                continue
            target = OFF
        if target != OFF and points[target] * side <= -2:
            continue
        updated, bars, outs = list(points), list(bar), list(off)
        if origin == BAR:
            bars[player] -= 1
        else:
            updated[origin] -= side
        if target == OFF:
            outs[player] += 1
        else:
            if updated[target] * side == -1:
                bars[1 - player] += 1
                updated[target] = 0
            updated[target] += side
        yield (origin, target, die), (tuple(updated), tuple(bars), tuple(outs))


@lru_cache(maxsize=64)
def turns(state, player):
    points, bar, off, dice = state

    def explore(position, remaining, path):
        found = False
        for die in set(remaining):
            tail = list(remaining)
            tail.remove(die)
            for step, after in steps(*position, player, die):
                found = True
                explore(after, tail, path + (step,))
        if not found:
            outcomes.append((path, position))

    outcomes = []
    explore((points, bar, off), dice, ())
    longest = max(len(path) for path, _ in outcomes)
    higher = max((path[0][2] for path, _ in outcomes if len(path) == 1), default=0)
    unique = {}
    for path, position in outcomes:
        if len(path) == longest and (longest != 1 or path[0][2] == higher):
            unique.setdefault(position, path)
    return tuple((path, position) for position, path in unique.items())


def label(path):
    def name(point):
        return 'bar' if point == BAR else 'off' if point == OFF else str(point + 1)

    return ', '.join(f'{name(origin)} → {name(target)}' for origin, target, _ in path) or 'Pass (no legal move)'


def choices(state):
    dice = '+'.join(map(str, state[3]))
    return [(tuple(cell for origin, target, _ in path for cell in (origin, target)),
             f'{dice} · {label(path)}', index)
            for index, (path, _) in enumerate(turns(state, 0))]


def best_turn(state, player, level):
    def score(option):
        _, (points, bar, off) = option
        side = 1 if player == 0 else -1
        return off[player] * 30 + bar[1 - player] * 15 - sum(
            (index + 1 if player == 0 else 24 - index) * abs(count) + (3 if abs(count) == 1 else 0)
            for index, count in enumerate(points) if count * side > 0
        ) + random.random()

    options = turns(state, player)
    if try_random_move(level):
        return random.randrange(len(options))
    return max(range(len(options)), key=lambda index: score(options[index]))


def preview(state, action, click_count):
    position = state[:3]
    dice = list(state[3])
    path = turns(state, 0)[action][0]
    for step in path[:click_count // 2]:
        die = step[2]
        position = next(after for candidate, after in steps(*position, 0, die) if candidate == step)
        dice.remove(die)
    return (*position, tuple(dice))


def move(state, player, action):
    if action is None:
        return None
    path, position = turns(state, player)[action]
    result = None
    if position[2][player] == 15:
        result = 'Mario won!' if player else 'You won!'
    dice = '+'.join(map(str, state[3]))
    description = f"{'Mario' if player else 'You'} rolled {dice} · {label(path)}"
    return description, (*position, roll_dice()), path, result


def marker(point):
    if point == BAR:
        return 240, 252
    if point == OFF:
        return 420, 252
    top = point >= 12
    column = point - 12 if top else 11 - point
    return 31 + column * 36 + (24 if column >= 6 else 0), 171 if top else 305


def hit_test(x, y):
    if 229 <= y <= 278:
        if 214 <= x <= 265:
            return BAR
        if 376 <= x <= 469:
            return OFF
    if not (55 <= y <= 205 or 275 <= y <= 421):
        return None
    for point in range(24):
        center, _ = marker(point)
        if (point >= 12) == (y < 240) and abs(x - center) <= 17:
            return point
    return None


def draw(state, player=None, last=()):
    points, bar, off, dice = state
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480" viewBox="0 0 480 480">',
             '<rect width="480" height="480" fill="#523824"/>',
             '<rect x="12" y="33" width="456" height="410" rx="8" fill="#eed0a1"/>',
             '<rect x="228" y="33" width="24" height="410" fill="#765237"/>']
    marked = {point for origin, target, _ in last for point in (origin, target)}
    for index, count in enumerate(points):
        top = index >= 12
        column = index - 12 if top else 11 - index
        x = 14 + column * 36 + (24 if column >= 6 else 0)
        y, direction = (55, 1) if top else (421, -1)
        color = '#e4a52e' if index in marked else '#a83c3c' if index % 2 else '#694633'
        parts.append(f'<path d="M{x},{y} h34 l-17,{direction * 145} Z" fill="{color}"/>')
        parts.append(f'<text x="{x + 17}" y="{y - direction * 8 + 4}" text-anchor="middle" font-size="12">{index + 1}</text>')
        for chip in range(min(abs(count), 6)):
            cy = y + direction * (13 + 21 * chip)
            fill = '#fff6da' if count > 0 else '#292431'
            parts.append(f'<circle cx="{x + 17}" cy="{cy}" r="12" fill="{fill}" stroke="#8c674d"/>')
            if chip == 5 and abs(count) > 6:
                ink = '#222' if count > 0 else '#fff'
                parts.append(f'<text x="{x + 17}" y="{cy + 4}" text-anchor="middle" fill="{ink}" font-size="12">{abs(count)}</text>')
    parts.append(f'<text x="17" y="229" font-size="15">You · Bar {bar[0]} · Off {off[0]}</text>')
    parts.append(f'<text x="260" y="229" font-size="15">Mario · Bar {bar[1]} · Off {off[1]}</text>')
    parts.append('<text x="240" y="273" text-anchor="middle" fill="#fff" font-size="12">BAR</text>')
    parts.append('<text x="420" y="273" text-anchor="middle" font-size="12">BEAR OFF</text>')
    roll = ' + '.join(map(str, dice))
    parts.append(f'<text x="240" y="463" text-anchor="middle" font-size="17" fill="#fff">Dice: {roll}</text>')
    return ''.join(parts) + '</svg>'


def create_backgammon_window(QtCore, QtWidgets, on_close, say, speech_busy, autosave_path=None):
    return create_board_game_window(
        QtCore, QtWidgets, on_close, 'Backgammon',
        'You play cream toward point 1. Click a checker’s point, then its destination. Repeat for the remaining dice. Click Bear off when eligible.',
        initial, draw, move, legal_moves=choices, hit_test=hit_test, marker=marker, preview_move=preview,
        cpu_move=best_turn,
        say=say, speech_busy=speech_busy, autosave_path=autosave_path,
    )
