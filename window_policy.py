import ctypes
import sys


_SDL = None


def keep_sdl_window_above(window):
    if not sys.platform.startswith('linux'):
        return
    global _SDL
    if _SDL is None:
        import pygame.base
        _SDL = ctypes.CDLL(pygame.base.__file__)
        _SDL.SDL_GetWindowFromID.argtypes = [ctypes.c_uint32]
        _SDL.SDL_GetWindowFromID.restype = ctypes.c_void_p
        _SDL.SDL_SetWindowAlwaysOnTop.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _SDL.SDL_SetWindowAlwaysOnTop.restype = None
    native_window = _SDL.SDL_GetWindowFromID(window.id)
    if native_window:
        _SDL.SDL_SetWindowAlwaysOnTop(native_window, 1)


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
