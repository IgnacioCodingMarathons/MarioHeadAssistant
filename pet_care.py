import math
import time
from pathlib import Path

ROM_EXTENSIONS = frozenset({
    '.xci', '.nsp', '.xcz', '.nsz', '.nes', '.fds', '.sfc', '.smc',
    '.gb', '.gbc', '.gba', '.nds', '.3ds', '.cia', '.n64', '.z64', '.v64',
    '.gcm', '.wbfs', '.wud', '.wux', '.rvz', '.iso', '.cso', '.chd',
    '.md', '.smd', '.sms', '.gg', '.pce', '.a26', '.a52', '.a78', '.lnx',
    '.vb', '.ws', '.wsc', '.ngp', '.ngc', '.rom', '.cue',
})
STEAM_SHORTCUT_EXTENSIONS = frozenset({'.url', '.desktop', '.webloc', '.lnk'})


def is_steam_shortcut(path):
    if path.suffix.lower() not in STEAM_SHORTCUT_EXTENSIONS:
        return False
    try:
        with path.open('rb') as shortcut:
            contents = shortcut.read(65536).lower()
    except OSError:
        return False
    if b'steam://' in contents:
        return True
    return (
        path.suffix.lower() == '.lnk'
        and b'steam.exe' in contents
        and (b'-applaunch' in contents or b'-gameid' in contents)
    )


class PetNeeds:
    def __init__(self, saved, now=None, difficulty=5):
        now = time.monotonic() if now is None else now
        self.caring_difficulty = max(0, min(10, int(difficulty)))
        self.depletion_count = 0
        self.levels = {}
        for name in ('hunger', 'thirst'):
            try:
                value = float(saved.get(name, 100))
            except (TypeError, ValueError):
                value = 100
            self.levels[name] = max(0, min(100, value)) if math.isfinite(value) else 100
        try:
            saved_at = float(saved['needs_saved_at'])
        except (KeyError, TypeError, ValueError):
            saved_at = None
        if self.caring_difficulty > 0 and saved_at is not None and math.isfinite(saved_at):
            offline_hours = max(0, time.time() - saved_at) / 3600
            for name in self.levels:
                self.levels[name] = max(
                    0, self.levels[name] - offline_hours * self._drain_multiplier()
                )
            if any(value <= 0 for value in self.levels.values()):
                self.levels = {name: 100.0 for name in self.levels}
                self.depletion_count += 1
        if self.caring_difficulty == 0:
            self.levels = {name: 100.0 for name in self.levels}
        self.last_tick = now
        self.needy = {name: value < 50 for name, value in self.levels.items()}
        self.reminders = {name: now if self.needy[name] else math.inf for name in self.levels}
        self.requested = None

    def set_difficulty(self, difficulty, now=None):
        self.caring_difficulty = max(0, min(10, int(difficulty)))
        self.last_tick = time.monotonic() if now is None else now
        if self.caring_difficulty == 0:
            self.levels = {name: 100.0 for name in self.levels}
            self.needy = {name: False for name in self.levels}
            self.reminders = {name: math.inf for name in self.levels}
            self.requested = None

    def _drain_multiplier(self):
        if self.caring_difficulty <= 5:
            return self.caring_difficulty / 5
        return 1 + (self.caring_difficulty - 5) * 0.8

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        elapsed = max(0, now - self.last_tick)
        self.last_tick = now
        if self.caring_difficulty == 0:
            self.levels = {name: 100.0 for name in self.levels}
            self.needy = {name: False for name in self.levels}
            self.reminders = {name: math.inf for name in self.levels}
            self.requested = None
            return False
        depleted = False
        drain_multiplier = self._drain_multiplier()
        for name in self.levels:
            self.levels[name] = max(
                0, self.levels[name] - elapsed / 60 * 1.75 * drain_multiplier
            )
            if self.levels[name] <= 0:
                depleted = True
            if self.levels[name] < 50 and not self.needy[name]:
                self.needy[name] = True
                self.reminders[name] = now
        if depleted:
            self.levels = {name: 100.0 for name in self.levels}
            self.needy = {name: False for name in self.levels}
            self.reminders = {name: math.inf for name in self.levels}
            self.requested = None
            self.depletion_count += 1
        return depleted

    def due(self, now=None):
        now = time.monotonic() if now is None else now
        name = min(self.reminders, key=self.reminders.get)
        return name if self.reminders[name] <= now else None

    def next_notification_at(self, name):
        if self.caring_difficulty == 0:
            return math.inf
        if self.needy[name]:
            return self.reminders[name]
        rate_per_second = 1.75 * self._drain_multiplier() / 60
        seconds_until_threshold = max(0, self.levels[name] - 50) / rate_per_second
        return self.last_tick + seconds_until_threshold + 1 / 60

    def is_full(self, name):
        return name in self.levels and self.levels[name] >= 99.9

    def remind(self, name, now=None):
        now = time.monotonic() if now is None else now
        self.requested = name
        self.reminders[name] = now + 150

    def feed(self, filename, target=None, now=None):
        if self.caring_difficulty == 0:
            return None
        path = Path(filename)
        if path.suffix.lower() in ROM_EXTENSIONS or not path.is_file():
            return None
        size = path.stat().st_size
        steam_shortcut = is_steam_shortcut(path)
        self.tick(now)
        if target not in self.levels:
            target = self.requested
            if target is None or not self.needy[target]:
                target = min(self.levels, key=self.levels.get)
        if self.is_full(target):
            return 'full'
        boost = 10 if steam_shortcut else size / 1_000_000 * 5
        self.levels[target] = min(100, self.levels[target] + boost)
        if self.levels[target] > 50:
            self.needy[target] = False
            self.reminders[target] = math.inf
            if self.requested == target:
                self.requested = None
        return target

    def snapshot(self):
        return dict(self.levels)


def create_pet_care_window(QtWidgets, values):
    window = QtWidgets.QWidget()
    window.setWindowTitle("Mario's Pet Care")
    window.resize(390, 200)
    layout = QtWidgets.QFormLayout(window)
    bars = {}
    for name in ('hunger', 'thirst'):
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, 1000)
        layout.addRow(name.capitalize(), bar)
        bars[name] = bar
    instruction = QtWidgets.QLabel(
        'Drop a file on Mario: left feeds hunger, right feeds thirst. Files stay untouched.\n'
        '10 MB restores 50%; accepted files earn 50 MarioCoins.\n'
        'At Caring Difficulty 0, Mario will not accept files and both meters stay full.'
    )
    instruction.setWordWrap(True)
    layout.addRow(instruction)

    def update():
        for name, bar in bars.items():
            value = values[name]
            bar.setValue(round(value * 10))
            bar.setFormat(f'{value:.1f}%')

    update()
    return window, update
