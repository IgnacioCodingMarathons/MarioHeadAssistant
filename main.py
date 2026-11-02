import queue
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
VIDEO = ASSETS / "firsttimegreeting.mp4"
VIDEO_SIZE = (800, 600)
WINDOW_SIZE = (800, 600)
VIDEO_FPS = 15
FRAME_BYTES = VIDEO_SIZE[0] * VIDEO_SIZE[1] * 3
TWEEN_SECONDS = 0.75


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
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to play the video.")
    if not VIDEO.is_file():
        raise FileNotFoundError(VIDEO)

    pygame.init()
    window = Window("MarioAssistant", size=WINDOW_SIZE, resizable=True)
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
    chord_latched = False
    finished = False
    frame_surface = None
    frame_dirty = False
    try:
        # Match Mario's startup preparation screen at its normal 800x600 size.
        preparing_surface = pygame.Surface(WINDOW_SIZE)
        preparing_surface.fill((0, 0, 0))
        label = font.render("Preparing videos 1 of 1…", True, (235, 235, 235))
        preparing_surface.blit(label, label.get_rect(center=(400, 300)))
        preparing_texture.update(preparing_surface)
        renderer.clear()
        preparing_texture.draw(dstrect=(0, 0, *WINDOW_SIZE))
        renderer.present()

        pygame.mixer.init(frequency=44_100, size=-16, channels=2, buffer=1024)
        audio_path = Path(temp_dir.name) / "firsttimegreeting.wav"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(VIDEO),
             "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        audio = pygame.mixer.Sound(str(audio_path))

        # Decode and hold the first frame while the window animates open.
        first = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(VIDEO),
             "-vf", "scale=800:600:flags=fast_bilinear", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        if len(first) != FRAME_BYTES:
            raise RuntimeError("Could not decode the first video frame.")
        frame_surface = pygame.image.frombuffer(first, VIDEO_SIZE, "RGB").copy()

        # Shrink to a centered pixel, then use Mario's back-in-out easing to restore size.
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

        # Begin the video and audio together after the opening tween.
        process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(VIDEO),
             "-an", "-vf", f"scale=800:600:flags=fast_bilinear,fps={VIDEO_FPS}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        frames: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        reader = threading.Thread(target=read_frames, args=(process.stdout, frames, stop), daemon=True)
        reader.start()
        consumed = 0
        started = time.monotonic()
        audio.play()

        running = True
        while running:
            clock.tick(60)
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
                    if star_down and minus_down and not chord_latched:
                        chord_latched = True
                        finished = True
                        stop.set()
                        if process.poll() is None:
                            process.terminate()
                elif event.type == pygame.KEYUP:
                    if event.key in (pygame.K_8, pygame.K_KP_MULTIPLY):
                        star_down = False
                    if event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        minus_down = False
                    if not (star_down and minus_down):
                        chord_latched = False

            if not finished:
                target_frames = max(1, int((time.monotonic() - started) * VIDEO_FPS) + 1)
                while consumed < target_frames:
                    try:
                        data = frames.get_nowait()
                    except queue.Empty:
                        break
                    if data is None:
                        finished = True
                        break
                    frame_surface = pygame.image.frombuffer(data, VIDEO_SIZE, "RGB").copy()
                    consumed += 1
                    frame_dirty = True

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
    """Mario's back-in-out resize easing."""
    c1 = 1.70158
    c2 = c1 * 1.525
    if t < 0.5:
        return ((2 * t) ** 2 * ((c2 + 1) * 2 * t - c2)) / 2
    return (((2 * t - 2) ** 2 * ((c2 + 1) * (2 * t - 2) + c2) + 2) / 2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"MarioAssistant: {exc}", file=sys.stderr)
        raise
