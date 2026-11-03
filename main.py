import json
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pygame
from pygame._sdl2 import Renderer, Texture, Window


ASSETS = Path(__file__).resolve().parent / "assets"
SAVE_NAME = "itsame.json"
VIDEO_SIZE = (800, 600)
WINDOW_SIZE = (800, 600)
VIDEO_FPS = 15
FRAME_BYTES = VIDEO_SIZE[0] * VIDEO_SIZE[1] * 3
TWEEN_SECONDS = 0.75


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
    data.setdefault("greeting_seen", True)
    data.setdefault("mariocoins", 0)
    data.setdefault("zen_mode", False)
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
    game_list.setCurrentRow(0)
    play_button = QtWidgets.QPushButton("Play!")
    play_button.clicked.connect(
        lambda: minigame_action.__setitem__(
            0, "eye" if game_list.currentRow() == 0 else None
        )
    )
    layout.addWidget(game_list)
    layout.addWidget(play_button)
    return window


def show_context_menu(Gtk, Gdk, menu_action, active_menu):
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


def main() -> None:
    import gi
    from PySide6 import QtWidgets

    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    Gtk.init([])
    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["MarioAssistant"]
    )

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
    zen_mode = bool(initial_save.get("zen_mode", False))
    if not first_run and "zen_mode" not in initial_save:
        write_save_data(save_path, {"zen_mode": False})
    video = ASSETS / ("firsttimegreeting.mp4" if first_run else "greeting.avi")
    if not video.is_file():
        raise FileNotFoundError(video)
    pygame.init()
    window = Window("MarioAssistant", size=WINDOW_SIZE, borderless=False, resizable=True)
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
    chord_latched = False
    zen_chord_latched = False
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
        eye_manual = False
        active_menu = [None]
        move_tween = None
        eye_video = ASSETS / "somethinginmyeye.avi"
        eye_event = None
        eye_notification = None
        next_eye_at = time.monotonic() + random.uniform(25, 75)
        audio.play()

        running = True
        while running:
            clock.tick(60)
            qt_app.processEvents()
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
                    if minus_down and plus_down and not zen_chord_latched:
                        zen_chord_latched = True
                        zen_mode = not zen_mode
                        write_save_data(save_path, {"zen_mode": zen_mode})
                    if star_down and minus_down and not chord_latched:
                        chord_latched = True
                        finished = True
                        stop.set()
                        if process.poll() is None:
                            process.terminate()
                        audio.stop()
                        frame_surface = last_frame_surface
                        frame_dirty = True
                        if eye_event is not None:
                            try:
                                close_notification(Gio, GLib, eye_notification)
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                            eye_notification = None
                            disappointment_sound.play()
                            eye_event = None
                            if not eye_manual:
                                next_eye_at = time.monotonic() + random.uniform(25, 75)
                            eye_manual = False
                elif event.type == pygame.KEYUP:
                    if event.key in (pygame.K_8, pygame.K_KP_MULTIPLY):
                        star_down = False
                    if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        minus_down = False
                    if (event.key in (pygame.K_EQUALS, pygame.K_KP_PLUS, pygame.K_PLUS)
                            or getattr(event, "unicode", "") == "+"):
                        plus_down = False
                    if not (minus_down and plus_down):
                        zen_chord_latched = False
                    if not (star_down and minus_down):
                        chord_latched = False
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 3:
                    show_context_menu(Gtk, Gdk, menu_action, active_menu)
                elif (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                      and eye_event is not None
                      and time.monotonic() - eye_event >= 2.75):
                    collect_eye = True

            while Gtk.events_pending():
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
                frame_surface = greeting_first_surface
                frame_dirty = True
                kaching_sound.play()
                try:
                    save_data = json.loads(save_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    save_data = {}
                if not isinstance(save_data, dict):
                    save_data = {}
                write_save_data(
                    save_path,
                    {"mariocoins": int(save_data.get("mariocoins", 0)) + 25},
                )
                if not eye_manual:
                    next_eye_at = time.monotonic() + random.uniform(25, 75)
                eye_manual = False

            selected_action = menu_action[0]
            menu_action[0] = None
            if selected_action == "goodbye":
                running = False
            elif selected_action == "move":
                desktop_sizes = pygame.display.get_desktop_sizes()
                display_index = min(window.display_index, len(desktop_sizes) - 1)
                desktop_width, desktop_height = desktop_sizes[display_index]
                width, height = window.size
                move_tween = {
                    "started": time.monotonic(),
                    "from": tuple(window.position),
                    "to": (
                        random.randint(0, max(0, desktop_width - width)),
                        random.randint(0, max(0, desktop_height - height)),
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

            manual_start = minigame_action[0] == "eye"
            minigame_action[0] = None
            automatic_start = time.monotonic() >= next_eye_at
            if eye_event is None and (manual_start or automatic_start):
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

            if not finished:
                target_frames = max(1, int((time.monotonic() - started) * VIDEO_FPS) + 1)
                while consumed < target_frames:
                    try:
                        data = frames.get_nowait()
                    except queue.Empty:
                        break
                    if data is None:
                        finished = True
                        frame_surface = last_frame_surface
                        frame_dirty = True
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
                                next_eye_at = time.monotonic() + random.uniform(25, 75)
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
                to_x = random.randint(0, max(0, desktop_width - to_width))
                to_y = random.randint(0, max(0, desktop_height - to_height))
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
                    1.0, (time.monotonic() - move_tween["started"]) / 0.75
                )
                eased = back_out(progress)
                start_x, start_y = move_tween["from"]
                end_x, end_y = move_tween["to"]
                window.position = (
                    round(start_x + (end_x - start_x) * eased),
                    round(start_y + (end_y - start_y) * eased),
                )
                if progress >= 1.0:
                    move_tween = None
                    last_window_position = tuple(window.position)
                    window_move_changed_at = None
                    audio.stop()
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
