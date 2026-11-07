import ctypes
import ctypes.util
import sys


_SDL = None


def _get_sdl():
    global _SDL
    if _SDL is None:
        import pygame.base
        candidates = [pygame.base.__file__, ctypes.util.find_library('SDL2')]
        if sys.platform == 'win32':
            candidates.append('SDL2.dll')
        elif sys.platform == 'darwin':
            candidates.append('SDL2')
        else:
            candidates.append('libSDL2-2.0.so.0')
        for candidate in candidates:
            if not candidate:
                continue
            try:
                _SDL = ctypes.CDLL(candidate)
                _SDL.SDL_GetWindowFromID.argtypes = [ctypes.c_uint32]
                _SDL.SDL_GetWindowFromID.restype = ctypes.c_void_p
                _SDL.SDL_SetWindowAlwaysOnTop.argtypes = [ctypes.c_void_p, ctypes.c_int]
                _SDL.SDL_SetWindowAlwaysOnTop.restype = None
                break
            except (AttributeError, OSError):
                _SDL = None
        if _SDL is None:
            raise OSError('SDL2 window APIs are unavailable')
        _SDL.SDL_FlashWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _SDL.SDL_FlashWindow.restype = ctypes.c_int
    return _SDL


def keep_sdl_window_above(window):
    if not sys.platform.startswith('linux'):
        return
    sdl = _get_sdl()
    native_window = sdl.SDL_GetWindowFromID(window.id)
    if native_window:
        sdl.SDL_SetWindowAlwaysOnTop(native_window, 1)


def set_sdl_window_attention(window, enabled):
    try:
        sdl = _get_sdl()
        native_window = sdl.SDL_GetWindowFromID(window.id)
        if native_window:
            sdl.SDL_FlashWindow(native_window, 2 if enabled else 0)
    except (AttributeError, OSError):
        return False
    return bool(native_window)


def install_linux_window_policy(app):
    if not sys.platform.startswith('linux'):
        return None
    from PySide6 import QtCore, QtWidgets
    import shiboken6
    import weakref

    class KeepAbove(QtCore.QObject):
        def eventFilter(self, widget, event):
            if (isinstance(widget, QtWidgets.QWidget) and widget.isWindow()
                    and event.type() in (QtCore.QEvent.Type.Polish, QtCore.QEvent.Type.Show)):
                flag = QtCore.Qt.WindowType.WindowStaysOnTopHint
                if not widget.windowFlags() & flag:
                    showing = event.type() == QtCore.QEvent.Type.Show
                    widget.setWindowFlag(flag, True)
                    if showing:
                        reference = weakref.ref(widget)

                        def show_again():
                            window = reference()
                            if window is not None and shiboken6.isValid(window):
                                window.show()

                        QtCore.QTimer.singleShot(0, show_again)
            return False

    policy = KeepAbove(app)
    app.installEventFilter(policy)
    for widget in app.topLevelWidgets():
        visible = widget.isVisible()
        widget.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        if visible:
            widget.show()
    return policy
