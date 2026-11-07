import json
import math
from datetime import datetime
import os
import queue
import random
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pygame
from pygame._sdl2 import Renderer, Texture, Window

from tunnel_of_doom import play_tunnel_of_doom
from settings_window import DEFAULT_SETTINGS, create_settings_window, random_delay, idle_delay
from desktop_support import DesktopNotifications, play_information_sound
from window_policy import (
    install_linux_window_policy,
    keep_sdl_window_above,
    set_sdl_window_attention,
)
from pet_care import PetNeeds, create_pet_care_window
from voice_commands import (
    MicrophoneCapture,
    SpeechTranscriber,
    create_beep_sound,
    interpret_voice_command,
)


ASSETS = Path(__file__).resolve().parent / "assets"
IDLE_VIDEOS = tuple(ASSETS / name for name in (
    "niceplace.avi",
    "mariosize.avi",
    "mynosefeelsfunny.avi",
    "madeyoulook.avi",
    "immabetyoucantdothis.avi",
    "haveyouseentheprincess.avi",
    "haveyouseenluigi.avi",
    "movingontheground.avi",
    "goodjoke.avi",
    "standingstill.avi",
))
SAVE_NAME = "itsame.json"
VIDEO_SIZE = (800, 600)
WINDOW_SIZE = (800, 600)
VIDEO_FPS = 15
FRAME_BYTES = VIDEO_SIZE[0] * VIDEO_SIZE[1] * 3
TWEEN_SECONDS = 0.75
COIN_POPUPS = []
NOTIFICATIONS = None


def app_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "MarioAssistant"


def has_save_data(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict)


