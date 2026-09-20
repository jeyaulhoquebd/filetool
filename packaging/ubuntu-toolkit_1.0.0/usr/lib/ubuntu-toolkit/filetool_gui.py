#!/usr/bin/env python3
"""
filetool_gui.py — the same rename and image work as filetool.py, in a window.

Usage
-----
    python3 filetool_gui.py

Drag files or folders onto the window, pick an operation, press Preview to
see a table of what would happen, then Run to do it with a progress bar.

Where the logic lives
---------------------
Nothing in this file decides how a name is built, where a result goes, or
what counts as a clash. All of that is in filetool.py, which knows nothing
about windows — it talks to a front end through three channels only:

    warn        a callable that takes one line of text
    progress    an object with update(step), which may raise Cancelled
    on_result   a callable given each finished image

This file supplies those three things, calls plan_rename / run_rename /
plan_images / run_images / plan_undo / run_undo, and draws what comes back.
The command line at the bottom of filetool.py is the other front end, and it
runs the very same functions.

Threads
-------
Scanning a folder and writing files take time, so every core call runs in a
QThread. A worker never touches a widget: it emits signals, and Qt queues
those across to the main thread. The one exception to "no shared state" is
the Cancel flag, which is a plain threading.Event for that reason.
"""

import os
import sys
import threading

import filetool

# PySide6 is preferred: it is what this was written against, and it is the
# binding Ubuntu ships from 26.04 onwards. Ubuntu 24.04 ships no PySide6 at
# all — only PyQt6 — so fall back to that rather than losing the window
# entirely on that release.
#
# The two bindings differ in exactly two ways that matter to this file: the
# signal factory is spelled pyqtSignal, and enums must be scoped rather than
# reached through Qt directly. The scoped spelling used below is valid in
# both bindings, so only the imports need the branch and the rest of the file
# stays single-path. The widget list is repeated rather than shared because a
# single list cannot be imported from whichever binding won.
try:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtGui import QBrush, QColor, QFontMetrics
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSpinBox,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:          # pragma: no cover - depends on the machine
    from PyQt6.QtCore import Qt, QThread, pyqtSignal as Signal
    from PyQt6.QtGui import QBrush, QColor, QFontMetrics
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSpinBox,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

# How the three states of a table row are coloured.
OK = "ok"
MUTED = "muted"
BAD = "bad"

ROW_COLOURS = {
    OK: None,
    MUTED: QColor("#8a8a8a"),
    BAD: QColor("#c0392b"),
}


# ===========================================================================
# The worker: one core call, off the main thread
# ===========================================================================
class QtProgress:
    """
    The core's progress channel, pointed at a Qt signal.

    filetool only ever calls update() on this object, so this is the whole
    protocol. Checking the stop flag here — at the one point the core hands
    control back — is how the Cancel button works: the core catches nothing
    of its own, so the exception travels out of the middle of the run.

    That is safe for a rename because every move is written to the log before
    it happens, so a run stopped this way can always be put back with undo.
    """

    def __init__(self, job, total):
        self.job = job
        self.total = -1 if total is None else int(total)
        self.count = 0

    def update(self, step=1):
        if self.job.stopping.is_set():
            raise filetool.Cancelled()
        self.count += step
        self.job.progressed.emit(self.count, self.total)

    def clear(self):
        """Nothing to do: a window has no terminal line to erase."""


