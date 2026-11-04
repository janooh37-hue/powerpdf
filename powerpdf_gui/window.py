"""Native browser/workspace shell. PDF operations and edit policies live in core."""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QDockWidget, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabWidget, QTextEdit, QToolBar, QVBoxLayout, QWidget,
)
from send2trash import send2trash

from powerpdf_core import files
from powerpdf_core.settings import SettingsStore
from powerpdf_core.workspace import Workspace
from . import desktop, resources, themes
from .navigation import FileBrowser, QueueList, RecentList
from .preview import PreviewPane


def human_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:,.0f} {unit}" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable size unit")


def plain_label(text="", parent=None):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class RenameDialog(QDialog):
    """Logical Unicode text goes straight into a native Qt line editor."""
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("Rename")
        self.setObjectName("renameDialog")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        title = plain_label(f"Rename {path.name}")
        title.setProperty("role", "title")
        layout.addWidget(title)
        label = QLabel("New name")
        self.name_input = QLineEdit(path.name)
        self.name_input.setObjectName("renameInput")
        self.name_input.setAccessibleName("New file or folder name")
        label.setBuddy(self.name_input)
        layout.addWidget(label)
        layout.addWidget(self.name_input)
        self.error_label = plain_label()
        self.error_label.setProperty("role", "muted")
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Rename")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name_input.textChanged.connect(self.validate)
        self.validate()
        self.name_input.selectAll()

    def validate(self):
        error = files.rename_error(self.path, self.name_input.text())
        self.error_label.setText(error or "The original contents will not change.")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(error is None and self.name_input.text() != self.path.name)

    def accept(self):
        if files.rename_error(self.path, self.name_input.text()) is None and self.name_input.text() != self.path.name:
            super().accept()


