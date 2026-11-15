import random

from board_game_window import create_board_game_window
from game_difficulty import try_random_move


SIZE = 9
POINTS = range(SIZE * SIZE)
NEIGHBORS = tuple(
    tuple(other for other in POINTS if abs(other // SIZE - point // SIZE) +
          abs(other % SIZE - point % SIZE) == 1)
    for point in POINTS
)
KOMI = 7.5


def group_at(board, point):
    group = {point}
    pending = [point]
    color = board[point]
    border = set()
    while pending:
        for neighbor in NEIGHBORS[pending.pop()]:
            if board[neighbor] != color:
                border.add(neighbor)
            elif neighbor not in group:
                group.add(neighbor)
                pending.append(neighbor)
    return group, border


def place(board, point, color, seen):
    if board[point]:
        return None
    updated = list(board)
    updated[point] = color
    for neighbor in NEIGHBORS[point]:
        if updated[neighbor] == 3 - color:
            group, border = group_at(updated, neighbor)
            if not any(updated[edge] == 0 for edge in border):
                for stone in group:
                    updated[stone] = 0
    _, border = group_at(updated, point)
    result = tuple(updated)
    if any(updated[edge] == 0 for edge in border) and result not in seen:
        return result
    return None


def coordinate(point):
    return f'{chr(65 + point % SIZE)}{SIZE - point // SIZE}'


def choices(state):
    board, seen, _ = state
    return [((point,), coordinate(point), point) for point in POINTS if place(board, point, 1, seen)] + [((), 'Pass', -1)]


def area(board):
    scores = [0, board.count(1), board.count(2) + KOMI]
    visited = set()
    for point in POINTS:
        if board[point] or point in visited:
            continue
        group, border = group_at(board, point)
        visited.update(group)
        colors = {board[edge] for edge in border}
        if len(colors) == 1:
            scores[colors.pop()] += len(group)
    return scores[1:]


def cpu_action(state, color, level):
    board, seen, passes = state
    if try_random_move(level):
        options = [point for point in POINTS if place(board, point, color, seen)]
        return random.choice(options) if options else -1
    old_area = area(board)
    direction = 1 if color == 2 else -1
    candidates = []
    for point in POINTS:
        updated = place(board, point, color, seen)
        if updated is None:
            continue
        black, white = area(updated)
        _, border = group_at(updated, point)
        liberties = sum(updated[edge] == 0 for edge in border)
        score = direction * ((white - black) - (old_area[1] - old_area[0]))
        score += min(liberties, 5) * .2 + random.random() * .6
        score += (board.count(3 - color) - updated.count(3 - color)) * 2
        candidates.append((score, point, updated))
    score, action, _ = max(candidates, default=(0, -1, board))
    if (passes or board.count(0) < 20) and score < 1.8:
        action = -1
    return action


def move(state, player, action):
    board, seen, passes = state
    color = player + 1
    if action is None:
        return None
    if action != -1:
        updated = place(board, action, color, seen)
        if updated is None:
            return None
    name = 'Mario' if player else 'You'
    if action == -1:
        updated = board
        passes += 1
        description = f'{name} passed'
    else:
        seen = seen | {updated}
        passes = 0
        description = f'{name} played {coordinate(action)}'
    result = None
    if passes == 2:
        black, white = area(updated)
        winner = 'You won' if black > white else 'Mario won'
        result = f'{winner}. Black {black:g} · White {white:g}'
    return description, (updated, seen, passes), action, result


def hit_test(x, y):
    column, row = round(x / 48) - 1, round(y / 48) - 1
    if 0 <= column < SIZE and 0 <= row < SIZE:
        return row * SIZE + column
    return None


def draw(state, player=None, last=None):
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480" viewBox="0 0 480 480">',
             '<rect width="480" height="480" fill="#e8bc7b"/>']
    for index in range(SIZE):
        position = 48 + index * 48
        parts.append(f'<path d="M48,{position} H432 M{position},48 V432" stroke="#594129" stroke-width="2"/>')
        parts.append(f'<text x="{position}" y="28" text-anchor="middle" font-size="15">{chr(65 + index)}</text>')
        parts.append(f'<text x="22" y="{position + 5}" text-anchor="middle" font-size="15">{SIZE - index}</text>')
    for point, color in enumerate(state[0]):
        if not color:
            continue
        x, y = 48 + point % SIZE * 48, 48 + point // SIZE * 48
        fill = '#262832' if color == 1 else '#fff9e7'
        outline = '#e54445' if point == last else '#695036'
        parts.append(f'<circle cx="{x}" cy="{y}" r="20" fill="{fill}" stroke="{outline}" stroke-width="3"/>')
    return ''.join(parts) + '</svg>'


def create_go_window(QtCore, QtWidgets, on_close, say, speech_busy):
    board = (0,) * len(POINTS)
    return create_board_game_window(
        QtCore, QtWidgets, on_close, 'Go',
        'Click an intersection to place a black stone. Two passes end the game. Area scoring counts stones as played, with 7.5 komi for White.',
        (board, frozenset({board}), 0), draw, move, legal_moves=choices, hit_test=hit_test,
        cpu_move=lambda state, player, level: cpu_action(state, player + 1, level),
        say=say, speech_busy=speech_busy,
    )
