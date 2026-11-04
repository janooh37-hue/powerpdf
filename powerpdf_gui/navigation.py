"""Native path views; Workspace remains the owner of queue and history policy."""
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QSignalBlocker, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QStyle,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from .text import FilenameEdit


def _local_paths(event) -> list[Path]:
    """Keep URL order intact; validation and folder expansion belong to the caller."""
    return [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]


def _accept_copy(event) -> None:
    # Never acknowledge an external MoveAction: its source may delete originals.
    event.setDropAction(Qt.DropAction.CopyAction)
    event.accept()


class _PdfFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_all = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def filterAcceptsRow(self, row, parent):
        source = self.sourceModel()
        index = source.index(row, 0, parent)
        return self.show_all or source.isDir(index) or Path(source.fileName(index)).suffix.lower() == ".pdf"


class _DropTree(QTreeView):
    paths_dropped = Signal(object)

    def dragEnterEvent(self, event):
        if _local_paths(event):
            _accept_copy(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = _local_paths(event)
        if paths:
            _accept_copy(event)
            self.paths_dropped.emit(paths)
        else:
            event.ignore()


class FileBrowser(QWidget):
    """Read-only filesystem view with native Unicode names and local URL drops.

    ``set_folder`` raises OSError for an unavailable folder. Folder activation is
    emitted, not interpreted: the main window decides navigation/drop policies.
    """

    selected = Signal(object)
    activated_path = Signal(object)
    paths_dropped = Signal(object)
    folder_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folder = Path.home()
        self._pending_selection = None
        self.proxy = _PdfFilter(self)
        self.tree = _DropTree(self)
        self.tree.setAccessibleName("Files and folders")
        self.tree.setModel(self.proxy)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setDragEnabled(False)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(False)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.activated.connect(self._activate)
        self.tree.paths_dropped.connect(self.paths_dropped.emit)
        self.tree.selectionModel().currentChanged.connect(self._select)
        self.proxy.rowsInserted.connect(self._apply_pending_selection)

        self.path_edit = FilenameEdit(parent=self)
        self.path_edit.setAccessibleName("Folder path")
        self.path_edit.returnPressed.connect(self._go_to_path)
        def folder_button(name, icon, callback):
            button = QToolButton(self)
            button.setIcon(self.style().standardIcon(icon))
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.clicked.connect(callback)
            return button

        home = folder_button("Home folder", QStyle.StandardPixmap.SP_DirHomeIcon, lambda: self.set_folder(Path.home()))
        self.up_button = folder_button("Parent folder", QStyle.StandardPixmap.SP_ArrowUp, lambda: self.set_folder(self.folder.parent))
        choose = folder_button("Open folder…", QStyle.StandardPixmap.SP_DirOpenIcon, self._choose_folder)
        refresh = folder_button("Refresh folder", QStyle.StandardPixmap.SP_BrowserReload, self.refresh)
        self.show_all = QCheckBox("All files", self)
        self.show_all.setToolTip("Show all files instead of PDFs only")
        self.show_all.toggled.connect(self.set_show_all)
        controls = QHBoxLayout()
        for widget in (home, self.up_button, choose, refresh):
            controls.addWidget(widget)
        controls.addStretch()
        controls.addWidget(self.show_all)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel("Folder", self)
        label.setBuddy(self.path_edit)
        layout.addWidget(label)
        layout.addWidget(self.path_edit)
        layout.addLayout(controls)
        layout.addWidget(self.tree)
        self._new_source()
        self.set_folder(self.folder)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.hideColumn(2)

    def _new_source(self):
        old = self.proxy.sourceModel()
        self.model = QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.model.directoryLoaded.connect(self._apply_pending_selection)
        self.proxy.setSourceModel(self.model)
        if old is not None:
            old.deleteLater()

    def set_folder(self, folder: Path) -> None:
        folder = Path(folder).expanduser().absolute()
        if not folder.is_dir():
            raise NotADirectoryError(f"Folder is not available: {folder}")
        changed = folder != self.folder
        self._pending_selection = None
        self.folder = folder
        root = self.model.setRootPath(str(folder))
        self.tree.setRootIndex(self.proxy.mapFromSource(root))
        self.path_edit.setText(str(folder))
        self.up_button.setEnabled(folder.parent != folder)
        if changed:
            self.folder_changed.emit(folder)

    def select_path(self, path: Path) -> None:
        path = Path(path).expanduser().absolute()
        if path.parent != self.folder:
            self.set_folder(path.parent)
        self._pending_selection = path
        self._apply_pending_selection()

    def _apply_pending_selection(self, *_args) -> None:
        if self._pending_selection is None:
            return
        index = self.proxy.mapFromSource(self.model.index(str(self._pending_selection)))
        if index.isValid():
            self._pending_selection = None
            self.tree.setCurrentIndex(index)
            self.tree.scrollTo(index)

    def selected_path(self) -> Path | None:
        return self._path(self.tree.currentIndex())

    def _path(self, index: QModelIndex) -> Path | None:
        if not index.isValid():
            return None
        source = self.proxy.mapToSource(index.siblingAtColumn(0))
        return Path(self.model.filePath(source)) if source.isValid() else None

    def _select(self, index, _previous):
        path = self._path(index)
        if path is not None:
            self.selected.emit(path)

    def _activate(self, index):
        path = self._path(index)
        if path is not None:
            self.activated_path.emit(path)

    def set_show_all(self, enabled: bool) -> None:
        self.proxy.beginFilterChange()
        self.proxy.show_all = bool(enabled)
        with QSignalBlocker(self.show_all):
            self.show_all.setChecked(enabled)
        self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        self._apply_pending_selection()

    def refresh(self) -> None:
        selected = self.selected_path()
        self._new_source()
        root = self.model.setRootPath(str(self.folder))
        self.tree.setRootIndex(self.proxy.mapFromSource(root))
        self._pending_selection = selected
        self._apply_pending_selection()

    def _go_to_path(self):
        folder = Path(self.path_edit.text()).expanduser()
        if not folder.is_absolute():
            folder = self.folder / folder
        try:
            self.set_folder(folder)
        except OSError as error:
            QMessageBox.warning(self, "Cannot open folder", str(error))
            self.path_edit.setText(str(self.folder))

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Open folder", str(self.folder))
        if folder:
            try:
                self.set_folder(Path(folder))
            except OSError as error:
                QMessageBox.warning(self, "Cannot open folder", str(error))


class _PathList(QListWidget):
    selected = Signal(object)
    activated_path = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.currentItemChanged.connect(self._current_changed)
        self.itemActivated.connect(lambda item: self.activated_path.emit(self._item_path(item)))

    @staticmethod
    def _item_path(item):
        return Path(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _current_changed(self, current, _previous):
        if current is not None:
            self.selected.emit(self._item_path(current))

    def paths(self) -> list[Path]:
        return [self._item_path(self.item(row)) for row in range(self.count())]

    def current_path(self) -> Path | None:
        return self._item_path(self.currentItem())

    def set_paths(self, paths: Iterable[Path]) -> None:
        current = self.current_path()
        with QSignalBlocker(self):
            self.clear()
            for path in paths:
                path = Path(path)
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(str(path))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
                self.addItem(item)
                if path == current:
                    self.setCurrentItem(item)


class QueueList(_PathList):
    """Queue projection with reorder results and policy-free mutation requests.

    Connect move_requested(index, step), remove_requested(path), clear_requested
    to Workspace operations, then call set_paths. Native drag reorder emits the
    complete new order once; set_paths never emits order_changed.
    """

    order_changed = Signal(object)
    paths_dropped = Signal(object)
    move_requested = Signal(int, int)
    remove_requested = Signal(object)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("PDF queue")
        self.setToolTip("Drag to reorder · Shift+Up/Down to move · Delete to remove · Enter to preview")
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setAcceptDrops(True)
        self._internal_drop = False
        self.model().rowsMoved.connect(self._rows_moved)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def _rows_moved(self, *_args):
        if not self._internal_drop:
            self.order_changed.emit(self.paths())

    def move_selected(self, step: int) -> None:
        if self.currentRow() >= 0:
            self.move_requested.emit(self.currentRow(), step)

    def remove_selected(self) -> None:
        path = self.current_path()
        if path is not None:
            self.remove_requested.emit(path)

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ShiftModifier and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.move_selected(-1 if event.key() == Qt.Key.Key_Up else 1)
            event.accept()
        elif event.key() == Qt.Key.Key_Delete and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.remove_selected()
            event.accept()
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.source() is self:
            super().dragEnterEvent(event)
        elif _local_paths(event):
            _accept_copy(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self:
            super().dragMoveEvent(event)
        elif _local_paths(event):
            _accept_copy(event)
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.source() is self:
            before = self.paths()
            self._internal_drop = True
            try:
                super().dropEvent(event)
            finally:
                self._internal_drop = False
            if self.paths() != before:
                self.order_changed.emit(self.paths())
        else:
            paths = _local_paths(event)
            if paths:
                _accept_copy(event)
                self.paths_dropped.emit(paths)
            else:
                event.ignore()

    def _context_menu(self, point):
        item = self.itemAt(point)
        if item is not None:
            self.setCurrentItem(item)
        menu = QMenu(self)
        up = menu.addAction("Move up", lambda: self.move_selected(-1))
        down = menu.addAction("Move down", lambda: self.move_selected(1))
        remove = menu.addAction("Remove from queue", self.remove_selected)
        up.setEnabled(self.currentRow() > 0)
        down.setEnabled(0 <= self.currentRow() < self.count() - 1)
        remove.setEnabled(self.current_path() is not None)
        menu.addSeparator()
        clear = menu.addAction("Clear queue", self.clear_requested.emit)
        clear.setEnabled(self.count() > 0)
        menu.exec(self.viewport().mapToGlobal(point))


class RecentList(_PathList):
    queue_requested = Signal(object)
    forget_requested = Signal(object)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Recent PDFs")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def _context_menu(self, point):
        item = self.itemAt(point)
        if item is not None:
            self.setCurrentItem(item)
        path = self.current_path()
        menu = QMenu(self)
        for label, signal in (("Preview", self.activated_path), ("Add to queue", self.queue_requested), ("Forget", self.forget_requested)):
            action = menu.addAction(label, lambda checked=False, signal=signal: signal.emit(path))
            action.setEnabled(path is not None)
        menu.addSeparator()
        clear = menu.addAction("Clear recent history", self.clear_requested.emit)
        clear.setEnabled(self.count() > 0)
        menu.exec(self.viewport().mapToGlobal(point))
