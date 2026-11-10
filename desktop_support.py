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


def play_task_complete_sound(QtWidgets):
    if sys.platform == 'win32':
        import winsound
        winsound.MessageBeep(winsound.MB_OK)
        return
    if sys.platform == 'darwin':
        player = shutil.which('afplay')
        sound = Path('/System/Library/Sounds/Glass.aiff')
    else:
        player = shutil.which('paplay') or shutil.which('pw-play') or shutil.which('ffplay')
        sound = next((p for p in (
            Path('/usr/share/sounds/freedesktop/stereo/complete.oga'),
            Path('/usr/share/sounds/freedesktop/stereo/dialog-information.oga'),
            Path('/usr/share/sounds/freedesktop/stereo/message.oga'),
        ) if p.exists()), None)
    if player and sound and sound.exists():
        command = [player, str(sound)]
        if Path(player).stem == 'ffplay':
            command = [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', str(sound)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        QtWidgets.QApplication.beep()


def eject_cd():
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = [wintypes.LPCWSTR]
        get_drive_type.restype = wintypes.UINT
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        device_io = kernel32.DeviceIoControl
        device_io.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        device_io.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        invalid_handle = wintypes.HANDLE(-1).value
        for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ':
            root = f'{letter}:\\'
            if get_drive_type(root) != 5:
                continue
            handle = create_file(
                f'\\\\.\\{letter}:', 0x80000000, 3, None, 3, 0, None
            )
            if handle == invalid_handle:
                continue
            try:
                returned = wintypes.DWORD()
                if device_io(handle, 0x002D4800, None, 0, None, 0,
                             ctypes.byref(returned), None):
                    return bool(device_io(
                        handle, 0x002D4808, None, 0, None, 0,
                        ctypes.byref(returned), None,
                    ))
            finally:
                close_handle(handle)
        return False
    if sys.platform == 'darwin':
        drutil = shutil.which('drutil')
        if not drutil:
            return False
        try:
            status = subprocess.run(
                [drutil, 'status'], capture_output=True, text=True,
                check=True, timeout=5,
            ).stdout.lower()
            if 'no media' in status or 'media type:' not in status:
                return False
            subprocess.run(
                [drutil, 'eject'], check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    eject = shutil.which('eject')
    if not eject:
        return False
    drives = sorted(Path('/sys/class/block').glob('sr*'))
    for drive in drives:
        try:
            if int((drive / 'size').read_text().strip()) <= 0:
                continue
            subprocess.run(
                [eject, f'/dev/{drive.name}'], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return False


def play_fatal_error_sound(QtWidgets):
    if sys.platform not in ('win32', 'darwin'):
        player = shutil.which('paplay') or shutil.which('pw-play') or shutil.which('ffplay')
        sound = next((path for path in (
            Path('/usr/share/sounds/Oxygen-Sys-App-Error-Critical.ogg'),
            Path('/usr/share/sounds/deepin/stereo/dialog-error-critical.wav'),
            Path('/usr/share/sounds/freedesktop/stereo/dialog-error.oga'),
        ) if path.is_file()), None)
        if player and sound:
            command = [player, str(sound)]
            if Path(player).stem == 'ffplay':
                command = [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', str(sound)]
            try:
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    play_error_sound(QtWidgets)


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


def speak_text(text):
    if sys.platform == 'win32':
        script = "$text = [Console]::In.ReadToEnd(); Add-Type -AssemblyName System.Speech; $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; $speaker.Speak($text)"
        process = subprocess.Popen(
            ['powershell', '-NoProfile', '-Command', script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        process.stdin.write(text)
        process.stdin.close()
        return True
    if sys.platform == 'darwin':
        player = shutil.which('say')
        command = [player, text] if player else None
    else:
        player = shutil.which('spd-say') or shutil.which('espeak-ng') or shutil.which('espeak')
        command = [player, text] if player else None
    if not command:
        return False
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def get_system_volume():
    if sys.platform == 'win32':
        try:
            from pycaw.pycaw import AudioUtilities
            device = AudioUtilities.GetSpeakers()
            interface = device.EndpointVolume
            return round(interface.GetMasterVolumeLevelScalar() * 100)
        except Exception:
            return None
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(
                ['osascript', '-e', 'output volume of (get volume settings)'],
                capture_output=True, text=True, check=True, timeout=3,
            )
            return max(0, min(100, int(result.stdout.strip())))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    wpctl = shutil.which('wpctl')
    if wpctl:
        try:
            result = subprocess.run(
                [wpctl, 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                capture_output=True, text=True, check=True, timeout=3,
            )
            return max(0, min(100, round(float(result.stdout.split()[1]) * 100)))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    pactl = shutil.which('pactl')
    if pactl:
        try:
            result = subprocess.run(
                [pactl, 'get-sink-volume', '@DEFAULT_SINK@'],
                capture_output=True, text=True, check=True, timeout=3,
            )
            import re
            match = re.search(r'(\d+)%', result.stdout)
            if match:
                return max(0, min(100, int(match.group(1))))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def set_system_volume(percent):
    percent = max(0, min(100, round(percent)))
    if sys.platform == 'win32':
        try:
            from pycaw.pycaw import AudioUtilities
            device = AudioUtilities.GetSpeakers()
            device.EndpointVolume.SetMasterVolumeLevelScalar(percent / 100, None)
            return True
        except Exception:
            return False
    if sys.platform == 'darwin':
        try:
            subprocess.run(
                ['osascript', '-e', f'set volume output volume {percent}'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    wpctl = shutil.which('wpctl')
    if wpctl:
        try:
            subprocess.run(
                [wpctl, 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{percent / 100:.2f}'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            pass
    pactl = shutil.which('pactl')
    if pactl:
        try:
            subprocess.run(
                [pactl, 'set-sink-volume', '@DEFAULT_SINK@', f'{percent}%'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False


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
