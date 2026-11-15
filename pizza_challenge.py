import math
import json
import pickle
import queue
import random
import zlib
from html import escape
from pathlib import Path

from game_difficulty import add_difficulty_slider


TITLE = "Mario's Pizza Pie Challenge"
ACTION = 'pizza_challenge'
CATEGORIES = (
    ('Crust', ('Thin', 'Classic', 'Deep dish')),
    ('Sauce', ('Tomato', 'White garlic', 'Pesto')),
    ('Cheese', ('Mozzarella', 'Provolone', 'No cheese')),
)
TOPPINGS = ('Pepperoni', 'Mushrooms', 'Olives', 'Peppers', 'Onions', 'Basil', 'Pineapple', 'Ham')
SPREAD_RADIUS = 26
SPREAD_STEP = 11
SPREAD_TARGET = 55
SPREAD_AREA_RADIUS = 108
SAUCE_SAMPLE_POINTS = tuple((x, y) for x in range(-100, 101, 10) for y in range(-100, 101, 10)
                            if x * x + y * y <= SPREAD_AREA_RADIUS ** 2)
INGREDIENT_COLORS = {
    'Thin': '#dca562', 'Classic': '#eabd75', 'Deep dish': '#b87739',
    'Tomato': '#c94932', 'White garlic': '#f1ebc5', 'Pesto': '#638949',
    'Mozzarella': '#fff0b4', 'Provolone': '#f2cb72', 'No cheese': '#70594d',
    'Pepperoni': '#a52628', 'Mushrooms': '#a38163', 'Olives': '#493e36',
    'Peppers': '#5b9535', 'Onions': '#b8749f', 'Basil': '#287540',
    'Pineapple': '#f0b936', 'Ham': '#df8391',
}


def failure_limit(level):
    return 5 * level if level <= 5 else 25 + 4 * (level - 5)


def ingredient_deductions(name, points, target, spread, budget, strictness=1):
    if spread:
        coverage = sum(
            any((x - px) ** 2 + (y - py) ** 2 <= SPREAD_RADIUS ** 2 for px, py in points)
            for x, y in SAUCE_SAMPLE_POINTS
        ) / len(SAUCE_SAMPLE_POINTS)
        target_coverage = sum(
            any((x - px) ** 2 + (y - py) ** 2 <= SPREAD_RADIUS ** 2 for px, py in target)
            for x, y in SAUCE_SAMPLE_POINTS
        ) / len(SAUCE_SAMPLE_POINTS)
        minimum_coverage = target_coverage * (.62 + .05 * max(0, strictness - 1))
        if coverage < minimum_coverage:
            yield f'{name} could cover more of the pizza', round(
                budget * min(1, (minimum_coverage - coverage) / max(minimum_coverage, .01))
            )
        return

    count = len(points)
    expected = len(target)
    minimum = expected * ((.55 if spread else .7) + .15 * (strictness - 1))
    maximum = expected * (1 + (.65 if spread else .35) / strictness)
    label = name.lower()
    if count < minimum:
        yield f'Too little {label}', round(budget * .55 * (1 - count / minimum))
    elif count > maximum:
        yield f'Too much {label}', round(budget * .55 * min(1, (count - maximum) / maximum))

    if count >= (12 if spread else 3):
        sectors = [0] * (8 if spread else 4)
        for x, y in points:
            sector = int((math.atan2(y, x) % math.tau) / math.tau * len(sectors))
            sectors[sector % len(sectors)] += 1
        imbalance = sum(abs(amount / count - 1 / len(sectors)) for amount in sectors) / 2
        stacking = 1 - len({(round(x / 12), round(y / 12)) for x, y in points}) / count
        uneven = max(imbalance - .24 / strictness, stacking - (.55 if spread else .27) / strictness, 0)
        yield f'{name} could be spread more evenly', round(budget * .65 * uneven)

    edge = sum(math.hypot(x, y) > 90 for x, y in points) / count
    center = sum(math.hypot(x, y) < 42 for x, y in points) / count
    edge_crowding = max(edge - .2 / strictness, 0)
    center_crowding = max(center - .5 / strictness, 0)
    location = 'crust' if edge_crowding > center_crowding else 'center'
    yield f'{name} is crowded near the {location}', round(budget * .5 * max(edge_crowding, center_crowding))