class CommandPalette(QDialog):
    """Searchable access to the same real actions as the menus."""
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Commands")
        self.resize(560, 440)
        self.actions = list(actions)
        self.chosen = None
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setAccessibleName("Find a command")
        self.search.setPlaceholderText("Find a command…")
        self.commands = QListWidget()
        self.commands.setAccessibleName("Matching commands")
        layout.addWidget(self.search)
        layout.addWidget(self.commands)
        self.search.textChanged.connect(self.filter_commands)
        self.search.returnPressed.connect(self.choose)
        self.commands.itemActivated.connect(self.choose)
        self.filter_commands("")

    def filter_commands(self, query):
        self.commands.clear()
        for action in self.actions:
            if query.casefold() in action.text().replace("&", "").casefold():
                shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
                item = QListWidgetItem(action.text().replace("&", "") + (f"    {shortcut}" if shortcut else ""))
                item.setData(Qt.ItemDataRole.UserRole, action)
                self.commands.addItem(item)
        if self.commands.count():
            self.commands.setCurrentRow(0)

    def choose(self, *_args):
        if item := self.commands.currentItem():
            self.chosen = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class MainWindow(QMainWindow):
    def __init__(self, root: Path | None = None, *, settings_path: Path | None = None):
        super().__init__()
        self.setObjectName("powerpdfWindow")
        self.setAcceptDrops(True)
        self.resize(1440, 920)
        self.setMinimumSize(940, 640)
        self.settings_store = SettingsStore(path=settings_path) if settings_path else SettingsStore()
        self.settings = self.settings_store.load()
        gui = self.settings.get("gui", {})
        self.workspace = Workspace(recent=self.settings.get("recent", []), card_labels=self.settings.get("card_labels", []))
        candidate = Path(root or gui.get("folder", Path.home())).expanduser()
        self.root = candidate.resolve() if candidate.is_dir() else Path.home()
        self.selected: Path | None = None
        self._closing = False
        self.theme_name = gui.get("theme", self.settings.get("theme", "powerpdf"))
        app = QApplication.instance()
        self._folder_changed(self.root)
        app.setStyle("Fusion")
        resources.load_fonts(app)
        self.setWindowIcon(QIcon(str(resources.asset_path("powerpdf.png"))))
        self._build_widgets()
        self._build_actions()
        self._connect_views()
        self.set_theme(self.theme_name, persist=False)
        self.browser.set_show_all(bool(gui.get("show_all", False)))
        self.actions["all_files"].setChecked(bool(gui.get("show_all", False)))
        self.browser.set_folder(self.root)
        self.browser.tree.hideColumn(1)
        self.refresh_lists()
        for key, restore in (("geometry", self.restoreGeometry), ("dock_state", self.restoreState)):
            if value := gui.get(key):
                try:
                    restore(base64.b64decode(value, validate=True))
                except (ValueError, TypeError):
                    pass  # An invalid window layout must not prevent opening documents.
        app.installEventFilter(self)
        app.styleHints().colorSchemeChanged.connect(self._system_theme_changed)
        self._show_folder_info(self.root)
        self.log("Open a folder or PDF. Files stay local; originals are never changed by preview.")

    def _build_widgets(self):
        self.browser = FileBrowser()
        self.recent_list = RecentList()
        self.navigation = QTabWidget()
        self.navigation.setObjectName("navigationTabs")
        self.navigation.addTab(self.browser, "Files")
        self.navigation.addTab(self.recent_list, "Recent")
        self.preview = PreviewPane()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("workspaceSplitter")
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([350, 760])
        self.setCentralWidget(splitter)

        self.info_dock = QDockWidget("Document info", self)
        self.info_dock.setObjectName("infoDock")
        info = QWidget()
        layout = QVBoxLayout(info)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(12)
        self.info_name = plain_label("No document selected")
        self.info_name.setProperty("role", "title")
        self.browser.tree.hideColumn(3)  # Full modified time lives in the info panel.
        self.info_name.setObjectName("infoName")
        self.info_path = plain_label()
        self.info_path.setProperty("role", "muted")
        layout.addWidget(self.info_name)
        layout.addWidget(self.info_path)
        self.info_form = QFormLayout()
        self.info_form.setSpacing(12)
        self.info_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(self.info_form)
        layout.addStretch()
        self.info_dock.setWidget(info)
        self.info_dock.setMinimumWidth(250)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.info_dock)

        self.queue_list = QueueList()
        self.queue_dock = QDockWidget("Queue", self)
        self.queue_dock.setObjectName("queueDock")
        queue_body = QWidget()
        queue_layout = QVBoxLayout(queue_body)
        queue_layout.setContentsMargins(10, 4, 10, 10)
        queue_layout.setSpacing(8)
        self.queue_hint = plain_label("Space adds the selected PDF. Drag to reorder.")
        self.queue_hint.setProperty("role", "muted")
        queue_layout.addWidget(self.queue_hint)
        queue_layout.addWidget(self.queue_list, 1)
        buttons = QHBoxLayout()
        for text, callback in (("Up", lambda: self.queue_list.move_selected(-1)),
                               ("Down", lambda: self.queue_list.move_selected(1)),
                               ("Remove", self.queue_list.remove_selected), ("Clear", self.clear_queue)):
            button = QPushButton(text)
            button.setAccessibleName(f"Queue: {text.lower()}")
            button.clicked.connect(callback)
            buttons.addWidget(button)
        queue_layout.addLayout(buttons)
        self.queue_dock.setWidget(queue_body)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.queue_dock)
        self.splitDockWidget(self.info_dock, self.queue_dock, Qt.Orientation.Vertical)
        self.resizeDocks([self.info_dock, self.queue_dock], [370, 310], Qt.Orientation.Vertical)
        self.resizeDocks([self.info_dock], [300], Qt.Orientation.Horizontal)

        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setAccessibleName("Activity")
        self.activity.document().setMaximumBlockCount(200)
        self.activity_dock = QDockWidget("Activity", self)
        self.activity_dock.setObjectName("activityDock")
        self.activity_dock.setWidget(self.activity)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.activity_dock)
        self.resizeDocks([self.activity_dock], [100], Qt.Orientation.Vertical)
        self.queue_status = QLabel()
        self.statusBar().addPermanentWidget(self.queue_status)

    def _action(self, key, text, callback, shortcut=None, checkable=False):
        action = QAction(text, self)
        action.setObjectName(key)
        action.setCheckable(checkable)
        action.triggered.connect(callback)
        if shortcut is not None:
            action.setShortcut(shortcut)
        self.actions[key] = action
        return action

    def _build_actions(self):
        self.actions = {}
        a = self._action
        file_menu = self.menuBar().addMenu("&File")
        for action in (
            a("open_files", "Open PDF files…", self.open_files_dialog, QKeySequence.StandardKey.Open),
            a("open_folder", "Open folder…", self.open_folder_dialog, "Ctrl+Shift+O"),
            a("open", "Open selected", self.open_selected),
            a("external", "Open externally…", self.open_externally),
            a("reveal", "Reveal in file manager", self.reveal_selected),
        ):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(a("rename", "Rename…", self.rename_selected, "F2"))
        file_menu.addAction(a("trash", "Move to Trash…", self.trash_selected))
        file_menu.addSeparator()
        file_menu.addAction(a("quit", "Quit", self.close, QKeySequence.StandardKey.Quit))

        navigate = self.menuBar().addMenu("&Navigate")
        for action in (
            a("home", "Home folder", lambda: self.navigate(Path.home()), "Alt+Home"),
            a("parent", "Parent folder", lambda: self.navigate(self.root.parent), "Alt+Up"),
            a("go", "Go to folder…", self.go_to_folder, "Ctrl+L"),
            a("refresh", "Refresh", self.refresh, "Ctrl+R"),
            a("recent", "Files / Recent", self.toggle_recent),
        ):
            navigate.addAction(action)

        queue = self.menuBar().addMenu("&Queue")
        for action in (
            a("toggle_queue", "Toggle selected PDF", self.toggle_queued),
            a("queue_files", "Add PDF files…", self.add_files_dialog),
            a("queue_folder", "Add current folder", self.queue_folder),
            a("clear_queue", "Clear queue", self.clear_queue),
        ):
            queue.addAction(action)
        queue.addSeparator()
        queue.addAction("Move selected up", lambda: self.queue_list.move_selected(-1))
        queue.addAction("Move selected down", lambda: self.queue_list.move_selected(1))
        queue.addAction("Remove selected", self.queue_list.remove_selected)
        view = self.menuBar().addMenu("&View")
        view.addAction(a("all_files", "Show all files", self.toggle_all_files, checkable=True))
        view.addSeparator()
        for label, callback, shortcut in (
            ("Previous page", self.preview.previous_page, "["),
            ("Next page", self.preview.next_page, "]"),
            ("Zoom in", self.preview.zoom_in, QKeySequence.StandardKey.ZoomIn),
            ("Zoom out", self.preview.zoom_out, QKeySequence.StandardKey.ZoomOut),
            ("Fit page", self.preview.fit_page, "Ctrl+0"),
            ("Fit width", self.preview.fit_width, "Ctrl+1"),
        ):
            action = QAction(label, self)
            action.triggered.connect(callback)
            # Plain brackets are routed only outside text entry by eventFilter.
            if shortcut not in ("[", "]"):
                action.setShortcut(shortcut)
            view.addAction(action)
        theme_menu = view.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions = {}
        for name, label in themes.THEMES.items():
            action = theme_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, name=name: self.set_theme(name))
            self.theme_group.addAction(action)
            self.theme_actions[name] = action
        view.addAction(a("cycle_theme", "Next theme", self.cycle_theme, "Ctrl+T"))
        view.addSeparator()
        for dock in (self.info_dock, self.queue_dock, self.activity_dock):
            view.addAction(dock.toggleViewAction())
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(a("commands", "Find a command…", self.show_commands, "Ctrl+Shift+P"))
        help_menu.addAction(a("shortcuts", "Keyboard shortcuts", self.show_shortcuts, "F1"))
        help_menu.addAction(a("license", "License and notices", self.show_license))
        help_menu.addAction(a("about", "About PowerPDF", self.show_about))

        toolbar = QToolBar("Files", self)
        toolbar.setObjectName("fileToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        brand = QLabel("PowerPDF")
        brand.setProperty("role", "brand")
        brand.setContentsMargins(8, 0, 18, 0)
        toolbar.addWidget(brand)
        for key in ("open_folder", "open_files", "toggle_queue", "queue_folder", "rename", "reveal"):
            toolbar.addAction(self.actions[key])
            toolbar.widgetForAction(self.actions[key]).setText({
                "open_folder": "Open folder…", "open_files": "Open files…",
                "toggle_queue": "Queue PDF", "queue_folder": "Queue folder",
                "rename": "Rename…", "reveal": "Reveal",
            }[key])
        self.addToolBar(toolbar)
        self.browser.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.browser.tree.customContextMenuRequested.connect(self._browser_menu)

    def _connect_views(self):
        self.browser.selected.connect(self.show_path)
        self.browser.activated_path.connect(self.activate_path)
        self.browser.paths_dropped.connect(self.open_paths)
        self.browser.folder_changed.connect(self._folder_changed)
        for view in (self.queue_list, self.recent_list):
            view.selected.connect(self.show_path)
            view.activated_path.connect(self.activate_path)
        self.queue_list.paths_dropped.connect(self.open_paths)
        self.queue_list.order_changed.connect(self.reorder_queue)
        self.queue_list.move_requested.connect(self.move_queue)
        self.queue_list.remove_requested.connect(self.remove_queued)
        self.queue_list.clear_requested.connect(self.clear_queue)
        self.recent_list.queue_requested.connect(lambda path: self.add_to_queue([path]))
        self.recent_list.forget_requested.connect(self.forget_recent)
        self.recent_list.clear_requested.connect(self.clear_recent)
        self.preview.info_ready.connect(self._document_info)
        self.browser.show_all.toggled.connect(self.actions["all_files"].setChecked)
        self.preview.error.connect(self._preview_error)

    def _browser_menu(self, point):
        index = self.browser.tree.indexAt(point)
        if index.isValid():
            self.browser.tree.setCurrentIndex(index)
        menu = QMenu(self)
        for key in ("open", "toggle_queue", "rename", "trash", "reveal", "external"):
            menu.addAction(self.actions[key])
        menu.exec(self.browser.tree.viewport().mapToGlobal(point))

    def _folder_changed(self, folder):
        self.root = Path(folder)
        self.setWindowTitle(f"PowerPDF — {self.root.name or self.root}")
        self.statusBar().showMessage(str(self.root))

    def navigate(self, folder: Path):
        try:
            folder = Path(folder).expanduser().resolve()
            if not folder.is_dir():
                raise NotADirectoryError(str(folder))
            self.browser.set_folder(folder)
            self.navigation.setCurrentIndex(0)
            self._folder_changed(folder)
        except OSError as error:
            self.report_error("Cannot open folder", error)

    def open_paths(self, paths):
        paths = [Path(path).expanduser().resolve() for path in paths]
        pdfs = [path for path in paths if files.is_pdf(path)]
        folders = [path for path in paths if path.is_dir()]
        self.add_to_queue(pdfs)
        if pdfs:
            self.browser.select_path(pdfs[0])
            self.show_path(pdfs[0])
        if folders:
            self.navigate(folders[-1])

    def open_files_dialog(self):
        names, _ = QFileDialog.getOpenFileNames(self, "Open PDF files", str(self.root), "PDF documents (*.pdf *.PDF);;All files (*)")
        if names:
            self.open_paths(names)

    def add_files_dialog(self):
        self.open_files_dialog()

    def open_folder_dialog(self):
        name = QFileDialog.getExistingDirectory(self, "Open folder", str(self.root))
        if name:
            self.navigate(Path(name))

    def go_to_folder(self):
        text, accepted = QInputDialog.getText(self, "Go to folder", "Folder path", text=str(self.root))
        if accepted and text:
            self.navigate(Path(text))

    def activate_path(self, path):
        path = Path(path)
        self.navigate(path) if path.is_dir() else self.show_path(path)

    def open_selected(self):
        if self.selected:
            self.activate_path(self.selected)

    def show_path(self, path):
        if path is None:
            return
        path = Path(path)
        if path == self.selected:
            return
        self.selected = path
        self._set_info(path)
        if files.is_pdf(path):
            self.preview.open_path(path)
        else:
            self.preview.clear()
            self._show_folder_info(path)

    def _set_info(self, path: Path, rows=()):
        self.info_name.setText(path.name or str(path))
        self.info_path.setText(str(path.parent))
        while self.info_form.rowCount():
            self.info_form.removeRow(0)
        for key, value in rows:
            label = plain_label(str(value))
            label.setObjectName("info" + key.replace(" ", ""))
            self.info_form.addRow(key, label)

    def _show_folder_info(self, path):
        try:
            stat = path.stat()
            rows = [("Type", "Folder" if path.is_dir() else "File"),
                    ("Modified", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"))]
            if path.is_file():
                rows.insert(1, ("Size", human_size(stat.st_size)))
            self._set_info(path, rows)
        except OSError as error:
            self._set_info(path, [("Unavailable", str(error))])

    def _document_info(self, path, info):
        if self._closing or Path(path) != self.selected:
            return
        try:
            stat = Path(path).stat()
            width, height = info.page_sizes[0] if info.page_sizes else (0, 0)
            rows = [("Pages", info.page_count), ("Size", human_size(stat.st_size)),
                    ("Page size", f"{width / 72 * 25.4:.1f} × {height / 72 * 25.4:.1f} mm"),
                    ("Modified", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"))]
            rows += [(key, info.metadata[key]) for key in ("Title", "Author", "Producer") if info.metadata.get(key)]
            self._set_info(Path(path), rows)
            self.workspace.remember(Path(path))
            self._persist_recent()
            self.recent_list.set_paths(self.workspace.recent)
        except OSError as error:
            self._set_info(Path(path), [("Unavailable", str(error))])

    def _preview_error(self, message):
        self.log(f"Preview: {message}")
        if self.selected:
            self._set_info(self.selected, [("Preview error", message)])

    def refresh_lists(self):
        self.queue_list.set_paths(self.workspace.queue)
        self.recent_list.set_paths(self.workspace.recent)
        count = len(self.workspace.queue)
        self.queue_dock.setWindowTitle(f"Queue · {count} PDF{'s' if count != 1 else ''}")
        self.queue_status.setText(f"{count} queued")
        self.queue_hint.setText("Space adds the selected PDF. Drag to reorder." if not count else "Shift+↑/↓ moves · Delete removes")

    def add_to_queue(self, paths):
        added = self.workspace.add(paths)
        self.refresh_lists()
        if added:
            self.log(f"Queued {added} PDF{'s' if added != 1 else ''}.")

    def toggle_queued(self):
        if self.selected and files.is_pdf(self.selected):
            self.workspace.toggle(self.selected)
            self.refresh_lists()

    def queue_folder(self):
        folder = self.selected if self.selected and self.selected.is_dir() else self.root
        try:
            added = self.workspace.add_folder(folder)
            self.refresh_lists()
            self.log(f"Queued {added} PDFs from {folder.name}.")
        except OSError as error:
            self.report_error("Cannot queue folder", error)

    def remove_queued(self, path):
        if path in self.workspace.queue:
            self.workspace.drop(self.workspace.queue.index(path))
            self.refresh_lists()

    def move_queue(self, index, step):
        position = self.workspace.move(index, step)
        self.refresh_lists()
        if position is not None:
            self.queue_list.setCurrentRow(position)

    def reorder_queue(self, paths):
        if len(paths) == len(self.workspace.queue) and set(paths) == set(self.workspace.queue):
            self.workspace.queue = list(paths)
        self.refresh_lists()

    def clear_queue(self):
        self.workspace.clear()
        self.refresh_lists()

    def _persist_recent(self):
        try:
            self.settings = self.settings_store.update(recent=[str(path) for path in self.workspace.recent])
        except OSError as error:
            self.log(f"Could not save recent documents: {error}")

    def forget_recent(self, path):
        if path in self.workspace.recent:
            self.workspace.forget(path)
            self._persist_recent()
            self.refresh_lists()

    def clear_recent(self):
        self.workspace.recent.clear()
        self._persist_recent()
        self.refresh_lists()

    def toggle_recent(self):
        self.navigation.setCurrentIndex(1 - self.navigation.currentIndex())
        (self.recent_list if self.navigation.currentIndex() else self.browser.tree).setFocus()

    def rename_selected(self):
        if not self.selected:
            return
        path = self.selected
        dialog = RenameDialog(path, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.rename_path(path, dialog.name_input.text())

    def rename_path(self, path: Path, name: str) -> bool:
        if error := files.rename_error(path, name):
            self.report_error("Cannot rename", error)
            return False
        destination = path.with_name(name)
        try:
            path.rename(destination)
        except OSError as error:
            self.report_error("Cannot rename", error)
            return False
        self._path_moved(path, destination)
        self.log(f"Renamed {path.name} → {name}")
        self.browser.select_path(destination)
        self.show_path(destination)
        return True

    def trash_selected(self):
        if not self.selected:
            return
        path = self.selected
        answer = QMessageBox.question(self, "Move to Trash", f"Move {path.name} to the system Trash?\nIt will not be permanently deleted.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.trash_path(path)

    def trash_path(self, path: Path) -> bool:
        try:
            send2trash(str(path))
        except OSError as error:
            self.report_error("Cannot move to Trash", error)
            return False
        self._path_moved(path, None)
        self.log(f"Moved {path.name} to Trash.")
        return True

    def _path_moved(self, old: Path, new: Path | None):
        self.workspace.moved(old, new)
        self._persist_recent()
        self.refresh_lists()
        if self.selected and self.selected.is_relative_to(old):
            previous = self.selected
            self.selected = None
            self.preview.clear()
            if new:
                self.show_path(new / previous.relative_to(old))
            else:
                self._show_folder_info(self.root)
        self.browser.refresh()

    def open_externally(self):
        if self.selected:
            try:
                desktop.open_external(self.selected)
            except (OSError, RuntimeError) as error:
                self.report_error("Cannot open externally", error)

    def reveal_selected(self):
        try:
            selected = desktop.reveal(self.selected or self.root)
            if not selected:
                self.log("Opened the containing folder; this file manager does not support selecting the file.")
        except (OSError, RuntimeError) as error:
            self.report_error("Cannot reveal file", error)

    def refresh(self):
        self.browser.refresh()
        self.workspace.recent[:] = [path for path in self.workspace.recent if path.is_file()]
        self._persist_recent()
        self.refresh_lists()
        if self.selected:
            path, self.selected = self.selected, None
            self.show_path(path)

    def toggle_all_files(self, checked=False):
        self.browser.set_show_all(checked)

    def set_theme(self, name, persist=True):
        self.theme_name = name if name in themes.THEMES else "powerpdf"
        themes.apply_theme(QApplication.instance(), self.theme_name)
        self.theme_actions[self.theme_name].setChecked(True)
        if persist:
            try:
                self._save_preferences()
            except OSError as error:
                self.report_error("Cannot save preferences", error)

    def _system_theme_changed(self, scheme):
        if self.theme_name == "system":
            themes.apply_theme(QApplication.instance(), "system")

    def cycle_theme(self):
        choices = list(themes.THEMES)
        self.set_theme(choices[(choices.index(self.theme_name) + 1) % len(choices)])

    def _save_preferences(self):
        self.settings = self.settings_store.update(gui={
            "folder": str(self.root), "theme": self.theme_name,
            "show_all": self.actions["all_files"].isChecked(),
            "geometry": base64.b64encode(bytes(self.saveGeometry())).decode("ascii"),
            "dock_state": base64.b64encode(bytes(self.saveState())).decode("ascii"),
        })

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.KeyPress or self._closing:
            return super().eventFilter(watched, event)
        app = QApplication.instance()
        # Route the event's actual receiver, not application-global focus. Focus
        # can be absent during native activation and on an offscreen Qt platform.
        if (app.activeModalWidget() or app.activePopupWidget()
                or not isinstance(watched, QWidget)
                or not (watched is self or self.isAncestorOf(watched))
                or isinstance(watched.window(), QDialog)):
            return False
        if isinstance(watched, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)) or isinstance(watched, QComboBox) and watched.isEditable():
            return False
        key, modifiers = event.key(), event.modifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            return False
        if key == Qt.Key.Key_Delete:
            if watched is self.queue_list or self.queue_list.isAncestorOf(watched):
                self.queue_list.remove_selected()
            elif watched is self.recent_list or self.recent_list.isAncestorOf(watched):
                if path := self.recent_list.current_path():
                    self.forget_recent(path)
            else:
                self.trash_selected()
            return True
        mapping = {
            Qt.Key.Key_Space: self.toggle_queued, Qt.Key.Key_O: self.open_selected,
            Qt.Key.Key_R: self.toggle_recent, Qt.Key.Key_G: self.go_to_folder,
            Qt.Key.Key_E: self.reveal_selected, Qt.Key.Key_A: self.queue_folder,
            Qt.Key.Key_X: self.clear_queue, Qt.Key.Key_Q: self.close,
            Qt.Key.Key_F: self.actions["all_files"].trigger,
            Qt.Key.Key_BracketLeft: self.preview.previous_page,
            Qt.Key.Key_BracketRight: self.preview.next_page,
            Qt.Key.Key_Question: self.show_shortcuts,
        }
        if callback := mapping.get(key):
            callback()
            return True
        return False

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.open_paths([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def show_commands(self):
        dialog = CommandPalette(self.actions.values(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen:
            dialog.chosen.trigger()

    def show_shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts", "Workspace (not while typing):\n"
            "Space — toggle queue     O — open selected     G — go to folder\n"
            "A — queue folder     X — clear queue     R — recent\n"
            "E — reveal     F2 — rename     Delete — Trash / remove queue entry\n"
            "[ / ] — page flip     F — all files     Q — quit\n\n"
            "Ctrl/Cmd+O — open PDFs     Ctrl+Shift+O — open folder\n"
            "Ctrl+L — go to folder     Ctrl+R — refresh     Ctrl+T — theme\n"
            "Ctrl+0 — fit page     Ctrl+1 — fit width     Ctrl++ / − — zoom\n"
            "Queue: Shift+↑/↓ — reorder, Delete — remove\n"
            "Tab moves focus. Escape cancels a dialog.")

    def show_about(self):
        QMessageBox.about(self, "About PowerPDF", "PowerPDF 0.7.0\nLocal PDF workspace\n\n"
            "Copyright © 2026 PowerPDF contributors\nAGPL-3.0-or-later — you may share and modify this application.\n"
            "No warranty. See Help → License and notices for the full license.\n\n"
            "PDFium rendering, PySide6 / Qt, and bundled Noto fonts. Your documents stay local.")

    def show_license(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("License and notices")
        dialog.resize(850, 650)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        try:
            text.setPlainText(resources.license_path().read_text(encoding="utf-8"))
        except OSError as error:
            self.report_error("Cannot read license", error)
            return
        layout.addWidget(text)
        note = plain_label("PowerPDF: AGPL-3.0-or-later. Qt/PySide6: LGPLv3. Noto fonts: SIL OFL 1.1. "
                           "Bundled component notices and corresponding source accompany distributed builds.")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def log(self, message):
        self.activity.appendPlainText(str(message))
        self.statusBar().showMessage(str(message), 8000)

    def report_error(self, title, error):
        self.log(f"{title}: {error}")
        QMessageBox.warning(self, title, str(error))

    def closeEvent(self, event):
        self._closing = True
        QApplication.instance().removeEventFilter(self)
        self.preview.shutdown()
        try:
            self._save_preferences()
        except OSError as error:
            QMessageBox.warning(self, "Preferences were not saved", str(error))
        super().closeEvent(event)
