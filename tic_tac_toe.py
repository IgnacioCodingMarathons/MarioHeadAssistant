import random

from board_game_window import create_board_game_window
from game_difficulty import try_random_move


TITLE = 'Tic-Tac-Toe'
LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)


def winning_line(board):
    return next((line for line in LINES if board[line[0]] and
                 all(board[cell] == board[line[0]] for cell in line)), ())


def place(board, cell, mark):
    return board[:cell] + (mark,) + board[cell + 1:]


def cpu_cell(board, mark, level):
    free = [index for index, value in enumerate(board) if not value]
    if try_random_move(level):
        return random.choice(free)
    for target in (mark, 'X' if mark == 'O' else 'O'):
        for index in free:
            if winning_line(place(board, index, target)):
                return index
    return random.choice(free)


def move(board, player, cell):
    if winning_line(board) or all(board):
        return None
    mark = 'O' if player else 'X'
    if cell is None or board[cell]:
        return None
    board = place(board, cell, mark)
    name = 'Mario' if player else 'You'
    result = f'{name} won!' if winning_line(board) else 'It’s a tie!' if all(board) else None
    return f'{name} placed {mark} at row {cell // 3 + 1}, column {cell % 3 + 1}', board, cell, result


def board_svg(board, mover=None, last_cell=None):
    wins = winning_line(board)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">']
    for index, mark in enumerate(board):
        x, y = index % 3 * 100, index // 3 * 100
        fill = '#b9e9c9' if index in wins else '#dbeaff' if index == last_cell else '#f4f7fc'
        parts.append(f'<rect x="{x + 2}" y="{y + 2}" width="96" height="96" rx="8" fill="{fill}" stroke="#9ab0cc"/>')
        if mark == 'X':
            parts.append(f'<path d="M{x + 28},{y + 28} l44,44 m0,-44 l-44,44" stroke="#245bbe" stroke-width="9" stroke-linecap="round"/>')
        elif mark == 'O':
            parts.append(f'<circle cx="{x + 50}" cy="{y + 50}" r="25" fill="none" stroke="#c83149" stroke-width="9"/>')
    parts.append('</svg>')
    return ''.join(parts)


def create_tic_tac_toe_window(QtCore, QtWidgets, on_close, say, speech_busy, autosave_path=None):
    return create_board_game_window(
        QtCore, QtWidgets, on_close, TITLE, 'Click an empty square to play X. Mario plays O.',
        ('',) * 9, board_svg, move, board_cells=True, cpu_move=lambda board, player, level: cpu_cell(board, 'O' if player else 'X', level),
        say=say, speech_busy=speech_busy, autosave_path=autosave_path,
    )
