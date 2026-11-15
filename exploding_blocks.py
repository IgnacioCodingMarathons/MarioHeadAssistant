import random
import queue
import json
import os
import struct
import zlib
import xml.etree.ElementTree as ET
from game_difficulty import add_difficulty_slider
from game_history import write_move_history

TITLE = "Mario's Exploding Blocks"
ACTION = 'exploding_blocks'
PRAISE = ("Good!", "Great!", "Great move!", "Wonderful!", "Spectacular!", "Perfect!",
          r"\showas=Let's-a\Let's \hidden\a go!")
PLACEMENT_POINTS = (16, 26, 32, 42, 56, 64, 128)
CLUTCH_LINES = ("Clutch!", "That was a close one!")
RESUME_LINE = "You have an autosave available! Do you want to continue?"
GAME_LINES = (TITLE + "!", RESUME_LINE, "Boom!", *CLUTCH_LINES, *PRAISE, "All clear!",
              "You ran out of space! Wanna try again?")
SIZE = 10
POINTS_PER_LINE = 16000
PIECE_MIME = 'application/x-marioassistant-block-piece'
SHAPES = (
    ('#45cada', ('####',)), ('#45cada', ('#', '#', '#', '#')),
    ('#f7c95c', ('##', '##')), ('#b894ff', ('.#.', '###')),
    ('#b894ff', ('#.', '##', '#.')), ('#f29259', ('#..', '###')),
    ('#f29259', ('##', '#.', '#.')), ('#79a8ff', ('..#', '###')),
    ('#60d59b', ('.##', '##.')), ('#eb758c', ('##.', '.##')),
    ('#f7c95c', ('###',)), ('#f29259', ('#.', '##')),
    ('#79a8ff', ('##',)), ('#60d59b', ('#',)),
)
PIECES = tuple((color, tuple((x, y) for y, row in enumerate(rows)
                             for x, cell in enumerate(row) if cell == '#'))
               for color, rows in SHAPES)


def placement(board, piece, origin):
    color, shape = piece
    column, row = origin
    added = {(column + x, row + y) for x, y in shape}
    if any(not (0 <= x < SIZE and 0 <= y < SIZE) for x, y in added) or added & board.keys():
        return None
    filled = board.keys() | added
    rows = [y for y in {y for _, y in added} if all((x, y) in filled for x in range(SIZE))]
    columns = [x for x in {x for x, _ in added} if all((x, y) in filled for y in range(SIZE))]
    cleared = {(x, y) for x, y in filled if x in columns or y in rows}
    return added, cleared, len(rows) + len(columns)



SAVE_MAGIC = b'MAEB\x02'


def snapshot_svg(pixels):
    if len(pixels) != SIZE * SIZE * 4:
        raise ValueError('Invalid history grid')
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 480">']
    for index in range(SIZE * SIZE):
        offset = index * 4
        color = '#' + pixels[offset:offset + 3].hex()
        stroke = '#ffffff' if pixels[offset + 3] else '#344868'
        x, y = index % SIZE, index // SIZE
        parts.append(f'<rect x="{x*48+1}" y="{y*48+1}" width="46" height="46" fill="{color}" stroke="{stroke}" stroke-width="2"/>')
    return ''.join(parts) + '</svg>'


def legacy_snapshot(svg):
    pixels = bytearray(SIZE * SIZE * 4)
    root = ET.fromstring(svg)
    cells = set()
    for rect in root.findall('{http://www.w3.org/2000/svg}rect'):
        x, y = int(rect.attrib['x']) // 48, int(rect.attrib['y']) // 48
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            raise ValueError('Invalid history square')
        offset = (y * SIZE + x) * 4
        pixels[offset:offset + 3] = bytes.fromhex(rect.attrib['fill'].removeprefix('#'))
        pixels[offset + 3] = rect.attrib.get('stroke') == '#ffffff'
        cells.add((x, y))
    if len(cells) != SIZE * SIZE or len(pixels) != SIZE * SIZE * 4:
        raise ValueError('Incomplete history grid')
    return bytes(pixels)


