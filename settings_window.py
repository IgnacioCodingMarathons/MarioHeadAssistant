import math
import random

DEFAULT_SETTINGS = {
    'chattiness': 5,
    'playfulness': 5,
    'wandering': 5,
    'use_entropy_for_position': False,
    'zen_mode': False,
    'super_zen_mode': False,
}


def random_delay(level, minimum, maximum):
    if level <= 0:
        return math.inf
    factor = 2 ** ((5 - level) / 2.5)
    return random.uniform(minimum, maximum) * factor



def idle_delay(level):
    return random_delay(level, 45, 90) * (1 - level / 20)


def create_settings_window(QtCore, QtWidgets, values, changed):
    window = QtWidgets.QWidget()
    window.setWindowTitle("Tweak Mario's Settings")
    window.resize(420, 320)
    layout = QtWidgets.QVBoxLayout(window)
    tabs = QtWidgets.QTabWidget()
    layout.addWidget(tabs)
    controls = {}
    for title, sliders, checks in (
        ('Communication', [('Chattiness', 'chattiness'), ('Playfulness', 'playfulness')],
         [('Zen Mode', 'zen_mode'), ('Super Zen Mode', 'super_zen_mode')]),
        ('Movement', [('Wandering', 'wandering')],
         [('Use entropy for position', 'use_entropy_for_position')]),
    ):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        for label, name in sliders:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            slider.setObjectName(name)
            slider.setRange(0, 10)
            slider.setValue(values[name])
            number = QtWidgets.QLabel(str(values[name]))
            slider.valueChanged.connect(lambda value, field=name: changed(field, value))
            slider.valueChanged.connect(lambda value, text=number: text.setText(str(value)))
            row_layout.addWidget(slider)
            row_layout.addWidget(number)
            controls[name] = slider
            form.addRow(label, row)
        for label, name in checks:
            check = QtWidgets.QCheckBox(label)
            check.setObjectName(name)
            check.setChecked(values[name])
            check.toggled.connect(lambda value, field=name: changed(field, value))
            controls[name] = check
            form.addRow(check)
        tabs.addTab(page, title)

    def sync(name, value):
        control = controls[name]
        blocker = QtCore.QSignalBlocker(control)
        if isinstance(control, QtWidgets.QCheckBox):
            control.setChecked(value)
        else:
            control.setValue(value)
        del blocker

    return window, sync
