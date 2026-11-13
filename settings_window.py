import math
import random
import threading
import urllib.request
from pathlib import Path

DEFAULT_SETTINGS = {
    'chattiness': 5,
    'playfulness': 5,
    'wandering': 5,
    'use_entropy_for_position': False,
    'zen_mode': False,
    'super_zen_mode': False,
    'oh_sorry': True,
    'caring_difficulty': 5,
    'autofeed': False,
    'speech_use_one_method_only': False,
    'speech_method_order': ['vosk_quick', 'google', 'vosk_smart'],
    'speech_preferred_method': 'google',
    'speech_enable_vosk': True,
    'speech_enable_smart_vosk': False,
    'speech_enable_google': True,
    'speech_enable_wake_word': True,
    'wake_word_sensitivity': 5,
    'finish_sensitivity': 5,
    'tts_live_sentence_mixing': False,
    'weather_city': '',
    'weather_country_code': '',
}


def random_delay(level, minimum, maximum):
    if level <= 0:
        return math.inf
    factor = 2 ** ((5 - level) / 2.5)
    return random.uniform(minimum, maximum) * factor



def idle_delay(level):
    return random_delay(level, 45, 90) * (1 - level / 20)


def create_settings_window(QtCore, QtWidgets, values, changed, assets_dir=None):
    from PySide6 import QtGui

    window = QtWidgets.QWidget()
    window.setWindowTitle("Tweak Mario's Settings")
    window.resize(500, 560)
    layout = QtWidgets.QVBoxLayout(window)
    tabs = QtWidgets.QTabWidget()
    layout.addWidget(tabs)
    controls = {}

    def add_scroll_tab(page, title):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        tabs.addTab(scroll, title)

    for title, sliders, checks in (
        ('Conversation', [('Chattiness', 'chattiness'), ('Playfulness', 'playfulness')],
         [('Zen Mode', 'zen_mode'), ('Super Zen Mode', 'super_zen_mode'),
          ('"Oh, sorry!"', 'oh_sorry')]),
        ('Movement', [('Wandering', 'wandering')],
         [('Use entropy for position', 'use_entropy_for_position')]),
        ('Pet Care', [('Caring Difficulty', 'caring_difficulty')], [('Autofeed', 'autofeed')]),
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
        add_scroll_tab(page, title)

    assets_dir = Path(assets_dir) if assets_dir is not None else Path(__file__).resolve().parent / 'assets'
    method_options = (
        ('vosk_quick', 'Vosk (quick)'),
        ('google', 'Google'),
        ('vosk_smart', 'Vosk (smart)'),
    )
    speech_page = QtWidgets.QWidget()
    speech_layout = QtWidgets.QVBoxLayout(speech_page)
    speech_layout.addWidget(QtWidgets.QLabel(
        'Both Vosk options work offline. Google needs an internet connection. '
        'It is recommended to use smart Vosk only if you have a mid-range to '
        'powerful computer so that it takes less time to process.'
    ))
    enable_wake_word = QtWidgets.QCheckBox('Enable wake word ("Hey Mario Head")')
    enable_wake_word.setChecked(values.get('speech_enable_wake_word', True))
    speech_layout.addWidget(enable_wake_word)
    speech_layout.addWidget(QtWidgets.QLabel('Wake word sensitivity'))
    wake_sensitivity_row = QtWidgets.QWidget()
    wake_sensitivity_layout = QtWidgets.QHBoxLayout(wake_sensitivity_row)
    wake_sensitivity = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    wake_sensitivity.setObjectName('wake_word_sensitivity')
    wake_sensitivity.setRange(0, 10)
    wake_sensitivity.setValue(int(values.get('wake_word_sensitivity', 5)))
    wake_sensitivity_value = QtWidgets.QLabel(str(wake_sensitivity.value()))
    enable_wake_word.setEnabled(wake_sensitivity.value() > 0)
    wake_sensitivity_layout.addWidget(wake_sensitivity)
    wake_sensitivity_layout.addWidget(wake_sensitivity_value)
    speech_layout.addWidget(wake_sensitivity_row)
    speech_layout.addWidget(QtWidgets.QLabel('Finish Sensitivity'))
    finish_sensitivity_row = QtWidgets.QWidget()
    finish_sensitivity_layout = QtWidgets.QHBoxLayout(finish_sensitivity_row)
    finish_sensitivity = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
    finish_sensitivity.setObjectName('finish_sensitivity')
    finish_sensitivity.setRange(0, 10)
    finish_sensitivity.setValue(int(values.get('finish_sensitivity', 5)))
    finish_sensitivity_value = QtWidgets.QLabel(str(finish_sensitivity.value()))
    finish_sensitivity_layout.addWidget(finish_sensitivity)
    finish_sensitivity_layout.addWidget(finish_sensitivity_value)
    speech_layout.addWidget(finish_sensitivity_row)
    use_one_method = QtWidgets.QCheckBox('Use one method only')
    use_one_method.setChecked(values['speech_use_one_method_only'])
    speech_layout.addWidget(use_one_method)
    speech_layout.addWidget(QtWidgets.QLabel('Method order'))
    method_order = QtWidgets.QListWidget()
    method_order.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
    method_order.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
    order = values.get('speech_method_order', [])
    order = [name for name in order if name in dict(method_options)]
    order = list(dict.fromkeys(order))
    order.extend(name for name, _ in method_options if name not in order)
    order_items = {}
    for method in order:
        label = dict(method_options)[method]
        item = QtWidgets.QListWidgetItem(label)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, method)
        method_order.addItem(item)
        order_items[method] = item
    speech_layout.addWidget(method_order)
    speech_layout.addWidget(QtWidgets.QLabel('Preferred method'))
    preferred_method = QtWidgets.QComboBox()
    preferred_model = QtGui.QStandardItemModel(preferred_method)
    for method, label in method_options:
        item = QtGui.QStandardItem(label)
        item.setData(method, QtCore.Qt.ItemDataRole.UserRole)
        preferred_model.appendRow(item)
    preferred_method.setModel(preferred_model)
    preferred_index = preferred_method.findData(
        values.get('speech_preferred_method', 'google'),
        QtCore.Qt.ItemDataRole.UserRole,
    )
    preferred_method.setCurrentIndex(max(0, preferred_index))
    speech_layout.addWidget(preferred_method)
    enable_vosk = QtWidgets.QCheckBox('Enable Vosk')
    enable_vosk.setChecked(values['speech_enable_vosk'])
    speech_layout.addWidget(enable_vosk)
    enable_smart_vosk = QtWidgets.QCheckBox('Enable smart Vosk')
    enable_smart_vosk.setChecked(values['speech_enable_smart_vosk'])
    speech_layout.addWidget(enable_smart_vosk)
    enable_google = QtWidgets.QCheckBox('Enable Google')
    enable_google.setChecked(values['speech_enable_google'])
    speech_layout.addWidget(enable_google)
    online_status = QtWidgets.QLabel('Checking Google availability…')
    speech_layout.addWidget(online_status)
    add_scroll_tab(speech_page, 'Speech Recognition')

    tts_page = QtWidgets.QWidget()
    tts_layout = QtWidgets.QVBoxLayout(tts_page)
    live_sentence_mixing = QtWidgets.QCheckBox(
        'Enable Live Sentence Mixing (experimental)'
    )
    live_sentence_mixing.setChecked(
        values.get('tts_live_sentence_mixing', False)
    )
    live_sentence_mixing.toggled.connect(
        lambda value: changed('tts_live_sentence_mixing', value)
    )
    controls['tts_live_sentence_mixing'] = live_sentence_mixing
    tts_layout.addWidget(live_sentence_mixing)
    add_scroll_tab(tts_page, 'TTS')

    weather_page = QtWidgets.QWidget()
    weather_form = QtWidgets.QFormLayout(weather_page)
    location_help = QtWidgets.QLabel('Choose a city and country for weather and local news. Leave them blank to use your approximate network location.')
    location_help.setWordWrap(True)
    weather_form.addRow(location_help)
    for label, name in (('City', 'weather_city'), ('Country code (for example, AR)', 'weather_country_code')):
        field = QtWidgets.QLineEdit(str(values.get(name, '') or ''))
        field.setObjectName(name)
        field.editingFinished.connect(lambda field=field, name=name: changed(name, field.text().strip()))
        controls[name] = field
        weather_form.addRow(label, field)
    add_scroll_tab(weather_page, 'Weather')

    controls.update({
        'speech_use_one_method_only': use_one_method,
        'speech_preferred_method': preferred_method,
        'speech_enable_vosk': enable_vosk,
        'speech_enable_smart_vosk': enable_smart_vosk,
        'speech_enable_google': enable_google,
        'speech_enable_wake_word': enable_wake_word,
        'wake_word_sensitivity': wake_sensitivity,
        'finish_sensitivity': finish_sensitivity,
    })
    google_online = [False]

    def current_order():
        return [
            method_order.item(row).data(QtCore.Qt.ItemDataRole.UserRole)
            for row in range(method_order.count())
        ]

    def method_availability():
        vosk_enabled = enable_vosk.isChecked()
        quick_available = vosk_enabled and (assets_dir / 'vosk').is_dir()
        smart_available = (
            vosk_enabled and enable_smart_vosk.isChecked()
            and (assets_dir / 'vosksmart').is_dir()
        )
        google_available = enable_google.isChecked() and google_online[0]
        return {
            'vosk_quick': quick_available,
            'google': google_available,
            'vosk_smart': smart_available,
        }

    def update_method_controls():
        available = method_availability()
        for method, item in order_items.items():
            flags = item.flags()
            if available[method]:
                flags |= (
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                    | QtCore.Qt.ItemFlag.ItemIsSelectable
                    | QtCore.Qt.ItemFlag.ItemIsDragEnabled
                )
            else:
                flags &= ~(
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                    | QtCore.Qt.ItemFlag.ItemIsSelectable
                    | QtCore.Qt.ItemFlag.ItemIsDragEnabled
                )
            item.setFlags(flags)
        for index, (method, _) in enumerate(method_options):
            item = preferred_model.item(index)
            flags = item.flags()
            if available[method]:
                flags |= QtCore.Qt.ItemFlag.ItemIsEnabled
            else:
                flags &= ~QtCore.Qt.ItemFlag.ItemIsEnabled
            item.setFlags(flags)
        one_only = use_one_method.isChecked()
        method_order.setEnabled(not one_only)
        preferred_method.setEnabled(one_only)
        enable_smart_vosk.setEnabled(
            enable_vosk.isChecked() and (assets_dir / 'vosksmart').is_dir()
        )
        if google_online[0]:
            online_status.setText('Google is online')
        else:
            online_status.setText('Google is offline or unavailable')

    def primary_method_changed(name, value):
        other_name = 'speech_enable_google' if name == 'speech_enable_vosk' else 'speech_enable_vosk'
        if not value and not controls[other_name].isChecked():
            control = controls[name]
            blocker = QtCore.QSignalBlocker(control)
            control.setChecked(True)
            del blocker
            QtWidgets.QMessageBox.information(
                window,
                'Speech Recognition',
                'At least one recognition provider must remain enabled.',
            )
            return
        changed(name, value)
        update_method_controls()

    use_one_method.toggled.connect(
        lambda value: (changed('speech_use_one_method_only', value), update_method_controls())
    )
    enable_vosk.toggled.connect(
        lambda value: primary_method_changed('speech_enable_vosk', value)
    )
    enable_smart_vosk.toggled.connect(
        lambda value: (changed('speech_enable_smart_vosk', value), update_method_controls())
    )
    enable_google.toggled.connect(
        lambda value: primary_method_changed('speech_enable_google', value)
    )
    enable_wake_word.toggled.connect(
        lambda value: changed('speech_enable_wake_word', value)
    )

    def update_wake_sensitivity(value):
        wake_sensitivity_value.setText(str(value))
        if value == 0 and enable_wake_word.isChecked():
            enable_wake_word.setChecked(False)
        enable_wake_word.setEnabled(value > 0)
        if not wake_sensitivity.isSliderDown():
            changed('wake_word_sensitivity', value)

    wake_sensitivity.valueChanged.connect(update_wake_sensitivity)
    wake_sensitivity.sliderReleased.connect(
        lambda: changed('wake_word_sensitivity', wake_sensitivity.value())
    )

    def update_finish_sensitivity(value):
        finish_sensitivity_value.setText(str(value))
        if not finish_sensitivity.isSliderDown():
            changed('finish_sensitivity', value)

    finish_sensitivity.valueChanged.connect(update_finish_sensitivity)
    finish_sensitivity.sliderReleased.connect(
        lambda: changed('finish_sensitivity', finish_sensitivity.value())
    )
    preferred_method.currentIndexChanged.connect(
        lambda index: changed(
            'speech_preferred_method',
            preferred_method.itemData(index, QtCore.Qt.ItemDataRole.UserRole),
        ) if index >= 0 else None
    )
    method_order.model().rowsMoved.connect(
        lambda *args: changed('speech_method_order', current_order())
    )
    update_method_controls()

    class OnlineCheck(QtCore.QObject):
        result = QtCore.Signal(bool)

        @QtCore.Slot(bool)
        def apply(self, value):
            google_online[0] = value
            update_method_controls()

    online_check = OnlineCheck(window)
    online_check.result.connect(online_check.apply)

    def check_online():
        available = False
        try:
            request = urllib.request.Request(
                'https://www.google.com/generate_204',
                method='GET',
                headers={'User-Agent': 'MarioAssistant'},
            )
            with urllib.request.urlopen(request, timeout=1.5) as response:
                available = response.status in (200, 204)
        except Exception:
            pass
        try:
            online_check.result.emit(available)
        except RuntimeError:
            pass

    threading.Thread(target=check_online, daemon=True).start()

    def sync(name, value):
        control = controls[name]
        blocker = QtCore.QSignalBlocker(control)
        if isinstance(control, QtWidgets.QCheckBox):
            control.setChecked(value)
        elif isinstance(control, QtWidgets.QComboBox):
            control.setCurrentIndex(control.findData(value, QtCore.Qt.ItemDataRole.UserRole))
        elif isinstance(control, QtWidgets.QLineEdit):
            control.setText(str(value or ''))
        else:
            control.setValue(value)
        del blocker
        if name == 'speech_use_one_method_only':
            update_method_controls()

    return window, sync
