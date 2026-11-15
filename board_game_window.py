from game_difficulty import add_difficulty_slider

from game_history import write_move_history


def create_board_game_window(
        QtCore, QtWidgets, on_close, title, instructions, initial, draw_board,
        make_move, board_cells=False, legal_moves=None, hit_test=None, marker=None,
        say=None, speech_busy=None, cpu_move=None, preview_move=None,
        draw_watch_board=None):
    from PySide6 import QtGui, QtSvg, QtSvgWidgets

    class GameWindow(QtWidgets.QWidget):
        def hideEvent(self, event):
            timer.stop()
            super().hideEvent(event)

        def closeEvent(self, event):
            timer.stop()
            on_close()
            super().closeEvent(event)

    window = GameWindow()
    window.setWindowTitle(title)
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.resize(800, 605)
    layout = QtWidgets.QVBoxLayout(window)
    status = QtWidgets.QLabel()
    status.setWordWrap(True)
    layout.addWidget(status)
    difficulty = add_difficulty_slider(QtCore, QtWidgets, layout) if cpu_move else None
    help_label = QtWidgets.QLabel(instructions)
    help_label.setWordWrap(True)
    layout.addWidget(help_label)
    content = QtWidgets.QHBoxLayout()
    layout.addLayout(content, 1)
    class Board(QtSvgWidgets.QSvgWidget):
        move_markers = ()

        def board_rect(self):
            size = min(self.width(), self.height())
            return QtCore.QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size)

        def cell_at(self, position):
            rect = self.board_rect()
            size = rect.width()
            x = position.x() - rect.x()
            y = position.y() - rect.y()
            if not (0 <= x < size and 0 <= y < size):
                return None
            if hit_test:
                return hit_test(x * 480 / size, y * 480 / size)
            return int(y * 3 / size) * 3 + int(x * 3 / size)

        def paintEvent(self, event):
            super().paintEvent(event)
            if not self.move_markers:
                return
            rect = self.board_rect()
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.translate(rect.topLeft())
            scale = rect.width() / 480
            painter.scale(scale, scale)
            painter.setPen(QtGui.QPen(QtGui.QColor('#20283c'), 2))
            for x, y, selected in self.move_markers:
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush if selected else QtGui.QColor('#2ee4b1'))
                if selected:
                    painter.setPen(QtGui.QPen(QtGui.QColor('#ffbc40'), 3))
                painter.drawEllipse(QtCore.QPointF(x, y), 24 if selected else 7, 24 if selected else 7)
                if selected:
                    painter.setPen(QtGui.QPen(QtGui.QColor('#20283c'), 2))
            painter.end()

        def update_pointer(self):
            cell = self.cell_at(self.mapFromGlobal(QtGui.QCursor.pos()))
            shape = (QtCore.Qt.CursorShape.PointingHandCursor if can_select(cell)
                     else QtCore.Qt.CursorShape.ArrowCursor)
            self.setCursor(shape)

        def mouseMoveEvent(self, event):
            self.update_pointer()
            super().mouseMoveEvent(event)

        def mousePressEvent(self, event):
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                cell = self.cell_at(event.position())
                if can_select(cell):
                    select_cell(cell)
                    self.update_pointer()
            super().mousePressEvent(event)

    board = Board()
    board.setMinimumSize(320, 320)
    board.setMouseTracking(bool(board_cells or hit_test))
    content.addWidget(board, 2)
    sidebar = QtWidgets.QVBoxLayout()
    content.addLayout(sidebar, 1)
    sidebar.addWidget(QtWidgets.QLabel('Move history'))
    history_list = QtWidgets.QListWidget()
    history_list.setIconSize(QtCore.QSize(96, 96))
    history_list.setWordWrap(True)
    sidebar.addWidget(history_list)
    controls = QtWidgets.QHBoxLayout()
    layout.addLayout(controls)
    roll = QtWidgets.QPushButton('Roll dice')
    undo = QtWidgets.QPushButton('Undo')
    redo = QtWidgets.QPushButton('Redo')
    save = QtWidgets.QPushButton('Save history…')
    proceed = QtWidgets.QPushButton('Continue')
    for button in (roll, undo, redo, save, proceed):
        controls.addWidget(button)

    timer = QtCore.QTimer(window)
    timer.setSingleShot(True)
    moves = []
    cursor = 0
    starting_position = initial
    watch = False
    draw_position = draw_board
    prefix = ()
    routes = []
    pending_line = None
    after_line = None

    def speak(line, callback):
        nonlocal pending_line, after_line
        pending_line = line
        after_line = callback
        timer.start(30)

    def speech_step():
        nonlocal pending_line, after_line
        if speech_busy and speech_busy():
            timer.start(60)
            return
        if pending_line is not None:
            line, pending_line = pending_line, None
            if say:
                say(line)
            timer.start(60)
            return
        if after_line:
            callback, after_line = after_line, None
            callback()

    def current_entry():
        return moves[cursor - 1] if cursor else None

    def current_position():
        entry = current_entry()
        return entry[3] if entry else starting_position

    def automated():
        return watch or cursor % 2

    def watch_text(value):
        if watch and value.startswith('You'):
            return 'Luigi' + value[3:]
        return value

    def live_board():
        entry = current_entry()
        render(entry, True)

    def controls_ready():
        sync()
        if automated() and not (current_entry() and current_entry()[2]):
            timer.start(450)
            roll.setEnabled(False)

    def announce_turn():
        line = 'My turn!' if cursor % 2 else 'Your turn!'
        speak(line, controls_ready)

    def announce_result(result):
        if result.startswith('You won'):
            line = 'Luigi wins!' if watch else "You're great at this game! You win!"
        elif result.startswith('Mario won'):
            line = 'Woohoo! I won!'
        else:
            line = "It's a tie! Want to try again?"
        speak(line, sync)


    def render(entry, live=False):
        svg = entry[1] if entry else draw_position(starting_position)
        if live and preview_move and prefix:
            route = next((route for route in routes if route[0][:len(prefix)] == prefix), None)
            if route:
                svg = draw_position(preview_move(current_position(), route[2], len(prefix)))
        board.move_markers = ()
        if live and marker and routes:
            targets = {path[len(prefix)] for path, _, _ in routes
                       if len(path) > len(prefix) and path[:len(prefix)] == prefix}
            board.move_markers = tuple((*marker(cell), False) for cell in targets - set(prefix[-1:]))
            if prefix:
                board.move_markers += ((*marker(prefix[-1]), True),)
        board.load(QtCore.QByteArray(svg.encode('utf-8')))
        board.renderer().setAspectRatioMode(QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def review(row):
        if row == cursor - 1:
            live_board()
        else:
            render(moves[row] if 0 <= row < cursor else current_entry())

    def sync():
        nonlocal prefix, routes
        prefix = ()
        entry = moves[cursor - 1] if cursor else None
        winner = entry[2] if entry else None
        message = winner or ("Mario's turn" if cursor % 2 else "Luigi’s turn" if watch else 'Your turn')
        if entry:
            message = f'{entry[0]} · {message}'
        status.setText(message)
        blocker = QtCore.QSignalBlocker(history_list)
        while history_list.count() > cursor:
            history_list.takeItem(history_list.count() - 1)
        for index in range(history_list.count(), cursor):
            description, svg, _, _ = moves[index]
            pixmap = QtGui.QPixmap(96, 96)
            pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode('utf-8')))
            painter = QtGui.QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            item = QtWidgets.QListWidgetItem(QtGui.QIcon(pixmap), f'{index + 1}. {description}')
            history_list.addItem(item)
        history_list.setCurrentRow(cursor - 1)
        history_list.scrollToBottom()
        del blocker
        locked = after_line is not None
        routes = legal_moves(current_position()) if legal_moves and not winner and not automated() and not locked else []
        live_board()
        if board_cells or hit_test:
            board.update_pointer()
        special = next(((label, action) for path, label, action in routes if not path), None)
        show_roll = bool(special or automated() or not (board_cells or hit_test))
        roll.setVisible(show_roll and not winner)
        roll.setText(special[0] if special else 'Resume' if automated() else 'Roll dice')
        roll.setEnabled(not winner and not locked and not timer.isActive())
        undo.setEnabled(cursor > 0 and not locked)
        redo.setEnabled(cursor < len(moves) and not locked)
        proceed.setVisible(winner is not None)
        proceed.setEnabled(not locked)

    def play(action=None):
        nonlocal cursor
        timer.stop()
        position = current_position()
        player = cursor % 2
        if cpu_move and automated():
            action = cpu_move(position, player, difficulty.value())
        move = make_move(position, player, action)
        if move is None:
            return
        description, position, highlight, winner = move
        del moves[cursor:]
        moves.append((watch_text(description), draw_position(position, player, highlight),
                      watch_text(winner) if winner else None, position))
        cursor += 1
        if winner:
            announce_result(winner)
        else:
            announce_turn()
        sync()

    def can_select(cell):
        if cell is None or automated() or after_line or (current_entry() and current_entry()[2]):
            return False
        if legal_moves:
            return any(path[:1] == (cell,) or path[:len(prefix) + 1] == prefix + (cell,)
                       for path, _, _ in routes)
        return board_cells and not current_position()[cell]

    def select_cell(cell):
        nonlocal prefix
        if automated() or after_line or (current_entry() and current_entry()[2]):
            return
        if history_list.currentRow() != cursor - 1:
            sync()
        if not legal_moves:
            play(cell)
            return
        candidate = prefix + (cell,)
        matching = [route for route in routes if route[0][:len(candidate)] == candidate]
        if not matching:
            candidate = (cell,)
            matching = [route for route in routes if route[0][:1] == candidate]
        prefix = candidate if matching else ()
        complete = [route for route in matching if route[0] == prefix]
        if complete and len(complete) == len(matching):
            if len(complete) == 1:
                play(complete[0][2])
                return
            menu = QtWidgets.QMenu(window)
            actions = {menu.addAction(label): action for _, label, action in complete}
            selected = menu.exec(QtGui.QCursor.pos())
            if selected in actions:
                play(actions[selected])
                return
        live_board()

    def primary_action():
        if after_line or (current_entry() and current_entry()[2]):
            return
        if automated() or not (board_cells or hit_test):
            play()
        else:
            special = next((route for route in routes if not route[0]), None)
            if special:
                play(special[2])

    def tick():
        if after_line:
            speech_step()
        elif automated():
            play()

    def seek(delta):
        nonlocal cursor, pending_line, after_line
        target = cursor + delta
        if not 0 <= target <= len(moves):
            return
        timer.stop()
        cursor = target
        pending_line = after_line = None
        sync()

    def reset(watch_mode=None):
        nonlocal cursor, starting_position, watch, pending_line, after_line, draw_position
        if watch_mode is not None:
            watch = watch_mode
        draw_position = draw_watch_board if watch and draw_watch_board else draw_board
        pending_line = after_line = None
        window.setWindowTitle(f'{title} · Mario vs. Luigi' if watch else title)
        help_label.setText('Luigi plays the first side. Mario plays the second.' if watch else instructions)
        starting_position = initial() if callable(initial) else initial
        timer.stop()
        moves.clear()
        cursor = 0
        speak("Let's-a go!", announce_turn)
        sync()

    def replay():
        buttons = QtWidgets.QMessageBox.StandardButton
        answer = QtWidgets.QMessageBox.question(
            window, title, 'Want to try again?', buttons.Yes | buttons.No, buttons.No
        )
        if answer == buttons.Yes:
            reset()
        else:
            window.close()

    def export():
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            window, 'Save move history', title.lower().replace(' ', '-') + '-history.mhtml', 'MHTML files (*.mhtml *.mht)'
        )
        if not path:
            return
        if not path.lower().endswith(('.mhtml', '.mht')):
            path += '.mhtml'
        snapshots = [
            (f'move-{index}', svg, f'Move {index}: {description}')
            for index, (description, svg, _, _) in enumerate(moves[:cursor], 1)
        ] or [('move-0', draw_position(starting_position), 'Starting board')]
        result = (moves[cursor - 1][2] if cursor else None) or 'Game in progress.'
        try:
            saved = write_move_history(path, title, result, snapshots)
            status.setText(f'Saved history: {saved.name} · {result}')
        except OSError as exc:
            QtWidgets.QMessageBox.critical(window, title, f'Could not save the move history.\n{exc}')

    roll.clicked.connect(primary_action)
    timer.timeout.connect(tick)
    undo.clicked.connect(lambda: seek(-1))
    redo.clicked.connect(lambda: seek(1))
    save.clicked.connect(export)
    proceed.clicked.connect(replay)
    history_list.currentRowChanged.connect(review)
    QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Undo, window).activated.connect(undo.click)
    QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Redo, window).activated.connect(redo.click)
    window.reset_game = reset
    return window
