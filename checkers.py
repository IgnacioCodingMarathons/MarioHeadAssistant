import random
from functools import lru_cache

from board_game_window import create_board_game_window
from game_difficulty import try_random_move


def square_name(index):
    return f'{chr(97 + index % 8)}{8 - index // 8}'


@lru_cache(maxsize=256)
def turns(board, side):
    captures = []
    steps = []

    def walk(position, path, piece):
        row, column = divmod(path[-1], 8)
        found = False
        for dr in (-1, 1) if abs(piece) == 2 else (-side,):
            for dc in (-1, 1):
                y, x = row + dr, column + dc
                if not (0 <= y < 8 and 0 <= x < 8):
                    continue
                middle = y * 8 + x
                if not position[middle] and len(path) == 1:
                    steps.append(path + (middle,))
                y, x = y + dr, x + dc
                if position[middle] * side >= 0 or not (0 <= y < 8 and 0 <= x < 8):
                    continue
                end = y * 8 + x
                if position[end]:
                    continue
                found = True
                updated = list(position)
                updated[path[-1]] = updated[middle] = 0
                updated[end] = piece
                if abs(piece) == 1 and y == (0 if side == 1 else 7):
                    captures.append(path + (end,))
                else:
                    walk(updated, path + (end,), piece)
        if not found and len(path) > 1:
            captures.append(path)

    for index, piece in enumerate(board):
        if piece * side > 0:
            walk(board, (index,), piece)
    return tuple(captures or steps)


def choices(state):
    return [(path, ' → '.join(map(square_name, path)), path) for path in turns(state[0], 1)]


def cpu_move(state, player, level):
    board = state[0]
    side = -1 if player else 1
    options = turns(board, side)
    if try_random_move(level):
        return random.choice(options)

    def score(path):
        target = path[-1]
        row, column = divmod(target, 8)
        progress = (7 - row if side == 1 else row) if abs(board[path[0]]) == 1 else 0
        return 4 * len(path) + progress + (3.5 - abs(column - 3.5)) * .4 + random.random()

    return max(options, key=score)


def move(state, player, action):
    board, seen, quiet = state
    side = -1 if player else 1
    if action is None:
        return None
    path = action
    updated = list(board)
    piece = updated[path[0]]
    updated[path[0]] = 0
    capture = abs(path[0] // 8 - path[1] // 8) == 2
    if capture:
        for origin, target in zip(path, path[1:]):
            middle = ((origin // 8 + target // 8) // 2) * 8 + (origin % 8 + target % 8) // 2
            updated[middle] = 0
    promoted = abs(piece) == 1 and path[-1] // 8 == (0 if side == 1 else 7)
    updated[path[-1]] = side * 2 if promoted else piece
    updated = tuple(updated)
    key = (updated, -side)
    quiet = 0 if capture or promoted else quiet + 1
    result = None
    if not turns(updated, -side):
        result = 'Mario won!' if player else 'You won!'
    elif seen.count(key) >= 2 or quiet >= 80:
        result = 'Draw.'
    label = ' → '.join(map(square_name, path))
    return f"{'Mario' if player else 'You'} played {label}", (updated, seen + (key,), quiet), path, result


def draw(state, player=None, last=()):
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480" viewBox="0 0 480 480">']
    for index, piece in enumerate(state[0]):
        row, column = divmod(index, 8)
        x, y = column * 60, row * 60
        color = '#e2bd55' if index in last else '#82705d' if (row + column) % 2 else '#efe1cc'
        parts.append(f'<rect x="{x}" y="{y}" width="60" height="60" fill="{color}"/>')
        if piece:
            fill = '#be3042' if piece < 0 else '#fff5d6'
            parts.append(f'<circle cx="{x + 30}" cy="{y + 30}" r="22" fill="{fill}" stroke="#302520" stroke-width="3"/>')
            if abs(piece) == 2:
                parts.append(f'<circle cx="{x + 30}" cy="{y + 30}" r="12" fill="none" stroke="#302520" stroke-width="3"/>')
        if not column or row == 7:
            label = (str(8 - row) if not column else '') + (chr(97 + column) if row == 7 else '')
            parts.append(f'<text x="{x + 3}" y="{y + 13}" font-size="12" fill="#181818">{label}</text>')
    return ''.join(parts) + '</svg>'


def create_checkers_window(QtCore, QtWidgets, on_close, say, speech_busy, autosave_path=None, player_specs=None):
    board = tuple((-1 if row < 3 else 1 if row > 4 else 0) if (row + col) % 2 else 0
                  for row in range(8) for col in range(8))
    return create_board_game_window(
        QtCore, QtWidgets, on_close, 'Checkers',
        'You play cream. Click a piece, then its destination. Follow the dots for each jump.',
        (board, ((board, 1),), 0), draw, move, legal_moves=choices,
        hit_test=lambda x, y: int(y // 60) * 8 + int(x // 60),
        marker=lambda cell: (cell % 8 * 60 + 30, cell // 8 * 60 + 30),
        say=say, speech_busy=speech_busy, cpu_move=cpu_move, autosave_path=autosave_path, player_specs=player_specs,
    )
