import math
import random
from functools import partial

from board_game_window import create_board_game_window


TITLE = 'Snakes and Ladders'
LADDERS = {4: 23, 13: 46, 31: 52, 40: 62, 63: 81, 71: 91}
SNAKES = {27: 8, 36: 17, 55: 34, 73: 51, 89: 68, 97: 76}
JUMPS = LADDERS | SNAKES


def square_origin(square):
    row, column = divmod(square - 1, 10)
    return (column if row % 2 == 0 else 9 - column) * 48, (9 - row) * 48


def board_svg(positions, mover=None, landing=None, *, watch=False):
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480" viewBox="0 0 480 480">']
    for square in range(1, 101):
        x, y = square_origin(square)
        fill = '#fff5dc' if (x // 48 + y // 48) % 2 else '#d9e9f6'
        stroke = '#7548a6' if square == landing else '#b2c6d5'
        parts.append(f'<rect x="{x}" y="{y}" width="48" height="48" fill="{fill}" stroke="{stroke}"/>')
        parts.append(f'<text x="{x + 4}" y="{y + 13}" font-family="sans-serif" font-size="12" fill="#203249">{square}</text>')
    for start, end in JUMPS.items():
        x, y = square_origin(start)
        ex, ey = square_origin(end)
        x, y, ex, ey = x + 24, y + 24, ex + 24, ey + 24
        distance = math.hypot(ex - x, ey - y)
        angle = math.degrees(math.atan2(ey - y, ex - x))
        parts.append(f'<g transform="translate({x},{y}) rotate({angle})">')
        if end > start:
            parts.append(
                f'<path d="M0,-6 H{distance} M0,6 H{distance}" '
                'fill="none" stroke="#207a49" stroke-width="4" stroke-linecap="round"/>'
            )
            for rung in range(5, int(distance), 12):
                parts.append(f'<path d="M{rung},-6 v12" stroke="#207a49" stroke-width="3"/>')
        else:
            waves = max(1, round(distance / 65))
            step = distance / waves
            segments = ['M0,0']
            for wave in range(waves):
                amplitude = 12 * (1 - 0.65 * wave / waves)
                segments.append(
                    f'c{step / 4},{amplitude} {step / 4},{-amplitude} {step / 2},0 '
                    f'c{step / 4},{amplitude} {step / 4},{-amplitude} {step / 2},0'
                )
            path = ' '.join(segments)
            parts.append(f'<path d="{path}" fill="none" stroke="#682040" stroke-width="9" stroke-linecap="round"/>')
            parts.append(f'<path d="{path}" fill="none" stroke="#cc4d80" stroke-width="6" stroke-linecap="round"/>')
            parts.append(
                '<path d="M-8,0 H-14 M-14,0 l-4,-3 M-14,0 l-4,3" fill="none" stroke="#a52d66" stroke-width="2"/>'
                '<ellipse rx="10" ry="8" fill="#a52d66" stroke="#682040" stroke-width="2"/>'
                '<circle cx="-4" cy="-3.5" r="2" fill="#fff"/>'
                '<circle cx="-4" cy="3.5" r="2" fill="#fff"/>'
                '<circle cx="-4.6" cy="-3.5" r="1" fill="#29202a"/>'
                '<circle cx="-4.6" cy="3.5" r="1" fill="#29202a"/>'
            )
        parts.append('</g>')
    for player, square in enumerate(positions):
        if not square:
            continue
        x, y = square_origin(square)
        x += 13 + player * 22
        color, letter, ink = ('#d32638', 'M', '#19283a') if player else (
            ('#218445', 'L', '#ffffff') if watch else ('#e5b500', 'Y', '#19283a')
        )
        stroke = '#19283a' if mover == player else '#fff'
        parts.append(f'<circle cx="{x}" cy="{y + 32}" r="10" fill="{color}" stroke="{stroke}" stroke-width="2"/>')
        parts.append(f'<text x="{x}" y="{y + 36}" text-anchor="middle" font-family="sans-serif" font-weight="bold" font-size="11" fill="{ink}">{letter}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def roll_move(position, player, cell):
    positions = list(position)
    if 100 in positions:
        return None
    name = ('You', 'Mario')[player]
    die = random.randint(1, 6)
    landing = positions[player] + die
    description = f'{name} rolled {die}'
    if landing > 100:
        description += ' and stayed put'
        landing = positions[player]
    else:
        destination = JUMPS.get(landing, landing)
        positions[player] = destination
        description += f' → {landing}'
        if destination != landing:
            path = 'ladder' if destination > landing else 'snake'
            description += f' → {destination} ({path})'
    winner = ('You won!' if player == 0 else 'Mario won!') if positions[player] == 100 else None
    return description, tuple(positions), landing, winner


def create_snakes_and_ladders_window(QtCore, QtWidgets, on_close, say, speech_busy, autosave_path=None, player_specs=None):
    return create_board_game_window(
        QtCore, QtWidgets, on_close, TITLE,
        'Reach 100 with an exact roll. Climb the green ladders and slide down the pink snakes.',
        (0, 0), board_svg, roll_move,
        say=say, speech_busy=speech_busy, draw_watch_board=partial(board_svg, watch=True), autosave_path=autosave_path, player_specs=player_specs,
    )