class Job(QThread):
    """
    Runs one core call and reports what it did through signals.

    Signals, not callbacks into widgets: the worker runs on its own thread,
    so the only safe thing it can do to the window is emit.
    """

    warned = Signal(str)          # one warning line
    progressed = Signal(int, int)  # steps done, total (-1 when not known)
    row = Signal(object)          # one finished image Result
    done = Signal(object)         # the payload the call returned
    failed = Signal(str)          # the call raised
    stopped = Signal(str)         # the call was cancelled

    def __init__(self, kind, parent=None, **kwargs):
        super().__init__(parent)
        self.kind = kind
        self.kwargs = kwargs
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    # -- the channels the core is handed --------------------------------
    def warn(self, message):
        self.warned.emit(str(message))

    def progress(self, total):
        return QtProgress(self, total)

    # -- the call itself ------------------------------------------------
    def run(self):
        try:
            payload = self._call_core()
        except filetool.Cancelled:
            self.stopped.emit(self.kind)
            return
        except Exception as error:            # noqa: BLE001 - shown, not hidden
            self.failed.emit("{}: {}".format(type(error).__name__, error))
            return
        self.done.emit(payload)

    def _call_core(self):
        k = self.kwargs

        if self.kind == "plan_rename":
            return filetool.plan_rename(k["paths"], replace=k["replace"],
                                        progress=self.progress(None),
                                        warn=self.warn, **k["options"])

        if self.kind == "run_rename":
            return filetool.run_rename(k["plan"], log_path=k["log_path"],
                                       progress=self.progress(k["total"]),
                                       warn=self.warn)

        if self.kind == "plan_undo":
            return filetool.plan_undo(k["log_path"], warn=self.warn)

        if self.kind == "run_undo":
            return filetool.run_undo(k["plan"],
                                     progress=self.progress(k["total"]),
                                     warn=self.warn)

        if self.kind == "plan_images":
            return filetool.plan_images(k["paths"], warn=self.warn, **k["options"])

        if self.kind == "run_images":
            return filetool.run_images(k["plan"],
                                       progress=self.progress(k["total"]),
                                       on_result=self.row.emit, warn=self.warn)

        raise ValueError("unknown job: {!r}".format(self.kind))


# ===========================================================================
# Small widgets
# ===========================================================================
class DropArea(QFrame):
    """The box you drag things onto. Clicking it also opens a file picker."""

    clicked = Signal()
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumHeight(72)

        label = QLabel("Drop files or folders here\n(or click to choose files)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setObjectName("dropLabel")

        layout = QVBoxLayout(self)
        layout.addWidget(label)

        self.setStyleSheet(
            "#dropArea { border: 2px dashed #9aa0a6; border-radius: 8px;"
            "           background: #fafafa; }"
            "#dropArea:hover { border-color: #4285f4; background: #f2f7ff; }"
            "#dropLabel { color: #5f6368; }"
        )

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile()]
        if paths:
            self.dropped.emit(paths)
        event.acceptProposedAction()


def make_item(text, kind=OK):
    """One uneditable table cell, coloured by `kind`."""
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    colour = ROW_COLOURS.get(kind)
    if colour is not None:
        item.setForeground(QBrush(colour))
    item.setToolTip(text)
    return item


def format_size(num_bytes):
    """Reuse the core's number formatting, so both front ends read the same."""
    return filetool.format_size(num_bytes)


