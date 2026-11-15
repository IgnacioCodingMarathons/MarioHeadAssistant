import json
import pickle
import random
import zlib
from game_difficulty import add_difficulty_slider
from game_history import write_move_history


CONNECT_FOUR_START_LINE = "\\showas=Let's-a\\let's \\hidden\\a go!"
CONNECT_FOUR_PLAYER_TURN_LINES = (
    'Your move!', 'Go!', r"\showas=It's-a\it's \hidden\a your turn!",
    r'\ignorerecdpronun\Your \ignorerecdpronun\turn!',
)
CONNECT_FOUR_MY_TURN_LINE = 'My turn!'
CONNECT_FOUR_MARIO_WIN_LINE = r'\showas=Woohoo!\oh \hidden\ho! I won!'
CONNECT_FOUR_PLAYER_WIN_LINE = "You're great at this game! You win!"
CONNECT_FOUR_DRAW_LINE = "It's a tie! Want to try again?"
CONNECT_FOUR_DIALOGUE = tuple(dict.fromkeys((
    CONNECT_FOUR_START_LINE, *CONNECT_FOUR_PLAYER_TURN_LINES,
    CONNECT_FOUR_MY_TURN_LINE, CONNECT_FOUR_MARIO_WIN_LINE,
    CONNECT_FOUR_PLAYER_WIN_LINE, CONNECT_FOUR_DRAW_LINE,
)))

