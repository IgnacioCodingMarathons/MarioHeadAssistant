import math
import queue
import re
import shutil
import subprocess
import time

import pygame


def capture_brightness():
    executable = shutil.which('xrandr')
    if executable is None:
        raise RuntimeError('Tunnel of Doom requires xrandr for display brightness.')
    result = subprocess.run(
        [executable, '--verbose'], capture_output=True, text=True,
        check=True, timeout=3,
    )
    outputs = {}
    output = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace():
            match = re.match(r'(\S+) connected(?: primary)? \d+x\d+[-+]\d+[-+]\d+', line)
            output = match.group(1) if match else None
        elif output is not None:
            match = re.match(r'\s+Brightness:\s+([\d.]+)', line)
            if match:
                outputs[output] = float(match.group(1))
    if not outputs:
        raise RuntimeError('No active X11 display with brightness control was found.')
    return executable, outputs


def set_brightness(executable, values):
    command = [executable]
    for output, value in values.items():
        command.extend(['--output', output, '--brightness', f'{value:.6f}'])
    subprocess.run(command, capture_output=True, check=True, timeout=3)


def play_tunnel_of_doom(
    assets, ffmpeg, temp_dir, window, renderer, texture, qt_app, Gtk,
    start_decoder, stop_decoder, decode_first_frame, decode_last_frame,
    video_size, video_fps, on_drop=None,
):
    from desktop_support import BrightnessController
    brightness = BrightnessController(qt_app)
    original_brightness = brightness.original
    video = assets / 'tunnelofdoom.avi'
    intro_audio_path = temp_dir / 'tunnelofdoom.wav'
    reversed_path = temp_dir / 'cymbal-reversed.wav'
    for source, destination, audio_filter in (
        (video, intro_audio_path, None),
        (assets / 'cymbal.wav', reversed_path, 'areverse'),
    ):
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-vn']
        if audio_filter is not None:
            command.extend(['-af', audio_filter])
        command.extend(['-ac', '2', '-ar', '44100', str(destination)])
        subprocess.run(command, capture_output=True, check=True)
    intro_sound = pygame.mixer.Sound(str(intro_audio_path))
    reverse_sound = pygame.mixer.Sound(str(reversed_path))
    normal_sound = pygame.mixer.Sound(str(assets / 'cymbal.wav'))
    last_frame = decode_last_frame(ffmpeg, video)
    texture.update(decode_first_frame(ffmpeg, video))
    process, frames, stop, reader = start_decoder(ffmpeg, video)
    clock = pygame.time.Clock()
    spaces = []
    star = False
    minus = False
    skipped_intro = False
    channel = None

    def pump():
        nonlocal star, minus, skipped_intro
        qt_app.processEvents()
        while Gtk is not None and Gtk.events_pending():
            Gtk.main_iteration_do(False)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return 'close'
            if event.type == pygame.DROPFILE and on_drop is not None:
                on_drop(event.file, pygame.mouse.get_pos()[0])
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return 'cancel'
                if event.key == pygame.K_SPACE and not getattr(event, 'repeat', False):
                    spaces.append(time.monotonic())
                if (event.key == pygame.K_KP_MULTIPLY
                        or getattr(event, 'unicode', '') == '*'):
                    star = True
                if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    minus = True
                if star and minus:
                    skipped_intro = True
            elif event.type == pygame.KEYUP:
                if event.key in (pygame.K_8, pygame.K_KP_MULTIPLY):
                    star = False
                if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    minus = False
        renderer.clear()
        texture.draw(dstrect=(0, 0, *window.size))
        renderer.present()
        clock.tick(240)
        return None

    try:
        window.focus()
        channel = intro_sound.play()
        started = time.monotonic()
        consumed = 0
        done = False
        while not done and not skipped_intro:
            action = pump()
            if action is not None:
                return action
            target = int((time.monotonic() - started) * video_fps) + 1
            while consumed < target:
                try:
                    data = frames.get_nowait()
                except queue.Empty:
                    break
                if data is None:
                    done = True
                    break
                texture.update(pygame.image.frombuffer(data, video_size, 'RGB').copy())
                consumed += 1
        intro_sound.stop()
        stop_decoder(process, stop, reader)
        process = None
        texture.update(last_frame)
        started = time.monotonic()
        previous = dict(original_brightness)
        while True:
            action = pump()
            if action is not None:
                return action
            progress = min(1.0, (time.monotonic() - started) / 0.75)
            values = {
                output: max(0.0, math.floor(value * (1 - progress) / 0.05 + 1e-9) * 0.05)
                for output, value in original_brightness.items()
            }
            if values != previous:
                brightness.set(values)
                previous = values
            if progress >= 1:
                break
        wait_until = time.monotonic() + 1
        while time.monotonic() < wait_until:
            action = pump()
            if action is not None:
                return action
        spaces.clear()
        channel = pygame.mixer.find_channel(force=True)
        channel.play(reverse_sound)
        while channel.get_busy():
            action = pump()
            if action is not None:
                return action
        normal_started = time.monotonic()
        channel.play(normal_sound)
        while channel.get_busy() or time.monotonic() < normal_started + 0.05:
            action = pump()
            if action is not None:
                return action
        return 'win' if any(normal_started - 0.05 <= pressed <= normal_started + 0.05 for pressed in spaces) else 'lose'
    finally:
        intro_sound.stop()
        if channel is not None:
            channel.stop()
        try:
            if process is not None:
                stop_decoder(process, stop, reader)
        finally:
            brightness.restore()
