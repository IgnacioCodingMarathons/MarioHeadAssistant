import argparse
import atexit
import array
import bisect
import copy
from functools import lru_cache
import io
import json
import math
import re
import shutil
import sys
import subprocess
import tempfile
import wave
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from sentence_mixer_binary import MODEL_NAME, read_model, write_model


class PremixTaskSignals(QtCore.QObject):
    completed = QtCore.Signal(int, object, str)
    progress = QtCore.Signal(str, int)


class PremixTask(QtCore.QRunnable):
    def __init__(self, row, indexed_pronunciations, indexed_variants, sentence, phonemes):
        super().__init__()
        self.row = row
        self.indexed_pronunciations = indexed_pronunciations
        self.indexed_variants = indexed_variants
        self.sentence = sentence
        self.phonemes = phonemes
        self.signals = PremixTaskSignals()

    def run(self):
        try:
            self.signals.progress.emit('Phonemizing the pronunciation sentence…', 20)
            from sentence_mixer import phonemize_premix_sentence

            phonemes = self.phonemes or phonemize_premix_sentence(
                self.sentence, self.indexed_pronunciations, self.indexed_variants
            )
            self.signals.progress.emit('Applying phonemes to the premix…', 80)
            result = {'phonemes': phonemes}
            self.signals.completed.emit(self.row, result, '')
        except Exception as exc:
            self.signals.completed.emit(
                self.row, None, f'{type(exc).__name__}: {exc}'
            )


class PremixPreviewTask(QtCore.QRunnable):
    def __init__(self, row, assets_dir, archive_path, ffmpeg, sentence, phonemes):
        super().__init__()
        self.row = row
        self.assets_dir = assets_dir
        self.archive_path = archive_path
        self.ffmpeg = ffmpeg
        self.sentence = sentence
        self.phonemes = phonemes
        self.signals = PremixTaskSignals()

    def run(self):
        engine = None
        try:
            from sentence_mixer import LiveSentenceMixer

            self.signals.progress.emit('Loading the processed audio index…', 15)
            engine = LiveSentenceMixer(
                self.assets_dir, self.ffmpeg, archive_path=self.archive_path,
                trust_archive=True,
            )
            engine.set_enabled(True)
            if not engine.load_done.wait(3600):
                raise TimeoutError('The sentence mixer took too long to load.')
            if not engine.ready:
                raise RuntimeError('The sentence mixer could not load its audio index.')
            engine.phonemized_patterns = {}
            self.signals.progress.emit('Mixing audio and video preview…', 55)
            text = sentence_with_premix_phonemes(self.sentence, self.phonemes)
            media = engine.mix(text, with_video=True)
            if media is None:
                raise RuntimeError('Mario could not create the audio and video preview.')
            video_path = Path(media['video'])
            result = {
                'audio': bytes(media['audio']),
                'video': video_path.read_bytes(),
                'text': media.get('text', ''),
            }
            video_path.unlink(missing_ok=True)
            self.signals.completed.emit(self.row, result, '')
        except Exception as exc:
            self.signals.completed.emit(
                self.row, None, f'{type(exc).__name__}: {exc}'
            )
        finally:
            if engine is not None:
                atexit.unregister(engine._stop_worker)
                engine._stop_worker()
                engine.temporary.cleanup()


PHONEMES = set('AA AE AH AO AW AY B CH D DH EH ER EY F G HH IH IY JH K L M N NG OW OY P R S SH T TH UH UW V W Y Z ZH'.split())


def sentence_with_premix_phonemes(sentence, phoneme_text):
    tag_spans = [match.span() for match in re.finditer(r'\\[^\\]*\\', sentence)]
    words = [
        match for match in re.finditer(r"[a-z]+(?:'[a-z]+)*", sentence, re.IGNORECASE)
        if not any(start <= match.start() < end for start, end in tag_spans)
    ]
    groups = [line.strip().split() for line in phoneme_text.splitlines() if line.strip()]
    if len(words) != len(groups):
        raise ValueError('Use one phoneme line per word in the sentence.')
    parts = []
    cursor = 0
    for match, phones in zip(words, groups):
        parts.append(sentence[cursor:match.start()])
        if len(phones) == 1 and phones[0].isdigit():
            parts.append(f"\\wvariant={phones[0]}\\")
        else:
            normalized = [phone.upper() for phone in phones]
            if not normalized or any(phone not in PHONEMES for phone in normalized):
                raise ValueError('Each phoneme line must contain valid ARPAbet phonemes separated by spaces.')
            parts.append(f"\\premixpronunphoneme={','.join(normalized)}\\")
        parts.append(match.group())
        cursor = match.end()
    parts.append(sentence[cursor:])
    return ''.join(parts)


def premix_list_label(premix):
    matches = premix.get('all_matches') or []
    sentence = next((match.strip() for match in matches if match.strip()), '')
    sentence = sentence or premix.get('pronunciation_sentence', premix.get('sentence', '')).strip()
    sentence = sentence or '(New premix)'
    if premix.get('phonemes'):
        sentence += '  ✓'
    if premix.get('case_sensitive'):
        sentence += '  (case sensitive)'
    if premix.get('punctuation_sensitive'):
        sentence += '  (punctuation sensitive)'
    if premix.get('use_pronunciation_as_first_match'):
        sentence += '  (pronunciation first)'
    return sentence


def edited_word(all_matches_text, start, end, rows, duration, rate):
    matches = []
    for line in all_matches_text.splitlines():
        line = line.strip().lower().replace("'", '')
        if not line:
            continue
        match_words = re.findall(r'[a-z]+', line)
        if not match_words or re.sub(r'[a-z]+|[\s,]+', '', line):
            raise ValueError('Each All matches item must contain one or more words.')
        match = ' '.join(match_words)
        if match not in matches:
            matches.append(match)
    if not matches:
        raise ValueError('Add at least one word or phrase to All matches.')
    text, aliases = matches[0], matches[1:]
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError('Word timings must be finite numbers.')
    start, end = round(start * rate), round(end * rate)
    if not 0 <= start < end <= round(duration * rate):
        raise ValueError('The word must start before it ends, inside the source audio.')
    if end <= start:
        raise ValueError('The word must contain at least one audio sample.')
    phones = []
    previous = start
    for label, left, right in rows:
        label = label.strip().upper()
        if label not in PHONEMES:
            raise ValueError(f'Unknown phoneme {label!r}. Use ARPAbet labels without stress digits.')
        left, right = float(left), float(right)
        if not math.isfinite(left) or not math.isfinite(right):
            raise ValueError('Phoneme timings must be finite numbers.')
        left, right = round(left * rate), round(right * rate)
        if not start <= left < right <= end:
            raise ValueError(f'{label} must have a positive length inside the word range.')
        if left < previous:
            raise ValueError('Phonemes must be in order without overlapping.')
        previous = right
        phones.append((label, left / rate, right / rate))
    return {'text': text, 'aliases': aliases, 'start': start / rate,
            'end': end / rate, 'phones': phones}


