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


ASSETS = Path(__file__).resolve().parent / "assets"
IDLE_VIDEOS = tuple(ASSETS / name for name in (
    "niceplace.avi",
    "mariosize.avi",
    "mynosefeelsfunny.avi",
    "madeyoulook.avi",
    "immabetyoucantdothis.avi",
    "haveyouseentheprincess.avi",
    "haveyouseenluigi.avi",
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
    data.setdefault("zen_mode", False)
    data.setdefault("super_zen_mode", False)
    for name, value in DEFAULT_SETTINGS.items():
        data.setdefault(name, value)
    if updates:
        data.update(updates)
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
    if process.poll() is None:
        process.terminate()
    if process.stdout is not None:
        process.stdout.close()
    process.wait()
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


def create_context_menu(Gtk, menu_action):
    menu = Gtk.Menu()
    for label, action in (
        ("Move", "move"),
        ("IM Minigames", "minigames"),
        ("Super Debug", "superdebug"),
        ("Settings", "settings"),
        ("Goodbye", "goodbye"),
    ):
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
    window.setMinimumSize(360, 180)
    layout = QtWidgets.QVBoxLayout(window)
    mariocoins_label = QtWidgets.QLabel()
    zen_mode_label = QtWidgets.QLabel()
    super_zen_mode_label = QtWidgets.QLabel()
    eye_schedule_label = QtWidgets.QLabel()
    idle_schedule_label = QtWidgets.QLabel()
    wander_schedule_label = QtWidgets.QLabel()

    value_labels = {
        "mariocoins": mariocoins_label,
        "zen_mode": zen_mode_label,
        "super_zen_mode": super_zen_mode_label,
        "eye_schedule": eye_schedule_label,
        "idle_schedule": idle_schedule_label,
        "wander_schedule": wander_schedule_label,
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

def show_context_menu(Gtk, Gdk, menu_action, active_menu):
    if Gtk is None:
        from PySide6 import QtGui, QtWidgets
        if active_menu[0] is not None:
            active_menu[0].close()
        menu = QtWidgets.QMenu()
        for label, action in (("Move", "move"), ("IM Minigames", "minigames"),
                              ("Super Debug", "superdebug"), ("Settings", "settings"),
                              ("Goodbye", "goodbye")):
            item = menu.addAction(label)
            item.triggered.connect(lambda checked=False, selected=action: menu_action.__setitem__(0, selected))
        active_menu[0] = menu
        menu.popup(QtGui.QCursor.pos())
        return
    if active_menu[0] is not None:
        active_menu[0].popdown()
        active_menu[0].destroy()
    menu = create_context_menu(Gtk, menu_action)
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


def send_notification(Gio, GLib, notification_id=0):
    if Gio is None:
        return NOTIFICATIONS.show()
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    parameters = GLib.Variant(
        "(susssasa{sv}i)",
        (
            "MarioAssistant",
            notification_id,
            "",
            "Hey!",
            "I got something in my eye, can you help me get it out?",
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

def run_goodbye(ffmpeg, temp_dir, window, renderer, texture, process, stop, reader, qt_app, Gtk):
    clock = pygame.time.Clock()

    def pump():
        qt_app.processEvents()
        while Gtk is not None and Gtk.events_pending():
            Gtk.main_iteration_do(False)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
        renderer.clear()
        texture.draw(dstrect=(0, 0, *window.size))
        renderer.present()
        clock.tick(60)
        return True

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
            if not pump():
                return False
            if progress >= 1:
                return True

    stop_decoder(process, stop, reader)
    desktop_sizes = pygame.display.get_desktop_sizes()
    desktop_width, desktop_height = desktop_sizes[min(window.display_index, len(desktop_sizes) - 1)]
    x = (desktop_width - WINDOW_SIZE[0]) // 2
    y = (desktop_height - WINDOW_SIZE[1]) // 2
    if not tween((x, y, *WINDOW_SIZE)):
        return True
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
            if not pump():
                return True
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
    for name in ("chattiness", "playfulness", "wandering"):
        preferences[name] = max(0, min(10, int(preferences[name])))
    mariocoins = int(initial_save.get("mariocoins", 0))
    zen_mode = bool(initial_save.get("zen_mode", False))
    super_zen_mode = bool(initial_save.get("super_zen_mode", False))
    live_values = {
        "mariocoins": mariocoins,
        "zen_mode": zen_mode,
        "super_zen_mode": super_zen_mode,
    }
    if not first_run and ("shuffling" in initial_save or any(name not in initial_save for name in DEFAULT_SETTINGS)):
        write_save_data(save_path, preferences)
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
        window.size = (1, 1)
        window.position = (center[0], center[1])
        video_texture.update(frame_surface)
        tween_started = time.monotonic()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
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
        menu_action = [None]
        minigame_action = [None]
        minigames_window = None
        debug_window = None
        debug_update = None
        settings_window = None
        settings_sync = None
        settings_changes = []
        eye_manual = False
        active_menu = [None]
        move_tween = None
        eye_video = ASSETS / "somethinginmyeye.avi"
        eye_event = None
        eye_notification = None
        next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
        next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
        idle_playing = False
        next_wander_at = time.monotonic() + random_delay(preferences["wandering"], 60, 120)
        random_events_ready_at = 0.0
        wall_clock_offset = time.time() - time.monotonic()
        audio.play()

        running = True
        while running:
            clock.tick(60)
            qt_app.processEvents()
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
                write_save_data(save_path, {name: value})
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
                if name in live_values:
                    live_values[name] = value
                    if debug_update is not None:
                        debug_update(name)
            settings_changes.clear()
            collect_eye = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
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
                        write_save_data(save_path, {"zen_mode": zen_mode})
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
                            save_path, {"super_zen_mode": super_zen_mode}
                        )
                    if star_down and minus_down and not chord_latched:
                        chord_latched = True
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
                elif event.type == pygame.KEYUP:
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
                    show_context_menu(Gtk, Gdk, menu_action, active_menu)
                elif (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                      and eye_event is not None
                      and time.monotonic() - eye_event >= 2.75):
                    collect_eye = True

            while Gtk is not None and Gtk.events_pending():
                Gtk.main_iteration_do(False)

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
                write_save_data(save_path, {"mariocoins": mariocoins})
                if not eye_manual:
                    next_eye_at = time.monotonic() + random_delay(preferences["playfulness"], 25, 75)
                eye_manual = False

            selected_action = menu_action[0]
            menu_action[0] = None
            if selected_action == "goodbye":
                audio.stop()
                try:
                    close_notification(Gio, GLib, eye_notification)
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                for auxiliary in (settings_window, minigames_window, debug_window):
                    if auxiliary is not None:
                        auxiliary.hide()
                goodbye_completed = run_goodbye(
                    ffmpeg, temp_dir, window, renderer, video_texture, process,
                    stop, reader, qt_app, Gtk,
                )
                running = False
                break
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
                    )
                except Exception as exc:
                    print(f"MarioAssistant Tunnel of Doom: {exc}", file=sys.stderr)
                    QtWidgets.QMessageBox.warning(
                        minigames_window, "Mario's Tunnel of Doom", str(exc)
                    )
                if outcome == "quit":
                    running = False
                elif outcome in ("win", "lose"):
                    previous_mariocoins = mariocoins
                    mariocoins += 50 if outcome == "win" else -10
                    live_values["mariocoins"] = mariocoins
                    write_save_data(save_path, {"mariocoins": mariocoins})
                    show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                    if debug_update is not None:
                        debug_update("mariocoins")
                    (kaching_sound if outcome == "win" else disappointment_sound).play()
                finished = True
                frame_surface = greeting_first_surface
                frame_dirty = True
                random_events_ready_at = time.monotonic() + 10

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
                write_save_data(save_path, {"mariocoins": mariocoins})
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
                write_save_data(save_path)
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
                    if random.random() < 0.5:
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
                        consumed = 0
                        started = time.monotonic()
                        finished = False
                        is_greeting = False
                        frame_dirty = False
            last_window_position = current_position

            now = time.monotonic()
            for name, label, deadline, active in (
                ("eye_schedule", "Eye minigame", next_eye_at, eye_event is not None),
                ("idle_schedule", "Idle clip", next_idle_at, idle_playing),
                ("wander_schedule", "Wandering", next_wander_at, move_tween is not None and move_tween.get("wander", False)),
            ):
                if not math.isfinite(deadline):
                    value = f"{label}: Disabled"
                    if live_values.get(name) != value:
                        live_values[name] = value
                        if debug_update is not None:
                            debug_update(name)
                    continue
                scheduled_time = datetime.fromtimestamp(
                    wall_clock_offset + deadline
                ).strftime("%Y-%m-%d %H:%M:%S")
                remaining = max(0, math.ceil(deadline - now))
                status = "Playing" if active else f"{remaining}s remaining"
                value = f"{label}: {scheduled_time} ({status})"
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