def grade_pizza(order_base, order_toppings, base, placements, preview, level=5):
    strictness = 1 + (level - 5) / (10 if level < 5 else 15)
    feedback = []
    for (category, _), expected, actual in zip(CATEGORIES, order_base, base):
        if expected == actual:
            feedback.append((f'Correct {category.lower()} ({expected.lower()})', 12))
        else:
            detail = f'got {actual.lower()}' if actual else 'missing'
            feedback.append((f'Wanted {expected.lower()} for {category.lower()} ({detail})', -12))

    expected_toppings = [name for name in TOPPINGS if name in order_toppings]
    count = len(expected_toppings)
    for index, name in enumerate(expected_toppings):
        if placements.get(name):
            points = round(19 * (index + 1) / count) - round(19 * index / count)
            feedback.append((f'Correct topping ({name.lower()})', points))
        else:
            points = round(9 * (index + 1) / count) - round(9 * index / count)
            feedback.append((f'Missing {name.lower()}', -points))
    for name in TOPPINGS:
        if name not in order_toppings and placements.get(name):
            feedback.append((f'Unrequested {name.lower()}', -3))

    for name, target in preview.items():
        points = placements.get(name)
        if points:
            spread = name not in TOPPINGS
            budget = 10 if spread else max(5, round(24 / count))
            feedback.extend((reason, -cost) for reason, cost in
                            ingredient_deductions(name, points, target, spread, budget, strictness) if cost)

    feedback = [(reason, points if points > 0 else round(points * strictness))
                for reason, points in feedback]
    score = max(0, 45 + sum(points for _, points in feedback))
    return score, feedback


