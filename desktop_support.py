import shutil
import subprocess
import sys
from pathlib import Path


def play_information_sound(QtWidgets):
    if sys.platform == 'win32':
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return
    if sys.platform == 'darwin':
        player = shutil.which('afplay')
        sound = Path('/System/Library/Sounds/Glass.aiff')
    else:
        player = shutil.which('paplay') or shutil.which('pw-play') or shutil.which('ffplay')
        sound = next((p for p in (
            Path('/usr/share/sounds/freedesktop/stereo/dialog-information.oga'),
            Path('/usr/share/sounds/freedesktop/stereo/message.oga'),
            Path('/usr/share/sounds/freedesktop/stereo/complete.oga'),
        ) if p.exists()), None)
    if player and sound and sound.exists():
        command = [player, str(sound)]
        if Path(player).stem == 'ffplay':
            command = [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', str(sound)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        QtWidgets.QApplication.beep()


def play_error_sound(QtWidgets):
    if sys.platform == 'win32':
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND)
        return
    if sys.platform == 'darwin':
        player = shutil.which('afplay')
        sound = Path('/System/Library/Sounds/Basso.aiff')
    else:
        player = shutil.which('paplay') or shutil.which('pw-play') or shutil.which('ffplay')
        sound = next((p for p in (
            Path('/usr/share/sounds/freedesktop/stereo/dialog-error.oga'),
            Path('/usr/share/sounds/gnome/default/alerts/bark.ogg'),
            Path('/usr/share/sounds/freedesktop/stereo/dialog-warning.oga'),
        ) if p.exists()), None)
    if player and sound and sound.exists():
        command = [player, str(sound)]
        if Path(player).stem == 'ffplay':
            command = [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', str(sound)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        QtWidgets.QApplication.beep()


class DesktopNotifications:
    def __init__(self, QtWidgets):
        self.tray = QtWidgets.QSystemTrayIcon(
            QtWidgets.QApplication.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation)
        )
        self.tray.setToolTip('MarioAssistant')
        self.tray.show()
        self.QtWidgets = QtWidgets

    def show(self, title='Hey!', body='I got something in my eye, can you help me get it out?', duration_ms=60000):
        self.tray.showMessage(title, body,
                              self.QtWidgets.QSystemTrayIcon.MessageIcon.Information, duration_ms)
        return self

    def close(self):
        self.tray.hide()
        self.tray.show()


class BrightnessController:
    def __init__(self, qt_app):
        from PySide6 import QtCore, QtWidgets
        self.native = None
        self.overlays = []
        self.original = {}
        if sys.platform.startswith('linux') and shutil.which('xrandr'):
            try:
                from tunnel_of_doom import capture_brightness
                self.executable, self.original = capture_brightness()
                self.native = 'xrandr'
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
        elif sys.platform == 'win32':
            try:
                import screen_brightness_control as sbc
                readings = sbc.get_brightness()
                if readings:
                    self.sbc = sbc
                    self.original = dict(enumerate(value / 100 for value in readings))
                    self.native = 'sbc'
            except Exception:
                pass
        elif sys.platform == 'darwin' and shutil.which('brightness'):
            import re
            try:
                result = subprocess.run(['brightness', '-l'], capture_output=True, text=True, check=True, timeout=3)
                values = re.findall(r'display (\d+): brightness ([\d.]+)', result.stdout)
                if values:
                    self.original = {int(i): float(value) for i, value in values}
                    self.native = 'mac'
            except (OSError, subprocess.SubprocessError):
                pass
        if self.native is None:
            self.original = {'overlay': 1.0}
            for screen in qt_app.screens():
                overlay = QtWidgets.QWidget()
                overlay.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Tool
                                       | QtCore.Qt.WindowType.WindowStaysOnTopHint
                                       | QtCore.Qt.WindowType.WindowTransparentForInput
                                       | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus)
                overlay.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
                overlay.setStyleSheet('background-color: black;')
                overlay.setGeometry(screen.geometry())
                overlay.setWindowOpacity(0)
                self.overlays.append(overlay)

    def set(self, values):
        if self.native == 'xrandr':
            from tunnel_of_doom import set_brightness
            set_brightness(self.executable, values)
        elif self.native == 'sbc':
            for display, value in values.items():
                self.sbc.set_brightness(round(value * 100), display=display)
        elif self.native == 'mac':
            for display, value in values.items():
                subprocess.run(['brightness', '-d', str(display), str(value)], check=True, timeout=3)
        else:
            for overlay in self.overlays:
                overlay.setWindowOpacity(1 - values['overlay'])
                overlay.show()
                overlay.raise_()

    def restore(self):
        try:
            if self.native:
                self.set(self.original)
        finally:
            for overlay in self.overlays:
                overlay.close()
                overlay.deleteLater()