def approximate_phone_timings(labels, start, end, pcm, rate):
    duration = end - start
    vowels = set('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())
    stops = set('P B T D K G'.split())
    fricatives = set('F V TH DH S Z SH ZH HH CH JH'.split())
    sonorants = set('M N NG L R W Y'.split())
    weights = [2.5 if phone in vowels else 0.72 if phone in stops else
               1.35 if phone in fricatives else 1.5 if phone in sonorants else 1.0
               for phone in labels]
    total_weight = sum(weights)
    if not labels or duration <= 0 or total_weight <= 0:
        return []
    minimum = min(0.012, duration / (len(labels) * 1.5))
    expected = [start]
    elapsed = 0.0
    for weight in weights[:-1]:
        elapsed += duration * weight / total_weight
        expected.append(start + elapsed)
    expected.append(end)
    samples = array.array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    left = max(0, round(start * rate))
    right = min(len(samples), round(end * rate))
    segment = samples[left:right]
    frame_size = max(1, round(rate * 0.005))
    levels = []
    crossings = []
    for offset in range(0, len(segment), frame_size):
        frame = segment[offset:offset + frame_size]
        if not frame:
            continue
        energy = math.sqrt(sum(sample * sample for sample in frame) / len(frame))
        crossing_count = sum((a < 0) != (b < 0) for a, b in zip(frame, frame[1:]))
        levels.append(energy)
        crossings.append(crossing_count / max(1, len(frame) - 1))
    if len(levels) < 3:
        return [(phone, expected[i], expected[i + 1]) for i, phone in enumerate(labels)]
    boundaries = [start]
    frame_time = frame_size / rate
    radius = min(0.045, max(0.018, duration * 0.12))
    for boundary_index, target in enumerate(expected[1:-1], 1):
        low = max(boundaries[-1] + minimum, target - radius)
        high = min(end - minimum * (len(labels) - boundary_index), target + radius)
        first = max(1, math.ceil((low - start) / frame_time))
        last = min(len(levels) - 1, math.floor((high - start) / frame_time))
        best_time = target
        best_score = -float('inf')
        for frame_index in range(first, last + 1):
            before = sum(levels[max(0, frame_index - 3):frame_index]) / max(1, len(levels[max(0, frame_index - 3):frame_index]))
            after = sum(levels[frame_index:min(len(levels), frame_index + 3)]) / max(1, len(levels[frame_index:min(len(levels), frame_index + 3)]))
            energy_change = min(1.5, abs(math.log((after + 24) / (before + 24)))) / 1.5
            before_cross = sum(crossings[max(0, frame_index - 3):frame_index]) / max(1, len(crossings[max(0, frame_index - 3):frame_index]))
            after_cross = sum(crossings[frame_index:min(len(crossings), frame_index + 3)]) / max(1, len(crossings[frame_index:min(len(crossings), frame_index + 3)]))
            acoustic_change = energy_change * 0.72 + min(1.0, abs(after_cross - before_cross) * 4) * 0.28
            candidate = start + frame_index * frame_time
            distance = abs(candidate - target) / max(radius, frame_time)
            score = acoustic_change - distance * 0.32
            if score > best_score:
                best_score = score
                best_time = candidate
        best_time = min(end - minimum * (len(labels) - boundary_index),
                        max(boundaries[-1] + minimum, best_time))
        boundaries.append(best_time)
    boundaries.append(end)
    return [(phone, boundaries[i], boundaries[i + 1]) for i, phone in enumerate(labels)]


class Waveform(QtWidgets.QWidget):
    rangeChanged = QtCore.Signal(float, float)
    phonemeMoved = QtCore.Signal(int, float, float, int)
    phonemeClicked = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(105)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.peaks = []
        self.duration = 0
        self.view_start = 0
        self.view_end = 0
        self.selection = (0, 0)
        self.anchor = None
        self.moved = False
        self.phonemes = []
        self.drag_phoneme = None
        self.drag_origin = 0
        self.drag_interval = None
        self.drag_edge = 0
        self.space_held = False
        self.setMouseTracking(True)

    def set_phonemes(self, phonemes):
        self.phonemes = list(phonemes)
        self.update()

    def set_audio(self, pcm, rate):
        samples = array.array('h')
        samples.frombytes(pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        self.duration = len(samples) / rate
        self.view_start = 0
        self.view_end = self.duration
        step = max(1, math.ceil(len(samples) / 600))
        self.peaks = [(min(samples[i:i + step]) / 32768, max(samples[i:i + step]) / 32768)
                      for i in range(0, len(samples), step)]
        self.selection = (0, min(self.duration, 0.5))
        self.update()

    def set_range(self, start, end):
        self.selection = (start, end)
        self.update()

    def zoom(self, factor):
        if not self.duration:
            return
        current = self.view_end - self.view_start
        minimum = min(self.duration, max(0.02, self.duration / max(1, len(self.peaks))))
        new_span = min(self.duration, max(minimum, current * factor))
        if self.selection[1] > self.selection[0]:
            center = (self.selection[0] + self.selection[1]) / 2
        else:
            center = (self.view_start + self.view_end) / 2
        left = center - new_span / 2
        right = left + new_span
        if left < 0:
            right -= left
            left = 0
        if right > self.duration:
            left -= right - self.duration
            right = self.duration
        self.view_start = max(0, left)
        self.view_end = min(self.duration, right)
        self.update()

    def reset_zoom(self):
        self.view_start = 0
        self.view_end = self.duration
        self.update()

    def time_at(self, x):
        return min(self.view_end, max(self.view_start,
                   self.view_start + x / max(1, self.width()) * (self.view_end - self.view_start)))

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if not self.duration:
            return
        width, height = self.width(), self.height()
        left, right = self.selection
        visible = self.view_end - self.view_start
        painter.save()
        painter.setClipRect(self.rect())
        selection_start = max(left, self.view_start)
        selection_end = min(right, self.view_end)
        if selection_end > selection_start:
            painter.fillRect(QtCore.QRectF((selection_start - self.view_start) / visible * width, 0,
                                          (selection_end - selection_start) / visible * width, height),
                             QtGui.QColor(60, 140, 230, 60))
        painter.setPen(self.palette().text().color())
        center = (height - 20) / 2
        for i, (low, high) in enumerate(self.peaks):
            timestamp = i / len(self.peaks) * self.duration
            if self.view_start <= timestamp <= self.view_end:
                x = round((timestamp - self.view_start) / visible * width)
            else:
                continue
            painter.drawLine(x, round(center - high * center), x, round(center - low * center))
        painter.setFont(QtGui.QFont(self.font().family(), 7))
        bar_y = height - 57
        vowels = set('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())
        voiced = set('B D DH G JH L M N NG R V W Y Z ZH'.split())
        for label, start, end in self.phonemes:
            if end < self.view_start or start > self.view_end:
                continue
            x1 = (start - self.view_start) / visible * width
            x2 = (end - self.view_start) / visible * width
            bar_width = max(5, x2 - x1)
            bar_x = (x1 + x2 - bar_width) / 2
            color = QtGui.QColor(235, 139, 36) if label in vowels else (
                QtGui.QColor(73, 134, 222) if label in voiced else QtGui.QColor(43, 165, 134)
            )
            painter.setPen(QtGui.QPen(color.darker(140), 1))
            painter.setBrush(color)
            painter.drawRoundedRect(QtCore.QRectF(bar_x, bar_y, bar_width, 27), 3, 3)
            painter.setPen(color.darker(160))
            painter.drawText(QtCore.QPointF(bar_x + bar_width + 2, bar_y + 18), label)
        painter.restore()
        painter.setPen(self.palette().text().color())
        painter.drawText(5, height - 5, f'{self.view_start:.3f}–{self.view_end:.3f} s / {self.duration:.3f} s')

    def phoneme_at(self, x, y):
        if not self.duration or abs(y - (self.height() - 43.5)) > 17:
            return None
        visible = self.view_end - self.view_start
        if visible <= 0:
            return None
        candidates = []
        for index, (_, start, end) in enumerate(self.phonemes):
            if end < self.view_start or start > self.view_end:
                continue
            x1 = (start - self.view_start) / visible * self.width()
            x2 = (end - self.view_start) / visible * self.width()
            bar_width = max(5, x2 - x1)
            bar_left = (x1 + x2 - bar_width) / 2
            bar_right = bar_left + bar_width
            if bar_left - 3 <= x <= bar_right + 3:
                distance = max(bar_left - x, 0, x - bar_right)
                candidates.append((distance, index))
        return min(candidates)[1] if candidates else None

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            self.reset_zoom()
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.duration:
            phone = self.phoneme_at(event.position().x(), event.position().y())
            modifiers = event.modifiers()
            if phone is not None:
                if modifiers & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier):
                    _, start, end = self.phonemes[phone]
                    self.drag_phoneme = phone
                    self.drag_origin = event.position().x()
                    self.drag_interval = (start, end)
                    if modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier:
                        visible = self.view_end - self.view_start
                        x1 = (start - self.view_start) / visible * self.width()
                        x2 = (end - self.view_start) / visible * self.width()
                        bar_width = max(5, x2 - x1)
                        bar_left = (x1 + x2 - bar_width) / 2
                        self.drag_edge = -1 if event.position().x() <= bar_left + bar_width / 2 else 1
                    else:
                        self.drag_edge = 0
                    self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
                else:
                    self.phonemeClicked.emit(phone)
                return
            required = QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier
            if modifiers & required == required:
                self.anchor = self.time_at(event.position().x())
                self.moved = False
            else:
                event.ignore()

    def enterEvent(self, event):
        self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        super().enterEvent(event)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Space:
            self.space_held = True
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Space:
            self.space_held = False
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self.space_held = False
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            self.zoom(0.8 if event.angleDelta().y() > 0 else 1.25)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_phoneme is not None and self.duration:
            delta = (event.position().x() - self.drag_origin) / self.width() * (self.view_end - self.view_start)
            start, end = self.drag_interval
            moved_start, moved_end = (start + delta, end) if self.drag_edge == -1 else (
                (start, end + delta) if self.drag_edge == 1 else (start + delta, end + delta)
            )
            self.phonemeMoved.emit(self.drag_phoneme, moved_start, moved_end, self.drag_edge)
            return
        if self.anchor is None:
            cursor = QtCore.Qt.CursorShape.PointingHandCursor if self.phoneme_at(
                event.position().x(), event.position().y()
            ) is not None else QtCore.Qt.CursorShape.CrossCursor
            self.setCursor(cursor)
        if self.anchor is not None:
            current = self.time_at(event.position().x())
            left, right = sorted((self.anchor, current))
            if right > left:
                self.moved = True
                self.selection = (left, right)
                self.rangeChanged.emit(left, right)
                self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_phoneme is not None:
            self.drag_phoneme = None
            self.drag_interval = None
            self.drag_edge = 0
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
            return
        if self.anchor is not None:
            if not self.moved:
                left = min(self.anchor, max(0, self.duration - 0.1))
                self.rangeChanged.emit(left, min(self.duration, left + 0.1))
            self.anchor = None


class MixerIndexEditor(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.path = None
        self.index = None
        self.audio = b''
        self.source_index = -1
        self.word_index = -1
        self.dirty = False
        self.form_dirty = False
        self.undo_stack = []
        self.redo_stack = []
        self.restoring_history = False
        self.loading = False
        self.preview_sound = None
        self.preview_channel = None
        self.pronunciations = None
        self.premix_loading = False
        self.premix_task = None
        self.premix_progress = None
        self.premix_preview_task = None
        self.premix_preview_progress = None
        self.premix_preview_temp = None
        self.setWindowTitle('Mario sentence mixer editor')
        self.resize(780, 620)
        self.create_menus()
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer_layout = QtWidgets.QVBoxLayout(central)
        self.tabs = QtWidgets.QTabWidget()
        outer_layout.addWidget(self.tabs)
        main_tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(main_tab)
        self.tabs.addTab(main_tab, 'Main Editor')
        self.sources = QtWidgets.QComboBox()
        self.sources.currentIndexChanged.connect(self.select_source)
        layout.addWidget(self.sources)
        help_label = QtWidgets.QLabel('Enter one or more words separated by spaces or commas to map this audio range to a whole phrase. Click a phoneme bar to play it. Control-drag to move it; Shift-drag to stretch or compress it. Hold Control+Shift and drag elsewhere on the waveform to change the word range.')
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.waveform = Waveform()
        self.waveform.rangeChanged.connect(self.select_range)
        self.waveform.phonemeMoved.connect(self.move_phoneme)
        self.waveform.phonemeClicked.connect(self.play_phoneme_at)
        zoom_controls = QtWidgets.QHBoxLayout()
        zoom_controls.addStretch(1)
        self.button(zoom_controls, 'Zoom in', lambda: self.waveform.zoom(0.5))
        self.button(zoom_controls, 'Zoom out', lambda: self.waveform.zoom(2))
        self.button(zoom_controls, 'Reset zoom', self.waveform.reset_zoom)
        layout.addLayout(zoom_controls)
        layout.addWidget(self.waveform)
        self.zoom_shortcuts = []
        for sequence, callback in (
            ('Ctrl++', lambda: self.waveform.zoom(0.5)),
            ('Ctrl+=', lambda: self.waveform.zoom(0.5)),
            ('Ctrl+-', lambda: self.waveform.zoom(2)),
        ):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self.zoom_shortcuts.append(shortcut)
        self.zoom_reset_shortcut = QtGui.QShortcut(QtGui.QKeySequence('Ctrl+Z, R'), self)
        self.zoom_reset_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.zoom_reset_shortcut.activated.connect(self.waveform.reset_zoom)
        previews = QtWidgets.QHBoxLayout()
        self.button(previews, 'Play source', self.play_source)
        self.button(previews, 'Play range', lambda: self.play(self.start.value(), self.end.value()))
        self.button(previews, 'Play phoneme', self.play_phone)
        self.button(previews, 'Stop audio', self.stop_audio)
        layout.addLayout(previews)
        splitter = QtWidgets.QSplitter()
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.words = QtWidgets.QTableWidget(0, 4)
        self.words.setHorizontalHeaderLabels(['All matches', 'Variant', 'Start', 'End'])
        self.words.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.words.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.words.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.words.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.words.horizontalHeader().setStretchLastSection(True)
        self.words.currentCellChanged.connect(self.select_word)
        left_layout.addWidget(self.words)
        word_buttons = QtWidgets.QHBoxLayout()
        self.button(word_buttons, 'New word', self.new_word)
        self.button(word_buttons, 'Delete word', self.delete_word)
        left_layout.addLayout(word_buttons)
        splitter.addWidget(left)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        form = QtWidgets.QFormLayout()
        self.alias_text = QtWidgets.QPlainTextEdit()
        self.alias_text.setPlaceholderText('One match per line; each match must contain one or more words')
        self.alias_text.setMaximumHeight(78)
        self.alias_text.textChanged.connect(self.form_changed)
        form.addRow('All matches', self.alias_text)
        self.start = self.time_field()
        self.end = self.time_field()
        form.addRow('Start (seconds)', self.start)
        form.addRow('End (seconds)', self.end)
        right_layout.addLayout(form)
        self.phones = QtWidgets.QTableWidget(0, 3)
        self.phones.setHorizontalHeaderLabels(['Phoneme', 'Start', 'End'])
        self.phones.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.phones.itemChanged.connect(self.form_changed)
        right_layout.addWidget(self.phones)
        self.no_phonemes = QtWidgets.QCheckBox('No phonemes for this word')
        self.no_phonemes.toggled.connect(self.set_no_phonemes)
        right_layout.addWidget(self.no_phonemes)
        phone_buttons = QtWidgets.QHBoxLayout()
        self.button(phone_buttons, '+ Phone', self.add_phone)
        self.button(phone_buttons, '− Phone', self.delete_phone)
        self.button(phone_buttons, 'Fill phonemes', self.fill_phones)
        right_layout.addLayout(phone_buttons)
        actions = QtWidgets.QHBoxLayout()
        self.button(actions, 'Fit phones to range', self.fit_phones)
        self.button(actions, 'Revert', self.revert_word)
        self.button(actions, 'Apply word', self.apply_word)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([310, 430])
        layout.addWidget(splitter, 1)
        self.status = QtWidgets.QLabel('Fill phonemes provides rough starting timings; refine them by listening.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.create_experiments_tab()
        self.create_premixed_tab()

    def create_menus(self):
        file_menu = self.menuBar().addMenu('&File')
        self.open_action = self.add_menu_action(file_menu, '&Open…', self.open_dialog, 'Ctrl+O')
        self.save_action = self.add_menu_action(file_menu, '&Save', self.save, 'Ctrl+S')
        self.save_as_action = self.add_menu_action(file_menu, 'Save &As…', self.save_as, 'Ctrl+Shift+S')
        file_menu.addSeparator()
        self.add_menu_action(file_menu, '&Close', self.close, 'Ctrl+W')
        edit_menu = self.menuBar().addMenu('&Edit')
        self.undo_action = self.add_menu_action(edit_menu, '&Undo', self.undo, 'Ctrl+Z')
        self.redo_action = self.add_menu_action(edit_menu, '&Redo', self.redo, 'Ctrl+Y')
        self.redo_action.setShortcuts([QtGui.QKeySequence('Ctrl+Y'), QtGui.QKeySequence('Ctrl+Shift+Z')])
        self.find_action = self.add_menu_action(edit_menu, '&Find word…', self.find_word, 'Ctrl+F')
        application_menu = self.menuBar().addMenu('&Application')
        self.add_menu_action(application_menu, '&About', self.about, '')
        application_menu.addSeparator()
        self.add_menu_action(application_menu, '&Quit', self.close, 'Ctrl+Q')

    def add_menu_action(self, menu, label, callback, shortcut):
        action = QtGui.QAction(label, self)
        if shortcut:
            action.setShortcut(QtGui.QKeySequence(shortcut))
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(lambda checked=False: callback())
        menu.addAction(action)
        return action

    def about(self):
        QtWidgets.QMessageBox.about(self, 'About Mario sentence mixer editor',
                                    'Edit and audition the Mario sentence-mixer index.')

    def create_experiments_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.addWidget(QtWidgets.QLabel('Audition matches and recordings across the entire mixer index.'))
        self.experiment_words = QtWidgets.QListWidget()
        self.experiment_words.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.experiment_words, 1)
        controls = QtWidgets.QHBoxLayout()
        self.play_variants_button = self.button(controls, 'Play all variants (Ctrl+Shift+P)', self.play_all_variants)
        self.play_all_words_button = self.button(controls, 'Play every word in the file (Ctrl+Alt+P)', self.play_every_word)
        self.experiment_stop_button = self.button(controls, 'Stop (Esc)', self.stop_audio)
        layout.addLayout(controls)
        export_controls = QtWidgets.QHBoxLayout()
        self.export_words_button = self.button(export_controls, 'Export word video…', self.export_every_word_video)
        layout.addLayout(export_controls)
        self.tabs.addTab(tab, 'Funny Experiments')
        self.experiment_shortcuts = []
        for sequence, callback in (
            ('Ctrl+Shift+P', self.play_all_variants),
            ('Ctrl+Alt+P', self.play_every_word),
            ('Escape', self.stop_audio),
        ):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self.experiment_shortcuts.append(shortcut)

    def create_premixed_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.addWidget(QtWidgets.QLabel(
            'Premixes store sentence matches and phonemes only. Mario generates audio/video at runtime; Preview renders a temporary video without saving media.'
        ))
        splitter = QtWidgets.QSplitter()
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.premix_list = QtWidgets.QListWidget()
        self.premix_list.currentRowChanged.connect(self.select_premix)
        left_layout.addWidget(self.premix_list, 1)
        list_buttons = QtWidgets.QHBoxLayout()
        self.premix_add_button = self.button(list_buttons, 'Add', self.add_premix)
        self.premix_remove_button = self.button(list_buttons, 'Remove', self.remove_premix)
        left_layout.addLayout(list_buttons)
        splitter.addWidget(left)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QtWidgets.QLabel('Pronunciation sentence'))
        self.premix_pronunciation_sentence = QtWidgets.QPlainTextEdit()
        self.premix_pronunciation_sentence.setPlaceholderText(
            'The sentence used to create the pronunciation. Needed only for automatic mixing.'
        )
        self.premix_pronunciation_sentence.textChanged.connect(self.premix_text_changed)
        right_layout.addWidget(self.premix_pronunciation_sentence, 1)
        right_layout.addWidget(QtWidgets.QLabel('All matches'))
        self.premix_all_matches = QtWidgets.QPlainTextEdit()
        self.premix_all_matches.setPlaceholderText(
            'Enter one or more matching phrases, one per line.'
        )
        self.premix_all_matches.textChanged.connect(self.premix_matches_changed)
        right_layout.addWidget(self.premix_all_matches, 1)
        right_layout.addWidget(QtWidgets.QLabel('Phoneme text'))
        self.premix_phonemes = QtWidgets.QPlainTextEdit()
        self.premix_phonemes.setPlaceholderText(
            'One line per word. Separate ARPAbet phonemes with spaces; leave blank to auto-select.'
        )
        self.premix_phonemes.textChanged.connect(self.premix_text_changed)
        right_layout.addWidget(self.premix_phonemes, 1)
        self.premix_case_sensitive = QtWidgets.QCheckBox('Case sensitive')
        self.premix_case_sensitive.setToolTip(
            'When enabled, Mario only reuses this premix when the sentence casing matches.'
        )
        self.premix_case_sensitive.stateChanged.connect(self.premix_case_changed)
        right_layout.addWidget(self.premix_case_sensitive)
        self.premix_punctuation_sensitive = QtWidgets.QCheckBox('Punctuation sensitive')
        self.premix_punctuation_sensitive.setToolTip(
            'When enabled, Mario only reuses this premix when punctuation matches.'
        )
        self.premix_punctuation_sensitive.stateChanged.connect(
            self.premix_punctuation_changed
        )
        right_layout.addWidget(self.premix_punctuation_sensitive)
        self.premix_use_pronunciation = QtWidgets.QCheckBox(
            'Use pronunciation text as first match'
        )
        self.premix_use_pronunciation.setToolTip(
            'Include the pronunciation sentence as the first match without storing it again in All matches.'
        )
        self.premix_use_pronunciation.stateChanged.connect(
            self.premix_use_pronunciation_changed
        )
        right_layout.addWidget(self.premix_use_pronunciation)
        action_row = QtWidgets.QHBoxLayout()
        self.premix_mix_button = self.button(
            action_row, 'Phonemize automatically', self.mix_premix
        )
        self.premix_preview_button = self.button(action_row, 'Preview', self.preview_premix)
        right_layout.addLayout(action_row)
        splitter.addWidget(right)
        splitter.setSizes([240, 540])
        layout.addWidget(splitter, 1)
        self.tabs.addTab(tab, 'Premixed')
        self.update_premix_buttons()

    def update_premix_buttons(self):
        has_index = self.index is not None
        self.premix_mix_button.setEnabled(has_index and self.premix_task is None)
        self.premix_add_button.setEnabled(has_index and self.premix_task is None)
        self.premix_remove_button.setEnabled(has_index and self.premix_task is None)
        self.premix_list.setEnabled(self.premix_task is None)
        self.premix_pronunciation_sentence.setEnabled(self.premix_task is None)
        self.premix_all_matches.setEnabled(self.premix_task is None)
        self.premix_phonemes.setEnabled(self.premix_task is None)
        self.premix_case_sensitive.setEnabled(self.premix_task is None)
        self.premix_punctuation_sensitive.setEnabled(self.premix_task is None)
        self.premix_use_pronunciation.setEnabled(self.premix_task is None)
        row = self.premix_list.currentRow()
        valid = has_index and 0 <= row < len(self.index.get('premixes', []))
        self.premix_preview_button.setEnabled(
            valid and self.premix_task is None and self.premix_preview_task is None
            and bool(self.index['premixes'][row].get('phonemes'))
        )

    def refresh_premix_list(self, selected=-1):
        self.premix_loading = True
        self.premix_list.blockSignals(True)
        self.premix_list.clear()
        premixes = self.index.get('premixes', []) if self.index is not None else []
        for premix in premixes:
            self.premix_list.addItem(premix_list_label(premix))
        if 0 <= selected < len(premixes):
            self.premix_list.setCurrentRow(selected)
        elif premixes:
            self.premix_list.setCurrentRow(0)
        else:
            self.premix_pronunciation_sentence.clear()
            self.premix_all_matches.clear()
            self.premix_phonemes.clear()
            self.premix_case_sensitive.setChecked(False)
            self.premix_punctuation_sensitive.setChecked(False)
            self.premix_use_pronunciation.setChecked(False)
        self.premix_list.blockSignals(False)
        self.premix_loading = False
        self.select_premix(self.premix_list.currentRow())

    def select_premix(self, row):
        self.premix_loading = True
        premixes = self.index.get('premixes', []) if self.index is not None else []
        premix = premixes[row] if 0 <= row < len(premixes) else None
        self.premix_pronunciation_sentence.setPlainText(
            premix.get('pronunciation_sentence', premix.get('sentence', '')) if premix else ''
        )
        matches = premix.get('all_matches') if premix else []
        if premix and matches is None:
            legacy = premix.get('sentence', '')
            matches = [legacy] if legacy else []
        self.premix_all_matches.setPlainText('\n'.join(matches or []))
        self.premix_phonemes.setPlainText(premix.get('phonemes', '') if premix else '')
        self.premix_case_sensitive.setChecked(
            bool(premix.get('case_sensitive', False)) if premix else False
        )
        self.premix_punctuation_sensitive.setChecked(
            bool(premix.get('punctuation_sensitive', False)) if premix else False
        )
        self.premix_use_pronunciation.setChecked(
            bool(premix.get('use_pronunciation_as_first_match', False))
            if premix else False
        )
        self.premix_loading = False
        self.update_premix_buttons()

    def premix_text_changed(self):
        if self.premix_loading or self.index is None or self.premix_task is not None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premix = premixes[row]
        sentence = self.premix_pronunciation_sentence.toPlainText().strip()
        phonemes = self.premix_phonemes.toPlainText().strip()
        sentence_changed = sentence != premix.get('pronunciation_sentence', premix.get('sentence', ''))
        phonemes_changed = phonemes != premix.get('phonemes', '')
        if sentence_changed and not phonemes_changed and phonemes:
            self.premix_loading = True
            self.premix_phonemes.clear()
            self.premix_loading = False
            phonemes = ''
        if sentence_changed or phonemes_changed:
            premix['pronunciation_sentence'] = sentence
            premix['phonemes'] = phonemes
            premix['audio'] = b''
            premix['video'] = b''
            self.dirty = True
            self.update_title()
            item = self.premix_list.item(row)
            if item is not None:
                item.setText(premix_list_label(premix))
            self.update_premix_buttons()

    def premix_matches_changed(self):
        if self.premix_loading or self.index is None or self.premix_task is not None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premix = premixes[row]
        matches = [line.strip() for line in self.premix_all_matches.toPlainText().splitlines()
                   if line.strip()]
        if matches == premix.get('all_matches', []):
            return
        premix['all_matches'] = matches
        self.dirty = True
        self.update_title()
        item = self.premix_list.item(row)
        if item is not None:
            item.setText(premix_list_label(premix))
        self.update_premix_buttons()

    def premix_case_changed(self, state):
        if self.premix_loading or self.index is None or self.premix_task is not None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premix = premixes[row]
        case_sensitive = bool(state)
        if case_sensitive == bool(premix.get('case_sensitive', False)):
            return
        premix['case_sensitive'] = case_sensitive
        self.dirty = True
        self.update_title()
        item = self.premix_list.item(row)
        if item is not None:
            item.setText(premix_list_label(premix))

    def premix_punctuation_changed(self, state):
        if self.premix_loading or self.index is None or self.premix_task is not None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premix = premixes[row]
        punctuation_sensitive = bool(state)
        if punctuation_sensitive == bool(premix.get('punctuation_sensitive', False)):
            return
        premix['punctuation_sensitive'] = punctuation_sensitive
        self.dirty = True
        self.update_title()
        item = self.premix_list.item(row)
        if item is not None:
            item.setText(premix_list_label(premix))

    def premix_use_pronunciation_changed(self, state):
        if self.premix_loading or self.index is None or self.premix_task is not None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premix = premixes[row]
        use_pronunciation = bool(state)
        if use_pronunciation == bool(
                premix.get('use_pronunciation_as_first_match', False)):
            return
        premix['use_pronunciation_as_first_match'] = use_pronunciation
        self.dirty = True
        self.update_title()
        item = self.premix_list.item(row)
        if item is not None:
            item.setText(premix_list_label(premix))

    def add_premix(self):
        if self.premix_task is not None:
            return
        if self.index is None:
            self.status.setText('Open a mixer index first.')
            return
        premixes = self.index.setdefault('premixes', [])
        premixes.append({'pronunciation_sentence': '', 'all_matches': [],
                         'case_sensitive': False,
                         'punctuation_sensitive': False,
                         'use_pronunciation_as_first_match': False,
                         'phonemes': '', 'audio': b'', 'video': b''})
        self.dirty = True
        self.update_title()
        self.refresh_premix_list(len(premixes) - 1)

    def remove_premix(self):
        if self.premix_task is not None:
            return
        if self.index is None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premixes.pop(row)
        self.dirty = True
        self.update_title()
        self.refresh_premix_list(min(row, len(premixes) - 1))

    def mix_premix(self):
        if self.index is None or self.path is None:
            self.error('Open and save the sentence-mixer index before creating a premix.')
            return
        row = self.premix_list.currentRow()
        premixes = self.index.setdefault('premixes', [])
        if not 0 <= row < len(premixes):
            self.error('Add or select a premix first.')
            return
        sentence = self.premix_pronunciation_sentence.toPlainText().strip()
        matches = [line.strip() for line in self.premix_all_matches.toPlainText().splitlines()
                   if line.strip()]
        phonemes = self.premix_phonemes.toPlainText().strip()
        if not sentence:
            self.error('Enter a pronunciation sentence before mixing.')
            return
        use_pronunciation = self.premix_use_pronunciation.isChecked()
        if not matches and not use_pronunciation:
            self.error('Enter at least one item in All matches.')
            return
        if not self.commit_form():
            return
        premixes[row].update({
            'pronunciation_sentence': sentence,
            'all_matches': matches,
            'use_pronunciation_as_first_match': use_pronunciation,
        })
        self.dirty = True
        if not self.save():
            self.update_premix_buttons()
            return
        self.refresh_premix_list(row)
        from sentence_mixer import indexed_pronunciations_from_index, indexed_variants_from_index
        indexed_pronunciations = indexed_pronunciations_from_index(self.index)
        indexed_variants = indexed_variants_from_index(self.index)
        task = PremixTask(row, indexed_pronunciations, indexed_variants, sentence, phonemes)
        task.signals.completed.connect(self.premix_mix_completed)
        task.signals.progress.connect(self.premix_progress_changed)
        self.premix_task = task
        self.update_premix_buttons()
        self.premix_progress = QtWidgets.QProgressDialog(
            'Starting automatic phonemization…', '', 0, 100, self
        )
        self.premix_progress.setWindowTitle('Phonemizing premix')
        self.premix_progress.setCancelButton(None)
        self.premix_progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.premix_progress.setMinimumDuration(0)
        self.premix_progress.setAutoClose(False)
        self.premix_progress.show()
        self.status.setText('Automatically phonemizing the premixed sentence…')
        QtCore.QThreadPool.globalInstance().start(task)

    def premix_progress_changed(self, label, progress):
        if self.premix_progress is not None:
            self.premix_progress.setLabelText(label)
            self.premix_progress.setValue(progress)

    def premix_mix_completed(self, row, result, error):
        self.premix_task = None
        if self.premix_progress is not None:
            self.premix_progress.setValue(100)
            self.premix_progress.close()
            self.premix_progress = None
        if error:
            self.error(f'Cannot create premix: {error}')
        elif self.index is not None and 0 <= row < len(self.index.get('premixes', [])):
            self.index['premixes'][row].update(result)
            self.dirty = True
            self.refresh_premix_list(row)
            if not self.save():
                self.status.setText('Premix created in memory; save the index to keep it.')
            else:
                self.status.setText('Premix phonemes generated and saved.')
        self.update_premix_buttons()

    def preview_premix(self):
        if self.index is None:
            return
        row = self.premix_list.currentRow()
        premixes = self.index.get('premixes', [])
        if not 0 <= row < len(premixes):
            return
        premix = premixes[row]
        if not premix.get('phonemes'):
            self.error('Generate or enter phonemes before previewing this premix.')
            return
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.error('FFmpeg is required to create a video preview.')
            return
        assets_dir = Path(__file__).resolve().parent / 'assets'
        task = PremixPreviewTask(
            row, assets_dir, self.path, ffmpeg,
            premix.get('pronunciation_sentence', ''), premix['phonemes'],
        )
        task.signals.completed.connect(self.premix_preview_completed)
        task.signals.progress.connect(self.premix_preview_progress_changed)
        self.premix_preview_task = task
        self.update_premix_buttons()
        self.premix_preview_progress = QtWidgets.QProgressDialog(
            'Preparing video preview…', '', 0, 100, self
        )
        self.premix_preview_progress.setWindowTitle('Premix Preview')
        self.premix_preview_progress.setCancelButton(None)
        self.premix_preview_progress.setWindowModality(
            QtCore.Qt.WindowModality.WindowModal
        )
        self.premix_preview_progress.setMinimumDuration(0)
        self.premix_preview_progress.setAutoClose(False)
        self.premix_preview_progress.show()
        QtCore.QThreadPool.globalInstance().start(task)

    def premix_preview_progress_changed(self, label, progress):
        if self.premix_preview_progress is not None:
            self.premix_preview_progress.setLabelText(label)
            self.premix_preview_progress.setValue(progress)

    def premix_preview_completed(self, row, media, error):
        self.premix_preview_task = None
        if self.premix_preview_progress is not None:
            self.premix_preview_progress.setValue(100)
            self.premix_preview_progress.close()
            self.premix_preview_progress = None
        self.update_premix_buttons()
        if error:
            self.error(f'Cannot preview premix: {error}')
            return
        self.show_premix_video_preview(media)

    def show_premix_video_preview(self, media):
        ffmpeg = shutil.which('ffmpeg')
        temporary = None
        try:
            if self.premix_preview_temp is not None:
                self.premix_preview_temp.cleanup()
            temporary = tempfile.TemporaryDirectory(prefix='mario-premix-preview-')
            self.premix_preview_temp = temporary
            folder = Path(temporary.name)
            video_path = folder / 'video.mp4'
            audio_path = folder / 'audio.wav'
            preview_path = folder / 'preview.mp4'
            video_path.write_bytes(media['video'])
            audio_path.write_bytes(media['audio'])
            subprocess.run(
                [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(video_path),
                 '-i', str(audio_path), '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
                 '-c:a', 'aac', '-shortest', str(preview_path)],
                check=True, capture_output=True, text=True,
            )
            try:
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
                from PySide6.QtMultimediaWidgets import QVideoWidget
            except ImportError:
                if not QtGui.QDesktopServices.openUrl(
                        QtCore.QUrl.fromLocalFile(str(preview_path))):
                    raise RuntimeError(
                        'Qt Multimedia is unavailable and no system video player opened.'
                    )
                self.status.setText('Opened the dynamically generated premix preview.')
                return

            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle('Premix Preview')
            dialog.resize(640, 520)
            layout = QtWidgets.QVBoxLayout(dialog)
            video_widget = QVideoWidget(dialog)
            layout.addWidget(video_widget, 1)
            controls = QtWidgets.QHBoxLayout()
            play_button = QtWidgets.QPushButton('Play again')
            close_button = QtWidgets.QPushButton('Close')
            controls.addWidget(play_button)
            controls.addStretch(1)
            controls.addWidget(close_button)
            layout.addLayout(controls)
            player = QMediaPlayer(dialog)
            audio_output = QAudioOutput(dialog)
            player.setAudioOutput(audio_output)
            player.setVideoOutput(video_widget)
            player.setSource(QtCore.QUrl.fromLocalFile(str(preview_path)))
            play_button.clicked.connect(player.play)
            close_button.clicked.connect(dialog.accept)
            dialog.finished.connect(player.stop)
            player.play()
            dialog.exec()
            player.stop()
            temporary.cleanup()
            self.premix_preview_temp = None
        except Exception as exc:
            if temporary is not None:
                temporary.cleanup()
            self.premix_preview_temp = None
            self.error(f'Cannot preview premix: {exc}')

    def button(self, layout, label, callback):
        button = QtWidgets.QPushButton(label)
        button.clicked.connect(lambda checked=False: callback())
        layout.addWidget(button)
        return button

    def time_field(self):
        field = QtWidgets.QDoubleSpinBox()
        field.setDecimals(6)
        field.setRange(0, 1000000)
        field.setSingleStep(0.01)
        field.valueChanged.connect(self.form_changed)
        return field

    def form_changed(self, *args):
        if not self.loading and self.index is not None:
            self.form_dirty = True
            self.update_waveform_phonemes()
            self.update_title()
            self.waveform.set_range(self.start.value(), self.end.value())

    def set_no_phonemes(self, enabled):
        self.phones.setEnabled(not enabled)
        if not self.loading:
            self.form_changed()

    def update_title(self):
        name = self.path.name if self.path else 'No file'
        self.setWindowTitle(f'Mixer index editor — {name}' + (' *' if self.dirty or self.form_dirty else ''))
        self.update_history_buttons()
        if hasattr(self, 'save_action'):
            self.save_action.setEnabled(self.index is not None)
            self.save_as_action.setEnabled(self.index is not None)

    def update_history_buttons(self):
        if hasattr(self, 'undo_action'):
            self.undo_action.setEnabled(bool(self.undo_stack))
            self.redo_action.setEnabled(bool(self.redo_stack))

    def remember_edit(self):
        if self.restoring_history or self.index is None:
            return
        self.undo_stack.append((copy.deepcopy(self.index), self.source_index, self.word_index))
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.update_history_buttons()

    def restore_history_state(self, state):
        self.index, self.source_index, self.word_index = copy.deepcopy(state[0]), state[1], state[2]
        self.restoring_history = True
        self.loading = True
        self.sources.blockSignals(True)
        self.sources.setCurrentIndex(self.source_index)
        self.sources.blockSignals(False)
        source = self.source()
        if source is not None:
            pcm = self.audio[source['offset']:source['offset'] + source['length']]
            self.waveform.set_audio(pcm, self.index['sample_rate'])
            self.start.setMaximum(self.waveform.duration)
            self.end.setMaximum(self.waveform.duration)
        self.loading = False
        self.dirty = True
        self.form_dirty = False
        self.refresh_words(self.word_index)
        self.restoring_history = False
        self.update_title()

    def undo(self):
        if not self.undo_stack:
            if self.form_dirty:
                self.revert_word()
            return
        self.redo_stack.append((copy.deepcopy(self.index), self.source_index, self.word_index))
        self.restore_history_state(self.undo_stack.pop())
        self.status.setText('Undid the last edit. Save to write the index.')

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append((copy.deepcopy(self.index), self.source_index, self.word_index))
        self.restore_history_state(self.redo_stack.pop())
        self.status.setText('Restored the edit. Save to write the index.')

    def source(self):
        if self.index is not None and self.source_index >= 0:
            return self.index['sources'][self.source_index]
        return None

    def commit_form(self):
        return not self.form_dirty or self.apply_word()

    def open_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open mixer index', str(self.path.parent if self.path else Path(__file__).parent / 'assets'), 'Mixer index (*.bin)')
        if path:
            self.open_file(Path(path))

    def find_word(self):
        if self.index is None or not self.commit_form():
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Find word in mixer index')
        dialog.resize(760, 460)
        layout = QtWidgets.QVBoxLayout(dialog)
        search = QtWidgets.QLineEdit()
        search.setPlaceholderText('Search words and All matches across every clip')
        layout.addWidget(search)
        results = QtWidgets.QTableWidget(0, 4)
        results.setHorizontalHeaderLabels(['Match', 'Clip', 'Start (s)', 'End (s)'])
        results.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        results.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        results.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        results.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        results.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        results.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        results.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(results, 1)
        hint = QtWidgets.QLabel('Double-click a result to open that clip and select the recorded instance.')
        layout.addWidget(hint)

        def refresh_results(query):
            results.setRowCount(0)
            query = query.strip().casefold()
            if not query:
                hint.setText('Type a word or phrase to search all clips and matches.')
                return
            found = []
            for source_index, source in enumerate(self.index['sources']):
                for word_index, record in enumerate(source['words']):
                    candidates = [record['text'], *record.get('aliases', [])]
                    matched = next((value for value in candidates if query in value.casefold()), None)
                    if matched is not None:
                        found.append((source_index, word_index, source, record, matched))
            found.sort(key=lambda item: (item[2]['name'].casefold(), item[3]['start'], item[3]['end']))
            for source_index, word_index, source, record, matched in found:
                row = results.rowCount()
                results.insertRow(row)
                match_item = QtWidgets.QTableWidgetItem(matched)
                match_item.setData(QtCore.Qt.ItemDataRole.UserRole, source_index)
                match_item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, word_index)
                values = (matched, source['name'], f'{record["start"]:.6f}', f'{record["end"]:.6f}')
                for column, value in enumerate(values):
                    results.setItem(row, column, match_item if column == 0 else QtWidgets.QTableWidgetItem(value))
            hint.setText(f'{len(found)} matching recording(s). Double-click a result to jump to it.')

        def jump_to_result(item):
            row = item.row()
            match_item = results.item(row, 0)
            source_index = match_item.data(QtCore.Qt.ItemDataRole.UserRole)
            word_index = match_item.data(QtCore.Qt.ItemDataRole.UserRole + 1)
            if source_index != self.source_index:
                self.sources.setCurrentIndex(source_index)
            self.tabs.setCurrentIndex(0)
            self.refresh_words(word_index)
            if word_index >= 0:
                target = self.words.item(word_index, 0)
                if target is not None:
                    self.words.scrollToItem(target)
                    self.words.selectRow(word_index)
            dialog.accept()

        search.textChanged.connect(refresh_results)
        results.itemDoubleClicked.connect(jump_to_result)
        dialog.show()
        search.setFocus()
        dialog.exec()

    def confirm_discard(self):
        if not self.dirty and not self.form_dirty:
            return True
        response = QtWidgets.QMessageBox.question(self, 'Unsaved changes', 'Save changes to this index?',
            QtWidgets.QMessageBox.StandardButton.Save | QtWidgets.QMessageBox.StandardButton.Discard | QtWidgets.QMessageBox.StandardButton.Cancel)
        if response == QtWidgets.QMessageBox.StandardButton.Save:
            return self.save()
        return response == QtWidgets.QMessageBox.StandardButton.Discard

    def open_file(self, path):
        if self.premix_task is not None:
            self.error('Wait for the current premix to finish before opening another index.')
            return False
        if not self.confirm_discard():
            return False
        try:
            index, audio = read_model(path)
        except Exception as exc:
            self.error(f'Cannot open index: {exc}')
            return False
        self.stop_audio()
        self.loading = True
        self.path, self.index, self.audio = Path(path), index, audio
        self.index.setdefault('premixes', [])
        self.source_index = -1
        self.word_index = -1
        self.dirty = self.form_dirty = False
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.update_history_buttons()
        self.waveform.set_audio(b'', index['sample_rate'])
        self.sources.clear()
        for source in index['sources']:
            self.sources.addItem(source['name'])
        self.loading = False
        self.statusBar().showMessage(str(self.path))
        self.refresh_experiment_words()
        self.refresh_premix_list(0 if self.index['premixes'] else -1)
        self.update_premix_buttons()
        self.update_title()
        if index['sources']:
            self.select_source(0)
        else:
            self.refresh_words()
        self.status.setText('Loaded. Original audio and video timing maps are preserved when saving.')
        return True

    def select_source(self, index):
        if self.loading or self.index is None or index < 0:
            return
        if not self.commit_form():
            self.sources.blockSignals(True)
            self.sources.setCurrentIndex(self.source_index)
            self.sources.blockSignals(False)
            return
        self.stop_audio()
        self.source_index = index
        self.word_index = -1
        source = self.source()
        pcm = self.audio[source['offset']:source['offset'] + source['length']]
        self.waveform.set_audio(pcm, self.index['sample_rate'])
        self.start.setMaximum(self.waveform.duration)
        self.end.setMaximum(self.waveform.duration)
        self.refresh_words(0)

    def refresh_words(self, row=-1):
        self.loading = True
        self.words.setRowCount(0)
        counts = {}
        if self.index is not None:
            for source_index, source in enumerate(self.index['sources']):
                for word in source['words']:
                    variant = counts.get(word['text'], 0)
                    counts[word['text']] = variant + 1
                    if source_index == self.source_index:
                        current = self.words.rowCount()
                        self.words.insertRow(current)
                        all_matches = ' | '.join([word['text'], *word.get('aliases', [])])
                        for column, value in enumerate((all_matches, str(variant), f'{word["start"]:.6f}', f'{word["end"]:.6f}')):
                            self.words.setItem(current, column, QtWidgets.QTableWidgetItem(value))
        self.words.setCurrentCell(row, 0)
        self.loading = False
        self.load_word(row)

    def select_word(self, row, column, previous_row, previous_column):
        if self.loading:
            return
        source = self.source()
        selected = source['words'][row] if source and 0 <= row < len(source['words']) else None
        if not self.commit_form():
            self.words.blockSignals(True)
            self.words.setCurrentCell(self.word_index, 0)
            self.words.blockSignals(False)
            return
        if selected is not None:
            row = next((index for index, word in enumerate(source['words']) if word is selected), -1)
        self.words.blockSignals(True)
        self.words.setCurrentCell(row, 0)
        self.words.blockSignals(False)
        self.load_word(row)

    def load_word(self, row):
        self.loading = True
        self.word_index = row
        source = self.source()
        word = source['words'][row] if source and 0 <= row < len(source['words']) else None
        self.alias_text.setPlainText('\n'.join([word['text'], *word.get('aliases', [])]) if word else '')
        left, right = (word['start'], word['end']) if word else self.waveform.selection
        self.start.setValue(left)
        self.end.setValue(right)
        self.set_phone_rows(word['phones'] if word else [])
        self.no_phonemes.blockSignals(True)
        self.no_phonemes.setChecked(bool(word is not None and not word['phones']))
        self.no_phonemes.blockSignals(False)
        self.phones.setEnabled(not self.no_phonemes.isChecked())
        self.form_dirty = False
        self.loading = False
        self.waveform.set_range(left, right)
        self.update_waveform_phonemes()
        self.update_title()

    def update_waveform_phonemes(self):
        markers = []
        for label, start, end in self.phone_rows():
            try:
                markers.append((label, float(start), float(end)))
            except (TypeError, ValueError):
                continue
        self.waveform.set_phonemes(markers)

    def move_phoneme(self, index, start, end, edge=0):
        if not 0 <= index < self.phones.rowCount():
            return
        try:
            word_start = self.start.value()
            word_end = self.end.value()
            left = float(self.phones.item(index, 1).text())
            right = float(self.phones.item(index, 2).text())
            duration = right - left
            if duration <= 0:
                return
            lower = word_start
            upper = word_end - duration
            if index:
                lower = max(lower, float(self.phones.item(index - 1, 2).text()))
            if index + 1 < self.phones.rowCount():
                upper = min(upper, float(self.phones.item(index + 1, 1).text()) - duration)
            if upper < lower:
                return
            if edge == -1:
                start = max(word_start, start)
                if index:
                    start = max(start, float(self.phones.item(index - 1, 2).text()))
                start = min(start, end - 1 / self.index['sample_rate'])
            elif edge == 1:
                end = min(word_end, end)
                if index + 1 < self.phones.rowCount():
                    end = min(end, float(self.phones.item(index + 1, 1).text()))
                end = max(end, start + 1 / self.index['sample_rate'])
            else:
                start = min(upper, max(lower, start))
                end = start + duration
            self.loading = True
            self.phones.item(index, 1).setText(f'{start:.6f}')
            self.phones.item(index, 2).setText(f'{end:.6f}')
            self.loading = False
            self.form_dirty = True
            self.dirty = True
            self.update_waveform_phonemes()
            self.update_title()
            self.status.setText('Phoneme moved. Save to write the index.')
        except (AttributeError, TypeError, ValueError):
            self.loading = False

    def set_phone_rows(self, rows):
        self.phones.blockSignals(True)
        self.phones.setRowCount(0)
        for phone, start, end in rows:
            row = self.phones.rowCount()
            self.phones.insertRow(row)
            for column, value in enumerate((phone, f'{start:.6f}', f'{end:.6f}')):
                self.phones.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.phones.blockSignals(False)

    def phone_rows(self):
        return [[self.phones.item(row, column).text() if self.phones.item(row, column) else '' for column in range(3)]
                for row in range(self.phones.rowCount())]

    def select_range(self, start, end):
        self.start.setValue(start)
        self.end.setValue(end)
        self.waveform.set_range(start, end)

    def apply_word(self):
        source = self.source()
        if source is None:
            return False
        try:
            rows = [] if self.no_phonemes.isChecked() else self.phone_rows()
            word = edited_word(self.alias_text.toPlainText(), self.start.value(), self.end.value(), rows,
                               source['length'] / 32000, self.index['sample_rate'])
        except (ValueError, OverflowError) as exc:
            self.error(str(exc))
            return False
        self.remember_edit()
        if 0 <= self.word_index < len(source['words']):
            source['words'][self.word_index] = word
        else:
            source['words'].append(word)
        source['words'].sort(key=lambda entry: entry['start'])
        row = next(index for index, entry in enumerate(source['words']) if entry is word)
        self.form_dirty = False
        self.dirty = True
        self.refresh_words(row)
        self.status.setText('Word applied. Save to write the index.')
        return True

    def new_word(self):
        if self.source() is None or not self.commit_form():
            return
        self.words.blockSignals(True)
        self.words.clearSelection()
        self.words.blockSignals(False)
        self.load_word(-1)
        self.alias_text.setFocus()

    def delete_word(self):
        source = self.source()
        if source is None or not 0 <= self.word_index < len(source['words']):
            return
        row = self.word_index
        self.remember_edit()
        source['words'].pop(row)
        self.form_dirty = False
        self.dirty = True
        self.refresh_words(min(row, len(source['words']) - 1))

    def revert_word(self):
        source = self.source()
        if source is not None:
            row = self.word_index if self.word_index >= 0 else self.words.currentRow()
            self.refresh_words(min(max(0, row), len(source['words']) - 1))

    def add_phone(self):
        if self.source() is None:
            return
        row = self.phones.rowCount()
        self.phones.insertRow(row)
        for column, value in enumerate(('AH', f'{self.start.value():.6f}', f'{self.end.value():.6f}')):
            self.phones.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.form_changed()

    def delete_phone(self):
        row = self.phones.currentRow()
        if row >= 0:
            self.phones.removeRow(row)
            self.form_changed()

    def fill_phones(self):
        if self.source() is None:
            return
        try:
            from phoneme_dict import load_pronunciations, guess_pronunciation

            if self.pronunciations is None:
                self.pronunciations = load_pronunciations()
            first_match = next((line.strip().lower() for line in self.alias_text.toPlainText().splitlines()
                                if line.strip()), '')
            labels = []
            for label in re.findall(r'[a-z]+', first_match):
                entries = self.pronunciations.get(label)
                labels.extend(entries[0] if entries else guess_pronunciation(label))
            if not labels or self.end.value() <= self.start.value():
                raise ValueError('Enter at least one All matches item and select a positive time range first.')
            source = self.source()
            pcm = self.audio[source['offset']:source['offset'] + source['length']]
            rows = approximate_phone_timings(labels, self.start.value(), self.end.value(), pcm,
                                             self.index['sample_rate'])
            self.set_phone_rows(rows)
            self.form_changed()
            self.status.setText('Phonemes estimated from pronunciation, phoneme duration, and nearby audio changes. Listen and refine before saving.')
        except Exception as exc:
            self.error(str(exc))

    def fit_phones(self):
        try:
            rows = self.phone_rows()
            if not rows:
                raise ValueError('Add phonemes first.')
            first, last = float(rows[0][1]), float(rows[-1][2])
            if not math.isfinite(first) or not math.isfinite(last) or last <= first:
                raise ValueError('The current phoneme range must have positive length.')
            if self.end.value() <= self.start.value():
                raise ValueError('Select a positive word range first.')
            scale = (self.end.value() - self.start.value()) / (last - first)
            self.set_phone_rows([(label, self.start.value() + (float(left) - first) * scale,
                                  self.start.value() + (float(right) - first) * scale)
                                 for label, left, right in rows])
            self.form_changed()
        except ValueError as exc:
            self.error(str(exc))

    def error(self, message):
        self.status.setText(message)
        QtWidgets.QMessageBox.warning(self, 'Mixer index editor', message)

    def stop_audio(self):
        if self.preview_channel is not None:
            self.preview_channel.stop()
        self.preview_channel = self.preview_sound = None

    def refresh_experiment_words(self):
        self.experiment_words.clear()
        if self.index is None:
            return
        matches = set()
        for source in sorted(self.index['sources'], key=lambda item: item['name'].casefold()):
            for record in source['words']:
                matches.add(record['text'])
                matches.update(record.get('aliases', []))
        for match in sorted(matches, key=str.casefold):
            self.experiment_words.addItem(match)

    def play_all_variants(self):
        selected = self.experiment_words.currentItem()
        if self.index is None or selected is None:
            self.status.setText('Select a match in Funny Experiments first.')
            return
        wanted = ' '.join(re.findall(r'[a-z]+', selected.text().lower()))
        segments = []
        seen = set()
        for source in sorted(self.index['sources'], key=lambda item: item['name'].casefold()):
            for record in source['words']:
                candidates = [record['text'], *record.get('aliases', [])]
                if not any(' '.join(re.findall(r'[a-z]+', value.lower())) == wanted
                           for value in candidates):
                    continue
                key = (source['name'], record['start'], record['end'])
                if key not in seen:
                    seen.add(key)
                    segments.append((source, record['start'], record['end']))
        if not segments:
            self.status.setText('No recorded variants found for this match.')
            return
        self.play_segments(segments)

    def play_every_word(self):
        if self.index is None:
            self.status.setText('Open a mixer index first.')
            return
        records = self.every_word_records()
        segments = [(source, record['start'], record['end']) for source, record in records]
        if not segments:
            self.status.setText('This index has no word recordings.')
            return
        self.play_segments(segments)

    def every_word_records(self):
        records = [
            (source, record)
            for source in self.index['sources']
            for record in source['words']
        ]
        records.sort(key=lambda pair: (
            min([pair[1]['text'], *pair[1].get('aliases', [])], key=str.casefold).casefold(),
            pair[0]['name'].casefold(),
            pair[1]['start'],
        ))
        return records

    def export_every_word_video(self):
        if self.index is None:
            self.status.setText('Open a mixer index first.')
            return
        records = self.every_word_records()
        if not records:
            self.status.setText('This index has no word recordings.')
            return
        path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Export all words as video', 'mario_words.mp4',
            'H.264 + Opus video (*.mp4);;WebM video (*.webm)'
        )
        if not path:
            return
        destination = Path(path)
        if selected_filter.startswith('H.264'):
            if destination.suffix.lower() != '.mp4':
                destination = destination.with_suffix('.mp4')
            container = 'mp4'
        else:
            if destination.suffix.lower() != '.webm':
                destination = destination.with_suffix('.webm')
            container = 'webm'
        progress = None
        try:
            from PIL import Image, ImageDraw, ImageFont

            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                raise RuntimeError('FFmpeg is required to export H.264 + Opus MP4 or WebM video.')
            ffprobe = shutil.which('ffprobe')
            rate = self.index['sample_rate']
            items = []
            variant_counts = {}
            for source, record in records:
                left = source['offset'] + round(record['start'] * rate) * 2
                right = source['offset'] + round(record['end'] * rate) * 2
                if not 0 <= left < right <= source['offset'] + source['length']:
                    continue
                chunk = self.audio[left:right]
                word = record['text']
                variant_counts[word] = variant_counts.get(word, -1) + 1
                items.append((source, record, chunk, word, variant_counts[word]))
            if not items:
                raise ValueError('No playable audio was found for those entries.')
            if not ffprobe and any(not source.get('audio_only') for source, _, _, _, _ in items):
                raise RuntimeError('FFprobe is required to export video backgrounds; install it with FFmpeg.')
            progress = QtWidgets.QProgressDialog('Preparing source video frames…', 'Cancel', 0, len(items), self)
            progress.setWindowTitle('Export word video')
            progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            with tempfile.TemporaryDirectory(prefix='mario-word-video-') as temporary:
                temp = Path(temporary)
                font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
                if not Path(font_path).is_file():
                    font_path = None
                concat_lines = []
                video_sources = {}
                total_video_duration = 5.0
                if ffprobe:
                    for source, _, _, _, _ in items:
                        if source.get('audio_only') or source['name'] in video_sources:
                            continue
                        clip_path = self.source_clip_path(source)
                        if clip_path is not None:
                            video_sources[source['name']] = self.extract_video_frames(
                                ffmpeg, ffprobe, clip_path, temp / f'video-{len(video_sources):05d}'
                            )
                rendered_samples = []
                export_color_cache = {}

                @lru_cache(maxsize=48)
                def load_background(path):
                    with Image.open(path) as video_frame:
                        return video_frame.convert('RGB')

                for index, (source, record, chunk, word, variant) in enumerate(items):
                    if progress.wasCanceled():
                        progress.close()
                        return
                    rendered_samples.append(chunk)
                    video_frames = video_sources.get(source['name'], [])
                    schedule = self.word_video_schedule(source, record, video_frames, rate)
                    segment_samples = len(chunk) // 2
                    if not schedule:
                        schedule = [(0, None)]
                    for frame_index, (sample_at, background) in enumerate(schedule):
                        next_at = schedule[frame_index + 1][0] if frame_index + 1 < len(schedule) else segment_samples
                        duration = max(1, next_at - sample_at) / rate
                        image = Image.new('RGB', (480, 480), 'black')
                        if background is not None:
                            image.paste(load_background(str(background)))
                        draw = ImageDraw.Draw(image)
                        word_bounds = (0, 0, 480, 240)
                        variant_bounds = (0, 240, 480, 480)
                        if background is None:
                            word_colors = variant_colors = ('white', 'black')
                        else:
                            color_key = str(background)
                            if (color_key, word_bounds) not in export_color_cache:
                                export_color_cache[(color_key, word_bounds)] = self.export_text_colors(
                                    image, word_bounds
                                )
                            if (color_key, variant_bounds) not in export_color_cache:
                                export_color_cache[(color_key, variant_bounds)] = self.export_text_colors(
                                    image, variant_bounds
                                )
                            word_colors = export_color_cache[(color_key, word_bounds)]
                            variant_colors = export_color_cache[(color_key, variant_bounds)]
                        self.draw_export_text(draw, word, word_bounds, font_path, *word_colors)
                        self.draw_export_text(draw, str(variant), variant_bounds, font_path, *variant_colors)
                        frame = temp / f'frame-{index:08d}-{frame_index:06d}.png'
                        image.save(frame, compress_level=1)
                        concat_lines.extend((f"file '{frame.name}'", f'duration {duration:.9f}'))
                        total_video_duration += duration
                    progress.setValue(index + 1)
                    QtWidgets.QApplication.processEvents()
                if progress.wasCanceled():
                    progress.close()
                    return
                labels = [(word, variant) for _, _, _, word, variant in items]
                summary = temp / 'summary.png'
                self.draw_export_summary(labels, summary, font_path)
                concat_lines.extend((f"file '{summary.name}'", 'duration 5.000000000'))
                concat_lines.append(f"file '{summary.name}'")
                concat_path = temp / 'frames.txt'
                concat_path.write_text('\n'.join(concat_lines), encoding='utf-8')
                audio_path = temp / 'audio.wav'
                with wave.open(str(audio_path), 'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(rate)
                    output.writeframes(b''.join(rendered_samples))
                progress.setLabelText('Encoding variable-frame-rate video…')
                progress.setRange(0, 100)
                progress.setValue(0)
                QtWidgets.QApplication.processEvents()
                encoder_probe = subprocess.run(
                    [ffmpeg, '-hide_banner', '-encoders'],
                    capture_output=True, text=True,
                )
                available_encoders = set()
                for encoder_line in encoder_probe.stdout.splitlines():
                    fields = encoder_line.split()
                    if len(fields) >= 2 and len(fields[0]) == 6:
                        available_encoders.add(fields[1])
                if container == 'mp4':
                    gpu_options = [
                        ('NVIDIA NVENC', 'h264_nvenc',
                         ['-c:v', 'h264_nvenc', '-preset', 'p1', '-rc', 'constqp',
                          '-qp', '16', '-pix_fmt', 'yuv444p']),
                        ('Intel Quick Sync', 'h264_qsv',
                         ['-c:v', 'h264_qsv', '-preset', 'veryfast',
                          '-global_quality', '16', '-pix_fmt', 'nv12']),
                        ('AMD AMF', 'h264_amf',
                         ['-c:v', 'h264_amf', '-quality', 'speed', '-rc', 'cqp',
                          '-qp_i', '16', '-qp_p', '16', '-pix_fmt', 'nv12']),
                        ('Apple VideoToolbox', 'h264_videotoolbox',
                         ['-c:v', 'h264_videotoolbox', '-realtime', '1',
                          '-q:v', '65', '-pix_fmt', 'yuv420p']),
                    ]
                    vaapi_devices = sorted(Path('/dev/dri').glob('renderD*'))
                    if vaapi_devices:
                        gpu_options.append((
                            'VA-API', 'h264_vaapi',
                            ['-init_hw_device', f'vaapi=va:{vaapi_devices[0]}',
                             '-filter_hw_device', 'va', '-vf', 'format=nv12,hwupload',
                             '-c:v', 'h264_vaapi', '-qp', '16', '-pix_fmt', 'nv12'],
                        ))
                    software_args = [
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '16',
                        '-pix_fmt', 'yuv444p', '-color_range', 'pc',
                    ]
                    muxer_args = ['-c:a', 'libopus', '-b:a', '96k', '-f', 'mp4']
                    extension = 'mp4'
                else:
                    gpu_options = [
                        ('NVIDIA NVENC', 'vp9_nvenc',
                         ['-c:v', 'vp9_nvenc', '-cq', '30', '-b:v', '0']),
                        ('Intel Quick Sync', 'vp9_qsv',
                         ['-c:v', 'vp9_qsv', '-global_quality', '30']),
                    ]
                    vaapi_devices = sorted(Path('/dev/dri').glob('renderD*'))
                    if vaapi_devices:
                        gpu_options.append((
                            'VA-API', 'vp9_vaapi',
                            ['-init_hw_device', f'vaapi=va:{vaapi_devices[0]}',
                             '-filter_hw_device', 'va', '-vf', 'format=nv12,hwupload',
                             '-c:v', 'vp9_vaapi', '-qp', '30'],
                        ))
                    software_args = [
                        '-c:v', 'libvpx-vp9', '-deadline', 'realtime',
                        '-cpu-used', '8', '-crf', '30', '-b:v', '0',
                        '-row-mt', '1', '-tile-columns', '2',
                    ]
                    muxer_args = ['-c:a', 'libopus', '-f', 'webm']
                    extension = 'webm'
                candidates = [
                    (label, options)
                    for label, encoder, options in gpu_options
                    if encoder in available_encoders
                ]
                candidates.append(('software', software_args))
                encoded_path = temp / f'encoded.{extension}'
                last_error = ''
                for encoder_label, video_args in candidates:
                    encoded_path.unlink(missing_ok=True)
                    if encoder_label == 'software':
                        progress.setLabelText('GPU encoding unavailable; using software…')
                    else:
                        progress.setLabelText(f'Encoding with {encoder_label}…')
                    QtWidgets.QApplication.processEvents()
                    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostats',
                               '-progress', 'pipe:1', '-stats_period', '0.1', '-y']
                    if video_args and video_args[0] == '-init_hw_device':
                        device_options = video_args[:4]
                        video_args = video_args[4:]
                        command.extend(device_options)
                    else:
                        device_options = []
                    command.extend([
                        '-f', 'concat', '-safe', '0', '-i', str(concat_path),
                        '-i', str(audio_path), '-map', '0:v:0', '-map', '1:a:0',
                        '-fps_mode', 'vfr', '-filter_threads', '0',
                        '-filter_complex_threads', '0', '-threads', '0',
                    ])
                    command.extend(video_args)
                    command.extend(muxer_args)
                    command.append(str(encoded_path))
                    result = subprocess.Popen(
                        command, cwd=temp, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding='utf-8',
                        errors='replace', bufsize=1,
                    )
                    progress_values = {}
                    canceled = False
                    for line in result.stdout:
                        if progress.wasCanceled():
                            canceled = True
                            result.terminate()
                        if '=' not in line:
                            continue
                        key, value = line.rstrip().split('=', 1)
                        progress_values[key] = value
                        if key == 'progress':
                            try:
                                elapsed = int(progress_values.get('out_time_us', '0')) / 1_000_000
                            except ValueError:
                                elapsed = 0
                            percent = min(99, round(
                                elapsed / max(total_video_duration, 0.001) * 100
                            ))
                            progress.setValue(percent)
                            QtWidgets.QApplication.processEvents()
                            progress_values.clear()
                            if canceled:
                                break
                    if canceled and result.poll() is None:
                        result.kill()
                    return_code = result.wait()
                    error_text = result.stderr.read().strip()
                    if canceled:
                        progress.close()
                        return
                    if return_code == 0:
                        break
                    last_error = error_text or f'{encoder_label} could not encode the export.'
                    encoded_path.unlink(missing_ok=True)
                else:
                    raise RuntimeError(last_error or 'FFmpeg could not encode the export.')
                shutil.move(str(encoded_path), str(destination))
                progress.setValue(100)
            progress.close()
            self.status.setText(f'Exported {len(items)} word variants to {destination}.')
        except Exception as exc:
            if progress is not None:
                progress.close()
            self.error(f'Cannot export word video: {exc}')

    def source_clip_path(self, source):
        assets = Path(__file__).resolve().parent / 'assets'
        candidate = (assets / source['name']).resolve()
        try:
            candidate.relative_to(assets.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def extract_video_frames(self, ffmpeg, ffprobe, clip_path, output_dir):
        from PIL import Image

        output_dir.mkdir(parents=True, exist_ok=True)
        probe = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
             '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(clip_path)],
            capture_output=True, text=True
        )
        if probe.returncode:
            return []
        try:
            timestamps = [float(frame['best_effort_timestamp_time'])
                          for frame in json.loads(probe.stdout).get('frames', [])
                          if 'best_effort_timestamp_time' in frame]
        except (ValueError, TypeError, KeyError):
            timestamps = []
        pattern = output_dir / 'video-%08d.png'
        decoded = subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(clip_path),
             '-map', '0:v:0', '-fps_mode', 'passthrough', '-vf', 'scale=480:480',
             str(pattern)], capture_output=True, text=True
        )
        if decoded.returncode:
            return []
        paths = sorted(output_dir.glob('video-*.png'))
        if not paths:
            return []
        if len(timestamps) != len(paths):
            timestamps = [index / 30 for index in range(len(paths))]
        return [(round(timestamp * 16000), path) for timestamp, path in zip(timestamps, paths)]

    def word_video_schedule(self, source, record, video_frames, rate):
        if not video_frames or source.get('audio_only'):
            return []
        frame_samples = [sample for sample, _ in video_frames]
        word_start = round(record['start'] * rate)
        word_end = round(record['end'] * rate)
        events = []
        for compact_start, original_start, length in source.get('time_map', []):
            compact_end = compact_start + length
            left = max(word_start, compact_start)
            right = min(word_end, compact_end)
            if right <= left:
                continue
            original_left = original_start + left - compact_start
            original_right = original_start + right - compact_start
            first_frame = max(0, bisect.bisect_right(frame_samples, original_left) - 1)
            events.append((left - word_start, video_frames[first_frame][1]))
            next_frame = bisect.bisect_right(frame_samples, original_left)
            while next_frame < len(video_frames) and frame_samples[next_frame] < original_right:
                compact_at = left + frame_samples[next_frame] - original_left
                events.append((compact_at - word_start, video_frames[next_frame][1]))
                next_frame += 1
        if not events:
            first_frame = max(0, bisect.bisect_right(frame_samples, 0) - 1)
            return [(0, video_frames[first_frame][1])]
        events.sort(key=lambda entry: entry[0])
        deduplicated = []
        for event in events:
            if deduplicated and event[0] == deduplicated[-1][0]:
                deduplicated[-1] = event
            else:
                deduplicated.append(event)
        if deduplicated[0][0] > 0:
            deduplicated.insert(0, (0, deduplicated[0][1]))
        return deduplicated

    def export_text_colors(self, image, bounds):
        from PIL import ImageStat

        average = ImageStat.Stat(image.crop(bounds)).mean
        brightness = 0.2126 * average[0] + 0.7152 * average[1] + 0.0722 * average[2]
        if brightness >= 150:
            return 'black', 'white'
        return 'white', 'black'

    def draw_export_text(self, draw, text, bounds, font_path, foreground='white', outline='black'):
        from PIL import ImageFont

        left, top, right, bottom = bounds
        max_width = right - left - 32
        max_height = bottom - top - 24
        size = 96
        while size > 18:
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
            lines = self.wrap_export_text(draw, text, font, max_width)
            boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
            line_height = max((box[3] - box[1] for box in boxes), default=size)
            widths = [box[2] - box[0] for box in boxes]
            if max(widths, default=0) <= max_width and line_height * len(lines) <= max_height:
                break
            size -= 4
        line_height = max((box[3] - box[1] for box in boxes), default=size)
        y = top + (bottom - top - line_height * len(lines)) / 2
        for line, box in zip(lines, boxes):
            width = box[2] - box[0]
            x = left + (right - left - width) / 2
            draw.text((x, y), line, font=font, fill=foreground, stroke_width=3, stroke_fill=outline)
            y += line_height

    def wrap_export_text(self, draw, text, font, max_width):
        words = str(text).split() or ['']
        lines = []
        line = ''
        for word in words:
            candidate = f'{line} {word}'.strip()
            if line and draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2] > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines

    def draw_export_summary(self, labels, path, font_path):
        from PIL import Image, ImageDraw, ImageFont

        counts = {}
        for word, _ in labels:
            key = word.casefold()
            if key not in counts:
                counts[key] = [word, 0]
            counts[key][1] += 1
        ordered = sorted(counts.values(), key=lambda entry: (-entry[1], entry[0].casefold()))
        highest = ordered[0][1]
        leaders = [word.upper() for word, count in ordered if count == highest]
        variant_word = 'variant' if highest == 1 else 'variants'
        if len(leaders) == 1:
            heading = f'{leaders[0]} has the most variants, coming in at {highest} {variant_word}!'
        elif len(leaders) == 2:
            heading = f'{leaders[0]} and {leaders[1]} are tied coming in at {highest} {variant_word}!'
        else:
            heading = f'{", ".join(leaders[:-1])} and {leaders[-1]} are tied coming in at {highest} {variant_word}!'
        entries = [f'• {word.upper()}: {count} {"variant" if count == 1 else "variants"}'
                   for word, count in ordered]
        image = Image.new('RGB', (480, 480), 'black')
        draw = ImageDraw.Draw(image)
        heading_font = None
        heading_lines = []
        heading_height = 0
        for size in range(32, 7, -1):
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
            lines = self.wrap_export_text(draw, heading, font, 448)
            boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=1) for line in lines]
            line_height = max((box[3] - box[1] for box in boxes), default=size)
            if max((box[2] - box[0] for box in boxes), default=0) <= 448 and line_height * len(lines) <= 60:
                heading_font, heading_lines, heading_height = font, lines, line_height
                break
        if heading_font is None:
            heading_font = ImageFont.truetype(font_path, 8) if font_path else ImageFont.load_default()
            heading_lines = self.wrap_export_text(draw, heading, heading_font, 448)
            heading_height = max(1, draw.textbbox((0, 0), 'Ag', font=heading_font)[3])
        y = max(0, (60 - heading_height * len(heading_lines)) / 2)
        for line in heading_lines:
            box = draw.textbbox((0, 0), line, font=heading_font, stroke_width=1)
            x = (480 - (box[2] - box[0])) / 2
            draw.text((x, y), line, font=heading_font, fill='white', stroke_width=1, stroke_fill='black')
            y += heading_height
        section_font = ImageFont.truetype(font_path, 16) if font_path else ImageFont.load_default()
        draw.text((12, 66), 'All word variant counts:', font=section_font, fill='white')
        list_top = 92
        list_bottom = 470
        list_width = 456
        list_font = None
        rendered = []
        line_height = 0
        for size in range(14, 5, -1):
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
            lines = []
            for entry in entries:
                lines.extend(self.wrap_export_text(draw, entry, font, list_width))
            boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
            candidate_height = max((box[3] - box[1] for box in boxes), default=size) + 2
            if candidate_height * len(lines) <= list_bottom - list_top:
                list_font, rendered, line_height = font, lines, candidate_height
                break
        if list_font is None:
            list_font = ImageFont.truetype(font_path, 6) if font_path else ImageFont.load_default()
            rendered = []
            for entry in entries:
                rendered.extend(self.wrap_export_text(draw, entry, list_font, list_width))
            line_height = max(1, draw.textbbox((0, 0), 'Ag', font=list_font)[3])
        y = list_top
        for line in rendered:
            if y + line_height > list_bottom:
                break
            draw.text((12, y), line, font=list_font, fill='white')
            y += line_height
        image.save(path)

    def play_segments(self, segments):
        try:
            import pygame

            self.stop_audio()
            mixer_state = pygame.mixer.get_init()
            if mixer_state != (16000, -16, 1):
                if mixer_state:
                    pygame.mixer.quit()
                pygame.mixer.init(frequency=16000, size=-16, channels=1)
            rate = self.index['sample_rate']
            chunks = []
            for source, start, end in segments:
                left = source['offset'] + round(start * rate) * 2
                right = source['offset'] + round(end * rate) * 2
                if 0 <= left < right <= source['offset'] + source['length']:
                    chunks.append(self.audio[left:right])
            if not chunks:
                raise ValueError('No playable audio was found for those entries.')
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(b''.join(chunks))
            buffer.seek(0)
            self.preview_sound = pygame.mixer.Sound(file=buffer)
            self.preview_channel = self.preview_sound.play()
            self.status.setText(f'Playing {len(chunks)} recordings continuously.')
        except Exception as exc:
            self.error(f'Cannot play recordings: {exc}')

    def play_source(self):
        if self.source() is not None:
            self.play(0, self.source()['length'] / 32000)

    def play_phone(self):
        row = self.phones.currentRow()
        if row >= 0:
            self.play_phoneme_at(row)

    def play_phoneme_at(self, row):
        if row >= 0:
            try:
                self.play(float(self.phones.item(row, 1).text()), float(self.phones.item(row, 2).text()))
            except (AttributeError, ValueError) as exc:
                self.error(f'Invalid phoneme timing: {exc}')

    def play(self, start, end):
        source = self.source()
        if source is None:
            return
        try:
            rate = self.index['sample_rate']
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError('Timings must be finite numbers.')
            left, right = round(start * rate) * 2, round(end * rate) * 2
            if not 0 <= left < right <= source['length']:
                raise ValueError('Select a positive range inside the source audio.')
            import pygame

            self.stop_audio()
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=16000, size=-16, channels=1)
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(self.audio[source['offset'] + left:source['offset'] + right])
            buffer.seek(0)
            self.preview_sound = pygame.mixer.Sound(file=buffer)
            self.preview_channel = self.preview_sound.play()
        except Exception as exc:
            self.error(f'Cannot play audio: {exc}')

    def save(self, path=None):
        if self.index is None or not self.commit_form():
            return False
        if path is None and self.path is None:
            return self.save_as()
        destination = Path(path) if path else self.path
        try:
            if destination.exists():
                shutil.copy2(destination, destination.with_suffix(destination.suffix + '.bak'))
            write_model(destination, self.index, self.audio)
            self.index, self.audio = read_model(destination)
        except Exception as exc:
            self.error(f'Cannot save index: {exc}')
            return False
        self.path = destination
        self.dirty = self.form_dirty = False
        self.update_title()
        self.statusBar().showMessage(str(destination))
        self.status.setText('Saved. Restart MarioAssistant to load this index. Unchanged source clips keep these edits during incremental preparation.')
        return True

    def save_as(self):
        if self.index is None:
            return False
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save mixer index', str(self.path or MODEL_NAME), 'Mixer index (*.bin)')
        if not path:
            return False
        if not Path(path).suffix:
            path += '.bin'
        return self.save(Path(path))

    def closeEvent(self, event):
        if self.confirm_discard():
            self.stop_audio()
            event.accept()
        else:
            event.ignore()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('index', nargs='?', type=Path)
    options = parser.parse_args()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    editor = MixerIndexEditor()
    path = options.index or Path(__file__).resolve().parent / 'assets' / MODEL_NAME
    if path.is_file():
        editor.open_file(path)
    elif options.index:
        editor.error(f'File does not exist: {path}')
    editor.show()
    app.exec()


if __name__ == '__main__':
    main()