def encode_save(data):
    metadata = dict(data, version=2, history=[(key, description) for key, _, description in data['history']])
    header = json.dumps(metadata, separators=(',', ':')).encode('utf-8')
    grids = b''.join(pixels for _, pixels, _ in data['history'])
    return SAVE_MAGIC + zlib.compress(struct.pack('<I', len(header)) + header + grids)


def decode_save(raw):
    if not raw.startswith(SAVE_MAGIC):
        data = json.loads(raw)
        data['history'] = [(key, legacy_snapshot(svg), description) for key, svg, description in data['history']]
        return data
    try:
        payload = zlib.decompress(raw[len(SAVE_MAGIC):])
        length, = struct.unpack_from('<I', payload)
        data = json.loads(payload[4:4 + length])
        grids = payload[4 + length:]
        size = SIZE * SIZE * 4
        if len(grids) != len(data['history']) * size:
            raise ValueError('Invalid history length')
        data['history'] = [(key, grids[index * size:(index + 1) * size], description)
                           for index, (key, description) in enumerate(data['history'])]
        return data
    except (zlib.error, struct.error) as error:
        raise ValueError('Invalid compressed autosave') from error


def create_exploding_blocks_window(QtCore, QtWidgets, on_close, play_clear_sound, say, speech_busy, start_music, stop_music, prepare_game, award_coins, save_path, get_high_score, save_high_score):
    from PySide6 import QtGui

    occupied = {}
    hand = []
    dragging = None
    hover = None
    burst = set()
    score = 0
    turn = 0
    sounds_left = 0
    stage = 1
    snapshots = []
    move_states = []
    move_cursor = 0
    speech_queue = []
    speech_active = False
    intro_complete = False
    intro_speaking = False
    stage_reward_pending = False
    ending = False
    reward_busy = None
    loading = None
    resume_pending = False
    background = QtGui.QColor('#182339')
    rainbow = tuple(QtGui.QColor.fromHsv(30 * index, 190, 255) for index in range(12))

    class Board(QtWidgets.QWidget):
        def metrics(self):
            side = min(self.width(), self.height()) - 8
            return (self.width() - side) / 2, (self.height() - side) / 2, side / SIZE

        def origin_at(self, position):
            if dragging is None:
                return None
            left, top, step = self.metrics()
            x, y = (position.x() - left) / step, (position.y() - top) / step
            if 0 <= x < SIZE and 0 <= y < SIZE:
                _, shape = hand[dragging]
                width = 1 + max(dx for dx, _ in shape)
                height = 1 + max(dy for _, dy in shape)
                return (min(SIZE - width, max(0, int(x - (width - 1) / 2))),
                        min(SIZE - height, max(0, int(y - (height - 1) / 2))))
            return None

        def track_drag(self, event):
            nonlocal hover
            if dragging is not None and event.mimeData().hasFormat(PIECE_MIME):
                hover = self.origin_at(event.position())
                self.update()
                event.acceptProposedAction()

        dragEnterEvent = track_drag
        dragMoveEvent = track_drag

        def dragLeaveEvent(self, event):
            nonlocal hover
            hover = None
            self.update()

        def dropEvent(self, event):
            if dragging is None or not event.mimeData().hasFormat(PIECE_MIME):
                return
            cell = self.origin_at(event.position())
            candidate = preview(cell)
            if candidate is not None:
                event.acceptProposedAction()
                drop(candidate)

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            left, top, step = self.metrics()
            candidate = preview(hover)
            ghost, projected, _ = candidate or (set(), set(), 0)
            for y in range(SIZE):
                for x in range(SIZE):
                    cell = x, y
                    if cell in burst or cell in projected:
                        color = rainbow[(x + 3 * y) % len(rainbow)]
                    elif cell in ghost:
                        color = QtGui.QColor(hand[dragging][0])
                        color.setAlpha(165)
                    else:
                        color = QtGui.QColor(occupied[cell]) if cell in occupied else background
                    tile = QtCore.QRectF(left + x * step + 1, top + y * step + 1, step - 2, step - 2)
                    painter.setBrush(color)
                    painter.drawRoundedRect(tile, 4, 4)
                    if cell in burst:
                        painter.setBrush(QtGui.QColor('#ffffff'))
                        painter.drawEllipse(tile.center(), step * .12, step * .12)
            painter.end()

    class PieceButton(QtWidgets.QPushButton):
        def __init__(self, slot):
            super().__init__()
            self.slot = slot
            self.press_position = None

        def mousePressEvent(self, event):
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self.press_position = event.position()
            super().mousePressEvent(event)

        def mouseReleaseEvent(self, event):
            self.press_position = None
            super().mouseReleaseEvent(event)

        def mouseMoveEvent(self, event):
            if (self.press_position is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton
                    and (event.position() - self.press_position).manhattanLength()
                    >= QtWidgets.QApplication.startDragDistance()):
                self.press_position = None
                self.setDown(False)
                drag_piece(self)
            else:
                super().mouseMoveEvent(event)

    class GameWindow(QtWidgets.QWidget):
        def hideEvent(self, event):
            dialogue_timer.stop()
            end_timer.stop()
            loading_timer.stop()
            if loading is not None:
                loading[1].set()
            stop_music(False)
            explosion_timer.stop()
            sound_timer.stop()
            super().hideEvent(event)

        def closeEvent(self, event):
            on_close()
            super().closeEvent(event)

    window = GameWindow()
    window.setWindowTitle(TITLE)
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.setObjectName('explodingBlocks')
    window.setStyleSheet("""
        QWidget#explodingBlocks { background: #0d1527; }
        QLabel { color: #eef4ff; }
        QPushButton { color: #eef4ff; background: #182339; border: 2px solid #344868;
                      border-radius: 9px; padding: 5px 12px; }
        QPushButton:hover:enabled { border-color: #79a8ff; }
        QPushButton:disabled { color: #8494ac; }
    """)
    layout = QtWidgets.QVBoxLayout(window)
    header = QtWidgets.QHBoxLayout()
    layout.addLayout(header)
    scoreboard = QtWidgets.QLabel()
    font = scoreboard.font()
    font.setPointSizeF(font.pointSizeF() * 1.25)
    font.setBold(True)
    scoreboard.setFont(font)
    header.addWidget(scoreboard)
    header.addStretch()
    restart = QtWidgets.QPushButton('New game')
    header.addWidget(restart)
    high_score_label = QtWidgets.QLabel()
    layout.addWidget(high_score_label)
    difficulty = add_difficulty_slider(QtCore, QtWidgets, layout)
    content = QtWidgets.QHBoxLayout()
    layout.addLayout(content, 1)
    board = Board()
    board.setMinimumSize(300, 300)
    board.setAcceptDrops(True)
    content.addWidget(board, 3)
    sidebar = QtWidgets.QVBoxLayout()
    content.addLayout(sidebar, 1)
    history = QtWidgets.QListWidget()
    history.setMaximumWidth(210)
    history.setWordWrap(True)
    sidebar.addWidget(history)
    save = QtWidgets.QPushButton("Save report…")
    sidebar.addWidget(save)
    move_controls = QtWidgets.QHBoxLayout()
    sidebar.addLayout(move_controls)
    undo = QtWidgets.QPushButton('Undo')
    redo = QtWidgets.QPushButton('Redo')
    move_controls.addWidget(undo)
    move_controls.addWidget(redo)
    status = QtWidgets.QLabel()
    status.setWordWrap(True)
    layout.addWidget(status)
    progress = QtWidgets.QProgressBar()
    layout.addWidget(progress)
    rack = QtWidgets.QHBoxLayout()
    layout.addLayout(rack)
    buttons = []
    for index in range(3):
        button = PieceButton(index)
        button.setMinimumHeight(62)
        button.setIconSize(QtCore.QSize(82, 46))
        button.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        rack.addWidget(button, 1)
        buttons.append(button)
    restart.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    explosion_timer = QtCore.QTimer(window)
    explosion_timer.setSingleShot(True)
    sound_timer = QtCore.QTimer(window)
    sound_timer.setInterval(170)

    def piece_icon(piece):
        color, shape = piece
        pixmap = QtGui.QPixmap(82, 46)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(color))
        left = (82 - 11 * (1 + max(x for x, _ in shape))) / 2
        top = (46 - 11 * (1 + max(y for _, y in shape))) / 2
        for x, y in shape:
            painter.drawRoundedRect(QtCore.QRectF(left + x * 11, top + y * 11, 10, 10), 2, 2)
        painter.end()
        return QtGui.QIcon(pixmap)

    dialogue_timer = QtCore.QTimer(window)
    dialogue_timer.setInterval(80)
    loading_timer = QtCore.QTimer(window)
    loading_timer.setInterval(60)
    end_timer = QtCore.QTimer(window)
    end_timer.setSingleShot(True)

    def deal():
        nonlocal hand, turn
        weights = [(1 + (difficulty.value() - 1) / 4) if len(shape) == 4
                   else (1 + (20 - difficulty.value()) / 4) for _, shape in PIECES]
        hand = [(QtGui.QColor.fromHsv((stage * 47 + index * 75) % 360, 155, 245).name(), shape)
                for index, (_, shape) in enumerate(random.choices(PIECES, weights=weights, k=3))]
        turn += 1

    def preview(cell):
        if cell is not None and dragging is not None and not burst:
            return placement(occupied, hand[dragging], cell)
        return None

    def drag_piece(button):
        nonlocal dragging, hover
        dragging = button.slot
        mime = QtCore.QMimeData()
        mime.setData(PIECE_MIME, QtCore.QByteArray())
        drag = QtGui.QDrag(button)
        drag.setMimeData(mime)
        drag.setPixmap(button.icon().pixmap(button.iconSize()))
        drag.setHotSpot(QtCore.QPoint(41, -10))
        try:
            drag.exec(QtCore.Qt.DropAction.MoveAction)
        finally:
            dragging = hover = None
            board.update()

    def update_hand():
        best = get_high_score()
        if score > best:
            try:
                save_high_score(score)
                best = score
            except OSError as error:
                QtWidgets.QMessageBox.warning(window, 'High score', f'Could not save the high score. {error}')
        high_score_label.setText(f'High score: {best:,} points')
        fits = [index for index, piece in enumerate(hand) if piece and
                any(placement(occupied, piece, (x, y)) is not None for y in range(SIZE) for x in range(SIZE))]
        blocked = not fits
        nonlocal ending
        if blocked and not ending:
            ending = True
            save_path.unlink(missing_ok=True)
            speech_queue.clear()
            restart.setEnabled(False)
            stop_music(True)
            end_timer.start(800)
        for index, (button, piece) in enumerate(zip(buttons, hand)):
            button.setIcon(piece_icon(piece) if piece else QtGui.QIcon())
            button.setAccessibleName(f'Piece {index + 1}' if piece else f'Piece {index + 1} placed')
            button.setEnabled(index in fits and intro_complete and not burst and not ending)
        scoreboard.setText(f'{score:,} points · Stage {stage} · Turn {turn}')
        status.setText('No pieces fit. Start a new game to try again.' if blocked else
                       'Drag a piece from the tray to the grid. Release to place it.')
        board.update()

    def play_next_sound():
        nonlocal sounds_left
        if sounds_left:
            play_clear_sound()
            sounds_left -= 1
        if not sounds_left:
            sound_timer.stop()

    def next_piece():
        burst.clear()
        if not any(hand):
            deal()
        update_hand()

    def drop(candidate):
        nonlocal score, sounds_left, dragging, hover, stage, background
        available_before = sum(
            any(placement(occupied, piece, (x, y)) is not None
                for y in range(SIZE) for x in range(SIZE))
            for piece in hand if piece
        )
        added, cleared, lines = candidate
        occupied.update(dict.fromkeys(added, hand[dragging][0]))
        hand[dragging] = None
        dragging = hover = None
        rank = min(lines, len(PRAISE) - 1)
        praise = PRAISE[rank]
        score += PLACEMENT_POINTS[rank]
        if lines:
            speech_queue.append("Boom!")
        if lines:
            for square in cleared:
                del occupied[square]
            burst.update(cleared)
            remaining_playable = sum(
                any(placement(occupied, piece, (x, y)) is not None
                    for y in range(SIZE) for x in range(SIZE))
                for index, piece in enumerate(hand) if piece and index != dragging
            )
            if available_before <= 1 and remaining_playable >= 2:
                speech_queue.append(random.choice(CLUTCH_LINES))
            gained = lines * POINTS_PER_LINE
            score += gained
            scoreboard.setText(f'{score:,} points · Stage {stage} · Turn {turn}')
            status.setText(f'{lines} {"line" if lines == 1 else "lines"} exploded · +{gained:,} points')
            for button in buttons:
                button.setEnabled(False)
            sounds_left += lines
            if not sound_timer.isActive():
                play_next_sound()
                if sounds_left:
                    sound_timer.start()
            explosion_timer.start(340)
            board.update()
        else:
            next_piece()
        speech_queue.append(praise)
        new_stage = score // 100000 + 1
        if new_stage > stage:
            speech_queue.extend(['All clear!'] * (new_stage - stage))
            stage = new_stage
            background = QtGui.QColor.fromHsv(stage * 47 % 360, 130, 55)
            for cell in occupied:
                occupied[cell] = QtGui.QColor.fromHsv(stage * 47 % 360, 155, 245).name()
            hand[:] = [(QtGui.QColor.fromHsv((stage * 47 + i * 75) % 360, 155, 245).name(), piece[1])
                       if piece else None for i, piece in enumerate(hand)]
        record_move(added, cleared, lines)
        if not any(hand):
            deal()
        update_hand()
        if not ending:
            autosave()
        scoreboard.setText(f'{score:,} points · Stage {stage} · Turn {turn}')
        push_state()

    def queue_end():
        nonlocal reward_busy
        if not ending:
            return
        reward_busy = award_coins(score // 3)
        status.setText(f'You earned {score // 3:,} MarioCoins.')

    def finish_reward():
        nonlocal reward_busy
        if reward_busy is not None and not reward_busy():
            reward_busy = None
            speech_queue.append("You ran out of space! Wanna try again?")
            restart.setEnabled(True)

    def speak_next():
        nonlocal speech_active, stage_reward_pending, intro_complete, intro_speaking
        finish_reward()
        if speech_busy():
            return
        if stage_reward_pending:
            stage_reward_pending = False
            award_coins(100)
        if speech_queue:
            speech_active = True
            line = speech_queue.pop(0)
            intro_speaking = line in (TITLE + "!", RESUME_LINE)
            stage_reward_pending = line == 'All clear!'
            say(line)
        elif speech_active:
            speech_active = False
            if intro_speaking:
                intro_complete = True
                intro_speaking = False
                update_hand()
            if resume_pending:
                ask_resume()
                return
            if turn == 1 and not snapshots:
                start_music()
            if not ending and not burst:
                update_hand()

    def record_move(added, cleared, lines):
        pixels = bytearray()
        for y in range(SIZE):
            for x in range(SIZE):
                cell = x, y
                color = occupied.get(cell, background.name())
                if cell in cleared:
                    color = rainbow[(x + 3 * y) % len(rainbow)].name()
                pixels.extend(bytes.fromhex(color.removeprefix('#')))
                pixels.append(int(cell in added))
        bonus = PLACEMENT_POINTS[min(lines, len(PLACEMENT_POINTS) - 1)]
        description = f'Move {len(snapshots)+1} · Stage {stage} · {lines} lines · +{bonus} placement points · {score:,} points · Difficulty {difficulty.value()}'
        snapshots.append((f'move-{len(snapshots)+1}', bytes(pixels), description))
        history.addItem(description)
        history.scrollToBottom()

    def capture_state():
        return {
            'occupied': dict(occupied),
            'hand': [None if piece is None else (piece[0], tuple(piece[1])) for piece in hand],
            'score': score, 'stage': stage, 'turn': turn,
            'snapshots': list(snapshots),
        }

    def push_state():
        nonlocal move_cursor
        del move_states[move_cursor + 1:]
        move_states.append(capture_state())
        move_cursor = len(move_states) - 1
        undo.setEnabled(move_cursor > 0 and not ending)
        redo.setEnabled(move_cursor + 1 < len(move_states) and not ending)

    def restore_state(state, cursor):
        nonlocal hand, score, stage, turn, snapshots, background, move_cursor, speech_active
        dialogue_timer.stop()
        speech_queue.clear()
        speech_active = False
        burst.clear()
        occupied.clear()
        occupied.update(state['occupied'])
        hand = [None if piece is None else (piece[0], tuple(piece[1])) for piece in state['hand']]
        score, stage, turn = state['score'], state['stage'], state['turn']
        snapshots[:] = state['snapshots']
        history.clear()
        history.addItems([entry[2] for entry in snapshots])
        background = QtGui.QColor('#182339') if stage == 1 else QtGui.QColor.fromHsv(stage * 47 % 360, 130, 55)
        move_cursor = cursor
        update_hand()
        autosave()
        dialogue_timer.start()
        undo.setEnabled(move_cursor > 0)
        redo.setEnabled(move_cursor + 1 < len(move_states))

    def undo_move():
        nonlocal move_cursor
        if move_cursor:
            move_cursor -= 1
            restore_state(move_states[move_cursor], move_cursor)

    def redo_move():
        nonlocal move_cursor
        if move_cursor + 1 < len(move_states):
            move_cursor += 1
            restore_state(move_states[move_cursor], move_cursor)

    def review(item):
        from PySide6 import QtSvgWidgets
        index = history.row(item)
        dialog = QtWidgets.QDialog(window)
        dialog.setWindowTitle(snapshots[index][2])
        panel = QtWidgets.QVBoxLayout(dialog)
        image = QtSvgWidgets.QSvgWidget()
        image.load(QtCore.QByteArray(snapshot_svg(snapshots[index][1]).encode()))
        panel.addWidget(image)
        dialog.resize(440, 440)
        dialog.show()

    def export():
        path, _ = QtWidgets.QFileDialog.getSaveFileName(window, 'Save report', 'exploding-blocks.mhtml', 'MHTML (*.mhtml)')
        if path:
            if not path.lower().endswith(('.mhtml', '.mht')):
                path += '.mhtml'
            try:
                write_move_history(path, TITLE, f'{score:,} points · Stage {stage}',
                                   [(key, snapshot_svg(pixels), description) for key, pixels, description in snapshots])
            except OSError as error:
                QtWidgets.QMessageBox.warning(window, 'Save report', str(error))

    def autosave():
        data = {
            "version": 1, "history": snapshots, "blocks": hand,
            "points": score, "stage": stage, "turn": turn, "move": len(snapshots),
            "difficulty": difficulty.value(),
            "grid": [[x, y, color] for (x, y), color in occupied.items()],
        }
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = save_path.with_suffix('.win.tmp')
            with temporary.open('wb') as stream:
                stream.write(encode_save(data))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(save_path)
        except OSError as error:
            QtWidgets.QMessageBox.warning(window, 'Autosave', str(error))

    def restore():
        nonlocal hand, score, stage, turn, background
        data = decode_save(save_path.read_bytes())
        grid = {(int(x), int(y)): color for x, y, color in data['grid']}
        pieces = [(color, tuple(tuple(cell) for cell in shape)) if item else None
                  for item in data['blocks']
                  for color, shape in (item if item else (None, ()),)]
        if (len(pieces) != 3 or any(not (0 <= x < SIZE and 0 <= y < SIZE) for x, y in grid)
                or any(not isinstance(data[key], int) or data[key] < minimum
                       for key, minimum in (('points', 0), ('stage', 1), ('turn', 1), ('difficulty', 1)))
                or data['difficulty'] > 20 or data['move'] != len(data['history'])):
            raise ValueError('Invalid autosave data')
        for piece in pieces:
            if piece and (not piece[1] or any(not (0 <= x < SIZE and 0 <= y < SIZE) for x, y in piece[1])):
                raise ValueError('Invalid saved block')
        occupied.clear()
        occupied.update(grid)
        hand = pieces
        score, stage, turn = data['points'], data['stage'], data['turn']
        difficulty.setValue(data['difficulty'])
        snapshots[:] = data['history']
        history.clear()
        history.addItems([entry[2] for entry in snapshots])
        background = QtGui.QColor('#182339') if stage == 1 else QtGui.QColor.fromHsv(stage * 47 % 360, 130, 55)
        update_hand()
        if not ending:
            autosave()
            start_music()

    def ask_resume():
        nonlocal resume_pending
        choices = QtWidgets.QMessageBox.StandardButton
        while True:
            answer = QtWidgets.QMessageBox.question(window, 'Hey!', 'You wanna pick up where you left off?',
                                                    choices.Yes | choices.No, choices.Yes)
            if answer == choices.Yes:
                try:
                    restore()
                except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
                    QtWidgets.QMessageBox.warning(window, 'Autosave', f'Could not load your game. {error}')
                    window.close()
                resume_pending = False
                return
            answer = QtWidgets.QMessageBox.question(window, 'Hey!', 'Are ya sure? This will delete your old autosave!',
                                                    choices.Yes | choices.No, choices.No)
            if answer == choices.Yes:
                try:
                    save_path.unlink(missing_ok=True)
                except OSError as error:
                    QtWidgets.QMessageBox.warning(window, 'Autosave', str(error))
                    return
                resume_pending = False
                reset()
                return

    def reset():
        nonlocal dragging, hover, score, turn, sounds_left, stage, background, ending, speech_active, loading, reward_busy, intro_complete, intro_speaking, move_states, move_cursor
        nonlocal stage_reward_pending, resume_pending
        stage_reward_pending = False
        end_timer.stop()
        reward_busy = None
        if loading is not None:
            loading[1].set()
        dialogue_timer.stop()
        explosion_timer.stop()
        sound_timer.stop()
        stop_music(False)
        stage = 1
        background = QtGui.QColor("#182339")
        ending = speech_active = False
        intro_complete = intro_speaking = False
        snapshots.clear()
        history.clear()
        resume_pending = save_path.exists()
        speech_queue[:] = [RESUME_LINE if resume_pending else TITLE + "!"]
        occupied.clear()
        burst.clear()
        dragging = hover = None
        score = turn = sounds_left = 0
        deal()
        update_hand()
        move_states = [capture_state()]
        move_cursor = 0
        undo.setEnabled(False)
        redo.setEnabled(False)
        restart.setEnabled(False)
        status.setText('Loading SELECT.MID…')
        progress.setRange(0, 0)
        progress.show()
        loading = prepare_game(GAME_LINES)
        loading_timer.start()

    def check_loading():
        nonlocal loading
        try:
            completed, total, message = loading[0].get_nowait()
        except queue.Empty:
            return
        status.setText(message)
        progress.setRange(0, total)
        progress.setValue(completed)
        if completed == total:
            loading_timer.stop()
            loading = None
            progress.hide()
            restart.setEnabled(True)
            dialogue_timer.start()

    loading_timer.timeout.connect(check_loading)
    end_timer.timeout.connect(queue_end)
    dialogue_timer.timeout.connect(speak_next)
    history.itemDoubleClicked.connect(review)
    save.clicked.connect(export)
    undo.clicked.connect(undo_move)
    redo.clicked.connect(redo_move)
    explosion_timer.timeout.connect(next_piece)
    sound_timer.timeout.connect(play_next_sound)
    restart.clicked.connect(reset)
    window.reset_game = reset
    window.resize(740, 590)
    return window