def create_pizza_window(QtCore, QtWidgets, on_close, say, say_uncached, get_high_score, save_high_score, speech_busy, prepare_line, play_line,
                        load_music, start_music, stop_music, start_meal, cancel_meal, autosave_path=None):
    from PySide6 import QtGui

    class PizzaWindow(QtWidgets.QWidget):
        def hideEvent(self, event):
            receipt.hide()
            cancel_meal()
            speech_timer.stop()
            oven_timer.stop()
            cancel_preparation()
            stop_music()
            super().hideEvent(event)

        def closeEvent(self, event):
            cancel_meal()
            speech_timer.stop()
            oven_timer.stop()
            preparation_timer.stop()
            cancel_preparation()
            stop_music()
            if not served[0]:
                persist()
            restored[0] = False
            on_close()
            super().closeEvent(event)

    def topping_shape(painter, name, x, y, size):
        painter.save()
        painter.translate(x, y)
        painter.setBrush(QtGui.QColor(INGREDIENT_COLORS[name]))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        if name in ('Pepperoni', 'Olives'):
            painter.drawEllipse(QtCore.QRectF(-size, -size, 2 * size, 2 * size))
            if name == 'Olives':
                painter.setBrush(QtGui.QColor('#eac682'))
                painter.drawEllipse(QtCore.QRectF(-size / 2, -size / 2, size, size))
        elif name == 'Mushrooms':
            painter.drawChord(QtCore.QRectF(-size, -size, 2 * size, 1.6 * size), 0, 180 * 16)
            painter.drawRoundedRect(QtCore.QRectF(-size / 3, -size / 4, 2 * size / 3, size), 2, 2)
        elif name in ('Peppers', 'Onions'):
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor(INGREDIENT_COLORS[name]), size / 2))
            painter.drawArc(QtCore.QRectF(-size, -size, 2 * size, 2 * size), 24 * 16, 260 * 16)
        else:
            if name == 'Basil':
                painter.rotate(-30)
                painter.drawEllipse(QtCore.QRectF(-size, -size / 2, 2 * size, size))
            else:
                painter.rotate(25)
                painter.drawRoundedRect(QtCore.QRectF(-size, -size * .65, 2 * size, size * 1.3), 3, 3)
        painter.restore()

    def spread_ingredient(name):
        return name in ('Tomato', 'White garlic', 'Pesto', 'Mozzarella', 'Provolone')

    def sample_order(base_order, topping_order):
        samples = {}
        for name in (*base_order[1:], *(name for name in TOPPINGS if name in topping_order)):
            if name == 'No cheese':
                continue
            spread = spread_ingredient(name)
            count = SPREAD_TARGET if spread else 7
            offset = 0 if spread else TOPPINGS.index(name)
            samples[name] = [
                (math.cos(index * 2.39996 + offset) * radius,
                 math.sin(index * 2.39996 + offset) * radius)
                for index in range(count)
                for radius in (88 * math.sqrt((index + .5) / count),)
            ]
        return samples

    class Pie(QtWidgets.QWidget):
        def __init__(self, preview=False):
            super().__init__()
            self.preview = preview
            self.last_point = None
            if not preview:
                self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
                self.setAccessibleName('Pizza work surface')

        def pizza_point(self, event):
            scale = 300 / min(self.width(), self.height())
            return ((event.position().x() - self.width() / 2) * scale,
                    (event.position().y() - self.height() / 2) * scale)

        def apply_at(self, point):
            name = active_ingredient
            if math.hypot(*point) > 96:
                return
            points = placements.setdefault(name, [])
            if len(points) >= 1600:
                return
            points.append(point)
            if spread_ingredient(name):
                slot = 1 if name in CATEGORIES[1][1] else 2
                if base[slot] and base[slot] != name:
                    placements.pop(base[slot], None)
                base[slot] = name

        def mousePressEvent(self, event):
            if (self.preview or not station.isEnabled() or not active_ingredient
                    or event.button() != QtCore.Qt.MouseButton.LeftButton):
                return
            point = self.pizza_point(event)
            self.last_point = point if spread_ingredient(active_ingredient) else None
            self.apply_at(point)
            self.update()

        def mouseMoveEvent(self, event):
            if self.last_point is None or not station.isEnabled():
                return
            if not event.buttons() & QtCore.Qt.MouseButton.LeftButton:
                self.last_point = None
                return
            point = self.pizza_point(event)
            x, y = self.last_point
            dx, dy = point[0] - x, point[1] - y
            distance = math.hypot(dx, dy)
            steps = int(distance / SPREAD_STEP)
            if steps:
                for index in range(1, steps + 1):
                    fraction = SPREAD_STEP * index / distance
                    self.apply_at((x + dx * fraction, y + dy * fraction))
                fraction = SPREAD_STEP * steps / distance
                self.last_point = (x + dx * fraction, y + dy * fraction)
                self.update()

        def mouseReleaseEvent(self, event):
            self.last_point = None

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            size = min(self.width(), self.height())
            painter.translate(self.width() / 2, self.height() / 2)
            painter.scale(size / 300, size / 300)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            layers = order_base if self.preview else base
            ingredients = order_preview if self.preview else placements
            cooked = self.preview or baked

            def disk(radius, color, offset=0):
                painter.setBrush(QtGui.QColor(color))
                painter.drawEllipse(QtCore.QRectF(-radius, -radius + offset, 2 * radius, 2 * radius))

            disk(139, '#38201a', 5)
            disk(137, '#a46441')
            disk(129, '#74432e')
            if any(layers) or ingredients:
                disk(115, '#ad652b' if cooked else '#ba7b3d', 4)
                disk(114, INGREDIENT_COLORS.get(layers[0], '#e6a44d' if cooked else '#e8bd82'))
                painter.save()
                topping_area = QtGui.QPainterPath()
                topping_area.addEllipse(QtCore.QPointF(), SPREAD_AREA_RADIUS, SPREAD_AREA_RADIUS)
                painter.setClipPath(topping_area)
                for name in filter(None, layers[1:]):
                    painter.setBrush(QtGui.QColor(INGREDIENT_COLORS[name]))
                    sauce = name in CATEGORIES[1][1]
                    for x, y in ingredients.get(name, ()):
                        if sauce:
                            painter.drawEllipse(QtCore.QPointF(x, y), SPREAD_RADIUS, SPREAD_RADIUS)
                        else:
                            for dx, dy in ((-18, -9), (-9, -18), (7, -19), (17, -10), (-4, -5),
                                           (8, -1), (-19, 5), (-9, 10), (2, 17), (15, 12), (0, 4)):
                                painter.drawRoundedRect(QtCore.QRectF(x + dx, y + dy, 6, 3), 1, 1)
                painter.restore()
                for name in TOPPINGS:
                    for x, y in ingredients.get(name, ()):
                        topping_shape(painter, name, x, y, 7)
            painter.end()

    window = PizzaWindow()
    window.setWindowTitle(TITLE)
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.resize(800, 565)
    window.setStyleSheet("""
        QWidget#pizzaCounter { background: #f7ead6; color: #39251e; }
        QLabel { color: #39251e; }
        QGroupBox { font-weight: bold; color: #39251e; background: #fff7e7;
                    border: 1px solid #dcc3a4; border-radius: 8px; margin-top: 12px; }
        QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
        QPushButton { color: #39251e; background: #fff7e7; border: 2px solid #d6ba96;
                      border-radius: 8px; padding: 7px 12px; font-weight: bold; }
        QPushButton:hover:enabled { border-color: #a84e32; background: #fffdf6; }
        QPushButton:checked { background: #f6d99f; border-color: #a84e32; }
        QPushButton#servePizza:enabled { background: #a83f2e; color: white; border-color: #873122; }
        QPushButton:disabled { color: #7f6e63; }
    """)
    window.setObjectName('pizzaCounter')
    layout = QtWidgets.QVBoxLayout(window)
    layout.setContentsMargins(18, 14, 18, 14)
    high_score = QtWidgets.QLabel()
    records = QtWidgets.QHBoxLayout()
    records.addWidget(high_score)
    records.addStretch()
    clear_score = QtWidgets.QPushButton('Clear high score')
    records.addWidget(clear_score)
    layout.addLayout(records)
    difficulty = add_difficulty_slider(QtCore, QtWidgets, layout)
    difficulty.setAccessibleName('Pizza challenge difficulty')
    limit_label = QtWidgets.QLabel()
    layout.addWidget(limit_label)

    columns = QtWidgets.QHBoxLayout()
    layout.addLayout(columns, 1)
    ticket_box = QtWidgets.QGroupBox("Mario's order ticket")
    ticket_layout = QtWidgets.QVBoxLayout(ticket_box)
    ticket = QtWidgets.QLabel()
    ticket.setWordWrap(True)
    ticket_scroll = QtWidgets.QScrollArea()
    ticket_scroll.setWidgetResizable(True)
    ticket_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    ticket_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    ticket_scroll.setStyleSheet(
        'QScrollArea { background: #f7ead6; border: none; } '
        'QScrollArea > QWidget > QWidget { background: #f7ead6; }'
    )
    ticket.setStyleSheet('background: #f7ead6; color: #39251e;')
    ticket_scroll.setWidget(ticket)
    ticket_scroll.setMinimumHeight(82)
    ticket_scroll.setMaximumHeight(150)
    ticket_layout.addWidget(ticket_scroll)
    preview = Pie(preview=True)
    preview.setMinimumSize(145, 145)
    preview.setAccessibleName("Preview of Mario’s pizza")
    ticket_layout.addWidget(preview, 1)
    columns.addWidget(ticket_box, 1)
    pie = Pie()
    pie.setMinimumSize(220, 205)
    columns.addWidget(pie, 3)

    station = QtWidgets.QTabWidget()
    layout.addWidget(station)
    base = [None] * len(CATEGORIES)
    placements = {}
    order_preview = {}
    active_ingredient = None
    baked = False
    ingredient_buttons = []

    def tile(name, topping=False):
        button = QtWidgets.QPushButton(name)
        button.setCheckable(True)
        button.setMinimumHeight(50)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        art = QtGui.QPixmap(34, 34)
        art.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(art)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if topping:
            topping_shape(painter, name, 17, 17, 12)
        else:
            painter.setPen(QtGui.QColor('#804b32'))
            painter.setBrush(QtGui.QColor(INGREDIENT_COLORS[name]))
            painter.drawEllipse(QtCore.QRectF(5, 5, 24, 24))
            if name == 'No cheese':
                painter.drawLine(8, 26, 26, 8)
        painter.end()
        button.setIcon(QtGui.QIcon(art))
        button.setIconSize(QtCore.QSize(32, 32))
        ingredient_buttons.append(button)
        return button

    instruction = QtWidgets.QLabel('Choose a crust or select an ingredient.')
    layout.addWidget(instruction)
    clear = QtWidgets.QPushButton('Clear selected ingredient')
    clear.setEnabled(False)
    layout.addWidget(clear)

    def select_ingredient(name):
        nonlocal active_ingredient
        active_ingredient = name
        pie.last_point = None
        clear.setEnabled(name is not None)
        if name is None:
            instruction.setText('Choose a crust or select an ingredient.')
        elif spread_ingredient(name):
            instruction.setText(f'Click and drag on the pizza to spread {name.lower()}.')
        else:
            instruction.setText(f'Click the pizza to place one piece of {name.lower()}.')

    def choose_base(index, name):
        if index == 0 or name == 'No cheese':
            placements.pop(base[index], None)
            base[index] = name
            select_ingredient(None)
            instruction.setText(f'{name} selected. Choose another ingredient.')
        else:
            select_ingredient(name)
        pie.update()

    def clear_ingredient():
        name = active_ingredient
        placements.pop(name, None)
        for index, layer in enumerate(base):
            if layer == name:
                base[index] = None
        pie.update()

    clear.clicked.connect(clear_ingredient)

    for index, (category, options) in enumerate(CATEGORIES):
        page = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(page)
        group = QtWidgets.QButtonGroup(page)
        for name in options:
            button = tile(name)
            group.addButton(button)
            button.clicked.connect(lambda checked, i=index, value=name: choose_base(i, value))
            row.addWidget(button)
        station.addTab(page, category)

    page = QtWidgets.QWidget()
    grid = QtWidgets.QGridLayout(page)
    topping_group = QtWidgets.QButtonGroup(page)
    for index, name in enumerate(TOPPINGS):
        button = tile(name, topping=True)
        topping_group.addButton(button)
        button.clicked.connect(lambda checked, value=name: select_ingredient(value))
        grid.addWidget(button, index // 4, index % 4)
    station.addTab(page, 'Toppings')
    station.currentChanged.connect(lambda index: select_ingredient(None))

    result_label = QtWidgets.QLabel()
    result_label.setWordWrap(True)
    layout.addWidget(result_label)
    receipt = QtWidgets.QDialog(window)
    receipt.setWindowTitle('Pizza receipt')
    receipt.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    receipt.resize(430, 380)
    receipt_layout = QtWidgets.QVBoxLayout(receipt)
    feedback_view = QtWidgets.QTextBrowser(receipt)
    receipt_layout.addWidget(feedback_view)
    receipt_buttons = QtWidgets.QHBoxLayout()
    receipt_layout.addLayout(receipt_buttons)
    save_report = QtWidgets.QPushButton('Save pizza report')
    dismiss_receipt = QtWidgets.QPushButton('Close')
    receipt_buttons.addWidget(save_report)
    receipt_buttons.addStretch()
    receipt_buttons.addWidget(dismiss_receipt)
    buttons = QtWidgets.QHBoxLayout()
    layout.addLayout(buttons)
    bake = QtWidgets.QPushButton('Bake pizza')
    submit = QtWidgets.QPushButton('Serve pizza')
    submit.setObjectName('servePizza')
    another = QtWidgets.QPushButton('New order')
    view_receipt = QtWidgets.QPushButton('View receipt')
    buttons.addWidget(bake)
    buttons.addWidget(submit)
    buttons.addWidget(another)
    buttons.addWidget(view_receipt)
    view_receipt.setEnabled(False)
    for button in (bake, submit, another, view_receipt, save_report, dismiss_receipt, clear_score):
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    oven_timer = QtCore.QTimer(window)
    oven_timer.setSingleShot(True)
    speech_timer = QtCore.QTimer(window)
    speech_timer.setInterval(60)
    preparation_timer = QtCore.QTimer(window)
    preparation_timer.setInterval(40)
    preparation = None
    preparation_callback = None
    result_media = None
    result_message = ''
    result_score = 0
    tip_cents = 0
    result_is_record = False
    result_feedback = []
    music_ready = False
    next_button = None
    order_base = ()
    order_toppings = set()
    save_magic = b'MARIOPIZZA1'
    restored = [False]
    served = [False]

    def persist():
        if autosave_path is None:
            return
        try:
            autosave_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'difficulty': difficulty.value(), 'order_base': order_base,
                'order_toppings': sorted(order_toppings), 'base': base,
                'placements': placements, 'baked': baked, 'score': result_score,
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
        nonlocal order_base, order_toppings, baked, result_score
        if autosave_path is None or not autosave_path.exists():
            return False
        try:
            data = autosave_path.read_bytes()
            if data.startswith(save_magic):
                payload = pickle.loads(zlib.decompress(data[len(save_magic):]))
            else:
                payload = json.loads(data.decode('utf-8'))
            order_base = tuple(payload['order_base'])
            order_toppings = set(payload['order_toppings'])
            base[:] = list(payload.get('base', base))
            placements.clear()
            placements.update(payload.get('placements', {}))
            baked = bool(payload.get('baked', False))
            result_score = int(payload.get('score', 0))
            difficulty.setValue(int(payload.get('difficulty', difficulty.value())))
            persist()
            return True
        except (OSError, EOFError, ValueError, TypeError, KeyError, UnicodeError, pickle.PickleError, zlib.error):
            return False

    def show_high_score():
        value = get_high_score()
        high_score.setVisible(value is not None)
        clear_score.setEnabled(value is not None)
        if value is not None:
            high_score.setText(f'High score: {value}%')

    def clear_high_score():
        try:
            save_high_score(None)
        except OSError as error:
            QtWidgets.QMessageBox.warning(window, 'High score', f'Could not clear the high score.\n{error}')
        show_high_score()

    def feedback_html():
        html = [f'<p>Score: {result_score} points</p>']
        for heading, positive in (('What Mario liked', True), ("What Mario didn't like", False)):
            rows = [f'<li>{escape(reason)} <strong>{points:+d} points</strong></li>'
                    for reason, points in result_feedback if (points > 0) == positive]
            html.append(f'<h3>{heading}</h3>')
            html.append('<ul>' + ''.join(rows) + '</ul>' if rows else '<p>None</p>')
        return ''.join(html)

    def pizza_image(widget):
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        widget.grab().save(buffer, 'PNG')
        return bytes(buffer.data().toBase64()).decode('ascii')

    def export_report():
        path, _ = QtWidgets.QFileDialog.getSaveFileName(receipt, 'Save pizza report', 'pizza-report.html', 'HTML (*.html)')
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() not in ('.html', '.htm'):
            destination = destination.with_name(destination.name + '.html')
        try:
            destination.write_text(
                f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Pizza report</title>
                <style>
                body {{ margin: 2rem auto; padding: 0 1rem; max-width: 52rem; font: 16px system-ui; color: #39251e; }}
                .pizzas {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
                section {{ min-width: 0; padding: 1rem; background: #fff7e7; border-radius: 8px; }}
                img {{ display: block; width: 100%; aspect-ratio: 1; object-fit: contain; }}
                </style>
                <h1>{escape(TITLE)}</h1><p>{escape(result_message)}</p>
                <div class="pizzas">
                <section><h2>Mario's order ticket</h2>
                <img alt="Mario's desired pizza" src="data:image/png;base64,{pizza_image(preview)}">
                <p>{'<br>'.join(escape(line) for line in ticket.text().splitlines())}</p></section>
                <section><h2>Your pizza</h2>
                <img alt="The pizza you served" src="data:image/png;base64,{pizza_image(pie)}">
                <p>Accuracy: {result_score}%</p></section>
                </div>{feedback_html()}</html>''', encoding='utf-8')
        except OSError as error:
            QtWidgets.QMessageBox.warning(receipt, 'Pizza report', f'Could not save the report.\n{error}')

    def show_receipt():
        receipt.show()
        receipt.raise_()
        receipt.activateWindow()

    def show_limit():
        limit_label.setText(f'Pass with more than {failure_limit(difficulty.value())}% accuracy.')

    def wait_for_speech(button):
        nonlocal next_button
        next_button = button
        button.setEnabled(False)
        speech_timer.start()

    def check_speech():
        if not speech_busy():
            speech_timer.stop()
            if next_button is not None:
                if next_button is bake:
                    if music_ready:
                        start_music()
                    station.setEnabled(True)
                    result_label.clear()
                next_button.setEnabled(True)

    def cancel_preparation():
        nonlocal preparation, result_media
        preparation_timer.stop()
        if preparation is not None:
            results, cancelled, lock = preparation
            with lock:
                cancelled.set()
                try:
                    discard_media(results.get_nowait())
                except queue.Empty:
                    pass
            preparation = None
        discard_media(result_media)
        result_media = None

    def discard_media(media):
        if media and media.get('pizza_temporary'):
            media['video'].unlink(missing_ok=True)

    def prepare(text, cache, callback):
        nonlocal preparation, preparation_callback
        preparation_callback = callback
        preparation = prepare_line(text, cache)
        if preparation is None:
            callback(None)
        else:
            preparation_timer.start()

    def check_preparation():
        nonlocal preparation
        try:
            media = preparation[0].get_nowait()
        except queue.Empty:
            return
        preparation = None
        preparation_timer.stop()
        preparation_callback(media)

    def announce_intro(media):
        if media is None or not play_line(media):
            say(f'{TITLE}!')
        wait_for_speech(bake)

    def reset():
        nonlocal order_base, order_toppings, baked, music_ready
        cancel_meal()
        cancel_preparation()
        stop_music()
        loaded = not restored[0] and restore_autosave()
        restored[0] = True
        served[0] = False
        if not loaded:
            order_base = tuple(random.choice(options) for _, options in CATEGORIES)
            order_toppings = set(random.sample(TOPPINGS, random.randint(2, 4)))
        order_preview.clear()
        order_preview.update(sample_order(order_base, order_toppings))
        preview.update()
        lines = [f'{category}: {value}' for (category, _), value in zip(CATEGORIES, order_base)]
        lines.extend(('', 'Toppings:', *[name for name in TOPPINGS if name in order_toppings]))
        ticket.setText('\n'.join(lines))
        oven_timer.stop()
        if not loaded:
            baked = False
            base[:] = [None] * len(CATEGORIES)
            placements.clear()
        select_ingredient(None)
        for button in ingredient_buttons:
            group = button.group()
            if group:
                group.setExclusive(False)
            button.setChecked(False)
            if group:
                group.setExclusive(True)
        station.setCurrentIndex(0)
        station.setEnabled(False)
        bake.setEnabled(False)
        pie.update()
        submit.setEnabled(bool(baked))
        difficulty.setEnabled(True)
        another.setEnabled(False)
        view_receipt.setEnabled(False)
        receipt.hide()
        feedback_view.clear()
        result_label.setText('Loading pizza challenge…')
        show_limit()
        show_high_score()
        if loaded:
            music_ready = load_music()
            station.setEnabled(not baked)
            bake.setEnabled(not baked)
            def ask_resume():
                buttons = QtWidgets.QMessageBox.StandardButton
                answer = QtWidgets.QMessageBox.question(
                    window, 'Hey!', 'You have an autosave available! Do you want to continue?',
                    buttons.Yes | buttons.No, buttons.Yes
                )
                if answer == buttons.No:
                    clear_autosave()
                    restored[0] = True
                    reset()
                    return
                if music_ready and not baked:
                    start_music()
                station.setEnabled(not baked)
                bake.setEnabled(not baked)
            say('You have an autosave available! Do you want to continue?')
            QtCore.QTimer.singleShot(1200, ask_resume)
            return
        music_ready = load_music()
        persist()
        prepare(f'{TITLE}!', True, announce_intro)

    def start_baking():
        select_ingredient(None)
        station.setEnabled(False)
        bake.setEnabled(False)
        difficulty.setEnabled(False)
        clear_score.setEnabled(False)
        result_label.setText('In the oven…')
        oven_timer.start(900)

    def finish_baking():
        nonlocal baked, result_message, result_score, result_is_record, result_feedback, tip_cents
        baked = True
        pie.update()
        result_score, result_feedback = grade_pizza(order_base, order_toppings, base, placements, order_preview, difficulty.value())
        passed = result_score > failure_limit(difficulty.value())
        outcome = 'You win!' if passed else 'You lose!'
        result_message = f'Your accuracy is {result_score}%. {outcome}'
        previous = get_high_score()
        result_is_record = previous is None or result_score > previous
        if result_is_record:
            result_message += ' New high score!'
            if previous is not None:
                result_message += f' Your previous high score was {previous}%.'
        tip_cents = result_score * 10 if passed else 0
        if tip_cents:
            dollars, cents = divmod(tip_cents, 100)
            parts = []
            if dollars:
                parts.append(f'{dollars} {"dollar" if dollars == 1 else "dollars"}')
            if cents:
                parts.append(f'{cents} {"cent" if cents == 1 else "cents"}')
            result_message += f' I tipped you {" and ".join(parts)}.'
        else:
            result_message += " I didn't leave a tip this time."
        result_label.setText('Getting ready to serve…')
        prepare(result_message, False, ready_to_serve)

    def ready_to_serve(media):
        nonlocal result_media
        result_media = media
        result_label.setText('Pizza is ready to serve.')
        submit.setEnabled(True)

    def serve():
        served[0] = True
        submit.setEnabled(False)
        stop_music()
        start_meal(tip_cents, announce_result)
        display_message = result_message
        if result_is_record:
            try:
                save_high_score(result_score)
            except OSError:
                display_message += '\nCould not save the high score.'
        result_label.setText(f'Accuracy: {result_score}%')
        feedback_view.setHtml(f'<p>{escape(display_message).replace(chr(10), "<br>")}</p>' + feedback_html())
        view_receipt.setEnabled(True)
        show_receipt()
        show_high_score()
        clear_autosave()

    def announce_result():
        nonlocal result_media
        media, result_media = result_media, None
        if media is None or not play_line(media):
            say_uncached(result_message)
        wait_for_speech(another)

    preparation_timer.timeout.connect(check_preparation)
    speech_timer.timeout.connect(check_speech)
    difficulty.valueChanged.connect(show_limit)
    oven_timer.timeout.connect(finish_baking)
    bake.clicked.connect(start_baking)
    submit.clicked.connect(serve)
    view_receipt.clicked.connect(show_receipt)
    dismiss_receipt.clicked.connect(receipt.close)
    save_report.clicked.connect(export_report)
    clear_score.clicked.connect(clear_high_score)
    another.clicked.connect(lambda: reset())
    window.reset_game = reset
    return window