def write_save_data(path: Path, updates: dict | None = None) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if "wandering" not in data and "shuffling" in data:
        data["wandering"] = data["shuffling"]
    data.pop("shuffling", None)
    data.setdefault("greeting_seen", True)
    data.setdefault("mariocoins", 0)
    data.setdefault("hunger", 100.0)
    data.setdefault("thirst", 100.0)
    data.setdefault("zen_mode", False)
    data.setdefault("super_zen_mode", False)
    for name, value in DEFAULT_SETTINGS.items():
        data.setdefault(name, value)
    if updates:
        data.update(updates)
    data["needs_saved_at"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def decode_last_frame(ffmpeg: str, video: Path) -> pygame.Surface:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-sseof", "-1",
         "-i", str(video),
         "-vf", f"scale=800:600:flags=fast_bilinear,fps={VIDEO_FPS}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    if len(result.stdout) < FRAME_BYTES:
        raise RuntimeError("Could not decode the last video frame.")
    last_frame = result.stdout[-FRAME_BYTES:]
    return pygame.image.frombuffer(last_frame, VIDEO_SIZE, "RGB").copy()


def decode_first_frame(ffmpeg: str, video: Path) -> pygame.Surface:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-vf", "scale=800:600:flags=fast_bilinear", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    if len(result.stdout) != FRAME_BYTES:
        raise RuntimeError(f"Could not decode the first frame of {video.name}.")
    return pygame.image.frombuffer(result.stdout, VIDEO_SIZE, "RGB").copy()


def start_decoder(ffmpeg: str, video: Path):
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-an", "-vf", f"scale=800:600:flags=fast_bilinear,fps={VIDEO_FPS}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    frames: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
    stop = threading.Event()
    reader = threading.Thread(
        target=read_frames, args=(process.stdout, frames, stop), daemon=True
    )
    reader.start()
    return process, frames, stop, reader


def stop_decoder(process, stop, reader):
    stop.set()
    if process is not None:
        if process.poll() is None:
            process.terminate()
        if process.stdout is not None:
            process.stdout.close()
        process.wait()
    if reader is not None:
        reader.join(timeout=0.25)


def start_media(ffmpeg, temp_dir, video_texture, process, stop, reader, video):
    stop_decoder(process, stop, reader)
    audio_path = Path(temp_dir.name) / f"{video.stem}.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    audio = pygame.mixer.Sound(str(audio_path))
    frame = decode_first_frame(ffmpeg, video)
    last_frame = decode_last_frame(ffmpeg, video)
    video_texture.update(frame)
    process, frames, stop, reader = start_decoder(ffmpeg, video)
    audio.play()
    return audio, frame, last_frame, process, frames, stop, reader


def create_context_menu(Gtk, menu_action, stop_only=False):
    menu = Gtk.Menu()

    def keep_menu_above(widget):
        popup = widget.get_toplevel()
        if isinstance(popup, Gtk.Window):
            popup.set_keep_above(True)

    menu.connect("map", keep_menu_above)
    options = (("Stop", "stop"),) if stop_only else (
        ("Stop", "stop"),
        ("Move", "move"),
        ("IM Minigames", "minigames"),
        ("Super Debug", "superdebug"),
        ("Pet Care", "petcare"),
        ("Settings", "settings"),
        ("Speak", "speak"),
        ("Quench hunger", "quench_hunger"),
        ("Quench thirst", "quench_thirst"),
        ("Goodbye", "goodbye"),
    )
    for label, action in options:
        item = Gtk.MenuItem.new_with_label(label)
        item.connect(
            "activate",
            lambda _, selected=action: menu_action.__setitem__(0, selected),
        )
        menu.append(item)
    menu.show_all()
    return menu


def create_minigames_window(QtWidgets, minigame_action):
    window = QtWidgets.QWidget()
    window.setWindowTitle("Interactive Mario Minigames")
    window.setMinimumSize(360, 300)
    layout = QtWidgets.QVBoxLayout(window)
    game_list = QtWidgets.QListWidget()
    game_list.addItem("Hey, I Got Something In My Eye")
    game_list.addItem("Mario's Tunnel of Doom")
    game_list.setCurrentRow(0)
    play_button = QtWidgets.QPushButton("Play!")
    play_button.clicked.connect(
        lambda: minigame_action.__setitem__(
            0, "eye" if game_list.currentRow() == 0 else "tunnel"
        )
    )
    layout.addWidget(game_list)
    layout.addWidget(play_button)
    return window

def create_debug_window(QtWidgets, live_values):
    window = QtWidgets.QWidget()
    window.setWindowTitle("SUPER DEBUG!!!!")
    window.setMinimumSize(360, 230)
    layout = QtWidgets.QVBoxLayout(window)
    mariocoins_label = QtWidgets.QLabel()
    zen_mode_label = QtWidgets.QLabel()
    super_zen_mode_label = QtWidgets.QLabel()
    eye_schedule_label = QtWidgets.QLabel()
    idle_schedule_label = QtWidgets.QLabel()
    wander_schedule_label = QtWidgets.QLabel()
    hunger_schedule_label = QtWidgets.QLabel()
    thirst_schedule_label = QtWidgets.QLabel()
    hunger_label = QtWidgets.QLabel()
    thirst_label = QtWidgets.QLabel()

    value_labels = {
        "mariocoins": mariocoins_label,
        "zen_mode": zen_mode_label,
        "super_zen_mode": super_zen_mode_label,
        "eye_schedule": eye_schedule_label,
        "idle_schedule": idle_schedule_label,
        "wander_schedule": wander_schedule_label,
        "hunger_schedule": hunger_schedule_label,
        "thirst_schedule": thirst_schedule_label,
        "hunger": hunger_label,
        "thirst": thirst_label,
    }

    def update_value(name=None, force=False):
        if not force and not window.isVisible():
            return
        if name is None:
            names = value_labels
        else:
            names = (name,)
        for value_name in names:
            label = value_labels[value_name]
            if value_name == "mariocoins":
                label.setText(f"MarioCoins: {live_values[value_name]}")
            elif value_name in ("hunger", "thirst"):
                label.setText(f"{value_name.capitalize()}: {live_values[value_name]:.1f}%")
            elif value_name == "zen_mode":
                label.setText(f"Zen Mode: {live_values[value_name]}")
            elif value_name == "super_zen_mode":
                label.setText(f"Super Zen Mode: {live_values[value_name]}")
            else:
                label.setText(live_values.get(value_name, ""))

    layout.addWidget(mariocoins_label)
    layout.addWidget(zen_mode_label)
    layout.addWidget(super_zen_mode_label)
    layout.addWidget(eye_schedule_label)
    layout.addWidget(idle_schedule_label)
    layout.addWidget(wander_schedule_label)
    layout.addWidget(hunger_schedule_label)
    layout.addWidget(thirst_schedule_label)
    layout.addWidget(hunger_label)
    layout.addWidget(thirst_label)
    update_value(force=True)
    return window, update_value


def show_coin_change(QtCore, QtWidgets, previous, current):
    amount = abs(current - previous)
    if not amount:
        return
    popup = QtWidgets.QWidget()
    popup.setWindowFlags(
        QtCore.Qt.WindowType.FramelessWindowHint
        | QtCore.Qt.WindowType.Tool
        | QtCore.Qt.WindowType.WindowStaysOnTopHint
    )
    label = QtWidgets.QLabel(
        f"MarioCoins: {current} ({'+' if current > previous else '-'}{amount})"
    )
    coin_font = label.font()
    coin_font.setPointSizeF(coin_font.pointSizeF() * 2.5)
    coin_font.setBold(True)
    label.setFont(coin_font)
    label.setStyleSheet(
        "QLabel { background: #202020; color: white; padding: 8px; }"
    )
    layout = QtWidgets.QVBoxLayout(popup)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(label)
    popup.adjustSize()
    target = popup.sizeHint()
    screen = QtWidgets.QApplication.primaryScreen()
    if screen is not None:
        popup.move(screen.geometry().topLeft())
    popup.show()
    popup.raise_()
    popup.setMinimumSize(0, 0)
    popup.resize(1, 1)
    started = time.monotonic()
    state = ["in"]

    def animate():
        elapsed = time.monotonic() - started
        if state[0] == "in":
            t = min(1.0, elapsed / TWEEN_SECONDS)
            eased = back_in_out(t)
            popup.resize(
                max(1, round(1 + (target.width() - 1) * eased)),
                max(1, round(1 + (target.height() - 1) * eased)),
            )
            if t >= 1:
                state[0] = "hold"
                timer.stop()
                QtCore.QTimer.singleShot(1750, begin_out)
        else:
            t = min(1.0, elapsed / TWEEN_SECONDS)
            eased = back_in_out(t)
            popup.resize(
                max(1, round(target.width() + (1 - target.width()) * eased)),
                max(1, round(target.height() + (1 - target.height()) * eased)),
            )
            if t >= 1:
                popup.close()
                timer.stop()

    def begin_out():
        state[0] = "out"
        nonlocal started
        started = time.monotonic()
        timer.start(16)

    timer = QtCore.QTimer(popup)
    timer.timeout.connect(animate)
    popup._coin_animation_timer = timer
    COIN_POPUPS.append(popup)
    popup.destroyed.connect(lambda: COIN_POPUPS.remove(popup) if popup in COIN_POPUPS else None)
    timer.start(16)

def show_context_menu(Gtk, Gdk, menu_action, active_menu, stop_only=False):
    if Gtk is None:
        from PySide6 import QtGui, QtWidgets
        if active_menu[0] is not None:
            active_menu[0].close()
        menu = QtWidgets.QMenu()
        options = (("Stop", "stop"),) if stop_only else (
            ("Stop", "stop"), ("Move", "move"), ("IM Minigames", "minigames"),
            ("Super Debug", "superdebug"), ("Pet Care", "petcare"),
            ("Settings", "settings"), ("Speak", "speak"),
            ("Quench hunger", "quench_hunger"),
            ("Quench thirst", "quench_thirst"), ("Goodbye", "goodbye"),
        )
        for label, action in options:
            item = menu.addAction(label)
            item.triggered.connect(lambda checked=False, selected=action: menu_action.__setitem__(0, selected))
        active_menu[0] = menu
        menu.popup(QtGui.QCursor.pos())
        return
    if active_menu[0] is not None:
        active_menu[0].popdown()
        active_menu[0].destroy()
    menu = create_context_menu(Gtk, menu_action, stop_only)
    _, x, y = (
        Gdk.Display.get_default()
        .get_default_seat()
        .get_pointer()
        .get_position()
    )
    rect = Gdk.Rectangle()
    rect.x = x
    rect.y = y
    rect.width = 1
    rect.height = 1
    root_window = Gdk.get_default_root_window()
    trigger_event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    trigger_event.button.window = root_window
    trigger_event.button.send_event = True
    trigger_event.button.time = Gdk.CURRENT_TIME
    trigger_event.button.x = x
    trigger_event.button.y = y
    trigger_event.button.x_root = x
    trigger_event.button.y_root = y
    trigger_event.button.button = 3
    trigger_event.button.device = (
        Gdk.Display.get_default().get_default_seat().get_pointer()
    )
    active_menu[0] = menu
    menu.popup_at_rect(
        root_window,
        rect,
        Gdk.Gravity.NORTH_WEST,
        Gdk.Gravity.NORTH_WEST,
        trigger_event,
    )


def create_tray_menu(QtWidgets, tray, menu_action, live_values):
    menu = QtWidgets.QMenu()
    stats = {}
    for name in ("mariocoins", "hunger", "thirst"):
        action = menu.addAction("")
        action.setEnabled(False)
        stats[name] = action
    menu.addSeparator()
    for label, action_name in (
        ("Stop", "stop"),
        ("Move", "move"),
        ("IM Minigames", "minigames"),
        ("Super Debug", "superdebug"),
        ("Settings", "settings"),
        ("Speak", "speak"),
        ("Quench hunger", "quench_hunger"),
        ("Quench thirst", "quench_thirst"),
        ("Goodbye", "goodbye"),
    ):
        action = menu.addAction(label)
        action.triggered.connect(
            lambda checked=False, selected=action_name: menu_action.__setitem__(0, selected)
        )
    tray.setContextMenu(menu)
    previous = {}

    def update():
        values = {
            "mariocoins": f"MarioCoins: {live_values['mariocoins']}",
            "hunger": f"Hunger: {live_values['hunger']:.1f}%",
            "thirst": f"Thirst: {live_values['thirst']:.1f}%",
        }
        for name, value in values.items():
            if previous.get(name) != value:
                stats[name].setText(value)
                previous[name] = value

    update()
    return update


def set_tray_stop_menu(QtWidgets, tray, menu_action):
    menu = QtWidgets.QMenu()
    action = menu.addAction("Stop")
    action.triggered.connect(
        lambda checked=False: menu_action.__setitem__(0, "stop")
    )
    tray.setContextMenu(menu)
    return menu


def show_full_meter_message(meter, hunger, thirst):
    import tkinter
    from tkinter import messagebox
    if hunger >= 99.9 and thirst >= 99.9:
        message = "Mario is full at the moment. Remember: Left half for food and right half for drinks!"
    else:
        item = "food" if meter == "hunger" else "drinks"
        message = f"Mario is full on {item} at the moment. Remember: Left half for food and right half for drinks!"
    root = tkinter.Tk()
    root.withdraw()
    messagebox.showerror(
        "Hey!",
        message,
        parent=root,
    )
    root.destroy()


def send_notification(Gio, GLib, notification_id=0, title="Hey!", body="I got something in my eye, can you help me get it out?"):
    if Gio is None:
        return NOTIFICATIONS.show(title, body)
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    parameters = GLib.Variant(
        "(susssasa{sv}i)",
        (
            "MarioAssistant",
            notification_id,
            "",
            title,
            body,
            [],
            {
                "desktop-entry": GLib.Variant("s", "MarioAssistant"),
                "sound-name": GLib.Variant("s", "message-new-instant"),
            },
            0,
        ),
    )
    result = connection.call_sync(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
        parameters,
        GLib.VariantType.new("(u)"),
        Gio.DBusCallFlags.NONE,
        2000,
        None,
    )
    return connection, result.unpack()[0]


def close_notification(Gio, GLib, notification_id):
    if notification_id is None:
        return
    if Gio is None:
        notification_id.close()
        return
    connection, current_id = notification_id
    connection.call_sync(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "CloseNotification",
        GLib.Variant("(u)", (current_id,)),
        None,
        Gio.DBusCallFlags.NONE,
        2000,
        None,
    )


def start_apology(ffmpeg, temp_dir, video_texture, process, stop, reader):
    video = ASSETS / "ohsorry.mp4"
    if not video.is_file():
        raise FileNotFoundError(video)
    result = start_media(
        ffmpeg, temp_dir, video_texture, process, stop, reader, video
    )
    return (*result, video)


def read_frame(pipe) -> bytes:
    data = bytearray(FRAME_BYTES)
    view = memoryview(data)
    offset = 0
    while offset < FRAME_BYTES:
        count = pipe.readinto(view[offset:])
        if not count:
            break
        offset += count
    return bytes(data) if offset == FRAME_BYTES else bytes(view[:offset])


def read_frames(pipe, frames: queue.Queue, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            frame = read_frame(pipe)
            if len(frame) != FRAME_BYTES:
                break
            while not stop.is_set():
                try:
                    frames.put(frame, timeout=0.1)
                    break
                except queue.Full:
                    continue
    except (OSError, ValueError):
        pass
    finally:
        while not stop.is_set():
            try:
                frames.put(None, timeout=0.1)
                break
            except queue.Full:
                continue



def position_number(maximum, preferences):
    if preferences["use_entropy_for_position"]:
        return secrets.randbelow(maximum + 1)
    return random.randint(0, maximum)



def wander_destination(area, size, current, preferences):
    x, y, width, height = area
    travel_x = max(0, width - size[0])
    travel_y = max(0, height - size[1])
    margin_x = min(max(64, round(travel_x * 0.12)), travel_x // 4)
    margin_y = min(max(64, round(travel_y * 0.12)), travel_y // 4)
    left, right = x + margin_x, x + travel_x - margin_x
    top, bottom = y + margin_y, y + travel_y - margin_y
    candidates = [
        (left + position_number(right - left, preferences),
         top + position_number(bottom - top, preferences))
        for _ in range(12)
    ]
    minimum_distance = min(150, max(right - left, bottom - top) * 0.3)
    destination = next((point for point in candidates
                        if math.dist(current, point) >= minimum_distance), None)
    if destination is None:
        destination = max(((a, b) for a in (left, right) for b in (top, bottom)),
                          key=lambda point: math.dist(current, point))
    return destination, (x, y, x + travel_x, y + travel_y)

def run_goodbye(ffmpeg, temp_dir, window, renderer, texture, process, stop, reader, qt_app, Gtk, Gdk, QtWidgets, tray):
    clock = pygame.time.Clock()
    menu_action = [None]
    active_menu = [None]
    controls = {"star": False, "minus": False, "skip": False}
    set_tray_stop_menu(QtWidgets, tray, menu_action)

    def pump():
        qt_app.processEvents()
        while Gtk is not None and Gtk.events_pending():
            Gtk.main_iteration_do(False)
        for event in pygame.event.get():
            if event.type == pygame.MOUSEBUTTONUP and event.button == 3:
                show_context_menu(Gtk, Gdk, menu_action, active_menu, True)
            elif event.type == pygame.KEYDOWN:
                if (event.key == pygame.K_KP_MULTIPLY
                        or getattr(event, "unicode", "") == "*"
                        or (event.key == pygame.K_8 and event.mod & pygame.KMOD_SHIFT)):
                    controls["star"] = True
                if (event.key in (pygame.K_MINUS, pygame.K_KP_MINUS)
                        or getattr(event, "unicode", "") == "-"):
                    controls["minus"] = True
                if controls["star"] and controls["minus"]:
                    controls["skip"] = True
            elif event.type == pygame.KEYUP:
                if event.key in (pygame.K_8, pygame.K_KP_MULTIPLY):
                    controls["star"] = False
                if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    controls["minus"] = False
        renderer.clear()
        texture.draw(dstrect=(0, 0, *window.size))
        renderer.present()
        clock.tick(60)

    def tween(destination, fixed_center=None):
        origin = (*window.position, *window.size)
        started = time.monotonic()
        while True:
            progress = min(1.0, (time.monotonic() - started) / TWEEN_SECONDS)
            eased = back_in_out(progress)
            x, y, width, height = [round(a + (b - a) * eased) for a, b in zip(origin, destination)]
            window.size = (max(1, width), max(1, height))
            if fixed_center is not None:
                x = round(fixed_center[0] - max(1, width) / 2)
                y = round(fixed_center[1] - max(1, height) / 2)
            window.position = (x, y)
            pump()
            if progress >= 1:
                return True

    stop_decoder(process, stop, reader)
    desktop_sizes = pygame.display.get_desktop_sizes()
    desktop_width, desktop_height = desktop_sizes[min(window.display_index, len(desktop_sizes) - 1)]
    x = (desktop_width - WINDOW_SIZE[0]) // 2
    y = (desktop_height - WINDOW_SIZE[1]) // 2
    tween((x, y, *WINDOW_SIZE))
    video = ASSETS / "okbyebye.mp4"
    sound_path = Path(temp_dir.name) / "okbyebye.wav"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-vn", "-ac", "2", "-ar", "44100", str(sound_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
    )
    sound = pygame.mixer.Sound(str(sound_path))
    texture.update(decode_first_frame(ffmpeg, video))
    process, frames, stop, reader = start_decoder(ffmpeg, video)
    started = time.monotonic()
    consumed = 0
    done = False
    try:
        sound.play()
        while not done:
            pump()
            if menu_action[0] == "stop":
                menu_action[0] = None
                controls["skip"] = True
            if controls["skip"]:
                done = True
                break
            target = int((time.monotonic() - started) * VIDEO_FPS) + 1
            while consumed < target:
                try:
                    data = frames.get_nowait()
                except queue.Empty:
                    break
                if data is None:
                    done = True
                    break
                texture.update(pygame.image.frombuffer(data, VIDEO_SIZE, "RGB").copy())
                consumed += 1
    finally:
        sound.stop()
        stop_decoder(process, stop, reader)
    texture.update(decode_last_frame(ffmpeg, video))
    width, height = window.size
    x, y = window.position
    tween((x + width // 2, y + height // 2, 1, 1), (x + width / 2, y + height / 2))
    return True

def main() -> None:
    from PySide6 import QtCore, QtWidgets

    Gtk = Gdk = Gio = GLib = None
    if sys.platform.startswith("linux"):
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gdk, Gio, GLib, Gtk
            Gtk.init([])
            if Gdk.get_default_root_window() is None:
                Gtk = Gdk = None
        except (ImportError, ValueError, RuntimeError):
            Gtk = Gdk = Gio = GLib = None
    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["MarioAssistant"]
    )

    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setApplicationName("MarioAssistant")
    window_policy = install_linux_window_policy(qt_app)
    global NOTIFICATIONS
    NOTIFICATIONS = DesktopNotifications(QtWidgets)
    goodbye_completed = False
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to play the video.")
    save_path = app_data_dir() / SAVE_NAME
    first_run = not has_save_data(save_path)
    try:
        initial_save = json.loads(save_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        initial_save = {}
    if not isinstance(initial_save, dict):
        initial_save = {}
    if "wandering" not in initial_save and "shuffling" in initial_save:
        initial_save["wandering"] = initial_save["shuffling"]
    preferences = dict(DEFAULT_SETTINGS)
    preferences.update({name: initial_save.get(name, value) for name, value in DEFAULT_SETTINGS.items()})
    for name in ("chattiness", "playfulness", "wandering", "caring_difficulty"):
        preferences[name] = max(0, min(10, int(preferences[name])))
    pet = PetNeeds(
        initial_save, difficulty=preferences["caring_difficulty"]
    )
    pet_drops = []

    def queue_pet_drop(filename, drop_x=None):
        if drop_x is None:
            drop_x = pygame.mouse.get_pos()[0]
        side = "hunger" if drop_x < window.size[0] / 2 else "thirst"
        pet_drops.append((filename, side))
    last_pet_save = time.monotonic()
    mariocoins = int(initial_save.get("mariocoins", 0))
    zen_mode = bool(initial_save.get("zen_mode", False))
    super_zen_mode = bool(initial_save.get("super_zen_mode", False))
    live_values = {
        "mariocoins": mariocoins,
        "zen_mode": zen_mode,
        "super_zen_mode": super_zen_mode,
    }
    live_values.update(pet.snapshot())
    if not first_run and ("shuffling" in initial_save or any(name not in initial_save for name in DEFAULT_SETTINGS)):
        write_save_data(save_path, {**preferences, **pet.snapshot()})
    video = ASSETS / ("firsttimegreeting.mp4" if first_run else "greeting.avi")
    if not video.is_file():
        raise FileNotFoundError(video)
    pygame.init()
    window = Window(
        "MarioAssistant",
        size=WINDOW_SIZE,
        borderless=False,
        resizable=True,
        always_on_top=True,
    )
    keep_sdl_window_above(window)
    pet_attention_active = any(pet.needy.values())
    window.title = "MarioAssistant (Hey!)" if pet_attention_active else "MarioAssistant"
    if pet_attention_active:
        set_sdl_window_attention(window, True)
    renderer = Renderer(window, accelerated=False)
    preparing_texture = Texture(renderer, WINDOW_SIZE, streaming=True)
    clock = pygame.time.Clock()
    font = pygame.font.Font(None, 30)
    process = None
    reader = None
    stop = threading.Event()
    temp_dir = tempfile.TemporaryDirectory(prefix="marioassistant-")
    star_down = False
    minus_down = False
    plus_down = False
    slash_down = False
    chord_latched = False
    zen_chord_latched = False
    super_zen_chord_latched = False
    finished = False
    frame_surface = None
    frame_dirty = False
    try:
        preparing_surface = pygame.Surface(WINDOW_SIZE)
        preparing_surface.fill((0, 0, 0))
        label = font.render("Preparing videos 1 of 1…", True, (235, 235, 235))
        preparing_surface.blit(label, label.get_rect(center=(400, 300)))
        preparing_texture.update(preparing_surface)
        renderer.clear()
        preparing_texture.draw(dstrect=(0, 0, *WINDOW_SIZE))
        renderer.present()

        pygame.mixer.init(frequency=44_100, size=-16, channels=2, buffer=1024)
        voice_beep_sound = create_beep_sound(pygame)
        voice_beep_channel = pygame.mixer.Channel(7)
        voice_transcriber = SpeechTranscriber(ASSETS)
        voice_results = queue.Queue()
        voice_processing = False
        voice_animation_deadline = 0.0
        voice_animation_done = False
        voice_keys_down = set()
        voice_control_keys = {pygame.K_LCTRL, pygame.K_RCTRL}
        voice_shift_keys = {pygame.K_LSHIFT, pygame.K_RSHIFT}
        voice_state = {
            "phase": "idle",
            "step": 0,
            "deadline": 0.0,
            "capture": None,
            "samples": None,
            "error": None,
        }

        def voice_chord_held():
            return (
                bool(voice_keys_down & voice_control_keys)
                and bool(voice_keys_down & voice_shift_keys)
                and pygame.K_l in voice_keys_down
                and pygame.key.get_focused()
            )

        def begin_voice_beeps(phase):
            voice_state["phase"] = phase
            voice_state["step"] = 0
            voice_state["deadline"] = time.monotonic() + 0.25
            voice_beep_channel.play(voice_beep_sound)

        def finish_voice_capture():
            phase = voice_state["phase"]
            if phase == "listening":
                try:
                    voice_state["samples"] = voice_state["capture"].stop()
                    voice_state["capture"] = None
                except Exception as exc:
                    voice_state["error"] = exc
                    voice_state["capture"] = None
            else:
                voice_state["samples"] = None
            begin_voice_beeps("finishing")

        def report_voice_transcript(engine, transcript):
            action = interpret_voice_command(transcript)
            engine_name = "Google" if engine == "google" else "Vosk"
            if action is None:
                result = "did not match a supported command"
            else:
                result = f"got it right: command {action!r}"
            print(
                f"MarioAssistant voice: {engine_name} {result}; "
                f"transcript: {transcript!r}",
                file=sys.stderr,
                flush=True,
            )
            return action

        def transcribe_voice(samples):
            try:
                transcript = voice_transcriber.transcribe(samples)
                engine = voice_transcriber.last_engine
                action = report_voice_transcript(engine, transcript)
                if engine == "vosk" and action is None:
                    print(
                        "MarioAssistant voice: trying Google SpeechRecognition "
                        "because Vosk did not match a command",
                        file=sys.stderr,
                        flush=True,
                    )
                    transcript = voice_transcriber.transcribe_google(samples)
                    engine = voice_transcriber.last_engine
                    action = report_voice_transcript(engine, transcript)
                if action is None:
                    print(
                        "MarioAssistant voice: neither recognizer matched a supported command",
                        file=sys.stderr,
                        flush=True,
                    )
                voice_results.put((transcript, None))
            except Exception as exc:
                voice_results.put((None, exc))

        audio_path = Path(temp_dir.name) / f"{video.stem}.wav"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        audio = pygame.mixer.Sound(str(audio_path))
        disappointment_sound = pygame.mixer.Sound(str(ASSETS / "disappointment.wav"))
        kaching_sound = pygame.mixer.Sound(str(ASSETS / "kaching.wav"))

        frame_surface = decode_first_frame(ffmpeg, video)
        last_frame_surface = decode_last_frame(ffmpeg, video)
        greeting_first_surface = decode_first_frame(ffmpeg, ASSETS / "greeting.avi")

        desktop_width, desktop_height = pygame.display.get_desktop_sizes()[0]
        center = (desktop_width // 2, desktop_height // 2)
        video_texture = Texture(renderer, VIDEO_SIZE, streaming=True)
        menu_action = [None]
        active_menu = [None]
        set_tray_stop_menu(QtWidgets, NOTIFICATIONS.tray, menu_action)
        booting = True
        startup_goodbye = False
        window.size = (1, 1)
        window.position = (center[0], center[1])
        video_texture.update(frame_surface)
        tween_started = time.monotonic()
        while True:
            for event in pygame.event.get():
                if event.type in (pygame.QUIT, getattr(pygame, "WINDOWCLOSE", -1)):
                    startup_goodbye = True
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 3:
                    show_context_menu(
                        Gtk, Gdk, menu_action, active_menu, stop_only=True
                    )
            if startup_goodbye:
                goodbye_completed = run_goodbye(
                    ffmpeg, temp_dir, window, renderer, video_texture,
                    process, stop, reader, qt_app, Gtk, Gdk, QtWidgets,
                    NOTIFICATIONS.tray,
                )
                return
            progress = min(1.0, (time.monotonic() - tween_started) / TWEEN_SECONDS)
            eased = back_in_out(progress)
            width = max(1, round(1 + (WINDOW_SIZE[0] - 1) * eased))
            height = max(1, round(1 + (WINDOW_SIZE[1] - 1) * eased))
            if tuple(window.size) != (width, height):
                window.size = (width, height)
            window.position = (center[0] - width // 2, center[1] - height // 2)
            renderer.draw_color = (0, 0, 0, 255)
            renderer.clear()
            video_texture.draw(dstrect=(0, 0, width, height))
            renderer.present()
            if progress >= 1:
                break
            clock.tick(60)

        process, frames, stop, reader = start_decoder(ffmpeg, video)
        consumed = 0
        started = time.monotonic()
        save_written = False
        post_greeting_tween = None
        greeting_tween_started = False
        is_greeting = True
        last_window_position = tuple(window.position)
        window_move_changed_at = None
        tray_update = None
        minigame_action = [None]
        minigames_window = None
        debug_window = None
        debug_update = None
        last_debug_schedule_update = 0.0
        care_window = None
        care_update = None
        pet_video = None
        pet_alert_need = None
        pet_notification_close_at = {}

        def finish_pet_alert():
            nonlocal pet_alert_need
            if pet_alert_need is not None:
                pet_notification_close_at[pet_alert_need] = time.monotonic() + 7.5
                pet_alert_need = None
        settings_window = None
        settings_sync = None
        settings_changes = []
        eye_manual = False
        move_tween = None
        eye_video = ASSETS / "somethinginmyeye.avi"
        eye_event = None
        eye_notification = None
        pet_notifications = {}
        next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
        next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
        idle_playing = False
        next_wander_at = time.monotonic() + random_delay(preferences["wandering"], 60, 120)
        random_events_ready_at = 0.0
        wall_clock_offset = time.time() - time.monotonic()
        audio.play()
        if pet.depletion_count:
            disappointment_sound.play()

        running = True
        goodbye_running = False
        while running:
            clock.tick(60)
            qt_app.processEvents()
            now = time.monotonic()
            if (voice_state["phase"] in ("arming", "finishing")
                    and now >= voice_state["deadline"]):
                if voice_state["step"] == 0:
                    voice_state["step"] = 1
                    voice_state["deadline"] = now + 0.25
                elif voice_state["step"] == 1:
                    voice_state["step"] = 2
                    voice_beep_channel.play(voice_beep_sound)
                    voice_state["deadline"] = now + 0.25
                elif voice_state["phase"] == "arming":
                    if voice_chord_held():
                        try:
                            voice_state["capture"] = MicrophoneCapture()
                            voice_state["phase"] = "listening"
                        except Exception as exc:
                            voice_state["error"] = exc
                            finish_voice_capture()
                    else:
                        finish_voice_capture()
                else:
                    voice_state["phase"] = "idle"
                    if voice_state["error"] is not None:
                        voice_results.put((None, voice_state["error"]))
                    elif voice_state["samples"] is not None:
                        voice_processing = True
                        voice_animation_done = False
                        audio.stop()
                        if pet_video is not None:
                            finish_pet_alert()
                            pet_video = None
                        if eye_event is not None:
                            try:
                                close_notification(Gio, GLib, eye_notification)
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                            eye_notification = None
                            eye_event = None
                            if not eye_manual:
                                next_eye_at = time.monotonic() + random_delay(
                                    preferences["playfulness"], 25, 75
                                )
                            eye_manual = False
                        if idle_playing:
                            idle_playing = False
                            next_idle_at = time.monotonic() + idle_delay(
                                preferences["chattiness"]
                            )
                        move_tween = None
                        post_greeting_tween = None
                        greeting_tween_started = True
                        is_greeting = False
                        (
                            audio, frame_surface, last_frame_surface, process,
                            frames, stop, reader,
                        ) = start_media(
                            ffmpeg, temp_dir, video_texture, process, stop, reader,
                            ASSETS / "niceplace.avi",
                        )
                        video = ASSETS / "niceplace.avi"
                        started = time.monotonic()
                        consumed = 0
                        finished = False
                        frame_dirty = False
                        voice_animation_deadline = started + 1.15
                        threading.Thread(
                            target=transcribe_voice,
                            args=(voice_state["samples"],),
                            daemon=True,
                        ).start()
                    voice_state["samples"] = None
                    voice_state["error"] = None
            try:
                transcript, voice_error = voice_results.get_nowait()
            except queue.Empty:
                pass
            else:
                if voice_processing:
                    voice_processing = False
                    audio.stop()
                    if not finished:
                        stop_decoder(process, stop, reader)
                    finished = True
                    frame_surface = greeting_first_surface
                    last_frame_surface = greeting_first_surface
                    frame_dirty = True
                    video = ASSETS / "greeting.avi"
                    if voice_error is not None:
                        print(
                            f"MarioAssistant voice: request failed: "
                            f"{type(voice_error).__name__}: {voice_error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        menu_action[0] = "voice_apology"
                    else:
                        voice_action = interpret_voice_command(transcript)
                        if voice_action in ("tunnel", "eye"):
                            minigame_action[0] = voice_action
                        else:
                            menu_action[0] = voice_action or "voice_apology"
                else:
                    if voice_error is not None:
                        print(
                            f"MarioAssistant voice: request failed: "
                            f"{type(voice_error).__name__}: {voice_error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        menu_action[0] = "voice_apology"
                    else:
                        voice_action = interpret_voice_command(transcript)
                        if voice_action in ("tunnel", "eye"):
                            minigame_action[0] = voice_action
                        else:
                            menu_action[0] = voice_action or "voice_apology"
            if voice_processing and not voice_animation_done and now >= voice_animation_deadline:
                audio.stop()
                if not finished:
                    stop_decoder(process, stop, reader)
                    finished = True
                if frame_surface is not None:
                    last_frame_surface = frame_surface
                voice_animation_done = True
            if pet.tick():
                disappointment_sound.play()
                for need_name in tuple(pet_notifications):
                    try:
                        close_notification(Gio, GLib, pet_notifications.pop(need_name))
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                write_save_data(save_path, pet.snapshot())
                last_pet_save = time.monotonic()
            if move_tween is not None and move_tween.get("wander"):
                observed = tuple(window.position)
                requested = move_tween["requested_positions"]
                if not any(abs(observed[0] - x) <= 3 and abs(observed[1] - y) <= 3
                           for x, y in requested):
                    move_tween = None
                    window_move_changed_at = None
                    last_window_position = observed
            for name, value in settings_changes:
                preferences[name] = value
                if name == "caring_difficulty":
                    pet.set_difficulty(value)
                write_save_data(save_path, {name: value, **pet.snapshot()})
                if name == "zen_mode":
                    zen_mode = value
                elif name == "super_zen_mode":
                    super_zen_mode = value
                elif name == "chattiness":
                    next_idle_at = time.monotonic() + idle_delay(value)
                    if value == 0 and idle_playing:
                        audio.stop()
                        stop_decoder(process, stop, reader)
                        idle_playing = False
                        finished = True
                        frame_surface = greeting_first_surface
                        frame_dirty = True
                elif name == "playfulness":
                    next_eye_at = time.monotonic() + random_delay(value, 25, 75)
                    if value == 0 and eye_event is not None and not eye_manual:
                        audio.stop()
                        stop_decoder(process, stop, reader)
                        try:
                            close_notification(Gio, GLib, eye_notification)
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                        eye_notification = None
                        eye_event = None
                        finished = True
                        frame_surface = greeting_first_surface
                        frame_dirty = True
                elif name == "wandering":
                    next_wander_at = time.monotonic() + random_delay(value, 60, 120)
                    if value == 0 and move_tween is not None and move_tween.get("wander"):
                        move_tween = None
                        last_window_position = tuple(window.position)
                elif name == "caring_difficulty" and value == 0:
                    pet_notification_close_at.clear()
                    pet_alert_need = None
                    for need_name in tuple(pet_notifications):
                        try:
                            close_notification(
                                Gio, GLib, pet_notifications.pop(need_name)
                            )
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    if pet_video is not None:
                        audio.stop()
                        stop_decoder(process, stop, reader)
                        pet_video = None
                        finished = True
                        frame_surface = greeting_first_surface
                        frame_dirty = True
                if name in live_values:
                    live_values[name] = value
                    if debug_update is not None:
                        debug_update(name)
            settings_changes.clear()
            collect_eye = False
            skip_requested = False
            for event in pygame.event.get():
                if event.type in (pygame.WINDOWSHOWN, pygame.WINDOWRESTORED):
                    keep_sdl_window_above(window)
                elif event.type == getattr(pygame, "WINDOWFOCUSLOST", -1):
                    voice_keys_down.clear()
                    if voice_state["phase"] in ("arming", "listening"):
                        finish_voice_capture()
                elif event.type in (pygame.QUIT, getattr(pygame, "WINDOWCLOSE", -1)):
                    if not goodbye_running:
                        menu_action[0] = "goodbye"
                elif event.type == pygame.DROPFILE:
                    queue_pet_drop(event.file)
                elif event.type == pygame.KEYDOWN:
                    if (event.key in voice_control_keys or event.key in voice_shift_keys
                            or event.key == pygame.K_l):
                        voice_keys_down.add(event.key)
                        if voice_state["phase"] == "idle" and voice_chord_held():
                            voice_state["samples"] = None
                            voice_state["error"] = None
                            begin_voice_beeps("arming")
                    if (event.key == pygame.K_KP_MULTIPLY
                            or getattr(event, "unicode", "") == "*"
                            or (event.key == pygame.K_8 and event.mod & pygame.KMOD_SHIFT)):
                        star_down = True
                    if (event.key in (pygame.K_MINUS, pygame.K_KP_MINUS)
                            or getattr(event, "unicode", "") == "-"):
                        minus_down = True
                    if (event.key in (pygame.K_EQUALS, pygame.K_KP_PLUS, pygame.K_PLUS)
                            or getattr(event, "unicode", "") == "+"):
                        plus_down = True
                    if (event.key == pygame.K_SLASH
                            or getattr(event, "unicode", "") == "/"):
                        slash_down = True
                    if minus_down and plus_down and not zen_chord_latched:
                        zen_chord_latched = True
                        zen_mode = not zen_mode
                        preferences["zen_mode"] = zen_mode
                        if settings_sync is not None:
                            settings_sync("zen_mode", zen_mode)
                        live_values["zen_mode"] = zen_mode
                        if debug_update is not None:
                            debug_update("zen_mode")
                        write_save_data(save_path, {"zen_mode": zen_mode, **pet.snapshot()})
                    if slash_down and minus_down and not super_zen_chord_latched:
                        super_zen_chord_latched = True
                        super_zen_mode = not super_zen_mode
                        preferences["super_zen_mode"] = super_zen_mode
                        if settings_sync is not None:
                            settings_sync("super_zen_mode", super_zen_mode)
                        live_values["super_zen_mode"] = super_zen_mode
                        if debug_update is not None:
                            debug_update("super_zen_mode")
                        write_save_data(
                            save_path, {"super_zen_mode": super_zen_mode, **pet.snapshot()}
                        )
                    if star_down and minus_down and not chord_latched:
                        chord_latched = True
                        skip_requested = True
                elif event.type == pygame.KEYUP:
                    voice_keys_down.discard(event.key)
                    if (voice_state["phase"] in ("arming", "listening")
                            and not voice_chord_held()):
                        finish_voice_capture()
                    if event.key in (pygame.K_8, pygame.K_KP_MULTIPLY):
                        star_down = False
                    if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        minus_down = False
                    if (event.key in (pygame.K_EQUALS, pygame.K_KP_PLUS, pygame.K_PLUS)
                            or getattr(event, "unicode", "") == "+"):
                        plus_down = False
                    if (event.key == pygame.K_SLASH
                            or getattr(event, "unicode", "") == "/"):
                        slash_down = False
                    if not (minus_down and plus_down):
                        zen_chord_latched = False
                    if not (slash_down and minus_down):
                        super_zen_chord_latched = False
                    if not (star_down and minus_down):
                        chord_latched = False
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 3:
                    show_context_menu(
                        Gtk, Gdk, menu_action, active_menu, stop_only=booting
                    )
                elif (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                      and eye_event is not None
                      and time.monotonic() - eye_event >= 2.75):
                    collect_eye = True

            for dropped_file, target_need in pet_drops:
                if preferences["caring_difficulty"] == 0:
                    continue
                try:
                    fed = pet.feed(dropped_file, target=target_need)
                except OSError:
                    fed = None
                if fed == "full":
                    levels = pet.snapshot()
                    show_full_meter_message(
                        target_need, levels["hunger"], levels["thirst"]
                    )
                    continue
                if fed is None:
                    disappointment_sound.play()
                    continue
                if not pet.needy[fed] and fed in pet_notifications:
                    try:
                        close_notification(Gio, GLib, pet_notifications.pop(fed))
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    pet_notification_close_at.pop(fed, None)
                    if pet_alert_need == fed:
                        pet_alert_need = None
                if pet_video is not None:
                    audio.stop()
                    stop_decoder(process, stop, reader)
                    finish_pet_alert()
                    pet_video = None
                    finished = True
                    frame_surface = greeting_first_surface
                    frame_dirty = True
                    random_events_ready_at = time.monotonic() + 10
                previous_mariocoins = mariocoins
                mariocoins += 50
                live_values["mariocoins"] = mariocoins
                write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                last_pet_save = time.monotonic()
                kaching_sound.play()
                show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                if debug_update is not None:
                    debug_update("mariocoins")
            pet_drops.clear()

            for need_name, deadline in tuple(pet_notification_close_at.items()):
                if time.monotonic() >= deadline:
                    notification = pet_notifications.pop(need_name, None)
                    if notification is not None:
                        try:
                            close_notification(Gio, GLib, notification)
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    pet_notification_close_at.pop(need_name, None)

            while Gtk is not None and Gtk.events_pending():
                Gtk.main_iteration_do(False)

            if skip_requested or menu_action[0] == "stop":
                if menu_action[0] == "stop":
                    menu_action[0] = None
                collect_eye = False
                finish_pet_alert()
                pet_video = None
                finished = True
                random_events_ready_at = time.monotonic() + 10
                stop.set()
                if process.poll() is None:
                    process.terminate()
                audio.stop()
                frame_surface = last_frame_surface
                frame_dirty = True
                if idle_playing:
                    idle_playing = False
                    next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                if eye_event is not None:
                    try:
                        close_notification(Gio, GLib, eye_notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    eye_notification = None
                    disappointment_sound.play()
                    eye_event = None
                    if not eye_manual:
                        next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
                    eye_manual = False

            if (zen_mode and eye_event is not None and not eye_manual
                    and time.monotonic() - eye_event >= 2.75):
                collect_eye = True

            if collect_eye and eye_event is not None:
                stop_decoder(process, stop, reader)
                audio.stop()
                try:
                    close_notification(Gio, GLib, eye_notification)
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                eye_notification = None
                eye_event = None
                finished = True
                random_events_ready_at = time.monotonic() + 10
                frame_surface = greeting_first_surface
                frame_dirty = True
                kaching_sound.play()
                try:
                    save_data = json.loads(save_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    save_data = {}
                if not isinstance(save_data, dict):
                    save_data = {}
                previous_mariocoins = int(save_data.get("mariocoins", 0))
                mariocoins = previous_mariocoins + 25
                live_values["mariocoins"] = mariocoins
                show_coin_change(
                    QtCore, QtWidgets, previous_mariocoins, mariocoins
                )
                if debug_update is not None:
                    debug_update("mariocoins")
                write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                if not eye_manual:
                    next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
                eye_manual = False

            selected_action = menu_action[0]
            menu_action[0] = None
            if selected_action == "voice_apology":
                audio.stop()
                if pet_video is not None:
                    finish_pet_alert()
                    pet_video = None
                if eye_event is not None:
                    try:
                        close_notification(Gio, GLib, eye_notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    eye_notification = None
                    eye_event = None
                    if not eye_manual:
                        next_eye_at = time.monotonic() + random_delay(
                            preferences["playfulness"], 25, 75
                        )
                    eye_manual = False
                idle_playing = False
                move_tween = None
                post_greeting_tween = None
                greeting_tween_started = True
                is_greeting = False
                (
                    audio, frame_surface, last_frame_surface, process,
                    frames, stop, reader, video,
                ) = start_apology(
                    ffmpeg, temp_dir, video_texture, process, stop, reader
                )
                started = time.monotonic()
                consumed = 0
                finished = False
                frame_dirty = False
                random_events_ready_at = time.monotonic() + 10
            elif selected_action == "speak":
                audio.stop()
                if pet_video is not None:
                    finish_pet_alert()
                    pet_video = None
                if eye_event is not None:
                    try:
                        close_notification(Gio, GLib, eye_notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    eye_notification = None
                    eye_event = None
                    eye_manual = False
                idle_video = random.choice(IDLE_VIDEOS)
                (
                    audio, frame_surface, last_frame_surface, process,
                    frames, stop, reader,
                ) = start_media(
                    ffmpeg, temp_dir, video_texture, process, stop, reader,
                    idle_video,
                )
                video = idle_video
                started = time.monotonic()
                consumed = 0
                finished = False
                frame_dirty = False
                idle_playing = True
                post_greeting_tween = None
                move_tween = None
                greeting_tween_started = True
                is_greeting = False
            elif selected_action in ("quench_hunger", "quench_thirst"):
                target_need = "hunger" if selected_action == "quench_hunger" else "thirst"
                levels = pet.snapshot()
                if pet.is_full(target_need):
                    show_full_meter_message(
                        target_need, levels["hunger"], levels["thirst"]
                    )
                else:
                    title = "Choose food for Mario" if target_need == "hunger" else "Choose drinks for Mario"
                    selected_files, _ = QtWidgets.QFileDialog.getOpenFileNames(
                        None, title, "", "All files (*)"
                    )
                    pet_drops.extend(
                        (selected_file, target_need)
                        for selected_file in selected_files
                    )
            elif selected_action == "goodbye" and not goodbye_running:
                goodbye_running = True
                audio.stop()
                try:
                    close_notification(Gio, GLib, eye_notification)
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                for need_name in tuple(pet_notifications):
                    try:
                        close_notification(
                            Gio, GLib, pet_notifications.pop(need_name)
                        )
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                pet_notification_close_at.clear()
                write_save_data(save_path, pet.snapshot())
                for auxiliary in (settings_window, minigames_window, debug_window, care_window):
                    if auxiliary is not None:
                        auxiliary.hide()
                goodbye_completed = run_goodbye(
                    ffmpeg, temp_dir, window, renderer, video_texture, process,
                    stop, reader, qt_app, Gtk, Gdk, QtWidgets,
                    NOTIFICATIONS.tray,
                )
                running = False
                break
            elif selected_action == "petcare":
                live_values.update(pet.snapshot())
                if care_window is None:
                    care_window, care_update = create_pet_care_window(QtWidgets, live_values)
                care_update()
                care_window.show()
                care_window.raise_()
                care_window.activateWindow()
            elif selected_action == "settings":
                if settings_window is None:
                    settings_window, settings_sync = create_settings_window(
                        QtCore, QtWidgets, preferences,
                        lambda name, value: settings_changes.append((name, value)),
                    )
                settings_window.show()
                settings_window.raise_()
                settings_window.activateWindow()
            elif selected_action == "move":
                desktop_sizes = pygame.display.get_desktop_sizes()
                display_index = min(window.display_index, len(desktop_sizes) - 1)
                desktop_width, desktop_height = desktop_sizes[display_index]
                width, height = window.size
                move_tween = {
                    "started": time.monotonic(),
                    "from": tuple(window.position),
                    "to": (
                        position_number(max(0, desktop_width - width), preferences),
                        position_number(max(0, desktop_height - height), preferences),
                    ),
                }
                post_greeting_tween = None
                greeting_tween_started = True
                is_greeting = False
                window_move_changed_at = None
            elif selected_action == "minigames":
                if minigames_window is None:
                    minigames_window = create_minigames_window(
                        QtWidgets, minigame_action
                    )
                minigames_window.show()
                minigames_window.raise_()
                minigames_window.activateWindow()
            elif selected_action == "superdebug":
                if debug_window is None:
                    debug_window, debug_update = create_debug_window(
                        QtWidgets, live_values
                    )
                debug_window.show()
                debug_update()
                debug_window.raise_()
                debug_window.activateWindow()

            if minigame_action[0] == "tunnel":
                minigame_action[0] = None
                finish_pet_alert()
                pet_video = None
                if eye_event is not None:
                    try:
                        close_notification(Gio, GLib, eye_notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    eye_notification = None
                    eye_event = None
                    eye_manual = False
                if idle_playing:
                    idle_playing = False
                    next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                audio.stop()
                stop_decoder(process, stop, reader)
                if minigames_window is not None:
                    minigames_window.hide()
                post_greeting_tween = None
                move_tween = None
                window_move_changed_at = None
                last_window_position = tuple(window.position)
                greeting_tween_started = True
                is_greeting = False
                outcome = "cancel"
                try:
                    outcome = play_tunnel_of_doom(
                        ASSETS, ffmpeg, Path(temp_dir.name), window, renderer,
                        video_texture, qt_app, Gtk, start_decoder, stop_decoder,
                        decode_first_frame, decode_last_frame, VIDEO_SIZE, VIDEO_FPS,
                        on_drop=queue_pet_drop,
                    )
                except Exception as exc:
                    print(f"MarioAssistant Tunnel of Doom: {exc}", file=sys.stderr)
                    QtWidgets.QMessageBox.warning(
                        minigames_window, "Mario's Tunnel of Doom", str(exc)
                    )
                if outcome == "close":
                    goodbye_running = True
                    audio.stop()
                    for need_name in tuple(pet_notifications):
                        try:
                            close_notification(
                                Gio, GLib, pet_notifications.pop(need_name)
                            )
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    pet_notification_close_at.clear()
                    for auxiliary in (settings_window, minigames_window, debug_window, care_window):
                        if auxiliary is not None:
                            auxiliary.hide()
                    write_save_data(save_path, pet.snapshot())
                    goodbye_completed = run_goodbye(
                        ffmpeg, temp_dir, window, renderer, video_texture,
                        process, stop, reader, qt_app, Gtk, Gdk, QtWidgets,
                        NOTIFICATIONS.tray,
                    )
                    running = False
                    break
                if outcome == "quit":
                    running = False
                elif outcome in ("win", "lose"):
                    previous_mariocoins = mariocoins
                    mariocoins += 50 if outcome == "win" else -10
                    live_values["mariocoins"] = mariocoins
                    write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                    show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                    if debug_update is not None:
                        debug_update("mariocoins")
                    (kaching_sound if outcome == "win" else disappointment_sound).play()
                finished = True
                frame_surface = greeting_first_surface
                frame_dirty = True
                random_events_ready_at = time.monotonic() + 10

            due_need = pet.due()
            if (due_need is not None and pet_video is not None
                    and due_need != pet_alert_need):
                audio.stop()
                pet_video = ASSETS / ("heyimhungry.avi" if due_need == "hunger" else "heyimthirsty.avi")
                need_messages = {
                    "hunger": "I'm hungry! Have you got any food?",
                    "thirst": "I'm thirsty! You got something to drinky?",
                }
                previous_notification = pet_notifications.get(due_need)
                replace_id = previous_notification[1] if Gio is not None and previous_notification else 0
                try:
                    pet_notifications[due_need] = send_notification(
                        Gio, GLib, replace_id, "Hey!", need_messages[due_need]
                    )
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                pet_alert_need = due_need
                (
                    audio, frame_surface, last_frame_surface, process,
                    frames, stop, reader,
                ) = start_media(ffmpeg, temp_dir, video_texture, process, stop, reader, pet_video)
                video = pet_video
                pet.remind(due_need)
                started = time.monotonic()
                consumed = 0
                finished = False
                frame_dirty = False
                is_greeting = False
                greeting_tween_started = True
            elif (due_need is not None and finished and eye_event is None
                    and move_tween is None and post_greeting_tween is None
                    and minigame_action[0] is None):
                audio.stop()
                pet_video = ASSETS / ("heyimhungry.avi" if due_need == "hunger" else "heyimthirsty.avi")
                need_messages = {
                    "hunger": "I'm hungry! Have you got any food?",
                    "thirst": "I'm thirsty! You got something to drinky?",
                }
                previous_notification = pet_notifications.get(due_need)
                replace_id = previous_notification[1] if Gio is not None and previous_notification else 0
                try:
                    pet_notifications[due_need] = send_notification(
                        Gio, GLib, replace_id, "Hey!", need_messages[due_need]
                    )
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                pet_alert_need = due_need
                (
                    audio, frame_surface, last_frame_surface, process,
                    frames, stop, reader,
                ) = start_media(ffmpeg, temp_dir, video_texture, process, stop, reader, pet_video)
                video = pet_video
                pet.remind(due_need)
                started = time.monotonic()
                consumed = 0
                finished = False
                frame_dirty = False
                is_greeting = False
                greeting_tween_started = True

            manual_start = minigame_action[0] == "eye"
            minigame_action[0] = None
            now = time.monotonic()
            if not super_zen_mode:
                if eye_event is None:
                    while next_eye_at < random_events_ready_at or (
                        not finished and next_eye_at <= now
                    ):
                        next_eye_at += 10
                if not idle_playing:
                    while next_idle_at < random_events_ready_at or (
                        not finished and next_idle_at <= now
                    ):
                        next_idle_at += 10
            if super_zen_mode and time.monotonic() >= next_wander_at:
                next_wander_at = time.monotonic() + random_delay(preferences["wandering"], 60, 120)
            if not super_zen_mode:
                while next_wander_at < random_events_ready_at or (
                    (not finished or move_tween is not None) and next_wander_at <= now
                ):
                    next_wander_at += 10
            earliest_event = min(
                ((next_eye_at, "eye"), (next_idle_at, "idle"), (next_wander_at, "wander"))
            )[1]
            automatic_start = preferences["playfulness"] > 0 and time.monotonic() >= next_eye_at
            if (super_zen_mode and automatic_start
                    and (eye_event is None or eye_manual)):
                try:
                    save_data = json.loads(save_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    save_data = {}
                if not isinstance(save_data, dict):
                    save_data = {}
                previous_mariocoins = int(save_data.get("mariocoins", 0))
                mariocoins = previous_mariocoins + 25
                live_values["mariocoins"] = mariocoins
                show_coin_change(
                    QtCore, QtWidgets, previous_mariocoins, mariocoins
                )
                if debug_update is not None:
                    debug_update("mariocoins")
                write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
                automatic_start = False
            if eye_event is None and (manual_start or (
                automatic_start and earliest_event == "eye" and finished and not idle_playing and move_tween is None
                and time.monotonic() >= random_events_ready_at
            )):
                if idle_playing:
                    idle_playing = False
                    next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                eye_manual = manual_start
                finish_pet_alert()
                pet_video = None
                audio.stop()
                (
                    audio, frame_surface, last_frame_surface, process,
                    frames, stop, reader,
                ) = start_media(
                    ffmpeg, temp_dir, video_texture, process, stop, reader,
                    eye_video,
                )
                video = eye_video
                started = time.monotonic()
                consumed = 0
                finished = False
                frame_dirty = False
                eye_event = started
                post_greeting_tween = None
                move_tween = None
                greeting_tween_started = True
                is_greeting = False
                if not eye_manual:
                    try:
                        eye_notification = send_notification(Gio, GLib)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)

            idle_due = preferences["chattiness"] > 0 and time.monotonic() >= next_idle_at
            if idle_due and super_zen_mode and not idle_playing:
                next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                idle_due = False
            if (idle_due and earliest_event == "idle" and eye_event is None and not idle_playing and finished and move_tween is None
                    and time.monotonic() >= random_events_ready_at):
                idle_video = random.choice(IDLE_VIDEOS)
                audio.stop()
                (
                    audio, frame_surface, last_frame_surface, process,
                    frames, stop, reader,
                ) = start_media(
                    ffmpeg, temp_dir, video_texture, process, stop, reader,
                    idle_video,
                )
                video = idle_video
                started = time.monotonic()
                consumed = 0
                finished = False
                frame_dirty = False
                idle_playing = True
                post_greeting_tween = None
                move_tween = None
                greeting_tween_started = True
                is_greeting = False

            if not finished:
                target_frames = max(1, int((time.monotonic() - started) * VIDEO_FPS) + 1)
                while consumed < target_frames:
                    try:
                        data = frames.get_nowait()
                    except queue.Empty:
                        break
                    if data is None:
                        finished = True
                        random_events_ready_at = time.monotonic() + 10
                        frame_surface = last_frame_surface
                        frame_dirty = True
                        if idle_playing:
                            idle_playing = False
                            next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                            frame_surface = greeting_first_surface
                        if pet_video is not None:
                            finish_pet_alert()
                            pet_video = None
                            frame_surface = greeting_first_surface
                        if video == ASSETS / "ohsorry.mp4":
                            frame_surface = greeting_first_surface
                        if eye_event is not None:
                            audio.stop()
                            try:
                                close_notification(Gio, GLib, eye_notification)
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                            eye_notification = None
                            disappointment_sound.play()
                            frame_surface = greeting_first_surface
                            frame_dirty = True
                            eye_event = None
                            if not eye_manual:
                                next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
                            eye_manual = False
                        break
                    frame_surface = pygame.image.frombuffer(data, VIDEO_SIZE, "RGB").copy()
                    consumed += 1
                    frame_dirty = True

            if finished and first_run and not save_written:
                write_save_data(save_path, pet.snapshot())
                save_written = True

            if finished and is_greeting and not greeting_tween_started:
                desktop_sizes = pygame.display.get_desktop_sizes()
                display_index = min(window.display_index, len(desktop_sizes) - 1)
                desktop_width, desktop_height = desktop_sizes[display_index]
                from_x, from_y = window.position
                from_width, from_height = window.size
                to_width = max(1, from_width // 2)
                to_height = max(1, from_height // 2)
                to_x = position_number(max(0, desktop_width - to_width), preferences)
                to_y = position_number(max(0, desktop_height - to_height), preferences)
                post_greeting_tween = {
                    "started": time.monotonic(),
                    "from": (from_x, from_y, from_width, from_height),
                    "to": (to_x, to_y, to_width, to_height),
                }
                greeting_tween_started = True

            if post_greeting_tween is not None:
                tween = post_greeting_tween
                progress = min(
                    1.0, (time.monotonic() - tween["started"]) / TWEEN_SECONDS
                )
                eased = back_in_out(progress)
                from_x, from_y, from_width, from_height = tween["from"]
                to_x, to_y, to_width, to_height = tween["to"]
                width = max(1, round(from_width + (to_width - from_width) * eased))
                height = max(1, round(from_height + (to_height - from_height) * eased))
                if tuple(window.size) != (width, height):
                    window.size = (width, height)
                window.position = (
                    round(from_x + (to_x - from_x) * eased),
                    round(from_y + (to_y - from_y) * eased),
                )
                if progress >= 1.0:
                    post_greeting_tween = None
                    last_window_position = tuple(window.position)
                    window_move_changed_at = None
                    if booting:
                        booting = False
                        tray_update = create_tray_menu(
                            QtWidgets, NOTIFICATIONS.tray, menu_action, live_values
                        )

            if move_tween is not None:
                progress = min(
                    1.0, (time.monotonic() - move_tween["started"]) / move_tween.get("duration", 0.75)
                )
                eased = back_in_out(progress) if move_tween.get("wander") else back_out(progress)
                start_x, start_y = move_tween["from"]
                end_x, end_y = move_tween["to"]
                position = (
                    round(start_x + (end_x - start_x) * eased),
                    round(start_y + (end_y - start_y) * eased),
                )
                if move_tween.get("wander"):
                    left, top, right, bottom = move_tween["bounds"]
                    position = (
                        max(left, min(right, position[0])),
                        max(top, min(bottom, position[1])),
                    )
                    move_tween["requested_positions"].append(position)
                    move_tween["requested_positions"] = move_tween["requested_positions"][-60:]
                window.position = position
                if progress >= 1.0 and move_tween.get("wander"):
                    move_tween = None
                    last_window_position = tuple(window.position)
                    window_move_changed_at = None
                    random_events_ready_at = time.monotonic() + 10
                elif progress >= 1.0:
                    move_tween = None
                    last_window_position = tuple(window.position)
                    window_move_changed_at = None
                    if preferences["oh_sorry"]:
                        audio.stop()
                        if idle_playing:
                            idle_playing = False
                            next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                        (
                            audio, frame_surface, last_frame_surface, process,
                            frames, stop, reader, video,
                        ) = start_apology(
                            ffmpeg, temp_dir, video_texture, process, stop, reader
                        )
                        finish_pet_alert()
                        pet_video = None
                        consumed = 0
                        started = time.monotonic()
                        finished = False
                        frame_dirty = False

            if (time.monotonic() >= next_wander_at and earliest_event == "wander" and preferences["wandering"] > 0
                    and not super_zen_mode and finished and eye_event is None and post_greeting_tween is None
                    and move_tween is None and time.monotonic() >= random_events_ready_at):
                screens = qt_app.screens()
                screen = screens[min(window.display_index, len(screens) - 1)]
                area = screen.availableGeometry()
                current = tuple(window.position)
                destination, bounds = wander_destination(
                    (area.x(), area.y(), area.width(), area.height()),
                    tuple(window.size), current, preferences,
                )
                move_tween = {
                    "started": time.monotonic(), "from": current,
                    "to": destination, "bounds": bounds,
                    "requested_positions": [current],
                    "wander": True, "duration": 1.5,
                }
                next_wander_at = time.monotonic() + random_delay(preferences["wandering"], 60, 120)
                window_move_changed_at = None

            current_position = tuple(window.position)
            if (greeting_tween_started and post_greeting_tween is None
                    and move_tween is None):
                if current_position != last_window_position:
                    window_move_changed_at = time.monotonic()
                elif (window_move_changed_at is not None
                      and time.monotonic() - window_move_changed_at >= 0.3):
                    window_move_changed_at = None
                    if preferences["oh_sorry"] and random.random() < 0.5:
                        audio.stop()
                        if idle_playing:
                            idle_playing = False
                            next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                        (
                            audio, frame_surface, last_frame_surface, process,
                            frames, stop, reader, video,
                        ) = start_apology(
                            ffmpeg, temp_dir, video_texture, process, stop, reader
                        )
                        finish_pet_alert()
                        pet_video = None
                        consumed = 0
                        started = time.monotonic()
                        finished = False
                        is_greeting = False
                        frame_dirty = False
            last_window_position = current_position

            for name, value in pet.snapshot().items():
                if round(live_values[name], 1) != round(value, 1):
                    live_values[name] = value
                    if debug_update is not None:
                        debug_update(name)
                    if care_update is not None and care_window.isVisible():
                        care_update()
            if tray_update is not None:
                tray_update()
            if time.monotonic() - last_pet_save >= 30:
                write_save_data(save_path, pet.snapshot())
                last_pet_save = time.monotonic()

            now = time.monotonic()
            pet_attention = any(pet.needy.values()) or eye_event is not None
            if pet_attention != pet_attention_active:
                pet_attention_active = pet_attention
                window.title = "MarioAssistant (Hey!)" if pet_attention else "MarioAssistant"
                set_sdl_window_attention(window, pet_attention)

            if (debug_window is not None and debug_window.isVisible()
                    and now - last_debug_schedule_update >= 1):
                last_debug_schedule_update = now
                schedules = [
                    ("eye_schedule", "Eye minigame", next_eye_at, eye_event is not None, False),
                    ("idle_schedule", "Idle clip", next_idle_at, idle_playing, False),
                    ("wander_schedule", "Wandering", next_wander_at, move_tween is not None and move_tween.get("wander", False), False),
                    ("hunger_schedule", "Hunger notification", pet.next_notification_at("hunger"), pet_alert_need == "hunger", not pet.needy["hunger"]),
                    ("thirst_schedule", "Thirst notification", pet.next_notification_at("thirst"), pet_alert_need == "thirst", not pet.needy["thirst"]),
                ]
                for name, label, deadline, active, estimated in schedules:
                    if not math.isfinite(deadline):
                        value = f"{label}: Disabled"
                    else:
                        scheduled_time = datetime.fromtimestamp(
                            wall_clock_offset + deadline
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        remaining = max(0, math.ceil(deadline - now))
                        status = "Playing" if active else f"{remaining}s remaining"
                        value = f"{label}: {scheduled_time} ({status})"
                        if estimated:
                            value += " (ESTIMATION)"
                    if live_values.get(name) != value:
                        live_values[name] = value
                        if debug_update is not None:
                            debug_update(name)

            if frame_dirty:
                video_texture.update(frame_surface)
                frame_dirty = False
            width, height = window.size
            renderer.draw_color = (0, 0, 0, 255)
            renderer.clear()
            video_texture.draw(dstrect=(0, 0, width, height))
            renderer.present()
    finally:
        pet.tick()
        if not first_run or (save_path.exists() or "save_written" in locals() and save_written):
            try:
                write_save_data(save_path, pet.snapshot())
            except OSError as exc:
                print(f"MarioAssistant save: {exc}", file=sys.stderr)
        stop.set()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            if process.stdout is not None:
                process.stdout.close()
            process.wait()
        if reader is not None:
            reader.join(timeout=0.25)
        preparing_texture = None
        if "video_texture" in locals():
            video_texture = None
        renderer = None
        window.destroy()
        pygame.quit()
        temp_dir.cleanup()
        if goodbye_completed:
            play_information_sound(QtWidgets)


def back_in_out(t: float) -> float:
    c1 = 1.70158
    c2 = c1 * 1.525
    if t < 0.5:
        return ((2 * t) ** 2 * ((c2 + 1) * 2 * t - c2)) / 2
    return (((2 * t - 2) ** 2 * ((c2 + 1) * (2 * t - 2) + c2) + 2) / 2)


def back_out(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"MarioAssistant: {exc}", file=sys.stderr)
        raise
