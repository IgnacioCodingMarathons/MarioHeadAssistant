import random


DIFFICULTY_NAMES = (
    (1, 'Baby'), (3, 'Easy'), (5, 'Sweet spot'), (7, 'Normal'),
    (10, 'Slightly hard'), (12, 'Hard'), (15, 'Insane'),
    (17, 'Manic'), (20, 'Madman'),
)


def add_difficulty_slider(QtCore, QtWidgets, layout):
    row = QtWidgets.QHBoxLayout()
    slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    slider.setRange(1, 20)
    slider.setValue(5)
    slider.setAccessibleName('CPU difficulty')
    label = QtWidgets.QLabel()

    def update(value):
        name = next(name for minimum, name in reversed(DIFFICULTY_NAMES) if value >= minimum)
        label.setText(f'Difficulty: {value} ({name})')

    slider.valueChanged.connect(update)
    update(slider.value())
    row.addWidget(label)
    row.addWidget(slider, 1)
    layout.addLayout(row)
    return slider


def try_random_move(level):
    return random.random() < .9 * (20 - level) / 19
