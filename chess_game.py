import random

from board_game_window import create_board_game_window
from game_difficulty import try_random_move


def create_chess_window(QtCore, QtWidgets, on_close, say, speech_busy, autosave_path=None):
    import chess
    import chess.svg

    def choices(board):
        return [((move.from_square, move.to_square), board.san(move), move.uci())
                for move in board.legal_moves]

    def draw(board, player=None, last=None):
        return chess.svg.board(board, lastmove=last, check=board.king(board.turn) if board.is_check() else None, size=480, coordinates=False)

    def best_move(board, level):
        board = board.copy()
        options = list(board.legal_moves)
        if try_random_move(level):
            return random.choice(options)

        def score(candidate):
            victim = board.piece_at(candidate.to_square)
            capture = (0, 1, 3, 3, 5, 9, 0)[victim.piece_type] if victim else int(board.is_en_passant(candidate))
            board.push(candidate)
            value = 100 if board.is_checkmate() else capture + .3 * board.is_check()
            if board.is_attacked_by(board.turn, candidate.to_square):
                piece = board.piece_at(candidate.to_square)
                value -= (0, 1, 3, 3, 5, 9, 0)[piece.piece_type]
            board.pop()
            return value + random.random()

        return max(options, key=score)

    def move(board, player, action):
        board = board.copy()
        if action:
            candidate = board.parse_uci(action)
        else:
            return None
        label = board.san(candidate)
        board.push(candidate)
        outcome = board.outcome(claim_draw=True)
        result = None
        if outcome:
            winner = 'Draw' if outcome.winner is None else 'You won' if outcome.winner else 'Mario won'
            result = f"{winner} ({outcome.termination.name.lower().replace('_', ' ')})."
        return f"{'Mario' if player else 'You'} played {label}", board, candidate, result

    return create_board_game_window(
        QtCore, QtWidgets, on_close, 'Chess', 'You play White. Click a piece, then a highlighted square. White starts at the bottom.',
        chess.Board, draw, move, legal_moves=choices,
        hit_test=lambda x, y: chess.square(int(x // 60), 7 - int(y // 60)),
        marker=lambda cell: ((cell % 8) * 60 + 30, (7 - cell // 8) * 60 + 30),
        say=say, speech_busy=speech_busy, cpu_move=lambda board, player, level: best_move(board, level).uci(), autosave_path=autosave_path,
    )
