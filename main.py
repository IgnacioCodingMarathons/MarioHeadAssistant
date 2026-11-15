import json
import io
import math
import difflib
from datetime import datetime
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import pygame
from pygame._sdl2 import Renderer, Texture, Window

from tunnel_of_doom import play_tunnel_of_doom
from settings_window import (
    DEFAULT_SETTINGS, create_settings_window, random_delay, idle_delay,
    proactivity_delay,
)
from desktop_support import (
    DesktopNotifications,
    eject_cd,
    get_system_volume,
    play_error_sound,
    play_fatal_error_sound,
    play_information_sound,
    play_task_complete_sound,
    set_system_volume,
    speak_text,
)
from window_policy import (
    install_linux_window_policy,
    keep_sdl_window_above,
    set_sdl_window_attention,
)
from pet_care import PetNeeds, create_pet_care_window
from sentence_mixer import LiveSentenceMixer, normalize_numbers
from phoneme_dict import is_spanish_text, sentence_mix_spanish
from sentence_mixer_passages import compatibility_tts_text, format_subtitle_text, tokenize_variant_text
from weather_service import WeatherService, weather_failure_message
from news_service import display_network_name, latest_news
from pizza_challenge import ACTION as PIZZA_ACTION, TITLE as PIZZA_TITLE, create_pizza_window
from exploding_blocks import ACTION as EXPLODING_BLOCKS_ACTION, TITLE as EXPLODING_BLOCKS_TITLE, create_exploding_blocks_window
from backgammon import create_backgammon_window
from checkers import create_checkers_window
from chess_game import create_chess_window
from go_game import create_go_window
from tic_tac_toe import TITLE as TIC_TAC_TOE_TITLE, create_tic_tac_toe_window
from snakes_and_ladders import TITLE as SNAKES_AND_LADDERS_TITLE, create_snakes_and_ladders_window
from connect_four import (
    CONNECT_FOUR_DIALOGUE,
    create_connect_four_window as create_connect_four_history_window,
)
from voice_commands import (
    MicrophoneCapture,
    SpeechTranscriber,
    WakeWordListener,
    LoudSoundListener,
    create_beep_sound,
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
MARIO_VOLUME = 1.0
SONG_EXTENSIONS = {
    '.aac', '.ac3', '.aif', '.aiff', '.aifc', '.alac', '.amr', '.ape', '.au',
    '.caf', '.dts', '.flac', '.kar', '.m4a', '.m4b', '.m4p', '.mid', '.midi',
    '.mka', '.mod', '.mp2', '.mp3', '.mpc', '.mpga', '.oga', '.ogg', '.opus',
    '.ra', '.s3m', '.tta', '.voc', '.wav', '.wma', '.wv', '.xm',
}
STREAMABLE_SONG_EXTENSIONS = {
    '.aac', '.flac', '.kar', '.m4a', '.m4b', '.m4p', '.mid', '.midi', '.mod', '.mp2',
    '.mp3', '.mpga', '.oga', '.ogg', '.opus', '.s3m', '.xm',
}


def show_uncaught_exception(exc, write_log=True):
    if write_log:
        log_dir = Path(__file__).resolve().parent / "logs"
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        log_path = log_dir / f"genericfailure-{stamp}.log"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                                encoding="utf-8")
        except OSError as log_error:
            print(f"MarioAssistant: couldn't write exception log: {log_error}", file=sys.stderr, flush=True)
    try:
        if pygame.mixer.get_init():
            error_sound = pygame.mixer.Sound(str(ASSETS / "error.flac"))
            channel = pygame.mixer.find_channel(force=True)
            if channel is not None:
                channel.play(error_sound)
    except Exception as sound_error:
        print(f"MarioAssistant: couldn't play error sound: {sound_error}", file=sys.stderr, flush=True)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        message = f"An exception has occurred!\n\n{exc or type(exc).__name__}"
        root.overrideredirect(True)
        root.geometry("1x1+0+0")
        root.attributes("-topmost", True)
        root.deiconify()
        root.lift()
        root.focus_force()
        root.update_idletasks()
        try:
            result = messagebox.Message(
                root,
                title="Error!",
                message=message,
                icon=messagebox.ERROR,
                type=messagebox.ABORTRETRYIGNORE,
            ).show()
        finally:
            root.destroy()
        return {
            messagebox.ABORT: "Abort",
            messagebox.RETRY: "Retry",
            messagebox.IGNORE: "Ignore",
        }.get(result, "Ignore")
    except Exception as dialog_error:
        print(f"MarioAssistant: exception dialog failed: {dialog_error}", file=sys.stderr, flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        return "Ignore"


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


def decode_first_frame(ffmpeg: str, video: Path, start_time=0.0) -> pygame.Surface:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video)]
    if start_time > 0:
        command.extend(["-ss", f"{start_time:.3f}"])
    command.extend([
        "-vf", "scale=800:600:flags=fast_bilinear", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    if len(result.stdout) != FRAME_BYTES:
        raise RuntimeError(f"Could not decode the first frame of {video.name}.")
    return pygame.image.frombuffer(result.stdout, VIDEO_SIZE, "RGB").copy()


def format_spoken_time(moment):
    hour = moment.hour % 12 or 12
    period = 'A M' if moment.hour < 12 else 'P M'
    return f"It's {hour}:{moment.minute:02d} {period}."


def format_spoken_date(moment):
    weekdays = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
    months = ('January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December')
    day = moment.day
    suffix = 'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    return f"Today's {weekdays[moment.weekday()]}, {months[moment.month - 1]} {day}{suffix}, {moment.year}"


def create_sentence_captions(text, size):
    font = pygame.font.Font(None, 48)
    lines = []
    line = ""
    for word in str(text).split():
        candidate = f"{line} {word}".strip()
        if line and font.size(candidate)[0] > size[0] - 40:
            lines.append(line.strip())
            line = ""
        for character in word:
            if line and font.size(line + character)[0] > size[0] - 40:
                lines.append(line.strip())
                line = ""
            line += character
        line += " "
    if line.strip():
        lines.append(line.strip())
    line_height = font.get_linesize()
    lines_per_page = max(1, (size[1] - 40) // line_height)
    pages = []
    for offset in range(0, len(lines), lines_per_page):
        page_lines = lines[offset:offset + lines_per_page]
        overlay = pygame.Surface(size, pygame.SRCALPHA)
        height = len(page_lines) * line_height + 16
        top = size[1] - height - 12
        pygame.draw.rect(overlay, (0, 0, 0, 175), (10, top, size[0] - 20, height), border_radius=8)
        for index, line in enumerate(page_lines):
            label = font.render(line, True, (255, 255, 255))
            rectangle = label.get_rect(center=(size[0] // 2, top + 8 + line_height * (index + 0.5)))
            overlay.blit(label, rectangle)
        pages.append(overlay)
    return pages


def create_voice_progress_window(QtCore, QtWidgets):
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle('Please wait...')
    dialog.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    dialog.setModal(False)
    dialog.setFixedWidth(460)
    layout = QtWidgets.QVBoxLayout(dialog)
    stage_label = QtWidgets.QLabel('Transcribing... (ETA: estimating)')
    stage_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(stage_label)
    premix_label = QtWidgets.QLabel('0 premixes used')
    premix_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    premix_label.hide()
    layout.addWidget(premix_label)
    stage_bar = QtWidgets.QProgressBar()
    stage_bar.setRange(0, 100)
    layout.addWidget(stage_bar)
    total_layout = QtWidgets.QHBoxLayout()
    total_label = QtWidgets.QLabel('Total:')
    total_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    total_layout.addWidget(total_label)
    total_bar = QtWidgets.QProgressBar()
    total_bar.setRange(0, 100)
    total_layout.addWidget(total_bar, 1)
    layout.addLayout(total_layout)
    return dialog, stage_label, premix_label, stage_bar, total_bar


def start_decoder(ffmpeg: str, video: Path, start_time=0.0, duration=None):
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video)]
    if start_time > 0:
        command.extend(["-ss", f"{start_time:.3f}"])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend([
        "-an", "-vf", f"scale=800:600:flags=fast_bilinear,fps={VIDEO_FPS}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    process = subprocess.Popen(
        command,
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


def start_media(ffmpeg, temp_dir, video_texture, process, stop, reader, video, play_audio=True, start_time=0.0, duration=None):
    stop_decoder(process, stop, reader)
    if play_audio:
        audio_path = Path(temp_dir.name) / f"{video.stem}.wav"
        audio_command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
        if start_time > 0:
            audio_command.extend(["-ss", f"{start_time:.3f}"])
        if duration is not None:
            audio_command.extend(["-t", f"{duration:.3f}"])
        audio_command.extend([
            "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio_path),
        ])
        subprocess.run(
            audio_command,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        audio = pygame.mixer.Sound(str(audio_path))
        audio.set_volume(MARIO_VOLUME)
    else:
        audio = pygame.mixer.Sound(buffer=bytes(4))
    frame = decode_first_frame(ffmpeg, video, start_time)
    last_frame = decode_last_frame(ffmpeg, video) if duration is None else frame.copy()
    video_texture.update(frame)
    process, frames, stop, reader = start_decoder(
        ffmpeg, video, start_time, duration
    )
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
        ("IM Games", "im_games"),
        ("Super Debug", "superdebug"),
        ("Pet Care", "petcare"),
        ("Settings", "settings"),
        ("Speak", "speak"),
        ("Quench hunger", "quench_hunger"),
        ("Quench thirst", "quench_thirst"),
        ("Mario's voice command list", "commands"),
        ("Hello", "hello"),
        ("Make me say anything!", "say_anything"),
        ("Play a song!", "songs"),
        ("Stop music", "stop_music"),
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


def create_home_window(QtCore, QtGui, QtWidgets, menu_action):
    window = QtWidgets.QWidget()
    window.setWindowTitle("It's-a me, Mario!")
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.resize(750, 300)
    window.setMinimumSize(600, 220)
    layout = QtWidgets.QVBoxLayout(window)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    content = QtWidgets.QWidget()
    grid = QtWidgets.QGridLayout(content)
    grid.setContentsMargins(6, 6, 6, 6)
    grid.setHorizontalSpacing(4)
    grid.setVerticalSpacing(4)
    buttons = (
        ("Stop", "stop", "stop.png"),
        ("Move", "move", "move.png"),
        ("Interactive Mario Minigames", "minigames", "minigames.png"),
        ("Interactive Mario Games", "im_games", "minigames.png"),
        ("Pet Care", "petcare", "petcare.png"),
        ("Settings", "settings", "settings.png"),
        ("Quench hunger", "quench_hunger", "quench.png"),
        ("Quench thirst", "quench_thirst", "quench.png"),
        ("Voice command list", "commands", "voicelist.png"),
        ("Hello", "hello", "hello.png"),
        ("Goodbye", "goodbye", "goodbye.png"),
    )
    for index, (label, action, image_name) in enumerate(buttons):
        button = QtWidgets.QPushButton()
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(132, 122)
        button_layout = QtWidgets.QVBoxLayout(button)
        button_layout.setContentsMargins(2, 2, 2, 2)
        button_layout.setSpacing(1)
        image_label = QtWidgets.QLabel()
        image_label.setFixedSize(124, 87)
        image_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        pixmap = QtGui.QPixmap(str(ASSETS / "mariomenu" / image_name))
        if not pixmap.isNull():
            image_label.setPixmap(pixmap.scaled(
                124, 87,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            ))
        image_label.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        text_label = QtWidgets.QLabel(label)
        text_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        text_label.setWordWrap(True)
        text_label.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        button_layout.addWidget(image_label, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        button_layout.addWidget(text_label)
        button.clicked.connect(
            lambda checked=False, selected=action:
                menu_action.__setitem__(0, selected)
        )
        grid.addWidget(button, index // 5, index % 5)
    scroll.setWidget(content)
    layout.addWidget(scroll)
    return window


def create_minigames_window(QtWidgets, minigame_action, roll_sound):
    window = QtWidgets.QWidget()
    window.setWindowTitle("Interactive Mario Minigames")
    window.setMinimumSize(360, 300)
    layout = QtWidgets.QVBoxLayout(window)
    game_list = QtWidgets.QListWidget()
    game_list.addItem("Hey, I Got Something In My Eye")
    game_list.addItem("Mario's Tunnel of Doom")
    game_list.setCurrentRow(0)
    play_button = QtWidgets.QPushButton("Play!")
    roll_button = QtWidgets.QPushButton("Roll!")
    game_actions = ("eye", "tunnel")
    play_button.clicked.connect(
        lambda: minigame_action.__setitem__(
            0, game_actions[game_list.currentRow()]
        )
    )
    def roll_minigame():
        game_list.setCurrentRow(random.randrange(game_list.count()))
        roll_sound.play()

    roll_button.clicked.connect(roll_minigame)
    layout.addWidget(game_list)
    layout.addWidget(play_button)
    layout.addWidget(roll_button)
    return window


BOARD_GAMES = {
    'snakes_and_ladders': (SNAKES_AND_LADDERS_TITLE, create_snakes_and_ladders_window),
    'tic_tac_toe': (TIC_TAC_TOE_TITLE, create_tic_tac_toe_window),
    'backgammon': ('Backgammon', create_backgammon_window),
    'checkers': ('Checkers', create_checkers_window),
    'chess': ('Chess', create_chess_window),
    'go': ('Go', create_go_window),
}
IM_GAME_TITLES = {'connect4_start': 'Connect-4', **{
    action: title for action, (title, _) in BOARD_GAMES.items()
}, PIZZA_ACTION: PIZZA_TITLE, EXPLODING_BLOCKS_ACTION: EXPLODING_BLOCKS_TITLE}
SOLO_IM_GAMES = (PIZZA_ACTION, EXPLODING_BLOCKS_ACTION)


def create_im_games_window(QtWidgets, menu_action):
    window = QtWidgets.QWidget()
    window.setWindowTitle('Interactive Mario Games')
    window.setMinimumSize(360, 240)
    layout = QtWidgets.QVBoxLayout(window)
    game_list = QtWidgets.QListWidget()
    game_actions = tuple(IM_GAME_TITLES)
    game_list.addItems(list(IM_GAME_TITLES.values()))
    game_list.setCurrentRow(0)
    play_button = QtWidgets.QPushButton('Play!')
    watch_button = QtWidgets.QPushButton('Watch Mode')
    def play_selected():
        if game_list.currentRow() >= 0:
            menu_action[0] = game_actions[game_list.currentRow()]

    def watch_selected():
        row = game_list.currentRow()
        if row >= 0 and game_actions[row] not in SOLO_IM_GAMES:
            menu_action[0] = 'connect4_watch' if row == 0 else f'{game_actions[row]}_watch'

    game_list.currentRowChanged.connect(lambda row: watch_button.setEnabled(row >= 0 and game_actions[row] not in SOLO_IM_GAMES))
    play_button.clicked.connect(play_selected)
    watch_button.clicked.connect(watch_selected)
    layout.addWidget(game_list)
    layout.addWidget(play_button)
    layout.addWidget(watch_button)
    return window


def create_connect_four_window(QtCore, QtWidgets, say, on_result, on_turn, on_close):
    class ConnectFourWindow(QtWidgets.QWidget):
        def closeEvent(self, event):
            on_close()
            super().closeEvent(event)

    window = ConnectFourWindow()
    window.setWindowTitle('Connect-4')
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.setMinimumSize(430, 390)
    layout = QtWidgets.QVBoxLayout(window)
    status = QtWidgets.QLabel('Your turn')
    status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(status)
    grid = QtWidgets.QGridLayout()
    grid.setSpacing(5)
    layout.addLayout(grid)
    board_labels = []
    column_buttons = []
    board = [[0 for _ in range(7)] for _ in range(6)]
    active = [False]

    def winner(current, player):
        for row in range(6):
            for col in range(7):
                if current[row][col] != player:
                    continue
                for row_step, col_step in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    cells = []
                    for offset in range(4):
                        r = row + row_step * offset
                        c = col + col_step * offset
                        if not (0 <= r < 6 and 0 <= c < 7):
                            break
                        cells.append(current[r][c])
                    if len(cells) == 4 and all(cell == player for cell in cells):
                        return True
        return False

    def drop(current, column, player):
        for row in range(5, -1, -1):
            if current[row][column] == 0:
                current[row][column] = player
                return row
        return None

    def available_columns(current):
        return [col for col in (3, 2, 4, 1, 5, 0, 6) if current[0][col] == 0]

    def evaluate(current):
        score = sum(3 if current[row][3] == 2 else -3 if current[row][3] == 1 else 0
                    for row in range(6))
        for row in range(6):
            for col in range(7):
                for row_step, col_step in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    cells = []
                    for offset in range(4):
                        r = row + row_step * offset
                        c = col + col_step * offset
                        if not (0 <= r < 6 and 0 <= c < 7):
                            break
                        cells.append(current[r][c])
                    if len(cells) != 4:
                        continue
                    cpu_count = cells.count(2)
                    human_count = cells.count(1)
                    if not human_count:
                        score += (0, 2, 12, 90, 100000)[cpu_count]
                    if not cpu_count:
                        score -= (0, 2, 12, 90, 100000)[human_count]
        return score

    def choose_cpu_move(current):
        def search(position, depth, alpha, beta, maximizing):
            if winner(position, 2):
                return 100000 + depth, None
            if winner(position, 1):
                return -100000 - depth, None
            columns = available_columns(position)
            if depth == 0 or not columns:
                return evaluate(position), None
            best_column = columns[0]
            if maximizing:
                best_score = -math.inf
                for column in columns:
                    child = [row[:] for row in position]
                    drop(child, column, 2)
                    score, _ = search(child, depth - 1, alpha, beta, False)
                    if score > best_score:
                        best_score, best_column = score, column
                    alpha = max(alpha, best_score)
                    if beta <= alpha:
                        break
                return best_score, best_column
            best_score = math.inf
            for column in columns:
                child = [row[:] for row in position]
                drop(child, column, 1)
                score, _ = search(child, depth - 1, alpha, beta, True)
                if score < best_score:
                    best_score, best_column = score, column
                beta = min(beta, best_score)
                if beta <= alpha:
                    break
            return best_score, best_column

        _, column = search(current, 4, -math.inf, math.inf, True)
        return column

    def refresh_board():
        for row in range(6):
            for col in range(7):
                color = '#d9e7f5' if board[row][col] == 0 else '#e53935' if board[row][col] == 1 else '#fdd835'
                board_labels[row][col].setStyleSheet(
                    f'background-color: {color}; border: 2px solid #17365d; border-radius: 22px;'
                )

    def set_columns_enabled(enabled):
        for button in column_buttons:
            button.setEnabled(enabled)

    def finish_round(player_won):
        active[0] = False
        set_columns_enabled(False)
        if player_won:
            say("You're great at this game! You win!")
            on_result('player')
        elif player_won is False:
            say(r'\showas=Woohoo!\oh \hidden\ho! I won!')
            on_result('cpu')
        else:
            say("Let's try another round!")
            on_result('draw')

    def announce_player_turn():
        say(random.choice((
            'Your move!', 'Go!', r"\showas=It's-a\it's \hidden\a your turn!", 'Your turn!',
        )))
        status.setText('Your turn')
        set_columns_enabled(False)
        on_turn('enable_player')

    def enable_player_turn():
        if active[0]:
            status.setText('Your turn')
            set_columns_enabled(True)

    window.announce_player_turn = announce_player_turn
    window.enable_player_turn = enable_player_turn

    def cpu_turn():
        if not active[0]:
            return
        column = choose_cpu_move(board)
        if column is None:
            finish_round(None)
            return
        drop(board, column, 2)
        refresh_board()
        if winner(board, 2):
            finish_round(False)
        elif not available_columns(board):
            finish_round(None)
        else:
            on_turn('announce_player')

    window.cpu_turn = cpu_turn

    def human_move(column):
        if not active[0] or board[0][column] != 0:
            return
        set_columns_enabled(False)
        drop(board, column, 1)
        refresh_board()
        if winner(board, 1):
            finish_round(True)
        elif not available_columns(board):
            finish_round(None)
        else:
            say('My turn!')
            status.setText("Mario's turn")
            on_turn('cpu')

    for col in range(7):
        button = QtWidgets.QPushButton('↓')
        button.clicked.connect(lambda checked=False, selected=col: human_move(selected))
        grid.addWidget(button, 0, col)
        column_buttons.append(button)
    for row in range(6):
        label_row = []
        for col in range(7):
            cell = QtWidgets.QLabel()
            cell.setMinimumSize(44, 44)
            cell.setMaximumSize(58, 58)
            grid.addWidget(cell, row + 1, col)
            label_row.append(cell)
        board_labels.append(label_row)

    def reset_game():
        nonlocal board
        board = [[0 for _ in range(7)] for _ in range(6)]
        active[0] = True
        refresh_board()
        status.setText('Mario is ready')
        set_columns_enabled(False)
        say("\\showas=Let's-a\\let's \\hidden\\a go!")
        on_turn('announce_player')

    window.reset_game = reset_game
    refresh_board()
    return window


def scan_songs(songs_dir, ffprobe=None):
    songs_dir = Path(songs_dir)
    if not songs_dir.is_dir():
        return []
    songs = []
    for path in sorted(
        (candidate for candidate in songs_dir.rglob('*')
         if candidate.is_file() and candidate.suffix.lower() in SONG_EXTENSIONS),
        key=lambda candidate: str(candidate).casefold(),
    ):
        title = None
        try:
            from mutagen import File as MutagenFile

            metadata = MutagenFile(path, easy=True)
            if metadata is not None and metadata.tags:
                values = metadata.tags.get('title')
                if values:
                    value = values[0] if isinstance(values, (tuple, list)) else values
                    title = str(value).strip()
        except Exception:
            pass
        if not title and ffprobe:
            try:
                result = subprocess.run(
                    [ffprobe, '-v', 'error', '-show_entries',
                     'format_tags=title:stream_tags=title', '-of', 'json', str(path)],
                    capture_output=True, text=True, timeout=3, check=True,
                )
                metadata = json.loads(result.stdout)
                tags = metadata.get('format', {}).get('tags', {})
                if not tags:
                    tags = next((stream.get('tags', {}) for stream in metadata.get('streams', [])
                                 if stream.get('tags')), {})
                title = next((str(value).strip() for key, value in tags.items()
                              if key.casefold() == 'title' and str(value).strip()), None)
            except Exception:
                pass
        songs.append({
            'path': path,
            'title': title or path.stem,
            'streamable': path.suffix.casefold() in STREAMABLE_SONG_EXTENSIONS,
        })
    return songs


def scan_song_directories(songs_dir, extra_directories=(), ffprobe=None):
    directories = [Path(songs_dir)]
    if isinstance(extra_directories, (list, tuple)):
        directories.extend(
            Path(str(directory)).expanduser()
            for directory in extra_directories if isinstance(directory, str) and directory.strip()
        )
    songs = []
    seen = set()
    for directory in directories:
        for song in scan_songs(directory, ffprobe):
            try:
                identity = str(song['path'].resolve())
            except OSError:
                identity = str(song['path'].absolute())
            if identity in seen:
                continue
            seen.add(identity)
            songs.append(song)
    return sorted(songs, key=lambda song: (song['title'].casefold(), str(song['path']).casefold()))


def find_closest_song(songs, query):
    normalized_query = ' '.join(re.findall(r'[a-z0-9]+', query.casefold()))
    normalized_query = re.sub(r'^(?:a|the) ', '', normalized_query)
    if not normalized_query:
        return None
    ranked = []
    for song in songs:
        candidates = (song['title'], song['path'].stem)
        score = max(
            difflib.SequenceMatcher(
                None,
                normalized_query,
                ' '.join(re.findall(r'[a-z0-9]+', candidate.casefold())),
            ).ratio()
            for candidate in candidates
        )
        if any(normalized_query in ' '.join(re.findall(
                r'[a-z0-9]+', candidate.casefold())) for candidate in candidates):
            score = max(score, 0.92)
        ranked.append((score, song))
    if not ranked:
        return None
    score, song = max(ranked, key=lambda item: item[0])
    return song if score >= 0.35 else None


def cd_device_candidates():
    if sys.platform == 'win32':
        return [f'{letter}:' for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ']
    if sys.platform == 'darwin':
        return sorted(str(path) for path in Path('/dev').glob('rdisk*'))
    devices = sorted(str(path) for path in Path('/dev').glob('sr*'))
    device_link = Path('/dev/cdrom')
    if device_link.exists():
        devices.insert(0, str(device_link))
    return list(dict.fromkeys(devices))


def find_cd_track(track_number, ffprobe):
    if not ffprobe:
        raise RuntimeError('FFprobe is required to read audio CD tracks.')
    failures = []
    for device in cd_device_candidates():
        try:
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-f', 'libcdio', '-show_chapters',
                 '-show_entries', 'chapter=id,start_time,end_time', '-of', 'json', device],
                capture_output=True, text=True, check=True, timeout=10,
            )
            chapters = json.loads(result.stdout).get('chapters', [])
            for index, chapter in enumerate(chapters, start=1):
                chapter_id = chapter.get('id')
                try:
                    matches_track = int(chapter_id) == track_number
                except (TypeError, ValueError):
                    matches_track = index == track_number
                if matches_track:
                    start = float(chapter['start_time'])
                    end = float(chapter['end_time'])
                    if end > start:
                        return {
                            'device': device, 'start': start, 'duration': end - start,
                            'track': track_number,
                        }
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            failures.append(f'{device}: {exc}')
    details = '; '.join(failures[-3:])
    raise RuntimeError('No readable audio CD track was found.' + (f' {details}' if details else ''))


def start_cd_track_playback(track_info, ffplay, volume):
    if not ffplay:
        raise RuntimeError('FFplay is required to stream audio CD tracks.')
    return subprocess.Popen(
        [ffplay, '-nodisp', '-autoexit', '-loglevel', 'quiet', '-volume', str(volume), '-f', 'libcdio',
         '-ss', str(track_info['start']), '-t', str(track_info['duration']),
         track_info['device']],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def create_songs_window(QtCore, QtWidgets, song_action, roll_sound, songs):
    window = QtWidgets.QWidget()
    window.setWindowTitle('Play a song!')
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    window.setMinimumSize(360, 300)
    layout = QtWidgets.QVBoxLayout(window)
    song_list = QtWidgets.QListWidget()
    for song in songs:
        item = QtWidgets.QListWidgetItem(song['title'])
        item.setData(QtCore.Qt.ItemDataRole.UserRole, str(song['path']))
        song_list.addItem(item)
    if song_list.count():
        song_list.setCurrentRow(0)
    play_button = QtWidgets.QPushButton('Play!')
    roll_button = QtWidgets.QPushButton('Roll!')
    play_button.setEnabled(bool(songs))
    roll_button.setEnabled(bool(songs))

    def play_selected():
        row = song_list.currentRow()
        if 0 <= row < song_list.count():
            song_action[0] = row

    def roll_song():
        if song_list.count():
            song_list.setCurrentRow(random.randrange(song_list.count()))
            roll_sound.play()

    play_button.clicked.connect(play_selected)
    roll_button.clicked.connect(roll_song)
    layout.addWidget(song_list)
    layout.addWidget(play_button)
    layout.addWidget(roll_button)
    window._song_list = song_list
    window._songs = songs
    return window


def create_say_anything_window(QtCore, QtWidgets, menu_action):
    window = QtWidgets.QWidget()
    window.setWindowTitle("I'll say anything!")
    window.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    layout = QtWidgets.QHBoxLayout(window)
    text_input = QtWidgets.QLineEdit()
    say_button = QtWidgets.QPushButton("Say!")
    say_button.setEnabled(False)

    def submit():
        text = text_input.text().strip()
        if text:
            menu_action[0] = "simon_says:" + text

    text_input.textChanged.connect(
        lambda text: say_button.setEnabled(bool(text.strip()))
    )
    text_input.returnPressed.connect(submit)
    say_button.clicked.connect(submit)
    layout.addWidget(text_input)
    layout.addWidget(say_button)
    window.resize(320, window.sizeHint().height())
    window._text_input = text_input
    return window


def create_voice_commands_window(QtCore, QtWidgets):
    window = QtWidgets.QWidget()
    window.setWindowTitle("Mario's commands!")
    window.resize(520, 430)
    layout = QtWidgets.QVBoxLayout(window)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    content = QtWidgets.QWidget()
    content_layout = QtWidgets.QVBoxLayout(content)
    text = QtWidgets.QLabel(
        "Stop: ‘stop’, ‘shut up’, ‘skip’, ‘silence’, ‘shut your mouth’, or ‘stop it’.\n"
        "Hello: ‘hello’, ‘hi’, ‘howdy’, or ‘hey’; optionally add ‘there’.\n"
        "Eject CD: ‘eject CD’.\n"
        "Goodbye: ‘goodbye’.\n"
        "Move: ‘move’, ‘out of the way’, or ‘out of my way’.\n"
        "Open IM Minigames: ‘minigames’ with ‘open’, ‘interactive’, or ‘Mario’.\n"
        "Play a random minigame: ‘play a random minigame’.\n"
        "Play a song: ‘play’ followed by a song title, or ‘play a random song’.\n"
        "Play a CD track: ‘play’ followed by a track number, optionally saying ‘track’.\n"
        "Stop music: ‘stop music’ or ‘stop the music’.\n"
        "Play Tunnel of Doom: ‘play’ or ‘Mario’ with ‘Tunnel of Doom’.\n"
        "Play the eye minigame: ‘play’ or ‘Mario’ with ‘something in my eye’.\n"
        "Super Debug: ‘debug’.\n"
        "Pet Care: ‘pet care’.\n"
        "Settings: ‘settings’ or ‘configure’.\n"
        "Speak: ‘speak’ or ‘say something’.\n"
        "Repeat any text: ‘Simon says’ followed by what you want Mario to say.\n"
        "Oh, sorry!: ‘I didn’t say your name’ or ‘I didn’t call you’.\n"
        "Volume: ‘set volume to’, ‘lower volume by’, or ‘increase volume by’ with a number from 0 to 100; optionally add ‘percent’.\n"
        "Current volume: ‘what’s the volume’, ‘what’s the current volume’, or ‘what is the volume’ (optionally ‘current’).\n"
        "Repeat the last request: ‘what did you hear’ or ‘what’d you hear’.\n"
        "Time: ‘what’s the time’, ‘what is the time’, or ‘what time is it’.\n"
        "Date: ‘what day is it’.\n"
        "Weather: ‘what’s the weather’ or ‘what is the weather’; optionally add ‘current’ before ‘weather’ and ‘right now’ afterward.\n"
        "Wind: ‘what’s the wind’ or ‘what is the wind’; optionally add ‘current’ before ‘wind’ or ‘speed’ after it.\n"
        "News: ‘tell me the latest news from’ followed by a news network or ‘my current location’; optionally add ‘in text’ or ‘in audio’.\n"
        "Quench hunger or thirst: ‘quench’ with ‘hunger’ or ‘thirst’ (optionally say ‘Mario’s’).\n"
        "Show this list: ‘voice command’ or ‘voice commands’; optionally add ‘see’ and ‘Mario’s’, then ‘list’, ‘to say’, or ‘to use’."
    )
    text.setWordWrap(True)
    text.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    text.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
    content_layout.addWidget(text)
    scroll.setWidget(content)
    layout.addWidget(scroll)
    return window


def ask_proactive_suggestion(QtCore, QtWidgets, message):
    dialog = QtWidgets.QMessageBox()
    dialog.setWindowTitle('Just a suggestion!')
    dialog.setText(message)
    dialog.setStandardButtons(
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No
    )
    dialog.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
    dialog.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
    return dialog.exec() == QtWidgets.QMessageBox.StandardButton.Yes

def create_debug_window(
    QtCore, QtWidgets, live_values, test_exception_action=None,
    trigger_proactive_action=None, queue_getter=None, queue_setter=None,
    queue_adder=None, queue_clearer=None, queue_releaser=None,
):
    window = QtWidgets.QWidget()
    window.setWindowTitle("SUPER DEBUG!!!!")
    window.setMinimumSize(360, 230)
    layout = QtWidgets.QVBoxLayout(window)
    mariocoins_label = QtWidgets.QLabel()
    current_video_label = QtWidgets.QLabel()
    zen_mode_label = QtWidgets.QLabel()
    super_zen_mode_label = QtWidgets.QLabel()
    autofeed_label = QtWidgets.QLabel()
    eye_schedule_label = QtWidgets.QLabel()
    idle_schedule_label = QtWidgets.QLabel()
    wander_schedule_label = QtWidgets.QLabel()
    hunger_schedule_label = QtWidgets.QLabel()
    thirst_schedule_label = QtWidgets.QLabel()
    proactive_schedule_label = QtWidgets.QLabel()
    hunger_label = QtWidgets.QLabel()
    thirst_label = QtWidgets.QLabel()

    for label in (
        mariocoins_label, current_video_label, zen_mode_label,
        super_zen_mode_label, autofeed_label, eye_schedule_label, idle_schedule_label,
        wander_schedule_label, hunger_schedule_label, thirst_schedule_label,
        proactive_schedule_label, hunger_label, thirst_label,
    ):
        label.setWordWrap(True)

    value_labels = {
        "current_video": current_video_label,
        "mariocoins": mariocoins_label,
        "zen_mode": zen_mode_label,
        "super_zen_mode": super_zen_mode_label,
        "autofeed": autofeed_label,
        "eye_schedule": eye_schedule_label,
        "idle_schedule": idle_schedule_label,
        "wander_schedule": wander_schedule_label,
        "hunger_schedule": hunger_schedule_label,
        "thirst_schedule": thirst_schedule_label,
        "proactive_schedule": proactive_schedule_label,
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
            if value_name == "current_video":
                label.setText(live_values[value_name])
            elif value_name == "mariocoins":
                label.setText(f"MarioCoins: {live_values[value_name]}")
            elif value_name in ("hunger", "thirst"):
                label.setText(f"{value_name.capitalize()}: {live_values[value_name]:.1f}%")
            elif value_name == "zen_mode":
                label.setText(f"Zen Mode: {live_values[value_name]}")
            elif value_name == "super_zen_mode":
                label.setText(f"Super Zen Mode: {live_values[value_name]}")
            elif value_name == "autofeed":
                label.setText(f"Autofeed: {'On' if live_values[value_name] else 'Off'}")
            else:
                label.setText(live_values.get(value_name, ""))

    layout.addWidget(mariocoins_label)
    layout.addWidget(zen_mode_label)
    layout.addWidget(super_zen_mode_label)
    layout.addWidget(autofeed_label)
    layout.addWidget(eye_schedule_label)
    layout.addWidget(idle_schedule_label)
    layout.addWidget(wander_schedule_label)
    layout.addWidget(hunger_schedule_label)
    layout.addWidget(thirst_schedule_label)
    layout.addWidget(proactive_schedule_label)
    layout.addWidget(hunger_label)
    layout.addWidget(thirst_label)
    layout.addWidget(current_video_label)
    layout.addWidget(QtWidgets.QLabel("Queued actions"))
    queue_list = QtWidgets.QListWidget()
    queue_list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
    layout.addWidget(queue_list)
    queue_signature = [None]

    def refresh_queue():
        rows = queue_getter() if queue_getter is not None else []
        signature = tuple(rows)
        if signature == queue_signature[0]:
            return
        queue_signature[0] = signature
        queue_list.clear()
        for row in rows:
            triggered_at, sequence, kind, detail = row
            if kind == "proactive":
                label = {
                    "game": "Proactiveness (Minigames)",
                    "minigame": "Proactiveness (Minigames)",
                    "im_game": "Proactiveness (IM Games)",
                    "song": "Proactiveness (Music)",
                    "scheduled_game": "Proactiveness (Minigames)",
                    "scheduled_minigame": "Proactiveness (Minigames)",
                    "scheduled_im_game": "Proactiveness (IM Games)",
                    "scheduled_song": "Proactiveness (Music)",
                }.get(detail, "Proactiveness (Minigames, IM Games, or Music)")
            elif kind == "idle":
                label = f"Idle clip: {Path(detail).stem if detail else 'Random'}"
            elif kind == "eye":
                label = "Hey, I got something in my eye!"
            elif kind == "pet":
                label = f"Pet care ({detail})"
            elif kind == "autofeed":
                label = f"Autofeed ({detail})"
            elif kind == "wander":
                label = "Wandering"
            elif kind == "superzen_reward":
                label = f"Super Zen reward ({detail})" if detail else "Super Zen reward"
            elif kind == "scheduled_minigame":
                label = {
                    "eye": "Random minigame: Hey, I Got Something In My Eye!",
                    "tunnel": "Random minigame: Mario's Tunnel of Doom",
                }.get(detail, "Random minigame")
            else:
                label = str(kind)
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row)
            queue_list.addItem(item)

    def save_queue_order():
        if queue_setter is not None:
            rows = [queue_list.item(index).data(QtCore.Qt.ItemDataRole.UserRole)
                    for index in range(queue_list.count())]
            queue_setter(rows)

    queue_list.model().rowsMoved.connect(lambda *args: save_queue_order())
    queue_buttons = QtWidgets.QHBoxLayout()
    add_queue_button = QtWidgets.QPushButton("Add")
    remove_queue_button = QtWidgets.QPushButton("Remove")
    move_up_button = QtWidgets.QPushButton("Move up")
    move_down_button = QtWidgets.QPushButton("Move down")
    release_queue_button = QtWidgets.QPushButton("Release queue")
    clear_queue_button = QtWidgets.QPushButton("Clear queue")
    for button in (add_queue_button, remove_queue_button, move_up_button,
                   move_down_button, release_queue_button, clear_queue_button):
        queue_buttons.addWidget(button)
    layout.addLayout(queue_buttons)

    def add_queue_item():
        choices = [
            "Idle clip", "Random minigame", "Wandering",
            "Proactiveness (Minigames)", "Proactiveness (IM Games)",
            "Proactiveness (Music)",
        ]
        choice, accepted = QtWidgets.QInputDialog.getItem(
            window, "Add queued action", "Action:", choices, 0, False
        )
        if accepted and queue_adder is not None:
            queue_adder({
                "Idle clip": ("idle", None),
                "Random minigame": ("scheduled_minigame", None),
                "Wandering": ("wander", None),
                "Proactiveness (Minigames)": ("proactive", "minigame"),
                "Proactiveness (IM Games)": ("proactive", "im_game"),
                "Proactiveness (Music)": ("proactive", "song"),
            }[choice])
            refresh_queue()

    def remove_queue_item():
        index = queue_list.currentRow()
        if index >= 0:
            queue_list.takeItem(index)
            save_queue_order()

    def move_queue_item(delta):
        index = queue_list.currentRow()
        target = index + delta
        if 0 <= index < queue_list.count() and 0 <= target < queue_list.count():
            item = queue_list.takeItem(index)
            queue_list.insertItem(target, item)
            queue_list.setCurrentRow(target)
            save_queue_order()

    add_queue_button.clicked.connect(add_queue_item)
    remove_queue_button.clicked.connect(remove_queue_item)
    move_up_button.clicked.connect(lambda: move_queue_item(-1))
    move_down_button.clicked.connect(lambda: move_queue_item(1))
    clear_queue_button.clicked.connect(
        lambda: (queue_clearer() if queue_clearer is not None else None, refresh_queue())
    )
    release_queue_button.clicked.connect(
        lambda: (queue_releaser() if queue_releaser is not None else None, refresh_queue())
    )
    test_exception_button = QtWidgets.QPushButton("Test Exception")
    if test_exception_action is not None:
        test_exception_button.clicked.connect(test_exception_action)
    layout.addWidget(test_exception_button)
    proactive_button = QtWidgets.QPushButton('Trigger Proactive Activity')
    if trigger_proactive_action is not None:
        proactive_button.clicked.connect(trigger_proactive_action)
    layout.addWidget(proactive_button)
    update_value(force=True)
    refresh_queue()
    window._refresh_queue = refresh_queue
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
            ("IM Games", "im_games"),
            ("Super Debug", "superdebug"), ("Pet Care", "petcare"),
            ("Settings", "settings"), ("Speak", "speak"),
            ("Quench hunger", "quench_hunger"),
            ("Quench thirst", "quench_thirst"),
            ("Mario's voice command list", "commands"),
            ("Hello", "hello"),
            ("Make me say anything!", "say_anything"),
            ("Play a song!", "songs"),
            ("Stop music", "stop_music"),
            ("Goodbye", "goodbye"),
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
        ("IM Games", "im_games"),
        ("Super Debug", "superdebug"),
        ("Settings", "settings"),
        ("Speak", "speak"),
        ("Quench hunger", "quench_hunger"),
        ("Quench thirst", "quench_thirst"),
        ("Mario's voice command list", "commands"),
        ("Hello", "hello"),
        ("Make me say anything!", "say_anything"),
        ("Play a song!", "songs"),
        ("Stop music", "stop_music"),
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


def send_notification(Gio, GLib, notification_id=0, title="Hey!", body="I got something in my eye, can you help me get it out?", expire_timeout=0):
    if Gio is None:
        return NOTIFICATIONS.show(title, body, expire_timeout if expire_timeout > 0 else 60000)
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
            expire_timeout,
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
    from PySide6 import QtCore, QtGui, QtWidgets

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
    global NOTIFICATIONS, MARIO_VOLUME
    NOTIFICATIONS = DesktopNotifications(QtWidgets)
    goodbye_completed = False
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to play the video.")
    ffprobe = shutil.which('ffprobe')
    ffplay = shutil.which('ffplay')
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
    extra_song_folders = preferences.get('song_extra_folders')
    if not isinstance(extra_song_folders, (list, tuple)):
        extra_song_folders = []
    preferences['song_extra_folders'] = [
        str(Path(folder).expanduser())
        for folder in extra_song_folders
        if isinstance(folder, str) and folder.strip()
    ]
    allowed_random_minigames = ("eye", "tunnel")
    configured_random_minigames = preferences.get("random_minigames", ["eye"])
    if not isinstance(configured_random_minigames, (list, tuple)):
        configured_random_minigames = ["eye"]
    preferences["random_minigames"] = list(dict.fromkeys(
        game for game in configured_random_minigames
        if isinstance(game, str) and game in allowed_random_minigames
    ))
    for name in ("chattiness", "playfulness", "proactivity", "wandering", "caring_difficulty"):
        preferences[name] = max(0, min(10, int(preferences[name])))
    speech_methods = {"vosk_quick", "google", "vosk_smart"}
    speech_order = preferences.get("speech_method_order")
    if not isinstance(speech_order, list):
        speech_order = list(DEFAULT_SETTINGS["speech_method_order"])
    speech_order = list(dict.fromkeys(
        method for method in speech_order
        if isinstance(method, str) and method in speech_methods
    ))
    speech_order.extend(
        method for method in DEFAULT_SETTINGS["speech_method_order"]
        if method not in speech_order
    )
    preferences["speech_method_order"] = speech_order
    try:
        preferences["wake_word_sensitivity"] = max(
            0, min(10, int(preferences["wake_word_sensitivity"]))
        )
    except (TypeError, ValueError):
        preferences["wake_word_sensitivity"] = 5
    try:
        preferences["finish_sensitivity"] = max(
            0, min(10, int(preferences["finish_sensitivity"]))
        )
    except (TypeError, ValueError):
        preferences["finish_sensitivity"] = 5
    if (not isinstance(preferences.get("speech_preferred_method"), str)
            or preferences["speech_preferred_method"] not in speech_methods):
        preferences["speech_preferred_method"] = "google"
    for name in (
        "speech_use_one_method_only", "speech_enable_vosk",
        "speech_enable_smart_vosk", "speech_enable_google",
        "speech_enable_wake_word",
        "autofeed",
        "away_mode",
        "tts_live_sentence_mixing",
        "tts_subtitles",
    ):
        preferences[name] = bool(preferences[name])
    if preferences["wake_word_sensitivity"] == 0:
        preferences["speech_enable_wake_word"] = False
    pet = PetNeeds(
        initial_save, difficulty=preferences["caring_difficulty"]
    )
    pet_drops = []
    feeding_queue = []

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
        "current_video": "",
        "next_video": None,
        "mariocoins": mariocoins,
        "zen_mode": zen_mode,
        "super_zen_mode": super_zen_mode,
        "autofeed": preferences["autofeed"],
        "proactive_schedule": "Proactive activity: Disabled",
    }
    live_values.update(pet.snapshot())
    if not first_run and ("shuffling" in initial_save or any(name not in initial_save for name in DEFAULT_SETTINGS)):
        write_save_data(save_path, {**preferences, **pet.snapshot()})
    video = ASSETS / ("firsttimegreeting.mp4" if first_run else "greeting.avi")
    live_values["current_video"] = f"Video: {video.name}"
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
    hello_clip_deadline = None
    feeding_active = False
    feeding_is_rejection = False
    feeding_is_autofeed = False
    feeding_need = None
    feeding_audio_deadline = 0.0
    frame_surface = None
    frame_dirty = False
    restart_after_exception = False
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
        pygame.mixer.set_num_channels(16)
        voice_beep_sound = create_beep_sound(pygame)
        away_alarm_sound = pygame.mixer.Sound(str(ASSETS / "alarm.wav"))
        voice_beep_channel = pygame.mixer.Channel(7)
        news_beep_channel = pygame.mixer.Channel(5)
        mario_tts_channel = pygame.mixer.Channel(6)
        song_channel = pygame.mixer.Channel(8)
        wait_dialog, wait_stage_label, wait_premix_label, wait_stage_bar, wait_total_bar = create_voice_progress_window(
            QtCore, QtWidgets
        )
        wait_state = {'stage': None, 'started': 0.0, 'estimate': 1.0, 'reported': 0.0,
                      'close_at': None, 'total': 0.0, 'hmm_active': False,
                      'hmm_deadline': None}
        wait_stage_ranges = {
            'Transcribing...': (0, 60),
            'Processing...': (60, 20),
            'Sentence mixing...': (80, 20),
        }

        def stop_wait_hmm():
            nonlocal audio, process, reader, stop, frames, finished, frame_surface
            nonlocal last_frame_surface, frame_dirty, consumed, video
            if not wait_state['hmm_active']:
                return
            audio.stop()
            stop_decoder(process, stop, reader)
            process = None
            reader = None
            stop = threading.Event()
            frames = queue.Queue()
            finished = True
            if last_frame_surface is not None:
                frame_surface = last_frame_surface
                frame_dirty = True
            video = ASSETS / 'greeting.avi'
            wait_state['hmm_active'] = False
            wait_state['hmm_deadline'] = None

        def play_wait_hmm():
            nonlocal audio, frame_surface, last_frame_surface, process, frames, stop, reader
            nonlocal video, started, consumed, finished, frame_dirty
            stop_wait_hmm()
            audio.stop()
            stop_decoder(process, stop, reader)
            (
                audio, frame_surface, last_frame_surface, process,
                frames, stop, reader,
            ) = start_media(
                ffmpeg, temp_dir, video_texture, process, stop, reader,
                ASSETS / 'niceplace.avi',
            )
            video = ASSETS / 'niceplace.avi'
            started = time.monotonic()
            consumed = 0
            finished = False
            frame_dirty = False
            wait_state['hmm_active'] = True
            wait_state['hmm_deadline'] = started + 1.15

        def enter_wait_stage(stage, progress=0.0, estimate=1.0):
            now = time.monotonic()
            if stage != wait_state['stage']:
                wait_state['stage'] = stage
                wait_state['started'] = now
                wait_state['reported'] = 0.0
                wait_premix_label.setVisible(stage == 'Sentence mixing...')
                if stage == 'Sentence mixing...':
                    wait_premix_label.setText('0 premixes used')
                play_wait_hmm()
            wait_state['estimate'] = max(0.15, float(estimate))
            wait_state['reported'] = max(wait_state['reported'], min(1.0, float(progress)))
            wait_state['close_at'] = None
            if not wait_dialog.isVisible():
                wait_dialog.show()
                wait_dialog.raise_()
            update_wait_progress(now)

        def update_wait_progress(now=None):
            now = time.monotonic() if now is None else now
            stage = wait_state['stage']
            if stage is None or not wait_dialog.isVisible():
                return
            estimate = wait_state['estimate']
            elapsed = max(0.0, now - wait_state['started'])
            progress = max(wait_state['reported'], min(0.95, elapsed / estimate * 0.85))
            eta = max(0, math.ceil(estimate - elapsed))
            wait_stage_label.setText(f'{stage} (ETA: {eta} s)')
            wait_stage_bar.setValue(round(progress * 100))
            base, span = wait_stage_ranges[stage]
            wait_total_bar.setValue(min(100, round(base + span * progress)))
            if wait_state['close_at'] is not None and now >= wait_state['close_at']:
                stop_wait_hmm()
                wait_state['stage'] = None
                wait_state['total'] = 100.0
                wait_stage_bar.setValue(100)
                wait_total_bar.setValue(100)
                wait_dialog.hide()

        sentence_mixer_sound = [None]
        voice_transcriber = SpeechTranscriber(ASSETS, preferences)
        voice_transcriber.initialize_gpu()
        sentence_mixer = LiveSentenceMixer(ASSETS, ffmpeg)
        sentence_mixer.set_enabled(preferences["tts_live_sentence_mixing"])
        sentence_mix_results = queue.Queue()
        sentence_mix_premix_counts = queue.Queue()
        weather_results = queue.Queue()
        news_results = queue.Queue()
        news_generation = [0]
        news_audio_sound = [None]
        news_sequence = [None]
        weather_service = WeatherService(preferences['weather_city'], preferences['weather_country_code'])
        sentence_mix_generation = [0]
        mixed_caption = None
        mixed_video_path = None
        mixed_caption_page = -1
        mixed_caption_started = [0.0]
        mixed_caption_duration = [0.0]

        def speak_mario_text(text, language=None, cache_result=True, track_process=False):
            nonlocal mixed_caption, mixed_caption_page, frame_dirty
            mixed_caption = None
            mixed_caption_page = -1
            if preferences["tts_live_sentence_mixing"]:
                if requested_action["active"]:
                    requested_action["waiting"] = True
                    requested_action["deadline"] = None
                words = max(1, len(re.findall(r'[A-Za-z]+', text)))
                enter_wait_stage('Sentence mixing...', 0.0, max(2.0, 0.6 + words * 0.22))
                sentence_mix_generation[0] += 1
                generation = sentence_mix_generation[0]

                def synthesize():
                    try:
                        mix_text = sentence_mix_spanish(text) if language == 'es' else text
                        media = sentence_mixer.mix(
                            mix_text, with_video=True,
                            premix_count_callback=lambda count: sentence_mix_premix_counts.put(
                                (generation, count)
                            ),
                            cache=cache_result,
                        )
                    except Exception as exc:
                        media = None
                        print(f'MarioAssistant TTS: sentence-mixing failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
                    sentence_mix_results.put((generation, media))

                threading.Thread(target=synthesize, daemon=True).start()
                return True
            spoken_text = compatibility_tts_text(text, normalize_numbers)
            if requested_action["active"]:
                requested_action["waiting"] = False
                requested_action["deadline"] = time.monotonic() + max(
                    0.7, len(spoken_text.split()) / 2.4
                )
            cleaned, _, _, options, _ = tokenize_variant_text(text, normalize_numbers)
            subtitle_text = (
                format_subtitle_text(cleaned, options, normalize_numbers)
                if preferences.get('tts_subtitles', True) else ''
            )
            if subtitle_text:
                mixed_caption = create_sentence_captions(subtitle_text, VIDEO_SIZE)
                mixed_caption_page = -1
                mixed_caption_started[0] = time.monotonic()
                mixed_caption_duration[0] = max(1.0, len(spoken_text.split()) / 2.5)
            mixed_caption_duration[0] = max(0.0, mixed_caption_duration[0] if subtitle_text else 0.0)
            frame_dirty = True
            return speak_text(spoken_text, language=language, track_process=track_process)

        def game_speech_busy():
            deadline = requested_action.get("deadline")
            return (requested_action.get("waiting")
                    or (deadline is not None and time.monotonic() < deadline)
                    or mario_tts_channel.get_busy())

        def speak_uncached_mario_text(text, language=None):
            return speak_mario_text(text, language=language, cache_result=False)

        def speak_connect4_text(text):
            if not preferences.get("tts_live_sentence_mixing"):
                return speak_mario_text(text)
            prepared = connect4_line_cache.get(text)
            if prepared is None:
                if not sentence_mixer.enabled:
                    sentence_mixer.set_enabled(True)
                return speak_mario_text(text)
            try:
                sentence_mix_generation[0] += 1
                generation = sentence_mix_generation[0]
                source_video = Path(prepared["video"])
                play_video = Path(temp_dir.name) / (
                    f"connect4-line-{generation}{source_video.suffix or '.mp4'}"
                )
                shutil.copyfile(source_video, play_video)
                media = dict(prepared)
                media["video"] = play_video
                if requested_action["active"]:
                    requested_action["waiting"] = True
                    requested_action["deadline"] = None
                sentence_mix_results.put((generation, media))
                return True
            except (OSError, KeyError, TypeError) as exc:
                print(
                    f"MarioAssistant Connect-4: cached line unavailable: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return speak_mario_text(text)

        def request_weather(report_kind='weather'):
            sentence_mix_generation[0] += 1
            generation = sentence_mix_generation[0]
            service = weather_service

            def query():
                try:
                    report = service.get_wind_report() if report_kind == 'wind' else service.get_report()
                    weather_results.put((generation, report, None))
                except Exception as exc:
                    message = weather_failure_message(exc)
                    weather_results.put((generation, None, (exc, message)))

            threading.Thread(target=query, daemon=True).start()

        def request_latest_news(network, mode):
            news_generation[0] += 1
            generation = news_generation[0]
            city = preferences['weather_city']
            country = preferences['weather_country_code']

            def query():
                try:
                    result = latest_news(
                        network, mode, ffmpeg, temp_dir.name,
                        cancelled=lambda: generation != news_generation[0],
                        city=city, country=country,
                    )
                    if result is not None:
                        news_results.put((generation, result, None, network, mode))
                except Exception as exc:
                    news_results.put((generation, None, exc, network, mode))

            threading.Thread(target=query, daemon=True).start()

        voice_results = queue.Queue()
        voice_progress_events = queue.Queue()
        wake_events = queue.Queue()
        away_events = queue.Queue()
        wake_listener = WakeWordListener(voice_transcriber, wake_events)
        away_listener = LoudSoundListener(away_events)

        def set_away_listener(enabled):
            if enabled:
                away_listener.start()
            else:
                away_listener.stop()
                while True:
                    try:
                        away_events.get_nowait()
                    except queue.Empty:
                        break

        if preferences["away_mode"]:
            set_away_listener(True)

        def start_wake_listener():
            if (preferences.get("speech_enable_wake_word", True)
                    and preferences.get("wake_word_sensitivity", 5) > 0):
                wake_listener.start()
        voice_processing = False
        voice_silence_mode = False
        voice_request_pending = False
        voice_request_active = False
        voice_request_action = None
        last_heard_query = None
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
            "automatic": False,
        }
        ptt_paused_song = [False]

        def pause_song_for_push_to_talk():
            sequence = song_sequence[0]
            if sequence is None or sequence.get("stage") != "playing":
                return
            ptt_paused_song[0] = True
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.pause()
            song_channel.pause()

        def resume_song_after_push_to_talk():
            if not ptt_paused_song[0]:
                return
            ptt_paused_song[0] = False
            if song_sequence[0] is not None and song_sequence[0].get("stage") == "playing":
                pygame.mixer.music.unpause()
                song_channel.unpause()
        requested_action = {
            "active": False, "waiting": False, "deadline": None, "name": None,
        }
        automatic_action_queue = []
        automatic_queue_debug_order = [False]
        automatic_action_current = None
        automatic_queue_sequence = 0
        queued_auto_eye = False
        queued_auto_idle = None
        queued_auto_wander = False
        queued_auto_pet_need = None
        queued_pet_needs = set()
        queued_autofeed_need = None
        queued_autofeed_needs = set()

        next_random_minigame_selection = [None]

        def next_random_minigame_time(level=None):
            selected_level = preferences["playfulness"] if level is None else level
            if selected_level <= 0 or not preferences["random_minigames"]:
                next_random_minigame_selection[0] = None
                return math.inf
            next_random_minigame_selection[0] = random.choice(
                preferences["random_minigames"]
            )
            return time.monotonic() + random_delay(selected_level, 25, 75)

        def begin_requested_action(name, waiting=False):
            wake_listener.stop()
            requested_action.update({
                "active": True, "waiting": waiting, "deadline": None, "name": name,
            })

        def finish_requested_action():
            requested_action.update({
                "active": False, "waiting": False, "deadline": None, "name": None,
            })

        def choose_proactive_kind():
            songs = scan_song_directories(
                ASSETS / "songs", preferences["song_extra_folders"],
                shutil.which("ffprobe"),
            )
            return random.choice(
                ("minigame", "im_game", "song") if songs
                else ("minigame", "im_game")
            )

        def queue_automatic_action(triggered_at, kind, detail=None):
            nonlocal automatic_queue_sequence
            if kind == "proactive" and detail is None:
                detail = f"scheduled_{choose_proactive_kind()}"
            automatic_queue_sequence += 1
            automatic_action_queue.append((triggered_at, automatic_queue_sequence, kind, detail))
            if not automatic_queue_debug_order[0]:
                automatic_action_queue.sort(key=lambda item: (item[0], item[1]))

        def proactive_detail_is_forced(detail):
            return detail is True or detail in (
                "game", "minigame", "im_game", "song"
            )

        def queue_autofeed(need_name, triggered_at=None):
            if (not preferences["autofeed"] or need_name not in ("hunger", "thirst")
                    or pet.levels[need_name] >= 50):
                return False
            already_queued = any(
                kind == "autofeed" and detail == need_name
                for _, _, kind, detail in automatic_action_queue
            )
            already_playing = (
                feeding_active and feeding_is_autofeed and feeding_need == need_name
            )
            already_waiting = f"autofeed:{need_name}" in feeding_queue
            already_dispatched = (
                automatic_action_current == "autofeed"
                and queued_autofeed_need == need_name
            )
            if already_queued or already_playing or already_waiting or already_dispatched:
                queued_autofeed_needs.add(need_name)
                return False
            if need_name == "hunger":
                if any(
                    item[2] == "autofeed" and item[3] == "thirst"
                    for item in automatic_action_queue
                ):
                    automatic_action_queue[:] = [
                        item for item in automatic_action_queue
                        if not (item[2] == "autofeed" and item[3] == "thirst")
                    ]
                    queued_autofeed_needs.discard("thirst")
            queued_autofeed_needs.add(need_name)
            queue_automatic_action(
                time.monotonic() if triggered_at is None else triggered_at,
                "autofeed", need_name,
            )
            return True

        def debug_queue_set(rows):
            automatic_queue_debug_order[0] = True
            automatic_action_queue[:] = rows

        def debug_queue_add(entry):
            nonlocal automatic_queue_sequence
            kind, detail = entry
            if kind == "idle":
                detail = random.choice(IDLE_VIDEOS)
            elif kind == "scheduled_minigame":
                if not preferences["random_minigames"]:
                    return
                detail = random.choice(preferences["random_minigames"])
                kind = "superzen_reward" if super_zen_mode else "scheduled_minigame"
            automatic_queue_sequence += 1
            automatic_action_queue.append((time.monotonic(), automatic_queue_sequence, kind, detail))

        def debug_queue_clear():
            automatic_queue_debug_order[0] = True
            automatic_action_queue.clear()

        def debug_queue_release():
            nonlocal automatic_action_current
            automatic_queue_debug_order[0] = True
            if (song_sequence[0] is not None
                    and song_sequence[0].get("stage") == "playing"):
                if (requested_action["active"]
                        and requested_action.get("name") in ("play_song", "proactive_prompt")):
                    finish_requested_action()
                if automatic_action_current in ("proactive", "play_song"):
                    automatic_action_current = None
                if (proactive_sequence[0] is not None
                        and proactive_sequence[0].get("kind") == "song"):
                    proactive_sequence[0] = None
            elif (automatic_action_current == "proactive"
                    and proactive_sequence[0] is None
                    and not requested_action["active"]
                    and finished and not feeding_active and not idle_playing
                    and eye_event is None and pet_video is None and move_tween is None):
                automatic_action_current = None
            released_at = time.monotonic()
            automatic_action_queue[:] = [
                (released_at + index * 0.000001, row[1], row[2], row[3])
                for index, row in enumerate(automatic_action_queue)
            ]

        def voice_chord_held():
            return (
                bool(voice_keys_down & voice_control_keys)
                and bool(voice_keys_down & voice_shift_keys)
                and pygame.K_l in voice_keys_down
                and pygame.key.get_focused()
            )

        def begin_voice_beeps(phase):
            voice_beep_channel.stop()
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
            voice_state["automatic"] = False
            begin_voice_beeps("finishing")

        def transcribe_voice(samples):
            try:
                def report_progress(stage, progress, estimate):
                    voice_progress_events.put((stage, progress, estimate))
                transcript, engine, action = voice_transcriber.transcribe(
                    samples, progress_callback=report_progress
                )
                voice_results.put((transcript, None, action))
            except Exception as exc:
                voice_results.put((None, exc, None))

        audio_path = Path(temp_dir.name) / f"{video.stem}.wav"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        audio = pygame.mixer.Sound(str(audio_path))

        def mario_is_speaking():
            return (
                mario_tts_channel.get_busy()
                or audio.get_num_channels() > 0
            )

        disappointment_sound = pygame.mixer.Sound(str(ASSETS / "disappointment.wav"))
        kaching_sound = pygame.mixer.Sound(str(ASSETS / "kaching.wav"))
        minigame_roll_sound = pygame.mixer.Sound(str(ASSETS / "beep.wav"))
        eating_audio_path = Path(temp_dir.name) / "eating.wav"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i",
             str(ASSETS / "eating.mp3"), "-vn", "-ac", "2", "-ar", "44100",
             "-c:a", "pcm_s16le", str(eating_audio_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        eating_sound = pygame.mixer.Sound(str(eating_audio_path))
        drinking_sound = pygame.mixer.Sound(str(ASSETS / "glug.wav"))
        foodreject_audio_path = Path(temp_dir.name) / "foodreject.wav"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i",
             str(ASSETS / "foodreject.mp3"), "-vn", "-ac", "2", "-ar", "44100",
             "-c:a", "pcm_s16le", str(foodreject_audio_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        foodreject_sound = pygame.mixer.Sound(str(foodreject_audio_path))
        for sound in (
            audio, disappointment_sound, kaching_sound, eating_sound,
            drinking_sound, foodreject_sound, minigame_roll_sound,
            voice_beep_sound,
        ):
            sound.set_volume(MARIO_VOLUME)
        pygame.mixer.set_reserved(1)
        eating_channel = pygame.mixer.Channel(0)
        eating_video = ASSETS / "goodjoke.avi"
        eating_first_surface = decode_first_frame(ffmpeg, eating_video)
        eating_last_surface = decode_last_frame(ffmpeg, eating_video)

        frame_surface = decode_first_frame(ffmpeg, video)
        last_frame_surface = decode_last_frame(ffmpeg, video)
        greeting_first_surface = decode_first_frame(ffmpeg, ASSETS / "greeting.avi")

        desktop_width, desktop_height = pygame.display.get_desktop_sizes()[0]
        center = (desktop_width // 2, desktop_height // 2)
        video_texture = Texture(renderer, VIDEO_SIZE, streaming=True)
        menu_action = [None]
        active_menu = [None]
        set_tray_stop_menu(QtWidgets, NOTIFICATIONS.tray, menu_action)
        home_window = None
        booting = True
        startup_goodbye = False

        def show_home_window():
            if home_window is not None and not booting and not goodbye_running:
                home_window.show()
                home_window.raise_()
                home_window.activateWindow()

        def tray_activated(reason):
            if reason in (
                QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
                QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
            ):
                show_home_window()

        NOTIFICATIONS.tray.activated.connect(tray_activated)
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
        song_action = [None]
        song_results = queue.Queue()
        song_generation = [0]
        song_sequence = [None]
        cd_playback_process = [None]
        proactive_sequence = [None]
        proactive_debug_trigger = [False]
        proactive_pending_at = [None]
        random_song_notification_id = None
        random_song_notification_close_at = None
        random_minigame_notification = [None]
        random_minigame_notification_id = None
        random_minigame_notification_close_at = None
        voice_unrecognized_notification_id = None
        voice_unrecognized_notification_close_at = None

        def queue_random_minigame():
            selected = random.choice(("eye", "tunnel"))
            minigame_action[0] = selected
            minigame_roll_sound.play()
            random_minigame_notification[0] = (
                "Hey, I Got Something In My Eye"
                if selected == "eye"
                else "Mario's Tunnel of Doom"
            )

        def stop_song_playback():
            song_generation[0] += 1
            song_sequence[0] = None
            song_action[0] = None
            song_channel.stop()
            pygame.mixer.music.stop()
            process = cd_playback_process[0]
            cd_playback_process[0] = None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

        def song_audio_is_busy():
            process = cd_playback_process[0]
            if process is not None:
                if process.poll() is None:
                    return True
                cd_playback_process[0] = None
            return pygame.mixer.music.get_busy() or song_channel.get_busy()

        def start_song_request(song, random_pick=False):
            nonlocal random_song_notification_id, random_song_notification_close_at
            if song is None:
                if not requested_action['active']:
                    begin_requested_action('play_song')
                speak_mario_text("Oh, sorry! I couldn't find the song you were looking for.")
                return
            begin_requested_action('play_song', waiting=True)
            song_generation[0] += 1
            generation = song_generation[0]
            song_sequence[0] = {'stage': 'preparing', 'song': song, 'sound': None}
            if random_pick:
                if random_song_notification_id is not None:
                    try:
                        close_notification(Gio, GLib, random_song_notification_id)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                try:
                    random_song_notification_id = send_notification(
                        Gio, GLib, title='Hey!',
                        body=f"I'ma pick {song['title']} just for you!",
                        expire_timeout=10_000,
                    )
                    random_song_notification_close_at = time.monotonic() + 10
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    random_song_notification_id = None
                    random_song_notification_close_at = None

            def prepare():
                try:
                    sound = None
                    prepared_song = song
                    if song.get('cd_track') is not None:
                        prepared_song = dict(song)
                        prepared_song['cd_info'] = find_cd_track(song['cd_track'], ffprobe)
                    elif not song['streamable']:
                        try:
                            sound = pygame.mixer.Sound(str(song['path']))
                        except Exception:
                            decoded_path = Path(temp_dir.name) / f'song-{generation}.wav'
                            subprocess.run(
                                [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                                 '-i', str(song['path']), '-vn', '-ac', '2', '-ar', '44100',
                                 '-c:a', 'pcm_s16le', str(decoded_path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=True,
                            )
                            sound = pygame.mixer.Sound(str(decoded_path))
                            decoded_path.unlink(missing_ok=True)
                    song_results.put((generation, prepared_song, sound, None))
                except Exception as exc:
                    song_results.put((generation, song, None,
                                      f'{type(exc).__name__}: {exc}'))

            threading.Thread(target=prepare, daemon=True).start()

        def begin_proactive_activity(force=False, preferred_kind=None):
            nonlocal automatic_action_current
            proactive_pending_at[0] = None
            if preferred_kind == 'game':
                preferred_kind = 'minigame'
            if not force and (
                    preferences['proactivity'] <= 0 or super_zen_mode):
                proactive_pending_at[0] = None
                automatic_action_current = None
                return
            available_songs = scan_song_directories(
                ASSETS / 'songs', preferences['song_extra_folders'], shutil.which('ffprobe')
            )
            choices = ['minigame', 'im_game']
            if available_songs:
                choices.append('song')
            if preferred_kind is not None:
                if preferred_kind not in choices:
                    automatic_action_current = None
                    return
                kind = preferred_kind
            else:
                kind = random.choice(choices)
            if kind == 'song':
                song = (available_songs[0] if len(available_songs) == 1
                        else random.choice(available_songs))
                proactive_sequence[0] = {
                    'stage': 'prompt', 'kind': 'song', 'song': song,
                    'message': f"How 'bout we listen to some songs? My favorite pick is {song['title']}!",
                }
                spoken = (
                    "Hmm, right now's probably a good moment to take a break and relax! "
                    f"How about listening to some songs? Mario's favorite pick is {song['title']}! "
                    "How \\showas='bout\\about it?"
                )
            elif kind == 'im_game':
                game_action, game_name = random.choice(tuple(IM_GAME_TITLES.items()))
                proactive_sequence[0] = {
                    'stage': 'prompt', 'kind': 'im_game',
                    'action': game_action,
                    'message': f"How 'bout we play some games? My favorite pick is {game_name}!",
                }
                spoken = (
                    "I see you're looking quite bored, my friend! How about playing some games? "
                    f"Mario's favorite pick is {game_name}! How \\showas='bout\\about it?"
                )
            else:
                game_id = random.choice(('eye', 'tunnel'))
                game_name = (
                    'Hey, I Got Something In My Eye'
                    if game_id == 'eye' else "Mario's Tunnel of Doom"
                )
                proactive_sequence[0] = {
                    'stage': 'prompt', 'kind': 'game', 'game': game_id,
                    'message': f"How 'bout we play some games? My favorite pick is {game_name}!",
                }
                spoken = (
                    "I see you're looking quite bored, my friend! How about playing some games? "
                    f"Mario's favorite pick is {game_name}! How \\showas='bout\\about it?"
                )
            begin_requested_action('proactive_prompt')
            speak_mario_text(spoken)

        minigames_window = None
        im_games_window = None
        connect4_window = None
        board_game_windows = {}
        connect4_result = [None]
        connect4_continue_requested = [False]
        connect4_turn_event = [None]
        connect4_line_cache = {}
        connect4_prep_results = queue.Queue()
        connect4_prep_generation = [0]
        connect4_preparing = [None]
        connect4_previous_mixer_enabled = [None]
        connect4_lines_prepared = [False]

        BOARD_GAME_DIALOGUE = tuple(dict.fromkeys((
            r"\showas=Let's-a\let's \hidden\a go!", 'Your move!', 'Go!',
            r"\showas=It's-a\it's \hidden\a your turn!", 'Your turn!',
            'My turn!', r'\showas=Woohoo!\oh \hidden\ho! I won!',
            "You're great at this game! You win!", "It's a tie! Want to try again?",
            'You won!', 'Mario won!', 'Luigi wins!', 'Luigi played first.',
        )))

        def prepare_board_game_dialogue():
            if not preferences.get("tts_live_sentence_mixing"):
                return
            sentence_mixer.set_enabled(True)
            for line in BOARD_GAME_DIALOGUE:
                if line in connect4_line_cache:
                    continue
                try:
                    media = sentence_mixer.mix(line, with_video=True, cache=True)
                    if media is not None:
                        connect4_line_cache[line] = media
                except Exception as exc:
                    print(f"MarioAssistant game dialogue preparation: {exc}", file=sys.stderr)

        def cancel_connect4_preparation():
            connect4_prep_generation[0] += 1
            connect4_preparing[0] = None
            if connect4_previous_mixer_enabled[0] is not None:
                sentence_mixer.set_enabled(connect4_previous_mixer_enabled[0])
                connect4_previous_mixer_enabled[0] = None
        songs_window = None
        song_entries = []
        voice_commands_window = None
        say_anything_window = None
        debug_window = None
        debug_update = None
        last_debug_schedule_update = 0.0
        care_window = None
        care_update = None
        pet_video = None
        pet_alert_need = None
        pet_notification_close_at = {}
        next_autofeed_check = time.monotonic() + 5
        pizza_meal = None

        def start_pizza_meal(tip_coins, on_finished):
            nonlocal pizza_meal, process, frames, stop, reader, video, started, consumed
            nonlocal finished, frame_surface, last_frame_surface, frame_dirty
            audio.stop()
            stop_decoder(process, stop, reader)
            process, frames, stop, reader = start_decoder(ffmpeg, eating_video)
            video = eating_video
            frame_surface = eating_first_surface
            last_frame_surface = eating_last_surface
            started = time.monotonic()
            consumed = 0
            finished = False
            frame_dirty = True
            eating_channel.play(eating_sound)
            pizza_meal = ("eating", tip_coins, on_finished)

        def end_pizza_animation():
            nonlocal process, reader, video, finished, frame_surface, last_frame_surface, frame_dirty
            stop_decoder(process, stop, reader)
            process = reader = None
            video = ASSETS / "greeting.avi"
            frame_surface = last_frame_surface = greeting_first_surface
            finished = frame_dirty = True

        def cancel_pizza_meal():
            nonlocal pizza_meal
            if pizza_meal is not None:
                eating_channel.stop()
                if pizza_meal[0] == "eating":
                    end_pizza_animation()
                pizza_meal = None

        def advance_pizza_meal():
            nonlocal pizza_meal, mariocoins, last_pet_save
            if pizza_meal is None or eating_channel.get_busy():
                return
            stage, tip_coins, on_finished = pizza_meal
            if stage == "eating":
                end_pizza_animation()
                pet.fill("hunger")
                notification = pet_notifications.pop("hunger", None)
                pet_notification_close_at.pop("hunger", None)
                if notification is not None:
                    try:
                        close_notification(Gio, GLib, notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                previous_mariocoins = mariocoins
                mariocoins += tip_coins
                live_values["mariocoins"] = mariocoins
                write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                last_pet_save = time.monotonic()
                if tip_coins:
                    pizza_meal = ("tip", tip_coins, on_finished)
                    eating_channel.play(kaching_sound)
                    show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                    if debug_update is not None:
                        debug_update("mariocoins")
                    return
            pizza_meal = None
            on_finished()

        def finish_pet_alert():
            nonlocal pet_alert_need
            if pet_alert_need is not None:
                pet_notification_close_at[pet_alert_need] = time.monotonic() + 7.5
                pet_alert_need = None

        def close_mario_notifications():
            nonlocal eye_notification, voice_unrecognized_notification_id
            nonlocal random_minigame_notification_id
            nonlocal voice_unrecognized_notification_close_at
            nonlocal random_minigame_notification_close_at
            nonlocal random_song_notification_id, random_song_notification_close_at
            for notification in tuple(pet_notifications.values()):
                try:
                    close_notification(Gio, GLib, notification)
                except Exception as exc:
                    print(f"MarioAssistant notification: {exc}", file=sys.stderr)
            pet_notifications.clear()
            pet_notification_close_at.clear()
            for notification in (
                eye_notification,
                voice_unrecognized_notification_id,
                random_minigame_notification_id,
                random_song_notification_id,
            ):
                if notification is not None:
                    try:
                        close_notification(Gio, GLib, notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
            eye_notification = None
            voice_unrecognized_notification_id = None
            random_minigame_notification_id = None
            random_song_notification_id = None
            voice_unrecognized_notification_close_at = None
            random_minigame_notification_close_at = None
            random_song_notification_close_at = None

        def suspend_media_for_voice():
            nonlocal audio, process, reader, stop, frames, video, finished
            nonlocal frame_surface, last_frame_surface, frame_dirty, idle_playing
            nonlocal next_idle_at, pet_video, eye_event, eye_notification, eye_manual
            nonlocal next_eye_at, random_events_ready_at, feeding_active
            nonlocal feeding_is_rejection, feeding_is_autofeed, feeding_need
            nonlocal post_greeting_tween, move_tween, greeting_tween_started, is_greeting
            nonlocal voice_silence_mode, mixed_caption
            nonlocal automatic_action_current
            sentence_mix_generation[0] += 1
            mixed_caption = None
            mario_tts_channel.stop()
            voice_silence_mode = True
            voice_beep_channel.stop()
            news_beep_channel.stop()
            eating_channel.stop()
            interrupted_autofeed_needs = []
            if feeding_is_autofeed and feeding_need is not None:
                interrupted_autofeed_needs.append(feeding_need)
            interrupted_autofeed_needs.extend(
                item.removeprefix("autofeed:")
                for item in feeding_queue
                if item.startswith("autofeed:")
            )
            for need_name in interrupted_autofeed_needs:
                already_queued = any(
                    kind == "autofeed" and detail == need_name
                    for _, _, kind, detail in automatic_action_queue
                )
                if not already_queued:
                    queue_automatic_action(time.monotonic(), "autofeed", need_name)
                queued_autofeed_needs.add(need_name)
            if automatic_action_current == "autofeed":
                automatic_action_current = None
            feeding_active = False
            feeding_is_rejection = False
            feeding_is_autofeed = False
            feeding_need = None
            feeding_queue.clear()
            audio.stop()
            stop_decoder(process, stop, reader)
            process = None
            reader = None
            stop = threading.Event()
            frames = queue.Queue()
            finished = True
            if idle_playing:
                idle_playing = False
                next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
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
                    next_eye_at = next_random_minigame_time()
                eye_manual = False
            frame_surface = greeting_first_surface
            last_frame_surface = greeting_first_surface
            video_texture.update(greeting_first_surface)
            frame_dirty = True
            video = ASSETS / "greeting.avi"
            random_events_ready_at = max(
                random_events_ready_at, time.monotonic() + 10
            )
            post_greeting_tween = None
            move_tween = None
            greeting_tween_started = True
            is_greeting = False

        def finish_voice_request():
            nonlocal voice_silence_mode, voice_request_pending
            nonlocal voice_request_active, voice_request_action
            voice_silence_mode = False
            voice_request_pending = False
            voice_request_active = False
            voice_request_action = None
            if (voice_state["phase"] == "idle" and not voice_processing
                    and not goodbye_running and not requested_action["active"]):
                wake_listener.stop()
                start_wake_listener()
        settings_window = None
        settings_sync = None
        settings_changes = []
        wake_sensitivity_restart_at = None
        eye_manual = False
        move_tween = None
        eye_video = ASSETS / "somethinginmyeye.avi"
        eye_event = None
        eye_notification = None
        pet_notifications = {}
        next_eye_at = next_random_minigame_time()
        next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
        next_proactive_at = time.monotonic() + proactivity_delay(preferences["proactivity"])
        idle_prediction_deadline = None
        idle_prediction = None
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
            try:
                clock.tick(60)
                qt_app.processEvents()
                if (debug_window is not None and debug_window.isVisible()
                        and hasattr(debug_window, "_refresh_queue")):
                    debug_window._refresh_queue()
                now = time.monotonic()
                while True:
                    try:
                        away_events.get_nowait()
                    except queue.Empty:
                        break
                    if preferences["away_mode"] and not goodbye_running:
                        try:
                            send_notification(
                                Gio, GLib, title="Hey!",
                                body="I heard something loud! You might want to check who that is!",
                            )
                        except Exception as exc:
                            print(f"MarioAssistant away mode notification: {exc}",
                                  file=sys.stderr, flush=True)
                        away_alarm_sound.stop()
                        away_alarm_sound.play(loops=2)
                while True:
                    try:
                        progress_stage, progress_value, progress_eta = voice_progress_events.get_nowait()
                    except queue.Empty:
                        break
                    enter_wait_stage(progress_stage, progress_value, progress_eta)
                    if progress_value >= 1.0:
                        stop_wait_hmm()
                while True:
                    try:
                        premix_generation, premix_count = sentence_mix_premix_counts.get_nowait()
                    except queue.Empty:
                        break
                    if premix_generation == sentence_mix_generation[0]:
                        wait_premix_label.setText(
                            f'{premix_count} premix{"es" if premix_count != 1 else ""} used'
                        )
                update_wait_progress(now)
                if (wait_state['hmm_active'] and wait_state['hmm_deadline'] is not None
                        and now >= wait_state['hmm_deadline']):
                    stop_wait_hmm()
                try:
                    wake_transcript = wake_events.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if (voice_state["phase"] == "idle" and not voice_processing
                            and not requested_action["active"]):
                        wake_listener.stop()
                        suspend_media_for_voice()
                        voice_state["automatic"] = True
                        print(
                            f"MarioAssistant wake word: detected {wake_transcript!r}",
                            file=sys.stderr,
                            flush=True,
                        )
                        begin_voice_beeps("arming")
                if (
                    not booting
                    and preferences.get("speech_enable_wake_word", True)
                    and voice_state["phase"] == "idle"
                    and not voice_processing
                    and not voice_silence_mode
                    and not goodbye_running
                    and not requested_action["active"]
                ):
                    wake_listener.start()
                if (voice_state["phase"] in ("arming", "finishing")
                        and now >= voice_state["deadline"]):
                    if voice_state["step"] == 0:
                        voice_state["step"] = 1
                        voice_state["deadline"] = now + 0.25
                    elif voice_state["step"] == 1:
                        voice_state["step"] = 2
                        voice_beep_channel.stop()
                        voice_beep_channel.play(voice_beep_sound)
                        voice_state["deadline"] = now + 0.25
                    elif voice_state["phase"] == "arming":
                        if voice_chord_held() or voice_state["automatic"]:
                            try:
                                voice_state["capture"] = MicrophoneCapture(
                                    finish_sensitivity=preferences["finish_sensitivity"]
                                )
                                voice_state["phase"] = "listening"
                            except Exception as exc:
                                voice_state["error"] = exc
                                finish_voice_capture()
                        else:
                            finish_voice_capture()
                    else:
                        voice_state["phase"] = "idle"
                        resume_song_after_push_to_talk()
                        if voice_state["error"] is not None:
                            voice_results.put((None, voice_state["error"], None))
                        elif voice_state["samples"] is not None:
                            voice_processing = True
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
                                    next_eye_at = next_random_minigame_time()
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
                            threading.Thread(
                                target=transcribe_voice,
                                args=(voice_state["samples"],),
                                daemon=True,
                            ).start()
                        else:
                            voice_silence_mode = False
                            start_wake_listener()
                        voice_state["samples"] = None
                        voice_state["error"] = None
                if (
                    voice_state["phase"] == "listening"
                    and voice_state["automatic"]
                    and voice_state["capture"] is not None
                    and voice_state["capture"].should_auto_stop(now)
                ):
                    finish_voice_capture()
                try:
                    transcript, voice_error, voice_action = voice_results.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if wait_state['stage'] is not None:
                        wait_state['close_at'] = time.monotonic() + 0.45
                    stop_wait_hmm()
                    if transcript and voice_action != "what_heard":
                        last_heard_query = transcript
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
                            menu_action[0] = "voice_unrecognized"
                        else:
                            if voice_action in ("tunnel", "eye"):
                                minigame_action[0] = voice_action
                            elif voice_action == "random_minigame":
                                queue_random_minigame()
                            elif voice_action is not None:
                                menu_action[0] = voice_action
                            else:
                                menu_action[0] = voice_action or "voice_unrecognized"
                    else:
                        if voice_error is not None:
                            print(
                                f"MarioAssistant voice: request failed: "
                                f"{type(voice_error).__name__}: {voice_error}",
                                file=sys.stderr,
                                flush=True,
                            )
                            menu_action[0] = "voice_unrecognized"
                        else:
                            if voice_action in ("tunnel", "eye"):
                                minigame_action[0] = voice_action
                            elif voice_action == "random_minigame":
                                queue_random_minigame()
                            else:
                                menu_action[0] = voice_action or "voice_unrecognized"
                    voice_request_pending = True
                    voice_request_active = False
                    voice_request_action = (
                        minigame_action[0]
                        if voice_action == "random_minigame"
                        else voice_action or "voice_unrecognized"
                    )
                if pet.tick(paused=requested_action["active"]):
                    if not voice_silence_mode:
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
                    if name == "tts_live_sentence_mixing":
                        sentence_mixer.set_enabled(value)
                    if name == "away_mode":
                        set_away_listener(value)
                    if name == 'song_extra_folders':
                        preferences[name] = [
                            str(Path(folder).expanduser())
                            for folder in value
                            if isinstance(folder, str) and folder.strip()
                        ] if isinstance(value, (list, tuple)) else []
                        if songs_window is not None:
                            songs_window.close()
                            songs_window = None
                            song_entries = []
                        value = preferences[name]
                    if name == "random_minigames":
                        value = list(dict.fromkeys(
                            game for game in value
                            if game in ("eye", "tunnel")
                        )) if isinstance(value, (list, tuple)) else []
                        preferences[name] = value
                        next_eye_at = next_random_minigame_time()
                        automatic_action_queue[:] = [
                            item for item in automatic_action_queue
                            if item[2] not in ("scheduled_minigame", "superzen_reward")
                            or item[3] in value
                        ]
                    if name == "tts_subtitles" and not value:
                        mixed_caption = None
                        mixed_caption_page = -1
                        mixed_caption_duration[0] = 0.0
                    if name in ('weather_city', 'weather_country_code'):
                        weather_service = WeatherService(preferences['weather_city'], preferences['weather_country_code'])
                        sentence_mix_generation[0] += 1
                    if name == "caring_difficulty":
                        pet.set_difficulty(value)
                    write_save_data(save_path, {name: value, **pet.snapshot()})
                    if name == "zen_mode":
                        zen_mode = value
                    elif name == "super_zen_mode":
                        super_zen_mode = value
                        next_proactive_at = time.monotonic() + proactivity_delay(
                            preferences['proactivity']
                        )
                        if value:
                            automatic_action_queue[:] = [
                                item for item in automatic_action_queue
                                if item[2] != 'proactive' or proactive_detail_is_forced(item[3])
                            ]
                            proactive_pending_at[0] = None
                    elif name == "proactivity":
                        next_proactive_at = time.monotonic() + proactivity_delay(value)
                        if value == 0:
                            automatic_action_queue[:] = [
                                item for item in automatic_action_queue
                                if item[2] != 'proactive' or proactive_detail_is_forced(item[3])
                            ]
                            proactive_pending_at[0] = None
                    elif name == "speech_enable_wake_word":
                        if value and not voice_silence_mode and voice_state["phase"] == "idle":
                            start_wake_listener()
                        else:
                            wake_listener.stop()
                    elif name == "wake_word_sensitivity":
                        wake_sensitivity_restart_at = time.monotonic() + 0.3
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
                        next_eye_at = next_random_minigame_time(value)
                        if value == 0:
                            automatic_action_queue[:] = [
                                item for item in automatic_action_queue
                                if item[2] not in ("scheduled_minigame", "superzen_reward")
                            ]
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
                if (wake_sensitivity_restart_at is not None
                        and now >= wake_sensitivity_restart_at):
                    if (preferences.get("speech_enable_wake_word", True)
                            and preferences.get("wake_word_sensitivity", 5) > 0
                            and not voice_silence_mode
                            and voice_state["phase"] == "idle"
                            and not goodbye_running):
                        wake_listener.stop()
                        start_wake_listener()
                    elif preferences.get("wake_word_sensitivity", 5) <= 0:
                        wake_listener.stop()
                    wake_sensitivity_restart_at = None
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
                        required_modifiers = pygame.KMOD_CTRL | pygame.KMOD_SHIFT
                        if (event.key == pygame.K_m
                                and event.mod & required_modifiers == required_modifiers):
                            menu_action[0] = "home"
                        if (event.key in voice_control_keys or event.key in voice_shift_keys
                                or event.key == pygame.K_l):
                            voice_keys_down.add(event.key)
                            if (voice_state["phase"] == "idle" and voice_chord_held()
                                    and not requested_action["active"]):
                                wake_listener.stop()
                                pause_song_for_push_to_talk()
                                suspend_media_for_voice()
                                voice_state["automatic"] = False
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
                            next_proactive_at = time.monotonic() + proactivity_delay(
                                preferences['proactivity']
                            )
                            if super_zen_mode:
                                automatic_action_queue[:] = [
                                    item for item in automatic_action_queue
                                    if item[2] != 'proactive' or proactive_detail_is_forced(item[3])
                                ]
                                proactive_pending_at[0] = None
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
                        if not voice_silence_mode:
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
                    previous_mariocoins = mariocoins
                    mariocoins += 50
                    live_values["mariocoins"] = mariocoins
                    kaching_sound.play()
                    show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                    if debug_update is not None:
                        debug_update("mariocoins")
                    write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                    last_pet_save = time.monotonic()
                pet_drops.clear()

                if (feeding_active and not eating_channel.get_busy()
                        and now >= feeding_audio_deadline
                        and (not feeding_is_rejection or finished)):
                    completed_autofeed = feeding_is_autofeed
                    completed_need = feeding_need
                    stop_decoder(process, stop, reader)
                    process = None
                    reader = None
                    feeding_active = False
                    finished = True
                    video = ASSETS / "greeting.avi"
                    frame_surface = greeting_first_surface
                    last_frame_surface = greeting_first_surface
                    frame_dirty = True
                    random_events_ready_at = time.monotonic() + 10
                    if not feeding_is_rejection:
                        previous_mariocoins = mariocoins
                        mariocoins += 50
                        live_values["mariocoins"] = mariocoins
                        kaching_sound.play()
                        show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                        if debug_update is not None:
                            debug_update("mariocoins")
                        other_need = "thirst" if feeding_need == "hunger" else "hunger"
                        if pet.levels[other_need] < 50:
                            queue_autofeed(other_need, now)
                        write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                        last_pet_save = time.monotonic()
                    if (completed_autofeed and not any(
                        item.startswith("autofeed:") for item in feeding_queue
                    )):
                        close_mario_notifications()
                    feeding_is_rejection = False
                    feeding_is_autofeed = False
                    feeding_need = None
                    if completed_autofeed:
                        if completed_need is not None:
                            queued_autofeed_needs.discard(completed_need)
                        queued_autofeed_need = None
                        if (automatic_action_current == "autofeed"
                                and not any(item.startswith("autofeed:") for item in feeding_queue)):
                            automatic_action_current = None

                if now >= next_autofeed_check:
                    check_at = next_autofeed_check
                    next_autofeed_check = now + 5
                    if preferences["autofeed"] and not voice_silence_mode:
                        if (automatic_action_current == "autofeed"
                                and not feeding_active
                                and not any(item.startswith("autofeed:") for item in feeding_queue)
                                and not any(kind == "autofeed" for _, _, kind, _ in automatic_action_queue)):
                            automatic_action_current = None
                            queued_autofeed_need = None
                        for need_name in ("hunger", "thirst"):
                            if pet.levels[need_name] >= 50:
                                queued_autofeed_needs.discard(need_name)
                        for need_name in ("hunger", "thirst"):
                            if pet.levels[need_name] < 50:
                                queue_autofeed(need_name, check_at)
                                break

                if (feeding_queue and not feeding_active and not voice_silence_mode
                        and not requested_action["active"]
                        and automatic_action_current in (None, "autofeed")
                        and (not automatic_action_queue or automatic_action_current == "autofeed")):
                    feed_item = feeding_queue.pop(0)
                    feeding_is_rejection = feed_item == "rejected"
                    feeding_is_autofeed = feed_item.startswith("autofeed:")
                    if feeding_is_autofeed:
                        automatic_action_current = "autofeed"
                    feeding_need = (
                        feed_item.removeprefix("autofeed:")
                        if feeding_is_autofeed
                        else None if feeding_is_rejection else feed_item
                    )
                    if feeding_is_autofeed:
                        queued_autofeed_need = feeding_need
                    audio.stop()
                    if idle_playing:
                        idle_playing = False
                        next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
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
                            next_eye_at = next_random_minigame_time()
                        eye_manual = False
                    stop_decoder(process, stop, reader)
                    if feeding_is_rejection:
                        feed_video = eating_video
                        frame_surface = eating_first_surface
                        last_frame_surface = eating_last_surface
                        feed_sound = foodreject_sound
                    else:
                        feed_video = eating_video
                        frame_surface = eating_first_surface
                        last_frame_surface = eating_last_surface
                        feed_sound = drinking_sound if feeding_need == "thirst" else eating_sound
                    video_texture.update(frame_surface)
                    process, frames, stop, reader = start_decoder(ffmpeg, feed_video)
                    video = feed_video
                    started = time.monotonic()
                    consumed = 0
                    finished = False
                    frame_dirty = True
                    feeding_active = True
                    feeding_audio_deadline = started + max(0.1, feed_sound.get_length())
                    post_greeting_tween = None
                    move_tween = None
                    greeting_tween_started = True
                    is_greeting = False
                    eating_channel.play(feed_sound)

                for need_name, deadline in tuple(pet_notification_close_at.items()):
                    if time.monotonic() >= deadline:
                        notification = pet_notifications.pop(need_name, None)
                        if notification is not None:
                            try:
                                close_notification(Gio, GLib, notification)
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                        pet_notification_close_at.pop(need_name, None)

                if (voice_unrecognized_notification_id is not None
                        and voice_unrecognized_notification_close_at is not None
                        and now >= voice_unrecognized_notification_close_at):
                    try:
                        close_notification(
                            Gio, GLib, voice_unrecognized_notification_id
                        )
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    voice_unrecognized_notification_id = None
                    voice_unrecognized_notification_close_at = None

                if (random_minigame_notification_id is not None
                        and random_minigame_notification_close_at is not None
                        and now >= random_minigame_notification_close_at):
                    try:
                        close_notification(
                            Gio, GLib, random_minigame_notification_id
                        )
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    random_minigame_notification_id = None
                    random_minigame_notification_close_at = None

                if (random_song_notification_id is not None
                        and random_song_notification_close_at is not None
                        and now >= random_song_notification_close_at):
                    try:
                        close_notification(Gio, GLib, random_song_notification_id)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    random_song_notification_id = None
                    random_song_notification_close_at = None

                while Gtk is not None and Gtk.events_pending():
                    Gtk.main_iteration_do(False)

                if skip_requested or menu_action[0] == "stop":
                    game_action = requested_action["name"]
                    if game_action in SOLO_IM_GAMES:
                        game_window = board_game_windows.get(game_action)
                        if game_window is not None and game_window.isVisible():
                            game_window.close()
                    if game_action == PIZZA_ACTION:
                        cancel_pizza_meal()
                    proactive_sequence[0] = None
                    if automatic_action_current == 'proactive':
                        automatic_action_current = None
                    if song_sequence[0] is not None and song_sequence[0].get('stage') != 'playing':
                        song_generation[0] += 1
                        song_sequence[0] = None
                    sentence_mix_generation[0] += 1
                    news_generation[0] += 1
                    news_sequence[0] = None
                    news_audio_sound[0] = None
                    news_beep_channel.stop()
                    finish_requested_action()
                    mario_tts_channel.stop()
                    mixed_caption = None
                    stopped_from_menu = menu_action[0] == "stop"
                    if menu_action[0] == "stop":
                        menu_action[0] = None
                    if stopped_from_menu and voice_request_pending:
                        finish_voice_request()
                    if feeding_active or feeding_queue:
                        if feeding_active and feeding_is_autofeed and feeding_need is not None:
                            if feeding_need not in queued_autofeed_needs:
                                queue_automatic_action(now, "autofeed", feeding_need)
                                queued_autofeed_needs.add(feeding_need)
                            if automatic_action_current == "autofeed":
                                automatic_action_current = None
                        eating_channel.stop()
                        feeding_active = False
                        feeding_is_rejection = False
                        feeding_is_autofeed = False
                        feeding_need = None
                        feeding_queue.clear()
                    collect_eye = False
                    finish_pet_alert()
                    pet_video = None
                    finished = True
                    random_events_ready_at = time.monotonic() + 10
                    stop.set()
                    if process is not None and process.poll() is None:
                        process.terminate()
                    audio.stop()
                    if idle_playing:
                        idle_playing = False
                        next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                        frame_surface = greeting_first_surface
                        last_frame_surface = greeting_first_surface
                    else:
                        frame_surface = last_frame_surface
                    frame_dirty = True
                    if eye_event is not None:
                        if zen_mode and not eye_manual:
                            collect_eye = True
                        else:
                            try:
                                close_notification(Gio, GLib, eye_notification)
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                            eye_notification = None
                            disappointment_sound.play()
                            eye_event = None
                            if not eye_manual:
                                next_eye_at = next_random_minigame_time()
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
                        next_eye_at = next_random_minigame_time()
                    eye_manual = False

                selected_action = menu_action[0]
                menu_action[0] = None
                watch_board_game = (selected_action is not None and selected_action.endswith('_watch')
                                    and selected_action[:-6] in BOARD_GAMES)
                if watch_board_game:
                    selected_action = selected_action[:-6]
                if voice_silence_mode and not voice_request_pending and selected_action != "goodbye":
                    selected_action = None
                if requested_action["active"] and selected_action is not None:
                    if selected_action not in (
                            "quench_hunger", "quench_thirst", "stop", "stop_music"):
                        selected_action = None
                if selected_action == "stop":
                    proactive_sequence[0] = None
                    if automatic_action_current == 'proactive':
                        automatic_action_current = None
                    connect4_result[0] = None
                    connect4_turn_event[0] = None
                    cancel_connect4_preparation()
                    if connect4_window is not None:
                        connect4_window.hide()
                    for game_window in board_game_windows.values():
                        if game_window.isVisible():
                            game_window.close()
                    if song_sequence[0] is not None and song_sequence[0].get('stage') != 'playing':
                        song_generation[0] += 1
                        song_sequence[0] = None
                    finish_requested_action()
                elif selected_action == "stop_music":
                    stop_song_playback()
                    if requested_action["active"] and requested_action["name"] == "play_song":
                        finish_requested_action()
                    if voice_request_active:
                        finish_voice_request()
                elif selected_action is not None and not requested_action["active"]:
                    requested_action_names = (
                        "hello", "speak", "voice_unrecognized", "voice_apology", "ohsorry",
                        "goodbye", "time_query", "date_query", "weather_query", "wind_query",
                        "volume_query", "eject_cd", "move", "songs", "random_song",
                        "connect4_start", "connect4_watch", *BOARD_GAMES, *SOLO_IM_GAMES,
                    )
                    is_requested_action = (
                        selected_action in requested_action_names
                        or selected_action.startswith("simon_says:")
                        or selected_action.startswith("news_query:")
                        or selected_action.startswith("song_query:")
                        or selected_action.startswith("cd_track:")
                        or selected_action.startswith("volume_")
                    )
                    if is_requested_action:
                        begin_requested_action(
                            "connect4" if selected_action in ("connect4_start", "connect4_watch") else selected_action,
                            waiting=(selected_action in ("weather_query", "wind_query")
                                     or selected_action in BOARD_GAMES
                                     or selected_action.startswith("news_query:")
                                     or selected_action in ("connect4_start", "connect4_watch")),
                        )
                        interrupted_automatic = False
                        if eye_event is not None and not eye_manual:
                            queue_automatic_action(eye_event, "eye")
                            try:
                                close_notification(Gio, GLib, eye_notification)
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                            eye_notification = None
                            eye_event = None
                            interrupted_automatic = True
                        if idle_playing and video in IDLE_VIDEOS:
                            queue_automatic_action(started, "idle", video)
                            idle_playing = False
                            next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                            interrupted_automatic = True
                        if pet_video is not None and pet_alert_need is not None:
                            queue_automatic_action(started, "pet", pet_alert_need)
                            queued_pet_needs.add(pet_alert_need)
                            pet_video = None
                            pet_alert_need = None
                            interrupted_automatic = True
                        if move_tween is not None and move_tween.get("wander"):
                            queue_automatic_action(move_tween["started"], "wander")
                            move_tween = None
                            interrupted_automatic = True
                        if feeding_active and feeding_is_autofeed and feeding_need is not None:
                            if feeding_need not in queued_autofeed_needs:
                                queue_automatic_action(started, "autofeed", feeding_need)
                                queued_autofeed_needs.add(feeding_need)
                            eating_channel.stop()
                            feeding_active = False
                            feeding_is_autofeed = False
                            feeding_need = None
                            interrupted_automatic = True
                        if interrupted_automatic:
                            automatic_action_current = None
                            audio.stop()
                            stop_decoder(process, stop, reader)
                            process = None
                            reader = None
                            finished = True
                            video = ASSETS / "greeting.avi"
                            frame_surface = greeting_first_surface
                            last_frame_surface = greeting_first_surface
                            frame_dirty = True
                if voice_request_pending and selected_action is not None:
                    voice_request_pending = False
                    voice_request_active = True
                if feeding_active and (
                    (selected_action is not None
                     and selected_action not in ("quench_hunger", "quench_thirst"))
                    or minigame_action[0] is not None
                ):
                    if feeding_is_autofeed and feeding_need is not None:
                        if feeding_need not in queued_autofeed_needs:
                            queue_automatic_action(now, "autofeed", feeding_need)
                            queued_autofeed_needs.add(feeding_need)
                        if automatic_action_current == "autofeed":
                            automatic_action_current = None
                    eating_channel.stop()
                    feeding_active = False
                    feeding_is_rejection = False
                    feeding_is_autofeed = False
                    feeding_need = None
                if selected_action == "voice_unrecognized":
                    play_error_sound(QtWidgets)
                    if voice_unrecognized_notification_id is not None:
                        try:
                            close_notification(
                                Gio, GLib, voice_unrecognized_notification_id
                            )
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    try:
                        voice_unrecognized_notification_id = send_notification(
                            Gio,
                            GLib,
                            title="Hey!",
                            body="I didn't understand what you just said. Wanna try again?",
                            expire_timeout=5000,
                        )
                        voice_unrecognized_notification_close_at = time.monotonic() + 5
                        if requested_action["active"]:
                            requested_action["deadline"] = voice_unrecognized_notification_close_at
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                        voice_unrecognized_notification_id = None
                        voice_unrecognized_notification_close_at = None
                elif selected_action == "hello":
                    audio.stop()
                    idle_playing = False
                    if pet_video is not None:
                        finish_pet_alert()
                        pet_video = None
                    hello_video = ASSETS / "firsttimegreeting.mp4"
                    (
                        audio, frame_surface, last_frame_surface, process,
                        frames, stop, reader,
                    ) = start_media(
                        ffmpeg, temp_dir, video_texture, process, stop, reader,
                        hello_video, start_time=0.35, duration=1.8,
                    )
                    video = hello_video
                    started = time.monotonic()
                    hello_clip_deadline = started + 1.8
                    consumed = 0
                    finished = False
                    frame_dirty = False
                    post_greeting_tween = None
                    move_tween = None
                    greeting_tween_started = True
                    is_greeting = False
                elif selected_action == "volume_query":
                    volume_percent = get_system_volume()
                    if volume_percent is None:
                        print("MarioAssistant volume: couldn't read the system output volume", file=sys.stderr)
                    else:
                        speak_mario_text(f"The volume is set to {volume_percent} percent")
                elif selected_action == "eject_cd":
                    if eject_cd():
                        play_task_complete_sound(QtWidgets)
                    else:
                        disappointment_sound.play()
                    if requested_action["active"]:
                        requested_action["deadline"] = time.monotonic() + 0.8
                elif selected_action == "what_heard":
                    heard_text = last_heard_query or "I haven't heard a command yet"
                    speak_mario_text(f'I heard: "{heard_text}"')
                elif selected_action is not None and selected_action.startswith("simon_says:"):
                    said_text = selected_action.split(":", 1)[1]
                    said_language = 'es' if is_spanish_text(said_text) else None
                    speak_mario_text(said_text, language=said_language)
                elif selected_action == "time_query":
                    speak_mario_text(format_spoken_time(datetime.now()))
                elif selected_action == "weather_query":
                    request_weather()
                elif selected_action == "wind_query":
                    request_weather('wind')
                elif selected_action is not None and selected_action.startswith("news_query:"):
                    _, mode, network = selected_action.split(":", 2)
                    request_latest_news(network, mode)
                elif selected_action == "date_query":
                    speak_mario_text(format_spoken_date(datetime.now()))
                elif selected_action == "songs":
                    song_entries = scan_song_directories(
                        ASSETS / 'songs', preferences['song_extra_folders'], shutil.which('ffprobe')
                    )
                    if songs_window is not None:
                        songs_window.close()
                    songs_window = create_songs_window(
                        QtCore, QtWidgets, song_action, minigame_roll_sound, song_entries
                    )
                    songs_window.show()
                    songs_window.raise_()
                    songs_window.activateWindow()
                    if song_entries:
                        speak_mario_text('Pick a song for me to play back!')
                    else:
                        speak_mario_text(
                            "Oh, sorry! I couldn't find the song you were looking for."
                        )
                elif selected_action == "random_song":
                    song_entries = scan_song_directories(
                        ASSETS / 'songs', preferences['song_extra_folders'], shutil.which('ffprobe')
                    )
                    if song_entries:
                        minigame_roll_sound.play()
                        start_song_request(random.choice(song_entries), random_pick=True)
                    else:
                        start_song_request(None)
                elif selected_action is not None and selected_action.startswith('song_query:'):
                    query = selected_action.split(':', 1)[1]
                    song_entries = scan_song_directories(
                        ASSETS / 'songs', preferences['song_extra_folders'], shutil.which('ffprobe')
                    )
                    start_song_request(find_closest_song(song_entries, query))
                elif selected_action is not None and selected_action.startswith('cd_track:'):
                    track_number = int(selected_action.split(':', 1)[1])
                    start_song_request({
                        'title': f'Track {track_number}', 'path': None,
                        'streamable': True, 'cd_track': track_number,
                    })
                elif selected_action is not None and selected_action.startswith("volume_"):
                    operation, amount_text = selected_action.removeprefix("volume_").split(":", 1)
                    amount = float(amount_text)
                    current_volume = get_system_volume()
                    if current_volume is None:
                        print("MarioAssistant volume: couldn't read the system output volume", file=sys.stderr)
                    else:
                        if operation == "set":
                            new_volume = amount
                        elif operation == "lower":
                            new_volume = current_volume - amount
                        else:
                            new_volume = current_volume + amount
                        if set_system_volume(new_volume):
                            play_information_sound(QtWidgets)
                            if requested_action["active"]:
                                requested_action["deadline"] = time.monotonic() + 0.8
                        else:
                            print("MarioAssistant volume: couldn't change the system output volume", file=sys.stderr)
                elif selected_action in ("voice_apology", "ohsorry"):
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
                            next_eye_at = next_random_minigame_time()
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
                    cancel_connect4_preparation()
                    proactive_sequence[0] = None
                    if automatic_action_current == 'proactive':
                        automatic_action_current = None
                    if home_window is not None:
                        home_window.hide()
                    wake_listener.stop()
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
                    for auxiliary in (settings_window, minigames_window, im_games_window,
                                     connect4_window, *board_game_windows.values(), songs_window,
                                     debug_window, care_window, voice_commands_window,
                                     say_anything_window):
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
                elif selected_action == "home":
                    show_home_window()
                elif selected_action == "settings":
                    if settings_window is None:
                        def clear_mario_cache():
                            sentence_mixer.clear_cache()
                            connect4_line_cache.clear()
                            connect4_lines_prepared[0] = False

                        settings_window, settings_sync = create_settings_window(
                            QtCore, QtWidgets, preferences,
                            lambda name, value: settings_changes.append((name, value)),
                            ASSETS, SONG_EXTENSIONS, STREAMABLE_SONG_EXTENSIONS,
                            clear_mario_cache,
                        )
                    settings_window.show()
                    settings_window.raise_()
                    settings_window.activateWindow()
                elif selected_action == "say_anything":
                    if say_anything_window is None:
                        say_anything_window = create_say_anything_window(
                            QtCore, QtWidgets, menu_action
                        )
                    say_anything_window.show()
                    say_anything_window.raise_()
                    say_anything_window.activateWindow()
                    say_anything_window._text_input.setFocus()
                elif selected_action == "commands":
                    if voice_commands_window is None:
                        voice_commands_window = create_voice_commands_window(
                            QtCore, QtWidgets
                        )
                    voice_commands_window.show()
                    voice_commands_window.raise_()
                    voice_commands_window.activateWindow()
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
                            QtWidgets, minigame_action, minigame_roll_sound
                        )
                    minigames_window.show()
                    minigames_window.raise_()
                    minigames_window.activateWindow()
                elif selected_action == "im_games":
                    if im_games_window is None:
                        im_games_window = create_im_games_window(
                            QtWidgets, menu_action
                        )
                    im_games_window.show()
                    im_games_window.raise_()
                    im_games_window.activateWindow()
                elif selected_action == PIZZA_ACTION:
                    game_window = board_game_windows.get(PIZZA_ACTION)
                    if game_window is None:
                        def pizza_closed():
                            if requested_action.get("name") == PIZZA_ACTION:
                                finish_requested_action()

                        def pizza_high_score():
                            value = initial_save.get("pizza_high_score")
                            return value if type(value) is int and 0 <= value <= 100 else None

                        def save_pizza_high_score(score):
                            write_save_data(save_path, {"pizza_high_score": score})
                            initial_save["pizza_high_score"] = score

                        music_loaded = False
                        pizza_speech_process = None

                        def load_pizza_music():
                            nonlocal music_loaded
                            stop_song_playback()
                            try:
                                pygame.mixer.music.load(str(ASSETS / 'EXPRESS.MID'))
                            except (pygame.error, OSError) as exc:
                                print(f'MarioAssistant Pizza: EXPRESS.MID could not load: {exc}', file=sys.stderr)
                                return False
                            music_loaded = True
                            return True

                        def start_pizza_music():
                            try:
                                pygame.mixer.music.set_volume(MARIO_VOLUME * 0.45)
                                pygame.mixer.music.play(-1)
                            except pygame.error as exc:
                                print(f'MarioAssistant Pizza: music playback failed: {exc}', file=sys.stderr)

                        def stop_pizza_music():
                            nonlocal music_loaded
                            if music_loaded:
                                pygame.mixer.music.stop()
                                pygame.mixer.music.unload()
                                music_loaded = False

                        def prepare_pizza_line(line, cache):
                            if not preferences["tts_live_sentence_mixing"]:
                                return None
                            results = queue.Queue()
                            cancelled = threading.Event()
                            lock = threading.Lock()

                            def mix():
                                media = None
                                try:
                                    media = sentence_mixer.mix(line, with_video=True, cache=cache)
                                    if media is not None:
                                        media['pizza_temporary'] = media['video'].parent != sentence_mixer.cache_dir
                                except Exception as exc:
                                    print(f'MarioAssistant Pizza: dialogue preparation failed: {exc}', file=sys.stderr)
                                with lock:
                                    if not cancelled.is_set():
                                        results.put(media)
                                    elif media and media.get('pizza_temporary'):
                                        media['video'].unlink(missing_ok=True)

                            threading.Thread(target=mix, daemon=True).start()
                            return results, cancelled, lock

                        def play_pizza_line(media):
                            nonlocal pizza_speech_process
                            pizza_speech_process = None
                            if not media['video'].is_file():
                                return False
                            sentence_mix_generation[0] += 1
                            requested_action["waiting"] = True
                            requested_action["deadline"] = None
                            sentence_mix_results.put((sentence_mix_generation[0], media))
                            return True

                        def say_pizza_line(line, cache=True):
                            nonlocal pizza_speech_process
                            result = speak_mario_text(line, cache_result=cache, track_process=True)
                            pizza_speech_process = result if isinstance(result, subprocess.Popen) else None

                        def pizza_speech_busy():
                            if pizza_speech_process is not None:
                                return pizza_speech_process.poll() is None
                            return game_speech_busy()

                        game_window = create_pizza_window(
                            QtCore, QtWidgets, pizza_closed, say_pizza_line,
                            lambda line: say_pizza_line(line, cache=False),
                            pizza_high_score, save_pizza_high_score, pizza_speech_busy,
                            prepare_pizza_line, play_pizza_line,
                            load_pizza_music, start_pizza_music, stop_pizza_music,
                            start_pizza_meal, cancel_pizza_meal,
                            save_path.parent / "pizza.win",
                        )
                        board_game_windows[PIZZA_ACTION] = game_window
                    game_window.reset_game()
                    game_window.show()
                    game_window.raise_()
                    game_window.activateWindow()
                elif selected_action == EXPLODING_BLOCKS_ACTION:
                    game_window = board_game_windows.get(EXPLODING_BLOCKS_ACTION)
                    if game_window is None:
                        blocks_save_path = save_path.parent / "blocks.win"
                        legacy_blocks_path = save_path.parent / "explodingblocks.win"
                        if not blocks_save_path.exists() and legacy_blocks_path.exists():
                            try:
                                legacy_blocks_path.replace(blocks_save_path)
                            except OSError:
                                pass
                        def exploding_blocks_closed():
                            if requested_action.get("name") == EXPLODING_BLOCKS_ACTION:
                                finish_requested_action()

                        def blocks_high_score():
                            value = initial_save.get("exploding_blocks_high_score", 0)
                            return value if type(value) is int and value >= 0 else 0

                        def save_blocks_high_score(score):
                            write_save_data(save_path, {"exploding_blocks_high_score": score})
                            initial_save["exploding_blocks_high_score"] = score

                        blocks_music_loaded = False
                        blocks_speech_process = None
                        blocks_line_cache = {}

                        def say_blocks_line(line):
                            nonlocal blocks_speech_process
                            media = blocks_line_cache.get(line)
                            if preferences["tts_live_sentence_mixing"] and media is not None:
                                source_video = Path(media["video"])
                                try:
                                    playback_video = Path(temp_dir.name) / (
                                        f"exploding-blocks-{sentence_mix_generation[0] + 1}{source_video.suffix}"
                                    )
                                    shutil.copyfile(source_video, playback_video)
                                except OSError as error:
                                    blocks_line_cache.pop(line, None)
                                    print(f"MarioAssistant Exploding Blocks: rebuilding dialogue: {error}", file=sys.stderr)
                                else:
                                    blocks_speech_process = None
                                    sentence_mix_generation[0] += 1
                                    requested_action["waiting"] = True
                                    requested_action["deadline"] = None
                                    playback_media = dict(media, video=playback_video)
                                    sentence_mix_results.put((sentence_mix_generation[0], playback_media))
                                    return
                            result = speak_mario_text(line, track_process=True)
                            blocks_speech_process = result if isinstance(result, subprocess.Popen) else None

                        def blocks_speech_busy():
                            return (blocks_speech_process is not None and blocks_speech_process.poll() is None) or game_speech_busy()

                        def prepare_blocks_game(lines):
                            nonlocal blocks_music_loaded
                            stop_song_playback()
                            results = queue.Queue()
                            cancelled = threading.Event()
                            try:
                                pygame.mixer.music.load(str(ASSETS / "SELECT.MID"))
                                blocks_music_loaded = True
                            except (pygame.error, OSError) as error:
                                print(f"MarioAssistant Exploding Blocks: {error}", file=sys.stderr)
                            mixing = preferences["tts_live_sentence_mixing"]

                            def prepare():
                                total = len(lines) + 1 if mixing else 1
                                results.put((1, total, "Preparing Mario's dialogue…"))
                                if mixing:
                                    for index, line in enumerate(lines, 2):
                                        if cancelled.is_set():
                                            return
                                        try:
                                            media = sentence_mixer.mix(line, with_video=True, cache=True)
                                            if media is not None:
                                                blocks_line_cache[line] = media
                                        except Exception as error:
                                            print(f"MarioAssistant Exploding Blocks: {error}", file=sys.stderr)
                                        results.put((index, total, f"Preparing dialogue {index - 1}/{len(lines)}…"))

                            threading.Thread(target=prepare, daemon=True).start()
                            return results, cancelled

                        def start_blocks_music():
                            if not blocks_music_loaded:
                                return
                            try:
                                pygame.mixer.music.set_volume(MARIO_VOLUME * .45)
                                pygame.mixer.music.play(-1)
                            except (pygame.error, OSError) as error:
                                print(f"MarioAssistant Exploding Blocks: {error}", file=sys.stderr)

                        def stop_blocks_music(fade):
                            nonlocal blocks_music_loaded
                            if blocks_music_loaded:
                                if fade:
                                    pygame.mixer.music.fadeout(750)
                                else:
                                    pygame.mixer.music.stop()
                                blocks_music_loaded = False

                        def award_blocks_coins(amount):
                            nonlocal mariocoins, last_pet_save
                            if amount <= 0:
                                return lambda: False
                            previous = mariocoins
                            mariocoins += amount
                            live_values["mariocoins"] = mariocoins
                            write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                            last_pet_save = time.monotonic()
                            eating_channel.play(kaching_sound)
                            show_coin_change(QtCore, QtWidgets, previous, mariocoins)
                            if debug_update is not None:
                                debug_update("mariocoins")
                            return eating_channel.get_busy

                        game_window = create_exploding_blocks_window(
                            QtCore, QtWidgets, exploding_blocks_closed,
                            lambda: play_task_complete_sound(QtWidgets),
                            say_blocks_line, blocks_speech_busy,
                            start_blocks_music, stop_blocks_music,
                            prepare_blocks_game,
                            award_blocks_coins,
                            blocks_save_path,
                            blocks_high_score, save_blocks_high_score,
                        )
                        board_game_windows[EXPLODING_BLOCKS_ACTION] = game_window
                    game_window.reset_game()
                    game_window.show()
                    game_window.raise_()
                    game_window.activateWindow()
                elif selected_action in BOARD_GAMES:
                    game_window = board_game_windows.get(selected_action)
                    if game_window is None:
                        def game_closed(action=selected_action):
                            if requested_action.get("name") == action:
                                finish_requested_action()

                        title, factory = BOARD_GAMES[selected_action]
                        try:
                            game_window = factory(
                                QtCore, QtWidgets, game_closed, speak_connect4_text, game_speech_busy,
                                autosave_path=save_path.parent / {
                                    'snakes_and_ladders': 'snakes.win',
                                    'tic_tac_toe': 'tic.win',
                                    'backgammon': 'gammon.win',
                                    'checkers': 'check.win',
                                    'chess': 'chess.win',
                                    'go': 'go.win',
                                }[selected_action],
                            )
                        except ModuleNotFoundError as exc:
                            if selected_action != "chess" or exc.name != "chess":
                                raise
                            finish_requested_action()
                            QtWidgets.QMessageBox.warning(
                                im_games_window, title,
                                'Install the python-chess dependency from requirements.txt to play Chess.',
                            )
                            continue
                        board_game_windows[selected_action] = game_window
                    requested_action["waiting"] = False
                    prepare_board_game_dialogue()
                    game_window.reset_game(watch_mode=watch_board_game)
                    game_window.show()
                    game_window.raise_()
                    game_window.activateWindow()
                elif selected_action in ("connect4_start", "connect4_watch"):
                    connect4_result[0] = None
                    sentence_mix_generation[0] += 1
                    stop_wait_hmm()
                    if connect4_window is None:
                        def connect4_finished(result):
                            connect4_result[0] = result

                        def connect4_continue():
                            connect4_continue_requested[0] = True

                        def connect4_turn_requested(event):
                            connect4_turn_event[0] = event

                        def connect4_closed():
                            connect4_result[0] = None
                            connect4_continue_requested[0] = False
                            connect4_turn_event[0] = None
                            cancel_connect4_preparation()
                            if requested_action.get("name") == "connect4":
                                finish_requested_action()

                        connect4_window = create_connect_four_history_window(
                            QtCore, QtWidgets, speak_connect4_text,
                            connect4_finished, connect4_turn_requested, connect4_closed,
                            connect4_continue, save_path.parent / "connect.win",
                        )
                    connect4_window.show()
                    connect4_window.raise_()
                    connect4_window.activateWindow()
                    connect4_continue_requested[0] = False
                    connect4_turn_event[0] = None
                    watch_mode = selected_action == "connect4_watch"
                    if (preferences.get("tts_live_sentence_mixing")
                            and not connect4_lines_prepared[0]):
                        connect4_window.set_preparing()
                        connect4_previous_mixer_enabled[0] = sentence_mixer.enabled
                        sentence_mixer.set_enabled(True)
                        connect4_prep_generation[0] += 1
                        generation = connect4_prep_generation[0]
                        connect4_preparing[0] = (generation, watch_mode)
                        requested_action["waiting"] = True
                        requested_action["deadline"] = None

                        def prepare_connect4_lines():
                            prepared = {}
                            errors = []
                            for line in CONNECT_FOUR_DIALOGUE:
                                if generation != connect4_prep_generation[0]:
                                    return
                                try:
                                    media = sentence_mixer.mix(line, with_video=True)
                                    if media is not None:
                                        prepared[line] = media
                                    else:
                                        errors.append(f"{line}: no mixed media returned")
                                except Exception as exc:
                                    errors.append(
                                        f"{line}: {type(exc).__name__}: {exc}"
                                    )
                            connect4_prep_results.put(
                                (generation, watch_mode, prepared, errors)
                            )

                        threading.Thread(
                            target=prepare_connect4_lines, daemon=True
                        ).start()
                    else:
                        connect4_window.reset_game(watch=watch_mode)
                elif selected_action == "superdebug":
                    if debug_window is None:
                        def test_exception_action():
                            while True:
                                action = show_uncaught_exception(
                                    RuntimeError("This is a test exception."), write_log=False
                                )
                                if action == "Abort":
                                    os._exit(1)
                                if action != "Retry":
                                    break

                        def trigger_proactive_action():
                            proactive_debug_trigger[0] = True

                        debug_window, debug_update = create_debug_window(
                            QtCore, QtWidgets, live_values, test_exception_action,
                            trigger_proactive_action, lambda: list(automatic_action_queue),
                            debug_queue_set, debug_queue_add, debug_queue_clear,
                            debug_queue_release,
                        )
                    debug_window.show()
                    debug_update(force=True)
                    debug_window.raise_()
                    debug_window.activateWindow()

                if (song_action[0] is not None and not requested_action['active']
                        and not voice_silence_mode):
                    selected_song_row = song_action[0]
                    song_action[0] = None
                    if 0 <= selected_song_row < len(song_entries):
                        start_song_request(song_entries[selected_song_row])
                    else:
                        start_song_request(None)

                if voice_request_active and voice_request_action not in (
                    "speak", "voice_apology", "ohsorry", "move", "eye", "tunnel", "goodbye",
                    "random_song",
                ) and not str(voice_request_action or '').startswith('song_query:') \
                        and not str(voice_request_action or '').startswith('cd_track:'):
                    finish_voice_request()

                if voice_request_pending and minigame_action[0] is not None:
                    voice_request_pending = False
                    voice_request_active = True
                    voice_request_action = minigame_action[0]
                elif voice_silence_mode and minigame_action[0] is not None:
                    minigame_action[0] = None

                if requested_action["active"] and minigame_action[0] is not None:
                    minigame_action[0] = None
                    if voice_request_active:
                        finish_voice_request()

                if minigame_action[0] == "tunnel":
                    minigame_action[0] = None
                    if not requested_action["active"]:
                        begin_requested_action("tunnel")
                    if random_minigame_notification[0] is not None:
                        try:
                            random_minigame_notification_id = send_notification(
                                Gio,
                                GLib,
                                title="Hey!",
                                body=f"I'ma pick {random_minigame_notification[0]} just for you!",
                            )
                            random_minigame_notification_close_at = time.monotonic() + 10
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                        random_minigame_notification[0] = None
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
                    pet.last_tick = time.monotonic()
                    if outcome == "close":
                        goodbye_running = True
                        cancel_connect4_preparation()
                        wake_listener.stop()
                        audio.stop()
                        for need_name in tuple(pet_notifications):
                            try:
                                close_notification(
                                    Gio, GLib, pet_notifications.pop(need_name)
                                )
                            except Exception as exc:
                                print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                        pet_notification_close_at.clear()
                        for auxiliary in (
                                settings_window, minigames_window, im_games_window,
                                connect4_window, *board_game_windows.values(), debug_window, care_window):
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
                    if voice_request_active and voice_request_action == "tunnel":
                        finish_voice_request()
                    if automatic_action_current == "scheduled_minigame":
                        automatic_action_current = None

                blocked_automatic_events = (
                    requested_action["active"] or automatic_action_current is not None or booting
                    or feeding_active or idle_playing or eye_event is not None
                    or pet_video is not None or move_tween is not None
                    or bool(automatic_action_queue)
                )
                if blocked_automatic_events:
                    for need_name in ("hunger", "thirst"):
                        deadline = pet.reminders.get(need_name, math.inf)
                        if pet.needy.get(need_name, False) and deadline <= now and need_name not in queued_pet_needs:
                            queue_automatic_action(deadline, "pet", need_name)
                            queued_pet_needs.add(need_name)
                            pet.remind(need_name, now)
                due_need = queued_auto_pet_need or pet.due()
                if (not booting and not requested_action["active"]
                        and automatic_action_current in (None, "pet")
                        and (not automatic_action_queue or queued_auto_pet_need is not None)
                        and hello_clip_deadline is None and not voice_silence_mode
                        and not feeding_queue and due_need is not None and pet_video is not None
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
                    if queued_auto_pet_need == due_need:
                        queued_auto_pet_need = None
                        queued_pet_needs.discard(due_need)
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
                elif (not booting and not requested_action["active"]
                        and automatic_action_current in (None, "pet")
                        and (not automatic_action_queue or queued_auto_pet_need is not None)
                        and hello_clip_deadline is None and not voice_silence_mode
                        and not feeding_queue and due_need is not None and finished and not feeding_active and eye_event is None
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
                    if queued_auto_pet_need == due_need:
                        queued_auto_pet_need = None
                        queued_pet_needs.discard(due_need)
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

                manual_start = (
                    minigame_action[0] == "eye" and not requested_action["active"]
                )
                minigame_action[0] = None
                if manual_start and eye_event is not None:
                    try:
                        close_notification(Gio, GLib, eye_notification)
                    except Exception as exc:
                        print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                    eye_notification = None
                    eye_event = None
                    if not eye_manual:
                        next_eye_at = next_random_minigame_time()
                    eye_manual = False
                now = time.monotonic()
                if preferences['proactivity'] <= 0:
                    next_proactive_at = math.inf
                elif (not super_zen_mode and math.isfinite(next_proactive_at)
                      and next_proactive_at <= now):
                    proactive_pending_at[0] = next_proactive_at
                    queue_automatic_action(next_proactive_at, 'proactive')
                    next_proactive_at = now + proactivity_delay(preferences['proactivity'])
                if proactive_debug_trigger[0]:
                    proactive_debug_trigger[0] = False
                    if (not requested_action['active'] and automatic_action_current is None
                            and not booting and finished and not feeding_active
                            and not idle_playing and eye_event is None and pet_video is None
                            and move_tween is None and not voice_silence_mode
                            and not automatic_action_queue):
                        automatic_action_current = 'proactive'
                        begin_proactive_activity(force=True)
                    else:
                        pending_proactive_index = next((index for index, item in
                                                        enumerate(automatic_action_queue)
                                                        if item[2] == 'proactive'), None)
                        if pending_proactive_index is None:
                            proactive_pending_at[0] = now
                            queue_automatic_action(
                                now, 'proactive', choose_proactive_kind()
                            )
                        else:
                            trigger_at, sequence, kind, _ = automatic_action_queue[
                                pending_proactive_index
                            ]
                            current_detail = automatic_action_queue[pending_proactive_index][3]
                            forced_kind = (
                                current_detail.removeprefix("scheduled_")
                                if current_detail in (
                                    "scheduled_game", "scheduled_minigame",
                                    "scheduled_im_game", "scheduled_song",
                                )
                                else current_detail
                            )
                            if forced_kind not in (
                                    "game", "minigame", "im_game", "song"):
                                forced_kind = choose_proactive_kind()
                            automatic_action_queue[pending_proactive_index] = (
                                trigger_at, sequence, kind, forced_kind
                            )
                blocked_automatic_events = (
                    requested_action["active"] or automatic_action_current is not None
                    or booting or feeding_active or idle_playing or eye_event is not None
                    or pet_video is not None or move_tween is not None
                    or bool(automatic_action_queue)
                )
                if blocked_automatic_events and not voice_silence_mode:
                    if (preferences["playfulness"] > 0
                            and preferences["random_minigames"] and eye_event is None):
                        while next_eye_at <= now:
                            trigger = next_eye_at
                            selected_game = (
                                next_random_minigame_selection[0]
                                or random.choice(preferences["random_minigames"])
                            )
                            queue_automatic_action(
                                trigger,
                                "superzen_reward" if super_zen_mode else "scheduled_minigame",
                                selected_game,
                            )
                            next_eye_at = next_random_minigame_time()
                    if preferences["chattiness"] > 0 and not super_zen_mode and not idle_playing:
                        while next_idle_at <= now:
                            trigger = next_idle_at
                            idle_video = random.choice(IDLE_VIDEOS)
                            queue_automatic_action(trigger, "idle", idle_video)
                            next_idle_at += idle_delay(preferences["chattiness"])
                    if preferences["wandering"] > 0 and not super_zen_mode:
                        while next_wander_at <= now:
                            trigger = next_wander_at
                            queue_automatic_action(trigger, "wander")
                            next_wander_at += random_delay(preferences["wandering"], 60, 120)
                if (not requested_action["active"] and automatic_action_current is None
                        and automatic_action_queue and not booting and finished
                        and not feeding_active and not idle_playing and eye_event is None
                        and pet_video is None and move_tween is None
                        and minigame_action[0] is None and not voice_silence_mode):
                    _, _, kind, detail = automatic_action_queue.pop(0)
                    automatic_action_current = kind
                    if kind == "eye":
                        if preferences["playfulness"] > 0:
                            queued_auto_eye = True
                        else:
                            automatic_action_current = None
                    elif kind == "idle":
                        if preferences["chattiness"] > 0 and not super_zen_mode:
                            queued_auto_idle = detail
                        else:
                            automatic_action_current = None
                    elif kind == "wander":
                        if preferences["wandering"] > 0 and not super_zen_mode:
                            queued_auto_wander = True
                        else:
                            automatic_action_current = None
                    elif kind == "pet":
                        if pet.needy.get(detail, False):
                            queued_auto_pet_need = detail
                        else:
                            queued_pet_needs.discard(detail)
                            automatic_action_current = None
                    elif kind == "autofeed":
                        if preferences["autofeed"] and pet.levels[detail] < 50:
                            queued_autofeed_need = detail
                            pet.fill(detail)
                            feeding_queue.append(f"autofeed:{detail}")
                            write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                            last_pet_save = now
                        else:
                            queued_autofeed_needs.discard(detail)
                            queued_autofeed_need = None
                            automatic_action_current = None
                    elif kind == "superzen_reward":
                        if (preferences["playfulness"] > 0
                                and detail in preferences["random_minigames"]):
                            if super_zen_mode:
                                previous_mariocoins = mariocoins
                                mariocoins += 25
                                live_values["mariocoins"] = mariocoins
                                show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                                if debug_update is not None:
                                    debug_update("mariocoins")
                                write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                                automatic_action_current = None
                            else:
                                automatic_action_current = "scheduled_minigame"
                                if detail == "eye":
                                    queued_auto_eye = True
                                else:
                                    minigame_action[0] = "tunnel"
                        else:
                            automatic_action_current = None
                    elif kind == "scheduled_minigame":
                        if (detail not in preferences["random_minigames"]
                                or preferences["playfulness"] <= 0):
                            automatic_action_current = None
                        elif super_zen_mode:
                            previous_mariocoins = mariocoins
                            mariocoins += 25
                            live_values["mariocoins"] = mariocoins
                            show_coin_change(QtCore, QtWidgets, previous_mariocoins, mariocoins)
                            if debug_update is not None:
                                debug_update("mariocoins")
                            write_save_data(save_path, {"mariocoins": mariocoins, **pet.snapshot()})
                            automatic_action_current = None
                        elif detail == "eye":
                            queued_auto_eye = True
                        elif detail == "tunnel":
                            minigame_action[0] = "tunnel"
                    elif kind == 'proactive':
                        scheduled_detail = detail in (
                            "scheduled_game", "scheduled_minigame",
                            "scheduled_im_game", "scheduled_song",
                        )
                        forced_detail = proactive_detail_is_forced(detail)
                        if forced_detail or (preferences['proactivity'] > 0 and not super_zen_mode):
                            if detail in ("game", "minigame", "im_game", "song"):
                                preferred_kind = detail
                            elif scheduled_detail:
                                preferred_kind = detail.removeprefix("scheduled_")
                            else:
                                preferred_kind = None
                            begin_proactive_activity(
                                force=forced_detail, preferred_kind=preferred_kind
                            )
                        else:
                            proactive_pending_at[0] = None
                            automatic_action_current = None
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
                automatic_start = (
                    not voice_silence_mode
                    and not requested_action["active"]
                    and automatic_action_current in (None, "eye")
                    and not booting
                    and preferences["playfulness"] > 0
                    and bool(preferences["random_minigames"])
                    and time.monotonic() >= next_eye_at
                )
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
                    next_eye_at = next_random_minigame_time()
                    automatic_start = False
                direct_random_game = (
                    next_random_minigame_selection[0]
                    if automatic_start and preferences["random_minigames"] else None
                )
                if automatic_start and not super_zen_mode:
                    next_eye_at = next_random_minigame_time()
                if (automatic_start and not super_zen_mode
                        and direct_random_game == "tunnel"
                        and finished and not feeding_active and not idle_playing
                        and move_tween is None and not automatic_action_queue
                        and time.monotonic() >= random_events_ready_at
                        and hello_clip_deadline is None):
                    minigame_action[0] = "tunnel"
                    automatic_start = False
                if eye_event is None and (manual_start or (queued_auto_eye and not requested_action["active"]) or (
                    automatic_start and direct_random_game == "eye" and finished and not feeding_active and not idle_playing and move_tween is None
                    and time.monotonic() >= random_events_ready_at and hello_clip_deadline is None
                    and not automatic_action_queue
                )):
                    if queued_auto_eye:
                        queued_auto_eye = False
                    if random_minigame_notification[0] is not None:
                        try:
                            random_minigame_notification_id = send_notification(
                                Gio,
                                GLib,
                                title="Hey!",
                                body=f"I'ma pick {random_minigame_notification[0]} just for you!",
                            )
                            random_minigame_notification_close_at = time.monotonic() + 10
                        except Exception as exc:
                            print(f"MarioAssistant notification: {exc}", file=sys.stderr)
                        random_minigame_notification[0] = None
                    if idle_playing:
                        idle_playing = False
                        next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                    eye_manual = manual_start
                    if manual_start and not requested_action["active"]:
                        begin_requested_action("eye")
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

                idle_due = (
                    not voice_silence_mode
                    and preferences["chattiness"] > 0
                    and (queued_auto_idle is not None or time.monotonic() >= next_idle_at)
                )
                if idle_due and super_zen_mode and not idle_playing:
                    next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                    idle_due = False
                if idle_prediction_deadline != next_idle_at:
                    idle_prediction_deadline = next_idle_at
                    idle_prediction = (
                        random.choice(IDLE_VIDEOS)
                        if preferences["chattiness"] > 0 else None
                    )
                if (idle_due and (queued_auto_idle is not None or earliest_event == "idle")
                        and not requested_action["active"]
                        and automatic_action_current in (None, "idle")
                        and (not automatic_action_queue or queued_auto_idle is not None)
                        and not booting and hello_clip_deadline is None and eye_event is None
                        and not feeding_active and not idle_playing and finished and move_tween is None
                        and (queued_auto_idle is not None or time.monotonic() >= random_events_ready_at)):
                    idle_video = queued_auto_idle or idle_prediction or random.choice(IDLE_VIDEOS)
                    queued_auto_idle = None
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

                try:
                    generation, weather_report, weather_error = weather_results.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if (generation == sentence_mix_generation[0] and not goodbye_running
                            and voice_state["phase"] == "idle"):
                        if weather_error is not None:
                            weather_error, failure_message = weather_error
                            print(f"MarioAssistant weather: {type(weather_error).__name__}: {weather_error}",
                                  file=sys.stderr, flush=True)
                            try:
                                play_fatal_error_sound(QtWidgets)
                            except Exception as sound_error:
                                print(f"MarioAssistant weather: error sound failed: {sound_error}",
                                      file=sys.stderr, flush=True)
                            speak_uncached_mario_text(failure_message)
                        else:
                            try:
                                play_task_complete_sound(QtWidgets)
                            except Exception as sound_error:
                                print(f"MarioAssistant weather: task-complete sound failed: {sound_error}",
                                      file=sys.stderr, flush=True)
                            speak_uncached_mario_text(weather_report)

                try:
                    song_result = song_results.get_nowait()
                except queue.Empty:
                    pass
                else:
                    generation, song, prepared_sound, song_error = song_result
                    if (generation == song_generation[0] and song_sequence[0] is not None
                            and song_sequence[0].get('stage') == 'preparing'
                            and not goodbye_running):
                        if song_error:
                            song_sequence[0]['stage'] = 'failed'
                            speak_mario_text(
                                "Oh, sorry! I couldn't find the song you were looking for."
                            )
                        else:
                            song_sequence[0].update({
                                'stage': 'intro', 'song': song,
                                'sound': prepared_sound,
                            })
                            speak_mario_text(song['title'])

                try:
                    news_result = news_results.get_nowait()
                except queue.Empty:
                    pass
                else:
                    generation, result, news_error, requested_network, requested_mode = news_result
                    if (generation == news_generation[0] and requested_action["active"]
                            and not goodbye_running):
                        if news_error is not None:
                            print(f"MarioAssistant news: {type(news_error).__name__}: {news_error}",
                                  file=sys.stderr, flush=True)
                            play_error_sound(QtWidgets)
                            news_kind = 'broadcast' if requested_mode == 'audio' else 'article'
                            speak_uncached_mario_text(
                                f"I couldn't find the latest news {news_kind} from "
                                f"{display_network_name(requested_network)}. Try again later."
                            )
                        else:
                            news_kind = result['kind']
                            news_source = result['network']
                            news_language = result.get('language')
                            if result.get('headlines_only'):
                                introduction = f"Here are the latest headlines from {news_source}."
                            else:
                                introduction = f"Here's the latest news {news_kind} from {news_source}."
                            news_sequence[0] = {
                                'stage': 'intro', 'kind': news_kind,
                                'text': result.get('text'),
                                'audio_path': result.get('audio_path'),
                                'language': news_language,
                            }
                            if news_language == 'es':
                                introduction = f"Estas son las últimas noticias de {news_source}."
                            speak_uncached_mario_text(
                                introduction + " \\pause=500\\",
                                language=news_language,
                            )

                try:
                    prep_generation, prep_watch_mode, prepared_lines, prep_errors = (
                        connect4_prep_results.get_nowait()
                    )
                except queue.Empty:
                    pass
                else:
                    if (prep_generation == connect4_prep_generation[0]
                            and requested_action.get("name") == "connect4"
                            and connect4_preparing[0] is not None):
                        connect4_line_cache.update(prepared_lines)
                        connect4_lines_prepared[0] = True
                        connect4_preparing[0] = None
                        if prep_errors:
                            print(
                                "MarioAssistant Connect-4: some lines could not be "
                                "pre-mixed: " + " | ".join(prep_errors),
                                file=sys.stderr,
                                flush=True,
                            )
                        requested_action["waiting"] = False
                        requested_action["deadline"] = time.monotonic()
                        connect4_window.reset_game(watch=prep_watch_mode)

                try:
                    generation, mixed_media = sentence_mix_results.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if (generation != sentence_mix_generation[0] or goodbye_running
                            or voice_state["phase"] != "idle"):
                        if mixed_media is not None:
                            if mixed_media["video"].parent != sentence_mixer.cache_dir:
                                mixed_media["video"].unlink(missing_ok=True)
                    elif mixed_media is None:
                        stop_wait_hmm()
                        if wait_state['stage'] is not None:
                            wait_state['close_at'] = time.monotonic() + 0.25
                        if requested_action["active"]:
                            requested_action["waiting"] = False
                            requested_action["deadline"] = time.monotonic() + 0.5
                    else:
                        premix_count = int(mixed_media.get('premixes_used', 0))
                        wait_premix_label.setText(
                            f'{premix_count} premix{"es" if premix_count != 1 else ""} used'
                        )
                        stop_wait_hmm()
                        audio.stop()
                        mario_tts_channel.stop()
                        (
                            audio, frame_surface, last_frame_surface, process,
                            frames, stop, reader,
                        ) = start_media(
                            ffmpeg, temp_dir, video_texture, process, stop, reader,
                            mixed_media["video"], play_audio=False,
                        )
                        sound = pygame.mixer.Sound(file=io.BytesIO(mixed_media["audio"]))
                        sound.set_volume(MARIO_VOLUME)
                        sentence_mixer_sound[0] = sound
                        video = mixed_media["video"]
                        mixed_video_path = video
                        mixed_caption = (
                            create_sentence_captions(mixed_media["text"], VIDEO_SIZE)
                            if preferences.get('tts_subtitles', True) else None
                        )
                        mixed_caption_page = -1
                        started = time.monotonic()
                        mario_tts_channel.play(sound)
                        if requested_action["active"]:
                            requested_action["waiting"] = False
                            requested_action["deadline"] = started + sound.get_length()
                        consumed = 0
                        finished = False
                        idle_playing = True
                        hello_clip_deadline = None
                        mixed_caption_started[0] = started
                        mixed_caption_duration[0] = max(0.1, sound.get_length())
                        wait_state['reported'] = 1.0
                        wait_state['close_at'] = time.monotonic() + 0.35
                        update_wait_progress()
                        frame_dirty = True
                        random_events_ready_at = started + sound.get_length() + 10

                if not finished:
                    target_frames = max(1, int((time.monotonic() - started) * VIDEO_FPS) + 1)
                    while consumed < target_frames:
                        try:
                            data = frames.get_nowait()
                        except queue.Empty:
                            break
                        if data is None:
                            finished = True
                            if wait_state['hmm_active']:
                                audio.stop()
                                wait_state['hmm_active'] = False
                            random_events_ready_at = time.monotonic() + 10
                            if hello_clip_deadline is None:
                                frame_surface = last_frame_surface
                                frame_dirty = True
                            if idle_playing:
                                idle_playing = False
                                next_idle_at = time.monotonic() + idle_delay(preferences["chattiness"])
                                frame_surface = greeting_first_surface
                            if pet_video is not None:
                                completed_need = pet_alert_need
                                finish_pet_alert()
                                pet_video = None
                                frame_surface = greeting_first_surface
                                if (completed_need is not None
                                        and pet.needy.get(completed_need, False)):
                                    queue_autofeed(completed_need, time.monotonic())
                            if video == ASSETS / "ohsorry.mp4":
                                frame_surface = greeting_first_surface
                            if eye_event is not None:
                                if zen_mode and not eye_manual:
                                    collect_eye = True
                                    frame_surface = greeting_first_surface
                                    last_frame_surface = greeting_first_surface
                                    frame_dirty = True
                                    break
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
                                    next_eye_at = next_random_minigame_time()
                                eye_manual = False
                            break
                        frame_surface = pygame.image.frombuffer(data, VIDEO_SIZE, "RGB").copy()
                        consumed += 1
                        frame_dirty = True

                if (
                    voice_request_active
                    and (
                        voice_request_action in ("speak", "voice_apology", "ohsorry")
                        or voice_request_action == "move"
                        and video == ASSETS / "ohsorry.mp4"
                    )
                    and finished
                    and audio.get_num_channels() == 0
                ):
                    finish_voice_request()
                if (
                    voice_request_active
                    and voice_request_action == "eye"
                    and eye_event is None
                    and finished
                ):
                    finish_voice_request()

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
                            home_window = create_home_window(
                                QtCore, QtGui, QtWidgets, menu_action
                            )
                            home_window.show()
                            home_window.raise_()
                            home_window.activateWindow()
                            start_wake_listener()

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
                        if automatic_action_current == "wander":
                            automatic_action_current = None
                        last_window_position = tuple(window.position)
                        window_move_changed_at = None
                        random_events_ready_at = time.monotonic() + 10
                    elif progress >= 1.0:
                        move_tween = None
                        last_window_position = tuple(window.position)
                        window_move_changed_at = None
                        if preferences["oh_sorry"] and not mario_is_speaking():
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
                        elif voice_request_active and voice_request_action == "move":
                            finish_voice_request()

                if (not voice_silence_mode and not requested_action["active"]
                        and automatic_action_current in (None, "wander")
                        and preferences["wandering"] > 0 and not super_zen_mode and not booting
                        and (not automatic_action_queue or queued_auto_wander)
                        and (queued_auto_wander or (time.monotonic() >= next_wander_at
                                                     and earliest_event == "wander"))
                        and finished and not feeding_active and eye_event is None
                        and post_greeting_tween is None and move_tween is None
                        and (queued_auto_wander or time.monotonic() >= random_events_ready_at)):
                    queued_auto_wander = False
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
                if (not voice_silence_mode and not feeding_active and pizza_meal is None
                        and greeting_tween_started and post_greeting_tween is None
                        and move_tween is None):
                    if current_position != last_window_position:
                        window_move_changed_at = time.monotonic()
                    elif (window_move_changed_at is not None
                          and time.monotonic() - window_move_changed_at >= 0.3):
                        window_move_changed_at = None
                        if (preferences["oh_sorry"] and not mario_is_speaking()
                                and random.random() < 0.5):
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
                current_video_name = video.name if video is not None and not finished else "None"
                predicted_video_name = None
                if feeding_queue:
                    predicted_video_name = "goodjoke.avi"
                elif minigame_action[0] == "eye":
                    predicted_video_name = "somethinginmyeye.avi"
                elif minigame_action[0] == "tunnel":
                    predicted_video_name = "tunnelofdoom.avi"
                elif (automatic_action_queue
                        and automatic_action_queue[0][2] == "scheduled_minigame"
                        and not super_zen_mode):
                    predicted_video_name = {
                        "eye": "somethinginmyeye.avi",
                        "tunnel": "tunnelofdoom.avi",
                    }.get(automatic_action_queue[0][3])
                elif automatic_action_queue and automatic_action_queue[0][2] == "proactive":
                    predicted_video_name = "Mario talks (Proactiveness)"
                elif (not voice_silence_mode and not super_zen_mode
                      and hello_clip_deadline is None and not feeding_active):
                    upcoming = []
                    if (preferences["playfulness"] > 0 and eye_event is None
                            and next_random_minigame_selection[0] is not None):
                        upcoming.append((
                            max(next_eye_at, random_events_ready_at),
                            "somethinginmyeye.avi"
                            if next_random_minigame_selection[0] == "eye"
                            else "tunnelofdoom.avi",
                        ))
                    for need_name, need_video_name in (
                        ("hunger", "heyimhungry.avi"),
                        ("thirst", "heyimthirsty.avi"),
                    ):
                        need_deadline = pet.next_notification_at(need_name)
                        if math.isfinite(need_deadline):
                            upcoming.append((
                                max(need_deadline, random_events_ready_at),
                                need_video_name,
                            ))
                    if preferences["chattiness"] > 0 and not idle_playing and idle_prediction:
                        upcoming.append((
                            max(next_idle_at, random_events_ready_at),
                            idle_prediction.name,
                        ))
                    if preferences["proactivity"] > 0 and not super_zen_mode:
                        upcoming.append((
                            max(next_proactive_at, random_events_ready_at),
                            "Mario talks (Proactiveness)",
                        ))
                    if upcoming:
                        predicted_video_name = min(upcoming, key=lambda item: item[0])[1]
                debug_video_text = f"Video: {current_video_name}"
                if predicted_video_name is not None:
                    debug_video_text += f" (MOST LIKELY COMING UP: {predicted_video_name})"
                if live_values.get("current_video") != debug_video_text:
                    live_values["current_video"] = debug_video_text
                    if debug_update is not None:
                        debug_update("current_video")
                pet_attention = any(pet.needy.values()) or eye_event is not None
                if pet_attention != pet_attention_active:
                    pet_attention_active = pet_attention
                    window.title = "MarioAssistant (Hey!)" if pet_attention else "MarioAssistant"
                    set_sdl_window_attention(window, pet_attention)

                if (debug_window is not None and debug_window.isVisible()
                        and now - last_debug_schedule_update >= 1):
                    last_debug_schedule_update = now
                    schedules = [
                        ("eye_schedule", "Random minigame", next_eye_at, eye_event is not None, False),
                        ("idle_schedule", "Idle clip", next_idle_at, idle_playing, False),
                        ("wander_schedule", "Wandering", next_wander_at, move_tween is not None and move_tween.get("wander", False), False),
                        ("hunger_schedule", "Hunger notification", pet.next_notification_at("hunger"), pet_alert_need == "hunger", not pet.needy["hunger"]),
                        ("thirst_schedule", "Thirst notification", pet.next_notification_at("thirst"), pet_alert_need == "thirst", not pet.needy["thirst"]),
                        ("proactive_schedule", "Proactive activity", proactive_pending_at[0] if proactive_pending_at[0] is not None else next_proactive_at, proactive_sequence[0] is not None, False),
                    ]
                    for name, label, deadline, active, estimated in schedules:
                        if name == 'proactive_schedule' and preferences['proactivity'] <= 0:
                            value = f"{label}: Disabled"
                        elif not math.isfinite(deadline):
                            value = f"{label}: Disabled"
                        else:
                            scheduled_time = datetime.fromtimestamp(
                                wall_clock_offset + deadline
                            ).strftime("%Y-%m-%d %H:%M:%S")
                            remaining = max(0, math.ceil(deadline - now))
                            if name == 'proactive_schedule' and super_zen_mode:
                                status = 'Blocked by Super Zen Mode'
                            elif name == 'proactive_schedule' and active:
                                status = 'Suggestion in progress'
                            elif name == 'proactive_schedule' and proactive_pending_at[0] is not None:
                                status = 'Queued'
                            else:
                                status = "Playing" if active else f"{remaining}s remaining"
                            value = f"{label}: {scheduled_time} ({status})"
                            if estimated:
                                value += " (ESTIMATION)"
                        if live_values.get(name) != value:
                            live_values[name] = value
                            if debug_update is not None:
                                debug_update(name)

                if hello_clip_deadline is not None and now >= hello_clip_deadline:
                    audio.stop()
                    stop_decoder(process, stop, reader)
                    process = None
                    reader = None
                    finished = True
                    hello_clip_deadline = None
                    video = None
                    frame_surface = greeting_first_surface
                    last_frame_surface = greeting_first_surface
                    frame_dirty = True

                if mixed_video_path is not None and (finished or video != mixed_video_path):
                    mario_tts_channel.stop()
                    mixed_caption = None
                    mixed_caption_duration[0] = 0.0
                    try:
                        if mixed_video_path.parent != sentence_mixer.cache_dir:
                            mixed_video_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    mixed_video_path = None
                    frame_dirty = True
                advance_pizza_meal()
                if requested_action["active"] and not requested_action["waiting"]:
                    deadline = requested_action["deadline"]
                    song_is_playing = (
                        song_sequence[0] is not None
                        and song_sequence[0].get('stage') == 'playing'
                    )
                    if song_is_playing:
                        action_complete = not song_audio_is_busy()
                    elif deadline is not None:
                        action_complete = (
                            now >= deadline and not mario_tts_channel.get_busy()
                        )
                    else:
                        action_complete = (
                            finished and move_tween is None and eye_event is None
                            and not feeding_active and hello_clip_deadline is None
                            and not mario_tts_channel.get_busy()
                        )
                    if (action_complete
                            and requested_action['name'] == 'proactive_prompt'
                            and proactive_sequence[0] is not None):
                        proactive = proactive_sequence[0]
                        if proactive['stage'] == 'prompt':
                            accepted = ask_proactive_suggestion(
                                QtCore, QtWidgets, proactive['message']
                            )
                            if accepted:
                                proactive_sequence[0] = None
                                if automatic_action_current == 'proactive':
                                    automatic_action_current = None
                                finish_requested_action()
                                if proactive['kind'] == 'song':
                                    start_song_request(proactive['song'])
                                elif proactive['kind'] == 'im_game':
                                    menu_action[0] = proactive['action']
                                else:
                                    minigame_action[0] = proactive['game']
                            else:
                                proactive['stage'] = 'declined'
                                speak_mario_text("You're the boss!")
                            action_complete = False
                        elif proactive['stage'] == 'declined':
                            proactive_sequence[0] = None
                            if automatic_action_current == 'proactive':
                                automatic_action_current = None
                    if (action_complete and requested_action['name'] == 'play_song'
                            and song_sequence[0] is not None):
                        stage = song_sequence[0].get('stage')
                        if stage == 'intro':
                            song = song_sequence[0]['song']
                            try:
                                audio.stop()
                                mario_tts_channel.stop()
                                if song.get('cd_track') is not None:
                                    cd_playback_process[0] = start_cd_track_playback(
                                        song['cd_info'], ffplay,
                                        max(0, min(100, round(MARIO_VOLUME * 100))),
                                    )
                                elif song['streamable']:
                                    pygame.mixer.music.load(str(song['path']))
                                    pygame.mixer.music.set_volume(MARIO_VOLUME)
                                    pygame.mixer.music.play()
                                else:
                                    loaded_sound = song_sequence[0].get('sound')
                                    if loaded_sound is None:
                                        raise RuntimeError('The song was not loaded.')
                                    loaded_sound.set_volume(MARIO_VOLUME)
                                    song_channel.play(loaded_sound)
                                stop_decoder(process, stop, reader)
                                process = None
                                reader = None
                                finished = True
                                video = None
                                frame_surface = greeting_first_surface
                                last_frame_surface = greeting_first_surface
                                frame_dirty = True
                                song_sequence[0]['stage'] = 'playing'
                                finish_requested_action()
                                if automatic_action_current == 'proactive':
                                    automatic_action_current = None
                                if (proactive_sequence[0] is not None
                                        and proactive_sequence[0].get('kind') == 'song'):
                                    proactive_sequence[0] = None
                                if voice_request_active:
                                    finish_voice_request()
                            except Exception as exc:
                                print(f"MarioAssistant song: playback failed: {exc}",
                                      file=sys.stderr, flush=True)
                                song_sequence[0]['stage'] = 'failed'
                                speak_mario_text(
                                    "Oh, sorry! I couldn't find the song you were looking for."
                                )
                            action_complete = False
                        elif stage == 'failed':
                            song_sequence[0] = None
                    if action_complete and requested_action["name"].startswith("news_query:"):
                        sequence = news_sequence[0]
                        if sequence is not None:
                            stage = sequence['stage']
                            if stage in ('intro', 'content'):
                                news_beep_channel.play(voice_beep_sound)
                                sequence['stage'] = 'before' if stage == 'intro' else 'after'
                                requested_action['deadline'] = now + 0.25
                                action_complete = False
                            elif stage == 'before':
                                if sequence['kind'] == 'article':
                                    sequence['stage'] = 'content'
                                    speak_uncached_mario_text(
                                        sequence['text'], language=sequence['language'],
                                    )
                                else:
                                    try:
                                        news_audio_sound[0] = pygame.mixer.Sound(sequence['audio_path'])
                                        news_audio_sound[0].set_volume(MARIO_VOLUME)
                                        duration = news_audio_sound[0].get_length()
                                        mario_tts_channel.play(news_audio_sound[0])
                                        requested_action['deadline'] = now + duration
                                        sequence['stage'] = 'content'
                                        frame_surface = greeting_first_surface
                                        last_frame_surface = greeting_first_surface
                                        video = None
                                        finished = True
                                        frame_dirty = True
                                    except Exception as exc:
                                        print(f"MarioAssistant news: audio playback failed: {exc}",
                                              file=sys.stderr, flush=True)
                                        news_sequence[0] = None
                                        play_error_sound(QtWidgets)
                                        action = requested_action['name'] or "news_query:auto:my current location"
                                        _, mode, network = action.split(":", 2)
                                        news_kind = 'broadcast' if mode in ('audio', 'auto') else 'article'
                                        speak_uncached_mario_text(
                                            f"I couldn't find the latest news {news_kind} from "
                                            f"{display_network_name(network)}. Try again later."
                                        )
                                action_complete = False
                            elif stage == 'after':
                                news_sequence[0] = None
                    if (action_complete and requested_action["name"] not in ("goodbye", "connect4", *SOLO_IM_GAMES)
                            and requested_action["name"] not in BOARD_GAMES
                            and news_sequence[0] is None):
                        completed_action = requested_action['name']
                        finish_requested_action()
                        if (voice_request_active and (
                                completed_action == 'play_song'
                                or completed_action == 'random_song'
                                or completed_action.startswith('song_query:')
                                or completed_action.startswith('cd_track:')
                                or voice_request_action == 'random_song'
                                or str(voice_request_action or '').startswith('song_query:')
                                or str(voice_request_action or '').startswith('cd_track:')
                        )):
                            finish_voice_request()
                if (connect4_turn_event[0] is not None
                        and requested_action.get("name") == "connect4"
                        and not requested_action.get("waiting")
                        and requested_action.get("deadline") is not None
                        and now >= requested_action["deadline"]
                        and not mario_tts_channel.get_busy()):
                    turn_event = connect4_turn_event[0]
                    connect4_turn_event[0] = None
                    if turn_event == 'announce_player':
                        connect4_window.announce_player_turn()
                    elif turn_event == 'enable_player':
                        connect4_window.enable_player_turn()
                    elif turn_event == 'cpu':
                        connect4_window.cpu_turn()
                    elif turn_event == 'watch_mario':
                        connect4_window.announce_watch_mario_turn()
                    elif turn_event == 'cpu_mario':
                        connect4_window.cpu_turn()
                    elif turn_event == 'watch_luigi':
                        connect4_window.cpu_turn()
                if (connect4_result[0] is not None
                        and connect4_result[0] != "awaiting_continue"
                        and requested_action.get("name") == "connect4"
                        and not requested_action.get("waiting")
                        and requested_action.get("deadline") is not None
                        and now >= requested_action["deadline"]
                        and not mario_tts_channel.get_busy()):
                    connect4_window.show_continue()
                    connect4_result[0] = "awaiting_continue"
                if (connect4_continue_requested[0]
                        and connect4_result[0] == "awaiting_continue"
                        and requested_action.get("name") == "connect4"):
                    connect4_continue_requested[0] = False
                    connect4_result[0] = None
                    answer = QtWidgets.QMessageBox.question(
                        connect4_window, "Connect-4", "Want to try again?",
                        QtWidgets.QMessageBox.StandardButton.Yes
                        | QtWidgets.QMessageBox.StandardButton.No,
                        QtWidgets.QMessageBox.StandardButton.No,
                    )
                    if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                        connect4_result[0] = None
                        connect4_turn_event[0] = None
                        connect4_window.reset_game()
                    else:
                        connect4_result[0] = None
                        connect4_window.close()
                if (song_sequence[0] is not None
                        and song_sequence[0].get('stage') == 'playing'
                        and not song_audio_is_busy()):
                    song_sequence[0] = None
                if (song_sequence[0] is not None
                        and song_sequence[0].get('stage') == 'playing'):
                    if (requested_action["active"]
                            and requested_action.get("name") in (
                                "play_song", "proactive_prompt"
                            )):
                        finish_requested_action()
                    if automatic_action_current in ("proactive", "play_song"):
                        automatic_action_current = None
                    if (proactive_sequence[0] is not None
                            and proactive_sequence[0].get("kind") == "song"):
                        proactive_sequence[0] = None
                    if (voice_request_active and (
                            voice_request_action == "random_song"
                            or str(voice_request_action or "").startswith("song_query:")
                            or str(voice_request_action or "").startswith("cd_track:")
                    )):
                        finish_voice_request()
                elif (automatic_action_current == "proactive"
                        and proactive_sequence[0] is None
                        and not requested_action["active"]
                        and finished and not feeding_active and not idle_playing
                        and eye_event is None and pet_video is None and move_tween is None):
                    automatic_action_current = None
                if (automatic_action_current in ("eye", "scheduled_minigame")
                        and eye_event is None and finished
                        and not queued_auto_eye):
                    automatic_action_current = None
                elif (automatic_action_current == "idle" and finished and not idle_playing
                      and queued_auto_idle is None):
                    automatic_action_current = None
                elif (automatic_action_current == "pet" and finished and pet_video is None
                      and queued_auto_pet_need is None):
                    automatic_action_current = None
                    if pet_alert_need is not None:
                        queued_pet_needs.discard(pet_alert_need)
                elif (automatic_action_current == "wander" and move_tween is None
                      and not queued_auto_wander):
                    automatic_action_current = None
                if mixed_caption:
                    duration = max(0.001, mixed_caption_duration[0])
                    elapsed_caption = max(0.0, now - mixed_caption_started[0])
                    if elapsed_caption >= duration:
                        mixed_caption = None
                        mixed_caption_page = -1
                        frame_dirty = True
                    else:
                        page = max(0, min(len(mixed_caption) - 1, int(elapsed_caption / duration * len(mixed_caption))))
                        if page != mixed_caption_page:
                            mixed_caption_page = page
                            frame_dirty = True
                if frame_dirty:
                    display_surface = frame_surface
                    if mixed_caption:
                        display_surface = frame_surface.copy()
                        display_surface.blit(mixed_caption[mixed_caption_page], (0, 0))
                    video_texture.update(display_surface)
                    frame_dirty = False
                width, height = window.size
                renderer.draw_color = (0, 0, 0, 255)
                renderer.clear()
                video_texture.draw(dstrect=(0, 0, width, height))
                renderer.present()
            except KeyboardInterrupt:
                goodbye_running = True
                goodbye_completed = run_goodbye(
                    ffmpeg, temp_dir, window, renderer, video_texture,
                    process, stop, reader, qt_app, Gtk, Gdk, QtWidgets,
                    NOTIFICATIONS.tray,
                )
                running = False
            except Exception as exc:
                action = show_uncaught_exception(exc)
                if action == "Abort":
                    os._exit(1)
                if action == "Retry":
                    running = False
                    restart_after_exception = True
                else:
                    traceback.print_exception(type(exc), exc, exc.__traceback__)

    except KeyboardInterrupt:
        exception_texture = locals().get("video_texture", preparing_texture)
        if (exception_texture is not None and "temp_dir" in locals()
                and "QtWidgets" in locals()):
            try:
                if pygame.mixer.get_init() is None:
                    pygame.mixer.init(frequency=44_100, size=-16, channels=2, buffer=1024)
                goodbye_completed = run_goodbye(
                    ffmpeg, temp_dir, window, renderer, exception_texture,
                    process, stop, reader, qt_app, Gtk, Gdk, QtWidgets,
                    NOTIFICATIONS.tray if NOTIFICATIONS is not None else None,
                )
            except Exception as exc:
                print(f"MarioAssistant goodbye after Ctrl+C failed: {exc}",
                      file=sys.stderr, flush=True)
    except Exception as exc:
        action = show_uncaught_exception(exc)
        if action == "Abort":
            os._exit(1)
        if action == "Retry":
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            restart_after_exception = True
        else:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            restart_after_exception = True

    finally:
        if "cd_playback_process" in locals() and cd_playback_process[0] is not None:
            cd_process = cd_playback_process[0]
            cd_playback_process[0] = None
            if cd_process.poll() is None:
                cd_process.terminate()
                try:
                    cd_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    cd_process.kill()
        if "wake_listener" in locals():
            wake_listener.stop()
        if "away_listener" in locals():
            away_listener.stop()
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
    if restart_after_exception:
        main()


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
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            break
        except Exception as exc:
            action = show_uncaught_exception(exc)
            if action == "Abort":
                os._exit(1)