def create_connect_four_window(
        QtCore, QtWidgets, say, on_result, on_turn, on_close, on_continue=None,
        autosave_path=None, player_specs=None):
    from PySide6 import QtGui, QtSvg

    class ConnectFourWindow(QtWidgets.QWidget):
        def closeEvent(self, event):
            luigi_timer.stop()
            if not completed[0]:
                persist()
            restored[0] = False
            on_close()
            super().closeEvent(event)

    window = ConnectFourWindow()
    window.setWindowTitle('Connect-4')
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.resize(780, 560)
    window.setMinimumSize(700, 500)
    outer = QtWidgets.QVBoxLayout(window)
    status = QtWidgets.QLabel('Your turn')
    status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    outer.addWidget(status)
    difficulty = add_difficulty_slider(QtCore, QtWidgets, outer)
    content = QtWidgets.QHBoxLayout()
    outer.addLayout(content, 1)
    game_side = QtWidgets.QWidget()
    game_layout = QtWidgets.QVBoxLayout(game_side)
    grid = QtWidgets.QGridLayout()
    grid.setSpacing(5)
    game_layout.addLayout(grid)
    history_side = QtWidgets.QWidget()
    history_layout = QtWidgets.QVBoxLayout(history_side)
    history_layout.addWidget(QtWidgets.QLabel('Move history'))
    history_list = QtWidgets.QListWidget()
    history_list.setIconSize(QtCore.QSize(150, 105))
    history_layout.addWidget(history_list, 1)
    content.addWidget(game_side, 3)
    content.addWidget(history_side, 2)
    controls = QtWidgets.QHBoxLayout()
    undo_button = QtWidgets.QPushButton('Undo')
    redo_button = QtWidgets.QPushButton('Redo')
    save_button = QtWidgets.QPushButton('Save history…')
    controls.addWidget(undo_button)
    controls.addWidget(redo_button)
    controls.addWidget(save_button)
    game_layout.addLayout(controls)
    continue_button = QtWidgets.QPushButton('Continue')
    continue_button.hide()

    def continue_clicked():
        continue_button.hide()
        if on_continue is not None:
            on_continue()

    continue_button.clicked.connect(continue_clicked)
    game_layout.addWidget(continue_button)

    board_labels = []
    column_buttons = []
    board = [[0 for _ in range(7)] for _ in range(6)]
    active = [False]
    current_turn = [1]
    watch_mode = [False]
    history = []
    cursor = [0]
    last_move = [None]
    winning_cells = [set()]
    winner_player = [None]
    move_count = [0]
    completed = [False]
    restored = [False]
    save_magic = b'MARIOCONNECT1'
    player_turn_line_index = [0]
    luigi_timer = QtCore.QTimer(window)
    luigi_timer.setSingleShot(True)
    luigi_timer.timeout.connect(lambda: on_turn('watch_luigi'))

    def queue_luigi_turn(delay):
        luigi_timer.stop()
        luigi_timer.start(delay)

    def player_name(player):
        if watch_mode[0]:
            return 'Luigi' if player == 1 else 'Mario'
        if player_specs and player - 1 < len(player_specs):
            return player_specs[player - 1].get('name', 'Player 2' if player == 2 else 'You')
        return 'You' if player == 1 else 'Mario'

    def player_is_cpu(player):
        if watch_mode[0]:
            return True
        if player_specs and player - 1 < len(player_specs):
            return player_specs[player - 1].get('kind') == 'cpu'
        return player == 2

    def find_winning_cells(current, player):
        for row in range(6):
            for col in range(7):
                if current[row][col] != player:
                    continue
                for row_step, col_step in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    cells = []
                    for offset in range(4):
                        r = row + row_step * offset
                        c = col + col_step * offset
                        if not (0 <= r < 6 and 0 <= c < 7):
                            break
                        cells.append((r, c))
                    if len(cells) == 4 and all(current[r][c] == player for r, c in cells):
                        return set(cells)
        return set()

    def drop(current, column, player):
        for row in range(5, -1, -1):
            if current[row][column] == 0:
                current[row][column] = player
                return row
        return None

    def available_columns(current):
        return [col for col in (3, 2, 4, 1, 5, 0, 6) if current[0][col] == 0]

    def evaluate(current, player):
        opponent = 3 - player
        score = sum(7 if current[row][3] == player else -7 if current[row][3] == opponent else 0
                    for row in range(6))
        for row in range(6):
            for col in range(7):
                for row_step, col_step in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    cells = []
                    for offset in range(4):
                        r = row + row_step * offset
                        c = col + col_step * offset
                        if not (0 <= r < 6 and 0 <= c < 7):
                            break
                        cells.append(current[r][c])
                    if len(cells) != 4:
                        continue
                    own_count = cells.count(player)
                    opposing_count = cells.count(opponent)
                    empty_count = cells.count(0)
                    if not opposing_count:
                        score += (0, 2, 11, 55, 100000)[own_count]
                        if own_count == 3 and empty_count == 1:
                            empty_index = cells.index(0)
                            empty_row = row + row_step * empty_index
                            if empty_row == 5 or current[empty_row + 1][col + col_step * empty_index] != 0:
                                score += 32
                    if not own_count:
                        score -= (0, 3, 10, 46, 100000)[opposing_count]
        return score

    def choose_cpu_move(current, cpu_player):
        columns = available_columns(current)
        if not columns:
            return None
        move_style = random.randrange(3)
        if move_style == 0:
            return random.choice(columns)
        opponent = 3 - cpu_player
        preference = (3, 2, 4, 1, 5, 0, 6) if cpu_player == 2 else (3, 4, 2, 5, 1, 6, 0)
        preferred_rank = {column: index for index, column in enumerate(preference)}
        scored_moves = []
        for column in columns:
            child = [line[:] for line in current]
            drop(child, column, cpu_player)
            score = evaluate(child, cpu_player)
            if find_winning_cells(child, cpu_player):
                score += 100000
            opponent_reply_wins = 0
            for reply in available_columns(child):
                response = [line[:] for line in child]
                drop(response, reply, opponent)
                if find_winning_cells(response, opponent):
                    opponent_reply_wins += 1
            if opponent_reply_wins:
                score -= 500 * opponent_reply_wins
            score -= preferred_rank[column] * 0.01
            score += random.uniform(-0.35, 0.35)
            scored_moves.append((score, column, opponent_reply_wins))
        best_score = max(score for score, _, _ in scored_moves)
        if move_style == 1:
            level = difficulty.value()
            maximum_loss = 8 + (20 - level) * 2.5
            minimum_loss = max(1.0, maximum_loss * 0.15)
            lower_moves = [
                column for score, column, _ in scored_moves
                if minimum_loss <= best_score - score <= maximum_loss
            ]
            if lower_moves:
                return random.choice(lower_moves)
        strategy_slack = (20 - difficulty.value()) * 1.5 + 0.25
        reasonable_moves = [
            column for score, column, _ in scored_moves
            if score >= best_score - strategy_slack
        ]
        return random.choice(reasonable_moves)

    def make_svg(current, moved=None, wins=()):
        width, height = 350, 300
        cell = 40
        x0, y0 = 35, 30
        wins = set(wins)
        winning_color = '#e53935' if winner_player[0] == 2 else (
            '#20a94b' if watch_mode[0] else '#fdd835'
        )
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<defs><filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="6" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>',
            '<rect width="100%" height="100%" fill="#f5f7fa"/>',
            f'<rect x="{x0 - 18}" y="{y0 - 18}" width="{7 * cell + 36}" height="{6 * cell + 36}" rx="14" fill="#1769aa"/>',
        ]
        if len(wins) == 4:
            ordered = sorted(wins)
            first, last = ordered[0], ordered[-1]
            x1, y1 = x0 + first[1] * cell, y0 + first[0] * cell
            x2, y2 = x0 + last[1] * cell, y0 + last[0] * cell
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{winning_color}" stroke-width="8" stroke-linecap="round" opacity="0.8" filter="url(#glow)"/>')
        for row in range(6):
            for col in range(7):
                x = x0 + col * cell
                y = y0 + row * cell
                piece = current[row][col]
                color = (
                    '#e53935' if piece == 2 else
                    '#20a94b' if piece == 1 and watch_mode[0] else
                    '#fdd835' if piece == 1 else '#ffffff'
                )
                if (row, col) in wins:
                    parts.append(f'<circle cx="{x}" cy="{y}" r="16" fill="{color}" stroke="{winning_color}" stroke-width="8" filter="url(#glow)"/>')
                elif moved == (row, col):
                    parts.append(f'<circle cx="{x}" cy="{y}" r="16" fill="{color}" stroke="#ffffff" stroke-width="4"/>')
                else:
                    parts.append(f'<circle cx="{x}" cy="{y}" r="16" fill="{color}"/>')
                if piece == 2 or (piece == 1 and watch_mode[0]):
                    letter = 'M' if piece == 2 else 'L'
                    parts.append(f'<text x="{x}" y="{y + 6}" text-anchor="middle" font-family="sans-serif" font-size="17" font-weight="bold" fill="#ffffff">{letter}</text>')
        parts.append('</svg>')
        return ''.join(parts)

    def render_icon(svg):
        renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode('utf-8')))
        pixmap = QtGui.QPixmap(150, 105)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QtGui.QIcon(pixmap)

    def update_history():
        history_list.clear()
        for index, entry in enumerate(history[:cursor[0]]):
            who = player_name(entry['player'])
            svg = make_svg(entry['board'], entry['move'], entry.get('wins', ()))
            item = QtWidgets.QListWidgetItem(f"{index + 1}. {who} — column {entry['column'] + 1}")
            item.setIcon(render_icon(svg))
            item.setSizeHint(QtCore.QSize(250, 125))
            history_list.addItem(item)
        undo_button.setEnabled(cursor[0] > 0)
        redo_button.setEnabled(cursor[0] < len(history))

    def refresh_board():
        for row in range(6):
            for col in range(7):
                cell = board_labels[row][col]
                piece = board[row][col]
                color = (
                    '#f4f6f8' if piece == 0 else
                    '#e53935' if piece == 2 else
                    '#20a94b' if watch_mode[0] else '#fdd835'
                )
                letter = 'M' if piece == 2 else 'L' if piece == 1 and watch_mode[0] else ''
                cell.setText(letter)
                cell.setStyleSheet(
                    f'QPushButton {{ background-color: {color}; color: #ffffff; font-weight: bold; '
                    'font-size: 20px; border: 2px solid #17365d; border-radius: 22px; }'
                    'QPushButton:enabled:hover { border: 3px solid #3a86dd; }'
                )
                effect = cell.graphicsEffect()
                if (row, col) in winning_cells[0]:
                    if effect is None:
                        effect = QtWidgets.QGraphicsDropShadowEffect(cell)
                        cell.setGraphicsEffect(effect)
                    effect.setBlurRadius(30)
                    effect.setOffset(0, 0)
                    effect.setColor(QtGui.QColor(
                        '#e53935' if winner_player[0] == 2 else
                        '#20a94b' if watch_mode[0] else '#fdd835'
                    ))
                elif effect is not None:
                    cell.setGraphicsEffect(None)
        update_history()

    def set_columns_enabled(enabled):
        for index, button in enumerate(column_buttons):
            button.setEnabled(enabled and board[0][index % 7] == 0)

    def finish_round(player_won):
        completed[0] = True
        clear_autosave()
        active[0] = False
        set_columns_enabled(False)
        if watch_mode[0] and player_won in ('mario', 'luigi'):
            winner_player[0] = 2 if player_won == 'mario' else 1
            if player_won == 'mario':
                say(CONNECT_FOUR_MARIO_WIN_LINE)
            else:
                say(CONNECT_FOUR_PLAYER_WIN_LINE)
            on_result(player_won)
            refresh_board()
            return
        winner_player[0] = 1 if player_won is True else 2 if player_won is False else None
        if player_won:
            say(CONNECT_FOUR_PLAYER_WIN_LINE)
            on_result('player')
        elif player_won is False:
            say(CONNECT_FOUR_MARIO_WIN_LINE)
            on_result('cpu')
        else:
            say(CONNECT_FOUR_DRAW_LINE)
            on_result('draw')
        refresh_board()

    def record_move(player, row, column):
        del history[cursor[0]:]
        next_turn = 2 if player == 1 else 1
        move_wins = find_winning_cells(board, player)
        entry = {
            'player': player,
            'row': row,
            'column': column,
            'move': (row, column),
            'board': [line[:] for line in board],
            'next_turn': next_turn,
            'wins': set(move_wins),
            'winner': player if move_wins else None,
            'label': f"{player_name(player)} moved in column {column + 1}",
        }
        history.append(entry)
        cursor[0] += 1
        current_turn[0] = next_turn
        last_move[0] = (row, column)
        winning_cells[0] = move_wins
        move_count[0] = cursor[0]
        persist()

    def persist():
        if autosave_path is None:
            return
        try:
            autosave_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'watch': watch_mode[0], 'board': board, 'current_turn': current_turn[0],
                'history': history, 'cursor': cursor[0],
            }
            autosave_path.write_bytes(save_magic + zlib.compress(pickle.dumps(payload, protocol=5), 9))
        except (OSError, TypeError, ValueError, pickle.PickleError):
            pass

    def clear_autosave():
        if autosave_path is not None:
            try:
                autosave_path.unlink(missing_ok=True)
            except OSError:
                pass

    def restore_autosave():
        if autosave_path is None or not autosave_path.exists():
            return False
        try:
            data = autosave_path.read_bytes()
            if data.startswith(save_magic):
                payload = pickle.loads(zlib.decompress(data[len(save_magic):]))
            else:
                payload = json.loads(data.decode('utf-8'))
            board[:] = [line[:] for line in payload['board']]
            watch_mode[0] = bool(payload.get('watch', False))
            current_turn[0] = int(payload.get('current_turn', 1))
            history[:] = payload.get('history', [])
            cursor[0] = min(int(payload.get('cursor', len(history))), len(history))
            if cursor[0]:
                entry = history[cursor[0] - 1]
                last_move[0] = tuple(entry.get('move', (entry.get('row', 0), entry.get('column', 0))))
                winning_cells[0] = {tuple(cell) for cell in entry.get('wins', ())}
                winner_player[0] = entry.get('winner')
            persist()
            return True
        except (OSError, EOFError, ValueError, TypeError, KeyError, UnicodeError, pickle.PickleError, zlib.error):
            return False

    def announce_player_turn():
        line = CONNECT_FOUR_PLAYER_TURN_LINES[
            player_turn_line_index[0] % len(CONNECT_FOUR_PLAYER_TURN_LINES)
        ]
        player_turn_line_index[0] += 1
        say(line)
        status.setText('Your turn')
        set_columns_enabled(False)
        on_turn('enable_player')

    def enable_player_turn():
        if active[0] and not player_is_cpu(current_turn[0]):
            status.setText('Your turn')
            set_columns_enabled(True)

    def announce_watch_mario_turn():
        status.setText("Mario's turn")
        set_columns_enabled(False)
        say(CONNECT_FOUR_MY_TURN_LINE)
        on_turn('cpu_mario')

    def cpu_turn():
        player = current_turn[0]
        if not active[0] or player not in (1, 2):
            return
        if not player_is_cpu(player):
            return
        column = choose_cpu_move(board, player)
        if column is None:
            finish_round(None)
            return
        row = drop(board, column, player)
        record_move(player, row, column)
        refresh_board()
        if winning_cells[0]:
            finish_round('mario' if watch_mode[0] and player == 2 else 'luigi'
                         if watch_mode[0] else False)
        elif not available_columns(board):
            finish_round(None)
        elif watch_mode[0] and current_turn[0] == 2:
            on_turn('watch_mario')
        elif watch_mode[0]:
            status.setText("Luigi's turn")
            say('Your turn!')
            queue_luigi_turn(650)
        else:
            on_turn('announce_player')

    def human_move(column):
        if not active[0] or player_is_cpu(current_turn[0]) or board[0][column] != 0:
            return
        player = current_turn[0]
        set_columns_enabled(False)
        row = drop(board, column, player)
        record_move(player, row, column)
        refresh_board()
        if winning_cells[0]:
            finish_round(True if player == 1 else False)
        elif not available_columns(board):
            finish_round(None)
        elif player_is_cpu(current_turn[0]):
            say(CONNECT_FOUR_MY_TURN_LINE)
            status.setText("Mario's turn")
            on_turn('cpu')
        else:
            announce_player_turn()

    def snapshot_board(current):
        return [line[:] for line in current]

    def restore_to_cursor():
        nonlocal board
        board = snapshot_board(history[cursor[0] - 1]['board']) if cursor[0] else [[0] * 7 for _ in range(6)]
        last_move[0] = history[cursor[0] - 1]['move'] if cursor[0] else None
        winning_cells[0] = set(history[cursor[0] - 1].get('wins', ())) if cursor[0] else set()
        winner_player[0] = history[cursor[0] - 1].get('winner') if cursor[0] else None
        move_count[0] = cursor[0]
        active[0] = True
        if cursor[0]:
            current_turn[0] = history[cursor[0] - 1]['next_turn']
        else:
            current_turn[0] = 2 if watch_mode[0] else 1
        refresh_board()

    def undo_move():
        if cursor[0] <= 0:
            return
        luigi_timer.stop()
        on_turn(None)
        removed = history[cursor[0] - 1]
        cursor[0] -= 1
        restore_to_cursor()
        if watch_mode[0] and current_turn[0] == 1:
            status.setText("Luigi's turn")
            set_columns_enabled(False)
            queue_luigi_turn(300)
        elif watch_mode[0]:
            announce_watch_mario_turn()
        elif current_turn[0] == 2:
            say(CONNECT_FOUR_MY_TURN_LINE)
            status.setText("Mario's turn")
            set_columns_enabled(False)
            on_turn('cpu')
        else:
            announce_player_turn()

    def redo_move():
        if cursor[0] >= len(history):
            return
        luigi_timer.stop()
        on_turn(None)
        entry = history[cursor[0]]
        cursor[0] += 1
        restore_to_cursor()
        if entry['winner'] == 1:
            finish_round(True)
        elif entry['winner'] == 2:
            finish_round(False)
        elif not available_columns(board):
            finish_round(None)
        elif watch_mode[0] and current_turn[0] == 1:
            status.setText("Luigi's turn")
            set_columns_enabled(False)
            queue_luigi_turn(300)
        elif watch_mode[0]:
            announce_watch_mario_turn()
        elif current_turn[0] == 2:
            say(CONNECT_FOUR_MY_TURN_LINE)
            status.setText("Mario's turn")
            set_columns_enabled(False)
            on_turn('cpu')
        else:
            announce_player_turn()

    def export_history():
        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            window, 'Save Connect-4 move history', 'connect-4-history.mhtml',
            'MHTML files (*.mhtml *.mht)',
        )
        if not file_name:
            return
        snapshots = []
        for index, entry in enumerate(history[:cursor[0]], start=1):
            snapshots.append((
                f'move-{index}', make_svg(entry['board'], entry['move'], entry.get('wins', ())),
                f"Move {index}: {player_name(entry['player'])} — column {entry['column'] + 1}",
            ))
        if not snapshots:
            snapshots.append(('move-0', make_svg(board, last_move[0], winning_cells[0]), 'Starting board'))
        result_text = (
            f'{player_name(winner_player[0])} won!' if winner_player[0] in (1, 2) else
            'Draw.' if cursor[0] and not active[0] else 'Game in progress.'
        )
        try:
            output_path = write_move_history(file_name, 'Connect-4', result_text, snapshots)
            status.setText(f'Saved history: {output_path.name} — {result_text}')
        except OSError as exc:
            QtWidgets.QMessageBox.critical(window, 'Connect-4', f'Could not save the move history.\n{exc}')

    for row in range(6):
        label_row = []
        for col in range(7):
            cell = QtWidgets.QPushButton()
            cell.setAccessibleName(f'Drop in column {col + 1}, row {row + 1}')
            cell.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            cell.setMinimumSize(44, 44)
            cell.setMaximumSize(56, 56)
            cell.clicked.connect(lambda checked=False, column=col: human_move(column))
            grid.addWidget(cell, row, col)
            column_buttons.append(cell)
            label_row.append(cell)
        board_labels.append(label_row)
    undo_button.clicked.connect(undo_move)
    redo_button.clicked.connect(redo_move)
    save_button.clicked.connect(export_history)
    history_list.currentRowChanged.connect(
        lambda row: status.setText(history[row].get('label', status.text()) if 0 <= row < cursor[0] else status.text())
    )

    def reset_game(watch=None):
        nonlocal board
        if not restored[0] and restore_autosave():
            restored[0] = True
            continue_button.hide()
            active[0] = True
            refresh_board()
            status.setText('Mario and Luigi are ready' if watch_mode[0] else 'Mario is ready')
            set_columns_enabled(False)
            say('You have an autosave available! Do you want to continue?')
            def ask_resume():
                buttons = QtWidgets.QMessageBox.StandardButton
                answer = QtWidgets.QMessageBox.question(
                    window, 'Hey!', 'You have an autosave available! Do you want to continue?',
                    buttons.Yes | buttons.No, buttons.Yes
                )
                if answer == buttons.No:
                    clear_autosave()
                    restored[0] = True
                    reset_game(watch)
                    return
                if watch_mode[0]:
                    on_turn('watch_mario' if current_turn[0] == 2 else 'watch_luigi')
                elif current_turn[0] == 1:
                    on_turn('enable_player')
                else:
                    on_turn('cpu')
            QtCore.QTimer.singleShot(1200, ask_resume)
            return
        restored[0] = True
        completed[0] = False
        luigi_timer.stop()
        continue_button.hide()
        player_turn_line_index[0] = 0
        if watch is not None:
            watch_mode[0] = bool(watch)
        board = [[0 for _ in range(7)] for _ in range(6)]
        history.clear()
        cursor[0] = 0
        current_turn[0] = 2 if watch_mode[0] else 1
        active[0] = True
        last_move[0] = None
        winning_cells[0] = set()
        winner_player[0] = None
        move_count[0] = 0
        persist()
        refresh_board()
        status.setText('Mario and Luigi are ready' if watch_mode[0] else 'Mario is ready')
        set_columns_enabled(False)
        say(CONNECT_FOUR_START_LINE)
        on_turn('watch_mario' if watch_mode[0] else 'announce_player')

    def set_preparing():
        status.setText("Preparing Mario's lines…")
        set_columns_enabled(False)

    def show_continue():
        continue_button.show()
        continue_button.setFocus()

    def undo_shortcut():
        undo_move()

    undo_shortcut_action = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Undo, window)
    undo_shortcut_action.activated.connect(undo_shortcut)
    redo_shortcut_action = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Redo, window)
    redo_shortcut_action.activated.connect(redo_move)
    undo_button.setEnabled(False)
    redo_button.setEnabled(False)
    window.announce_player_turn = announce_player_turn
    window.announce_watch_mario_turn = announce_watch_mario_turn
    window.enable_player_turn = enable_player_turn
    window.cpu_turn = cpu_turn
    window.reset_game = reset_game
    window.set_preparing = set_preparing
    window.show_continue = show_continue
    refresh_board()
    return window