# ===========================================================================
# The window
# ===========================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("filetool — rename and reshape files")
        self.resize(1060, 780)
        self.setAcceptDrops(True)

        self.paths = []            # what the user dragged in, absolute
        self.plan = None           # the previewed plan, or None
        self.job = None            # the Job running now, or None
        self.rows_by_source = {}   # image source path -> table row

        self._build()
        self._operation_changed()

    # -- construction ---------------------------------------------------
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        layout.addWidget(self._build_inputs())
        layout.addWidget(self._build_operation())
        layout.addLayout(self._build_actions())
        layout.addWidget(self._build_table_group(), 1)
        layout.addWidget(self._build_log())

        # A drop landing on a text box would otherwise be typed into it as a
        # path, which is never what was meant. Turning drops off there lets
        # the event travel up to the window instead.
        for cls in (QLineEdit, QPlainTextEdit):
            for widget in self.findChildren(cls):
                widget.setAcceptDrops(False)

    def _build_inputs(self):
        box = QGroupBox("1. Files and folders")
        outer = QVBoxLayout(box)

        self.drop_area = DropArea()
        self.drop_area.clicked.connect(self.choose_files)
        self.drop_area.dropped.connect(self.add_paths)
        outer.addWidget(self.drop_area)

        self.path_list = QListWidget()
        self.path_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.path_list.setMinimumHeight(96)
        outer.addWidget(self.path_list)

        buttons = QHBoxLayout()
        for text, slot in (("Add files…", self.choose_files),
                           ("Add folder…", self.choose_folder),
                           ("Remove selected", self.remove_selected),
                           ("Clear", self.clear_paths)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.inputs_label = QLabel("No files or folders chosen yet.")
        outer.addWidget(self.inputs_label)
        return box

    def _build_operation(self):
        box = QGroupBox("2. What to do")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Operation:"))
        self.operation = QComboBox()
        self.operation.addItem("Rename files", "rename")
        self.operation.addItem("Optimize images", "image")
        self.operation.addItem("Undo a rename", "undo")
        self.operation.currentIndexChanged.connect(self._operation_changed)
        row.addWidget(self.operation)
        row.addStretch(1)
        outer.addLayout(row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_rename_panel())
        self.stack.addWidget(self._build_image_panel())
        self.stack.addWidget(self._build_undo_panel())
        outer.addWidget(self.stack)
        return box

    def _build_rename_panel(self):
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)

        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("e.g. trip_")
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("e.g. _2024")
        self.base_edit = QLineEdit()
        self.base_edit.setPlaceholderText("leave blank to keep each stem")
        self.pattern_edit = QLineEdit()
        self.pattern_edit.setPlaceholderText("*.jpg")

        grid.addWidget(QLabel("Prefix:"), 0, 0)
        grid.addWidget(self.prefix_edit, 0, 1)
        grid.addWidget(QLabel("Pattern:"), 0, 2)
        grid.addWidget(self.pattern_edit, 0, 3)
        grid.addWidget(QLabel("Suffix:"), 1, 0)
        grid.addWidget(self.suffix_edit, 1, 1)
        grid.addWidget(QLabel("Replace in:"), 1, 2)
        self.scope_combo = QComboBox()
        for label, value in (("the name only", "stem"),
                             ("the extension only", "ext"),
                             ("the whole name", "full")):
            self.scope_combo.addItem(label, value)
        grid.addWidget(self.scope_combo, 1, 3)
        grid.addWidget(QLabel("Base name:"), 2, 0)
        grid.addWidget(self.base_edit, 2, 1)
        grid.addWidget(QLabel("Case:"), 2, 2)
        self.case_combo = QComboBox()
        for label, value in (("leave as it is", "keep"), ("lowercase", "lower"),
                             ("UPPERCASE", "upper"), ("Title Case", "title")):
            self.case_combo.addItem(label, value)
        grid.addWidget(self.case_combo, 2, 3)

        self.replace_box = QPlainTextEdit()
        self.replace_box.setPlaceholderText(
            "One replacement per line, written old=new.\n"
            "For example:\n"
            "    Live at =\n"
            "    _=_")
        self.replace_box.setFixedHeight(88)
        grid.addWidget(QLabel("Replace text:"), 3, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.replace_box, 3, 1, 1, 3)

        numbering = QHBoxLayout()
        self.number_check = QCheckBox("Number the files")
        numbering.addWidget(self.number_check)
        numbering.addWidget(QLabel("start"))
        self.number_start = QSpinBox()
        self.number_start.setRange(0, 10 ** 9)
        numbering.addWidget(self.number_start)
        numbering.addWidget(QLabel("step"))
        self.number_step = QSpinBox()
        self.number_step.setRange(1, 10 ** 6)
        numbering.addWidget(self.number_step)
        numbering.addWidget(QLabel("pad to"))
        self.number_pad = QSpinBox()
        self.number_pad.setRange(1, 12)
        numbering.addWidget(self.number_pad)
        self.number_pos = QComboBox()
        self.number_pos.addItem("after the name", "suffix")
        self.number_pos.addItem("before the name", "prefix")
        numbering.addWidget(self.number_pos)
        numbering.addStretch(1)
        grid.addLayout(numbering, 4, 0, 1, 4)

        order = QHBoxLayout()
        order.addWidget(QLabel("Sort by"))
        self.sort_combo = QComboBox()
        for label, value in (("name", "name"), ("date modified", "mtime"),
                             ("size", "size"), ("keep the folder order", "none")):
            self.sort_combo.addItem(label, value)
        order.addWidget(self.sort_combo)
        self.reverse_check = QCheckBox("Reverse")
        order.addWidget(self.reverse_check)
        self.recursive_check = QCheckBox("Look inside subfolders")
        order.addWidget(self.recursive_check)
        self.hidden_check = QCheckBox("Include hidden files")
        order.addWidget(self.hidden_check)
        order.addStretch(1)
        grid.addLayout(order, 5, 0, 1, 4)

        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("History log:"))
        self.rename_log_edit = QLineEdit(filetool.DEFAULT_LOG_PATH)
        log_row.addWidget(self.rename_log_edit, 1)
        grid.addLayout(log_row, 6, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return page

    def _build_image_panel(self):
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            "leave blank for a folder named after the first one, e.g. Pictures_optimized")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_output_folder)
        grid.addWidget(QLabel("Write results to:"), 0, 0)
        grid.addWidget(self.output_edit, 0, 1)
        grid.addWidget(browse, 0, 2)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(filetool.DEFAULT_QUALITY)
        grid.addWidget(QLabel("Quality:"), 1, 0)
        grid.addWidget(self.quality_spin, 1, 1)
        grid.addWidget(QLabel("(used by JPEG and WebP)"), 1, 2)

        grid.addWidget(QLabel("Shrink to at most:"), 2, 0)
        size_row = QHBoxLayout()
        self.max_width_spin = QSpinBox()
        self.max_width_spin.setRange(0, 100000)
        self.max_width_spin.setSpecialValueText("no limit")
        self.max_height_spin = QSpinBox()
        self.max_height_spin.setRange(0, 100000)
        self.max_height_spin.setSpecialValueText("no limit")
        size_row.addWidget(self.max_width_spin)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.max_height_spin)
        size_row.addWidget(QLabel("pixels   (0 means no limit)"))
        size_row.addStretch(1)
        grid.addLayout(size_row, 2, 1, 1, 2)

        grid.addWidget(QLabel("Save as:"), 3, 0)
        self.format_combo = QComboBox()
        for label, value in (("keep each file's own format", "keep"),
                             ("JPEG", "jpeg"), ("PNG", "png"), ("WebP", "webp")):
            self.format_combo.addItem(label, value)
        grid.addWidget(self.format_combo, 3, 1)

        flags = QHBoxLayout()
        self.upscale_check = QCheckBox("Allow enlarging small images")
        self.overwrite_check = QCheckBox("Replace results already in the output folder")
        self.image_recursive_check = QCheckBox("Look inside subfolders")
        flags.addWidget(self.upscale_check)
        flags.addWidget(self.overwrite_check)
        flags.addWidget(self.image_recursive_check)
        flags.addStretch(1)
        grid.addLayout(flags, 4, 0, 1, 3)

        note = QLabel("The originals are only ever read, never changed.")
        note.setStyleSheet("color: #5f6368;")
        grid.addWidget(note, 5, 0, 1, 3)

        grid.setColumnStretch(1, 1)
        return page

    def _build_undo_panel(self):
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)

        self.undo_log_edit = QLineEdit(filetool.DEFAULT_LOG_PATH)
        grid.addWidget(QLabel("History log:"), 0, 0)
        grid.addWidget(self.undo_log_edit, 0, 1)

        note = QLabel(
            "This puts back the names written to that log. Preview first: undo "
            "refuses to run\nif anything has moved or changed since the rename.")
        note.setStyleSheet("color: #5f6368;")
        grid.addWidget(note, 1, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        return page

    def _build_actions(self):
        row = QHBoxLayout()
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self.start_preview)
        self.run_button = QPushButton("Run")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.start_run)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)

        row.addWidget(self.preview_button)
        row.addWidget(self.run_button)
        row.addWidget(self.cancel_button)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v of %m")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        row.addWidget(self.progress, 1)
        return row

    def _build_table_group(self):
        box = QGroupBox("3. Preview")
        outer = QVBoxLayout(box)

        self.table = QTableWidget(0, 4)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        outer.addWidget(self.table)

        self.status_label = QLabel("Nothing previewed yet.")
        outer.addWidget(self.status_label)
        return box

    def _build_log(self):
        box = QGroupBox("Messages")
        outer = QVBoxLayout(box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(110)
        outer.addWidget(self.log)
        return box

    # -- choosing files -------------------------------------------------
    def choose_files(self):
        paths, _filter = QFileDialog.getOpenFileNames(self, "Choose files")
        if paths:
            self.add_paths(paths)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder")
        if folder:
            self.add_paths([folder])

    def choose_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Where should results go?")
        if folder:
            self.output_edit.setText(folder)

    def add_paths(self, paths):
        added = 0
        for raw in paths:
            path = os.path.abspath(os.path.expanduser(raw))
            if not os.path.exists(path):
                self.log_line("Not found, so not added: {}".format(raw))
                continue
            if path in self.paths:
                continue
            self.paths.append(path)
            item = QListWidgetItem(path)
            item.setToolTip(path)
            self.path_list.addItem(item)
            added += 1

        if added:
            self.log_line("Added {} item(s).".format(added))
        self.invalidate_plan()
        self._refresh_inputs_label()

    def remove_selected(self):
        for item in self.path_list.selectedItems():
            row = self.path_list.row(item)
            self.path_list.takeItem(row)
            del self.paths[row]
        self.invalidate_plan()
        self._refresh_inputs_label()

    def clear_paths(self):
        self.path_list.clear()
        self.paths = []
        self.invalidate_plan()
        self._refresh_inputs_label()

    def _refresh_inputs_label(self):
        if not self.paths:
            self.inputs_label.setText("No files or folders chosen yet.")
        else:
            self.inputs_label.setText("{} item(s) chosen.".format(len(self.paths)))

    # -- drag and drop anywhere on the window ---------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile()]
        if paths:
            self.add_paths(paths)
        event.acceptProposedAction()

    # -- the operation in force -----------------------------------------
    def _operation_changed(self):
        self.stack.setCurrentIndex(self.operation.currentIndex())
        self.invalidate_plan()

    def operation_name(self):
        return self.operation.currentData()

    # -- turning the controls into settings ------------------------------
    def rename_settings(self):
        """
        The rename options, as the core wants them.

        Raises ValueError if a replacement line is unusable, which is the
        core's own check rather than a second copy of it here.
        """
        case = self.case_combo.currentData()
        lines = [line.strip() for line in self.replace_box.toPlainText().splitlines()
                 if line.strip()]
        replacements = filetool.parse_replacements(lines)

        options = {
            "pattern": self.pattern_edit.text().strip() or None,
            "recursive": self.recursive_check.isChecked(),
            "include_hidden": self.hidden_check.isChecked(),
            "prefix": self.prefix_edit.text(),
            "suffix": self.suffix_edit.text(),
            "base": self.base_edit.text() or None,
            "replace_scope": self.scope_combo.currentData(),
            "lower": case == "lower",
            "upper": case == "upper",
            "title": case == "title",
            "number": self.number_check.isChecked(),
            "number_start": self.number_start.value(),
            "number_step": self.number_step.value(),
            "number_pad": self.number_pad.value(),
            "number_position": self.number_pos.currentData(),
            "sort": self.sort_combo.currentData(),
            "reverse": self.reverse_check.isChecked(),
        }
        return replacements, options

    def image_settings(self):
        """The image options, as the core wants them."""
        return {
            "recursive": self.image_recursive_check.isChecked(),
            "max_width": self.max_width_spin.value() or None,
            "max_height": self.max_height_spin.value() or None,
            "allow_upscale": self.upscale_check.isChecked(),
            "format": self.format_combo.currentData(),
            "output": self.output_edit.text().strip() or None,
            "overwrite": self.overwrite_check.isChecked(),
            "quality": self.quality_spin.value(),
        }

    # -- preview ---------------------------------------------------------
    def start_preview(self):
        operation = self.operation_name()

        if operation != "undo" and not self.paths:
            self.warn_line("Choose some files or folders first.")
            return

        try:
            if operation == "rename":
                replace, options = self.rename_settings()
                job = Job("plan_rename", self, paths=list(self.paths),
                          replace=replace, options=options)
                self.set_titles(("Old name", "New name", "Folder", "Status"))
            elif operation == "image":
                job = Job("plan_images", self, paths=list(self.paths),
                          options=self.image_settings())
                self.set_titles(("File", "Result", "Before", "After", "Status"))
            else:
                job = Job("plan_undo", self,
                          log_path=self.undo_log_edit.text().strip())
                self.set_titles(("Where it is now", "Where it goes back", "Status"))
        except ValueError as error:
            self.warn_line("Error: {}".format(error))
            return

        self.rows_by_source = {}
        self.clear_table()
        self.start_job(job, "Working out what would happen…")

    # -- run -------------------------------------------------------------
    def start_run(self):
        if self.plan is None:
            self.warn_line("Preview first — nothing has been worked out yet.")
            return

        operation = self.operation_name()

        if operation == "rename":
            # Two steps per file (park it, then give it its name), which is
            # what the core counts, so the bar matches the work being done.
            job = Job("run_rename", self, plan=self.plan,
                      log_path=self.rename_log_edit.text().strip(),
                      total=len(self.plan.plan) * 2)
        elif operation == "image":
            job = Job("run_images", self, plan=self.plan,
                      total=len(self.plan.ready))
        else:
            job = Job("run_undo", self, plan=self.plan,
                      total=len(self.plan.moves))

        self.start_job(job, "Running…")

    # -- one place that starts a job -------------------------------------
    def start_job(self, job, message):
        if self.job is not None:
            self.warn_line("Still busy with the last job.")
            return

        self.job = job
        job.warned.connect(self.log_line)
        job.progressed.connect(self.on_progressed)
        job.row.connect(self.on_row)
        job.done.connect(self.on_done)
        job.failed.connect(self.on_failed)
        job.stopped.connect(self.on_stopped)
        job.finished.connect(self.on_job_finished)

        self.log_line(message)
        self.set_busy(True)
        job.start()

    def cancel(self):
        if self.job is not None:
            self.log_line("Asking it to stop…")
            self.job.stop()

    def on_job_finished(self):
        job = self.job
        self.job = None
        self.set_busy(False)
        if job is not None:
            job.deleteLater()

    def on_progressed(self, done, total):
        if total is None or total < 0:
            # The number of files in a folder is not known until it has been
            # walked, so the bar counts without claiming a percentage.
            if self.progress.maximum() != 0:
                self.progress.setRange(0, 0)
            return

        if self.progress.maximum() != total:
            self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(done, max(1, total)))

    # -- what comes back -------------------------------------------------
    def on_done(self, payload):
        # Dispatch on what the job was, not on what the operation box says
        # now — the two are only the same because the box is locked while a
        # job runs, and reading the job itself does not depend on that.
        kind = self.job.kind if self.job is not None else ""

        if kind == "plan_rename":
            self.show_rename_plan(payload)
        elif kind == "plan_images":
            self.show_image_plan(payload)
        elif kind == "plan_undo":
            self.show_undo_plan(payload)
        elif kind == "run_rename":
            self.show_rename_result(payload)
        elif kind == "run_images":
            self.show_image_result(payload)
        elif kind == "run_undo":
            self.show_undo_result(payload)

    def on_failed(self, message):
        self.warn_line("Stopped with an error — {}".format(message))

    def on_stopped(self, kind):
        self.warn_line("Cancelled. Nothing further was done.")
        if kind == "run_rename":
            # A cancelled rename can leave files parked at a temporary name.
            # Every step was logged before it happened, so the undo tab can
            # work out what is where and put it back.
            self.warn_line("Some files may be left at a temporary name. Open "
                           "\"Undo a rename\" and preview to see them, then run "
                           "it to put the names back.")
        self.plan = None
        self.run_button.setEnabled(False)

    # -- tables ----------------------------------------------------------
    def set_titles(self, headers):
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.setRowCount(0)

    def clear_table(self):
        self.table.clearContents()
        self.table.setRowCount(0)

    def fill_table(self, rows):
        """rows is a list of lists of (text, kind) pairs."""
        self.table.clearContents()
        self.table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                text, kind = cell if isinstance(cell, tuple) else (cell, OK)
                self.table.setItem(r, c, make_item(text, kind))

        # Sizing from every cell would mean measuring thousands of strings;
        # the headings are a good enough guide and cost nothing.
        metrics = QFontMetrics(self.table.font())
        for c in range(self.table.columnCount()):
            head = self.table.horizontalHeaderItem(c)
            width = metrics.horizontalAdvance(head.text() if head else "")
            self.table.setColumnWidth(c, min(460, max(150, width + 40)))

    def show_rename_plan(self, plan):
        self.plan = plan
        reasons = dict(plan.reasons())
        rows = []

        for item in plan.plan:
            reason = reasons.get(item.old, "")
            kind = BAD if reason else OK
            rows.append([
                (os.path.basename(item.old), MUTED),
                (os.path.basename(item.new), kind),
                (os.path.dirname(item.old), MUTED),
                (reason or "rename", kind),
            ])

        # Files whose new name is unusable never reach the plan, so they are
        # listed from `invalid` directly.
        for path, reason in plan.invalid:
            rows.append([
                (os.path.basename(path), MUTED),
                ("—", BAD),
                (os.path.dirname(path), MUTED),
                (reason, BAD),
            ])

        self.fill_table(rows)

        if plan.blocked:
            # A run that would collide or create an unusable name is refused
            # by the core, so there is nothing to offer here either.
            self.status_label.setText(
                "{} file(s) found. {} would be renamed. {} file(s) are held up "
                "by a problem, so Run is off until the options change.".format(
                    len(plan.files), len(plan.plan), len(plan.reasons())))
            for message, sources in plan.collisions:
                self.warn_line("Collision: {}".format(message))
                for source in sources:
                    self.warn_line("    {}".format(source))
            for path, reason in plan.invalid:
                self.warn_line("Unusable name: {} — {}".format(
                    os.path.basename(path), reason))
            self.run_button.setEnabled(False)
        else:
            self.status_label.setText(
                "{} file(s) found. {} would be renamed, {} already have the "
                "right name.".format(len(plan.files), len(plan.plan),
                                     len(plan.unchanged)))
            self.run_button.setEnabled(bool(plan.plan))

        self.describe_skips(plan.stats)

    def show_image_plan(self, plan):
        self.plan = plan
        self.rows_by_source = {}
        rows = []

        if not plan.results:
            self.fill_table([])
            if plan.output_folder is None:
                self.status_label.setText(
                    "The output folder was refused — see the messages below.")
            else:
                self.status_label.setText("No images found.")
            self.run_button.setEnabled(False)
            return

        for result in plan.results:
            kind = BAD if result.error else OK
            self.rows_by_source[result.source] = len(rows)
            rows.append([
                (os.path.basename(result.source), MUTED),
                (os.path.basename(result.dest) if result.dest else "—", kind),
                ("—", MUTED),
                ("—", MUTED),
                (result.error or "will be written", kind),
            ])

        self.fill_table(rows)

        if plan.blocked:
            self.status_label.setText(
                "{} image(s) found. {} will be written, {} will be skipped.".format(
                    len(plan.images), len(plan.ready), len(plan.blocked)))
        else:
            self.status_label.setText(
                "{} image(s) found — all will be written to {}.".format(
                    len(plan.images), plan.output_folder))
        self.run_button.setEnabled(bool(plan.ready))

    def show_undo_plan(self, plan):
        self.plan = plan
        rows = []

        for current, original in plan.moves:
            rows.append([
                (current, MUTED),
                (original, OK),
                ("put back", OK),
            ])

        for path, reason in plan.problems:
            rows.append([(path, MUTED), ("—", BAD), (reason, BAD)])

        self.fill_table(rows)

        for line in plan.message:
            self.log_line(line)

        if plan.ready:
            self.status_label.setText(
                "{} file(s) can be put back.".format(len(plan.moves)))
        else:
            self.status_label.setText("Nothing to put back.")
        self.run_button.setEnabled(plan.ready)

    def show_rename_result(self, payload):
        run_id, renamed, problems = payload
        self.status_label.setText(
            "Renamed {} file(s); {} failed. Run id {}".format(
                len(renamed), len(problems), run_id))
        for path, reason in problems:
            self.warn_line("Failed: {} — {}".format(os.path.basename(path), reason))
        if renamed:
            self.log_line("To put those names back, choose \"Undo a rename\" and "
                          "preview.")
        self.prune_missing_paths()
        self.finish_run()

    def show_image_result(self, payload):
        written = [r for r in payload if not r.error]
        skipped = [r for r in payload if r.error]
        before = sum(r.before_bytes for r in written)
        after = sum(r.after_bytes for r in written)
        self.status_label.setText(
            "Wrote {} image(s), {} skipped. {} before, {} after.".format(
                len(written), len(skipped), format_size(before),
                format_size(after)))
        self.finish_run()

    def show_undo_result(self, payload):
        restored, problems = payload
        self.status_label.setText(
            "Put back {} name(s); {} could not be moved.".format(
                len(restored), len(problems)))
        for path, reason in problems:
            self.warn_line("Could not restore {}: {}".format(path, reason))
        self.finish_run()

    def finish_run(self):
        """The work is done, so the plan it came from is spent."""
        self.plan = None
        self.run_button.setEnabled(False)

    def on_row(self, result):
        """One image finished. The core calls this from the worker thread."""
        row = self.rows_by_source.get(result.source)
        if row is None or row >= self.table.rowCount():
            return

        kind = BAD if result.error else OK
        before = format_size(result.before_bytes) if result.before_bytes else "—"
        after = format_size(result.after_bytes) if result.after_bytes else "—"
        note = result.error or result.note or "written"

        self.table.setItem(row, 2, make_item(before, MUTED))
        self.table.setItem(row, 3, make_item(after, MUTED))
        self.table.setItem(row, 4, make_item(note, kind))

    def prune_missing_paths(self):
        """Drop inputs that a rename moved away, so the list stays honest."""
        gone = [path for path in self.paths if not os.path.exists(path)]
        for path in gone:
            self.paths.remove(path)
        if gone:
            self.path_list.clear()
            for path in self.paths:
                item = QListWidgetItem(path)
                item.setToolTip(path)
                self.path_list.addItem(item)
            self.log_line("Removed {} path(s) from the list; they were renamed "
                          "and no longer exist under the old name.".format(len(gone)))
            self._refresh_inputs_label()

    # -- odds and ends ---------------------------------------------------
    def describe_skips(self, stats):
        parts = []
        if stats.get("hidden"):
            parts.append("{} hidden file(s)".format(stats["hidden"]))
        if stats.get("no_match"):
            parts.append("{} not matching the pattern".format(stats["no_match"]))
        if stats.get("symlinks"):
            parts.append("{} symlink(s)".format(stats["symlinks"]))
        if stats.get("empty_dirs"):
            parts.append("{} empty folder(s)".format(stats["empty_dirs"]))
        if stats.get("bad_paths"):
            parts.append("{} path(s) that do not exist".format(stats["bad_paths"]))
        if parts:
            self.log_line("Skipped: " + ", ".join(parts) + ".")

    def invalidate_plan(self):
        self.plan = None
        self.run_button.setEnabled(False)

    def set_busy(self, busy):
        self.preview_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        # Locking the choices too, so a job cannot be started against one
        # operation and read back against another.
        self.operation.setEnabled(not busy)
        self.stack.setEnabled(not busy)
        self.path_list.setEnabled(not busy)

        if busy:
            self.run_button.setEnabled(False)
            # Nothing is known about the size of the work yet, so the bar
            # moves without a percentage until the first count arrives.
            self.progress.setRange(0, 0)
        elif self.progress.maximum() == 0:
            # A scan, which never reported a total. A finished run is left
            # alone so its bar stays full and readable.
            self.progress.setRange(0, 1)
            self.progress.setValue(0)

    def log_line(self, text):
        self.log.appendPlainText(str(text))

    def warn_line(self, text):
        self.log.appendPlainText(str(text))

    def closeEvent(self, event):
        """Stop the worker before the window goes, or Qt aborts."""
        if self.job is not None:
            self.job.stop()
            self.job.wait(5000)
        event.accept()


def main(argv=None):
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
