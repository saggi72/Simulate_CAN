"""
==============================================================
ui/tab_dbc.py — DBC Management Tab (Tối ưu hoá)
==============================================================

Thay đổi so với bản gốc:
- [OPT-1]  Xoá _filter_messages() wrapper thừa
            → connect signal trực tiếp tới _rebuild_msg_tree
- [OPT-2]  Sửa signal emit kép trong _on_load_finished / _on_remove_selected
            → chỉ emit signal, KHÔNG gọi main.on_networks_changed() trực tiếp
            (MainWindow đã connect slot vào signal rồi → gọi 2 lần = bug)
- [OPT-3]  DbcFile.load() giảm từ 13 strategy xuống 5 strategy hợp lý
            → Bỏ strict=True loop (DBC files thực tế luôn cần lenient)
            → Gộp encoding fallback thông minh hơn
- [OPT-4]  MergedDatabase.messages → cache kết quả, invalidate khi DBC thay đổi
            (bản gốc duyệt toàn bộ files mỗi lần gọi property)
- [OPT-5]  _on_msg_clicked: tách import SignalTypeDetector ra khỏi vòng lặp
            (bản gốc import trong for loop mỗi row)
- [OPT-6]  Thêm type hints đầy đủ, docstring tiếng Việt cho mọi hàm
- [OPT-7]  Constants tập trung, bỏ magic numbers

Features:
- Load đơn hoặc nhiều DBC files qua file dialog
- DBC file list với remove/reload support
- Full message & signal browser với search/filter
- Signal detail view (start bit, length, factor, offset, choices)
- Network assignment per DBC file
- Auto-detect và hiển thị GenMsgCycleTime / GenMsgSendType
- Export parsed signal list ra CSV
- Emit networks_changed signal để đồng bộ với SendCAN tab
- Hỗ trợ nhiều DBCs gộp vào 1 logical network
"""

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QGroupBox, QHeaderView,
    QFileDialog, QMessageBox, QAbstractItemView,
    QTextEdit, QProgressBar, QCheckBox,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QBrush

logger = logging.getLogger(__name__)

# Kiểm tra cantools có cài không (optional dependency)
try:
    import cantools
    _CANTOOLS_AVAILABLE = True
except ImportError:
    cantools = None
    _CANTOOLS_AVAILABLE = False


# ==============================================================
# CONSTANTS — [OPT-7] Tập trung, bỏ magic numbers
# ==============================================================

# Danh sách encoding thử khi load DBC (theo thứ tự ưu tiên)
_DBC_ENCODINGS = ("utf-8", "cp1252", "latin-1")

# Fallback encodings cho manual read strategy
_DBC_FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

# Chiều rộng cột mặc định trong file tree
_FILE_TREE_COL_WIDTHS = {1: 40, 2: 40, 3: 55}

# Chiều rộng cột trong message tree
_MSG_TREE_COL_WIDTHS = {1: 55, 2: 35, 3: 55, 4: 75, 5: 50}

# Chiều rộng cột trong signal table
_SIG_TABLE_COL_WIDTHS = {1: 40, 2: 35, 3: 55, 4: 65, 5: 60, 6: 45}

# CSV export header
_CSV_HEADER = [
    "Network", "Message", "FrameID", "DLC", "CycleMS",
    "SendType", "Signal", "Start", "Length", "ByteOrder",
    "Factor", "Offset", "Unit", "Comment",
]


# ==============================================================
# DATA MODELS
# ==============================================================

class DbcFile:
    """
    Đại diện cho 1 file DBC đã load.

    Lưu trữ:
    - path: đường dẫn file gốc
    - network: tên network (do user gán hoặc tự lấy từ tên file)
    - database: cantools.db.Database object (None nếu chưa load)
    - loaded: True nếu parse thành công
    - error: thông báo lỗi nếu parse thất bại
    - msg_count / sig_count: đếm messages và signals

    Attributes:
        path:      Đường dẫn file DBC (Path object)
        name:      Tên file không có extension (stem)
        network:   Tên network được gán
        database:  cantools Database object hoặc None
        error:     Chuỗi lỗi nếu load thất bại
        loaded:    True nếu đã load thành công
        msg_count: Số lượng messages trong DBC
        sig_count: Số lượng signals trong DBC
    """

    def __init__(self, path: str, network: str = "") -> None:
        self.path = Path(path)
        self.name: str = self.path.stem
        self.network: str = network or self.path.stem
        self.database = None          # cantools.db.Database
        self.error: str = ""
        self.loaded: bool = False
        self.msg_count: int = 0
        self.sig_count: int = 0

    def load(self) -> bool:
        """
        Parse DBC file bằng cantools.

        [OPT-3] Giảm từ 13 strategy xuống 5:
        Strategy 1: cantools.db.load_file() mặc định (auto-detect encoding)
        Strategy 2: load_file() với từng encoding cụ thể, strict=False
        Strategy 3: Alternate import path (cantools.database.load_file)
        Strategy 4: Manual read file → parse string (fallback cuối cùng)

        Logic:
        - Hầu hết DBC files là UTF-8 hoặc cp1252 (Windows tool tạo)
        - strict=False luôn dùng vì DBC files thường có minor errors
        - Manual read chỉ dùng khi cantools API không nhận file trực tiếp

        Returns:
            True nếu load thành công, False nếu thất bại
        """
        if not _CANTOOLS_AVAILABLE:
            self.error = "cantools chưa cài (pip install cantools)"
            return False

        path_str = str(self.path)
        last_err: Optional[Exception] = None

        # --- Strategy 1: Default load (cantools tự detect) ---
        try:
            self.database = cantools.db.load_file(path_str, strict=False)
            return self._finalize_load()
        except TypeError:
            # cantools cũ không hỗ trợ strict param
            try:
                self.database = cantools.db.load_file(path_str)
                return self._finalize_load()
            except Exception as e:
                last_err = e
        except Exception as e:
            last_err = e

        # --- Strategy 2: Thử từng encoding cụ thể ---
        for encoding in _DBC_ENCODINGS:
            try:
                self.database = cantools.db.load_file(
                    path_str, encoding=encoding, strict=False,
                )
                return self._finalize_load()
            except (TypeError, Exception) as e:
                last_err = e

        # --- Strategy 3: Alternate import path ---
        try:
            import cantools.database as ctdb
            self.database = ctdb.load_file(path_str)
            return self._finalize_load()
        except Exception as e:
            last_err = e

        # --- Strategy 4: Manual read + parse string (fallback cuối) ---
        for encoding in _DBC_FALLBACK_ENCODINGS:
            try:
                with open(path_str, "r", encoding=encoding, errors="replace") as f:
                    content = f.read()
                self.database = cantools.db.load_string(
                    content, database_format="dbc",
                )
                return self._finalize_load()
            except Exception as e:
                last_err = e

        # Tất cả strategy đều thất bại
        self.error = str(last_err) if last_err else "Unknown error"
        self.loaded = False
        return False

    def _finalize_load(self) -> bool:
        """
        Cập nhật metadata sau khi parse thành công.

        Đếm messages và signals, đánh dấu loaded = True.
        Tách riêng để tránh lặp code trong mỗi strategy.

        Returns:
            Luôn trả về True (convenience cho caller)
        """
        self.msg_count = len(self.database.messages)
        self.sig_count = sum(
            len(m.signals) for m in self.database.messages
        )
        self.loaded = True
        self.error = ""
        return True


class Network:
    """
    Logical network gộp 1 hoặc nhiều DbcFile.

    Ví dụ: network "Powertrain" có thể chứa 3 DBC files
    (engine.dbc, transmission.dbc, emissions.dbc).

    Khi SendCAN tab cần danh sách messages → truy cập qua
    Network.database (MergedDatabase proxy).

    Attributes:
        name:      Tên network
        dbc_files: Danh sách DbcFile thuộc network này
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.dbc_files: List[DbcFile] = []
        # [OPT-4] Cache cho merged database, invalidate khi files thay đổi
        self._cached_db: Optional["MergedDatabase"] = None

    @property
    def database(self) -> "MergedDatabase":
        """
        Trả về proxy gộp tất cả DBC messages trong network.

        [OPT-4] Cache instance MergedDatabase. Chỉ tạo mới khi chưa có.
        Khi thêm/xoá DBC file → gọi invalidate_cache() để reset.

        Returns:
            MergedDatabase proxy, tương thích interface cantools.db.Database
        """
        if self._cached_db is None:
            self._cached_db = MergedDatabase(self.dbc_files)
        return self._cached_db

    def invalidate_cache(self) -> None:
        """Xoá cache MergedDatabase. Gọi khi thêm/xoá DBC files."""
        self._cached_db = None

    @property
    def msg_count(self) -> int:
        """Tổng số messages trong tất cả DBC files đã load."""
        return sum(f.msg_count for f in self.dbc_files if f.loaded)

    @property
    def sig_count(self) -> int:
        """Tổng số signals trong tất cả DBC files đã load."""
        return sum(f.sig_count for f in self.dbc_files if f.loaded)


class MergedDatabase:
    """
    Proxy gộp nhiều DBC databases thành 1 interface thống nhất.

    Tương thích với cantools.db.Database interface mà SendCAN tab sử dụng.
    Deduplicate messages theo frame_id (file đầu tiên thắng).

    [OPT-4] Cache messages list, chỉ rebuild khi _dirty flag set.

    Attributes:
        _files:         Danh sách DbcFile sources
        _msg_cache:     Cached list of messages (hoặc None nếu chưa build)
    """

    def __init__(self, dbc_files: List[DbcFile]) -> None:
        self._files = dbc_files
        self._msg_cache: Optional[list] = None

    @property
    def messages(self) -> list:
        """
        Trả về danh sách messages gộp từ tất cả DBC files.

        Deduplicate theo frame_id: nếu 2 files có cùng frame_id,
        giữ message từ file được load trước.

        [OPT-4] Cache kết quả. Reset bằng cách tạo MergedDatabase mới
        (Network.invalidate_cache() sẽ tạo instance mới).

        Returns:
            List of cantools message objects
        """
        if self._msg_cache is not None:
            return self._msg_cache

        seen: set = set()
        msgs: list = []
        for f in self._files:
            if not (f.loaded and f.database):
                continue
            for m in f.database.messages:
                if m.frame_id not in seen:
                    seen.add(m.frame_id)
                    msgs.append(m)

        self._msg_cache = msgs
        return msgs

    def get_message_by_name(self, name: str):
        """
        Tìm message theo tên, duyệt qua tất cả DBC files.

        Args:
            name: Tên message cần tìm (case-sensitive)

        Returns:
            cantools message object hoặc None nếu không tìm thấy
        """
        for f in self._files:
            if not (f.loaded and f.database):
                continue
            try:
                return f.database.get_message_by_name(name)
            except Exception:
                pass
        return None

    def get_message_by_frame_id(self, frame_id: int):
        """
        Tìm message theo CAN frame ID, duyệt qua tất cả DBC files.

        Args:
            frame_id: CAN arbitration ID (11-bit hoặc 29-bit)

        Returns:
            cantools message object hoặc None nếu không tìm thấy
        """
        for f in self._files:
            if not (f.loaded and f.database):
                continue
            try:
                return f.database.get_message_by_frame_id(frame_id)
            except Exception:
                pass
        return None


# ==============================================================
# LOAD WORKER THREAD
# ==============================================================

class DbcLoadThread(QThread):
    """
    Thread nền để load DBC files.

    Parse DBC file có thể mất vài giây với file lớn (>1000 messages).
    Chạy trong background thread để không block UI.

    Signals:
        progress(int, int, str):  (current_index, total, filename) — cập nhật progress bar
        file_loaded(DbcFile, bool): (dbc_file, success) — 1 file đã load xong
        finished():               Tất cả files đã load xong
    """

    progress = pyqtSignal(int, int, str)
    file_loaded = pyqtSignal(object, bool)
    finished = pyqtSignal()

    def __init__(self, dbc_files: List[DbcFile], parent=None) -> None:
        super().__init__(parent)
        self._files = dbc_files

    def run(self) -> None:
        """
        Parse từng DBC file tuần tự, emit signal sau mỗi file.

        Chạy trong worker thread — KHÔNG truy cập UI widgets ở đây.
        Chỉ emit signals, UI thread nhận và cập nhật widgets.
        """
        total = len(self._files)
        for i, f in enumerate(self._files):
            self.progress.emit(i + 1, total, f.name)
            success = f.load()
            self.file_loaded.emit(f, success)
        self.finished.emit()


# ==============================================================
# DBC TAB
# ==============================================================

class DbcTab(QWidget):
    """
    Tab quản lý DBC database.

    Layout 3 panel:
    ┌────────────┬──────────────────┬─────────────────┐
    │ File List  │  Message Tree    │  Signal Detail  │
    │ (networks  │  (search/filter) │  (info, table,  │
    │  + files)  │                  │   choices)      │
    └────────────┴──────────────────┴─────────────────┘

    Luồng dữ liệu:
    1. User chọn DBC file(s) → _load_dbc_paths()
    2. DbcLoadThread parse file nền → _on_file_loaded()
    3. Tạo/cập nhật Network objects → _on_load_finished()
    4. Emit networks_changed signal → MainWindow đồng bộ
    5. User click message → _on_msg_clicked() hiện signals
    6. User click signal → _on_sig_clicked() hiện value definitions

    Signals:
        networks_changed: Phát khi DBC files được load/remove/rename.
                         MainWindow connect slot để đồng bộ shared registry.
    """

    networks_changed = pyqtSignal()

    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main

        # Danh sách tất cả DBC files đã load (kể cả thất bại)
        self._dbc_files: List[DbcFile] = []

        # Network registry: tên → Network object
        self._networks: Dict[str, Network] = {}

        # Thread load DBC đang chạy (hoặc None)
        self._load_thread: Optional[DbcLoadThread] = None

        self._build_ui()
        logger.debug("DbcTab initialized")

    # ==========================================================
    # PROPERTIES — Exposed cho main_window / các tab khác
    # ==========================================================

    @property
    def networks(self) -> Dict[str, Network]:
        """
        Trả về network registry (read-only property).

        MainWindow đọc property này trong on_networks_changed()
        để copy vào shared registry.
        """
        return self._networks

    # ==========================================================
    # UI BUILD
    # ==========================================================

    def _build_ui(self) -> None:
        """
        Xây dựng layout chính của DBC tab.

        Cấu trúc từ trên xuống:
        1. Toolbar: buttons + network name input
        2. Progress bar (ẩn mặc định, hiện khi loading)
        3. 3-panel splitter: file list | message tree | signal detail
        4. Status bar: file count + cantools status
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # 1. Toolbar
        root.addLayout(self._build_toolbar())

        # 2. Progress bar (ẩn mặc định)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        # 3. Splitter 3 panel
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_file_panel())
        splitter.addWidget(self._build_msg_panel())
        splitter.addWidget(self._build_detail_panel())
        # Tỷ lệ ban đầu: file list nhỏ, msg tree + detail chia đều
        splitter.setSizes([220, 380, 340])
        splitter.setStretchFactor(0, 0)  # File list: không co giãn
        splitter.setStretchFactor(1, 1)  # Msg tree: co giãn
        splitter.setStretchFactor(2, 1)  # Detail: co giãn
        root.addWidget(splitter, stretch=1)

        # 4. Status bar
        root.addLayout(self._build_tab_status_bar())

    def _build_toolbar(self) -> QHBoxLayout:
        """
        Tạo toolbar phía trên tab.

        Layout:
        [+ Add DBC] [+ Add Folder] [Reload] [Remove] ─── Network name: [...] ─── [Export CSV]

        Buttons có màu sắc phân biệt:
        - Add DBC: xanh lá (d1e7dd) — hành động chính
        - Add Folder: xanh dương nhạt (cfe2ff)
        - Remove: đỏ nhạt (f8d7da) — hành động nguy hiểm
        - Reload / Export: mặc định (trắng)
        """
        bar = QHBoxLayout()
        bar.setSpacing(6)

        # Button: Thêm DBC file(s)
        add_btn = QPushButton("+ Add DBC File(s)")
        add_btn.setStyleSheet(
            "QPushButton{font-weight:bold;background:#d1e7dd;"
            "border:1px solid #a3cfbb;border-radius:4px;padding:4px 10px}"
            "QPushButton:hover{background:#badbcc}"
        )
        add_btn.clicked.connect(self._on_add_dbc)
        bar.addWidget(add_btn)

        # Button: Thêm folder chứa DBC files
        add_dir_btn = QPushButton("+ Add Folder")
        add_dir_btn.setStyleSheet(
            "QPushButton{background:#cfe2ff;border:1px solid #9ec5fe;"
            "border-radius:4px;padding:4px 10px}"
            "QPushButton:hover{background:#b6d4fe}"
        )
        add_dir_btn.clicked.connect(self._on_add_folder)
        bar.addWidget(add_dir_btn)

        # Button: Reload DBC files đã chọn
        reload_btn = QPushButton("Reload Selected")
        reload_btn.clicked.connect(self._on_reload_selected)
        bar.addWidget(reload_btn)

        # Button: Xoá DBC files đã chọn (màu đỏ cảnh báo)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.setStyleSheet(
            "QPushButton{background:#f8d7da;border:1px solid #f1aeb5;"
            "border-radius:4px;padding:4px 10px}"
            "QPushButton:hover{background:#f5c2c7}"
        )
        remove_btn.clicked.connect(self._on_remove_selected)
        bar.addWidget(remove_btn)

        bar.addSpacing(16)

        # Input: Network name override
        # Nếu để trống → dùng tên file DBC làm network name
        bar.addWidget(QLabel("Network name:"))
        self.net_name_edit = QLineEdit()
        self.net_name_edit.setPlaceholderText(
            "e.g. PT_Network or leave blank (uses DBC name)"
        )
        self.net_name_edit.setMaximumWidth(220)
        bar.addWidget(self.net_name_edit)

        bar.addStretch()

        # Button: Export tất cả signals ra CSV
        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self._on_export_csv)
        bar.addWidget(export_btn)

        return bar

    def _build_file_panel(self) -> QWidget:
        """
        Panel trái: danh sách DBC files theo network groups.

        TreeWidget 2 cấp:
        - Level 0: Network name (bold, nền xanh)
          - Level 1: DBC file name + msg count + sig count + status (OK/ERR)

        Supports multi-selection để reload/remove nhiều files cùng lúc.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # Header
        hdr = QLabel("Loaded DBC Files")
        hdr.setFont(QFont("", -1, QFont.Bold))
        hdr.setStyleSheet("padding:2px 4px;background:#e9ecef;border-radius:3px")
        layout.addWidget(hdr)

        # Tree widget
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels(["File / Network", "Msgs", "Sigs", "Status"])

        # Cột 0 co giãn, còn lại fixed width
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in _FILE_TREE_COL_WIDTHS.items():
            self.file_tree.header().setSectionResizeMode(col, QHeaderView.Fixed)
            self.file_tree.setColumnWidth(col, width)

        self.file_tree.setFont(QFont("Consolas", 9))
        self.file_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Khi chọn file/network → filter message tree tương ứng
        self.file_tree.itemSelectionChanged.connect(self._on_file_selected)
        layout.addWidget(self.file_tree)

        return w

    def _build_msg_panel(self) -> QWidget:
        """
        Panel giữa: danh sách messages với search/filter.

        Layout:
        [Search input] [✓ All Networks]
        ┌────────────────────────────────┐
        │ Network Name (bold, nền xanh)  │
        │   Message1  0x100  8  100ms    │
        │   Message2  0x101  8  Event    │
        │ Network Name 2                 │
        │   Message3  0x200  6  50ms     │
        └────────────────────────────────┘

        Search filter:
        - Theo tên message (case-insensitive)
        - Theo hex ID (e.g. "0x100")
        - Theo decimal ID (e.g. "256")
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # Search row
        search_row = QHBoxLayout()
        self.msg_search = QLineEdit()
        self.msg_search.setPlaceholderText("Filter by name or 0xID...")
        # [OPT-1] Connect trực tiếp tới _rebuild_msg_tree, bỏ wrapper _filter_messages
        self.msg_search.textChanged.connect(self._rebuild_msg_tree)
        search_row.addWidget(self.msg_search)

        self.show_all_cb = QCheckBox("All Networks")
        self.show_all_cb.setChecked(True)
        # [OPT-1] Connect trực tiếp
        self.show_all_cb.stateChanged.connect(self._rebuild_msg_tree)
        search_row.addWidget(self.show_all_cb)
        layout.addLayout(search_row)

        # Header
        hdr = QLabel("Messages")
        hdr.setFont(QFont("", -1, QFont.Bold))
        hdr.setStyleSheet("padding:2px 4px;background:#e9ecef;border-radius:3px")
        layout.addWidget(hdr)

        # Message tree (2 cấp: network → messages)
        self.msg_tree = QTreeWidget()
        self.msg_tree.setHeaderLabels([
            "Message", "0xID", "DLC", "Cycle", "Type", "Signals",
        ])
        self.msg_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in _MSG_TREE_COL_WIDTHS.items():
            self.msg_tree.header().setSectionResizeMode(col, QHeaderView.Fixed)
            self.msg_tree.setColumnWidth(col, width)

        self.msg_tree.setFont(QFont("Consolas", 9))
        self.msg_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.msg_tree.itemClicked.connect(self._on_msg_clicked)
        self.msg_tree.setAlternatingRowColors(True)
        layout.addWidget(self.msg_tree)

        return w

    def _build_detail_panel(self) -> QWidget:
        """
        Panel phải: thông tin chi tiết message + signal table + value definitions.

        3 phần:
        1. Message Info (GroupBox): tên, ID, DLC, cycle, send type, comment
        2. Signals (Table): tên, start bit, length, byte order, factor, offset, unit
        3. Value Definitions (TextEdit): choices mapping (raw → text)
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        # --- 1. Message Info ---
        self.msg_info_box = QGroupBox("Message Info")
        info_layout = QVBoxLayout()
        self.msg_info_label = QLabel("No message selected")
        self.msg_info_label.setWordWrap(True)
        self.msg_info_label.setFont(QFont("Consolas", 9))
        self.msg_info_label.setStyleSheet("color:#495057")
        info_layout.addWidget(self.msg_info_label)
        self.msg_info_box.setLayout(info_layout)
        layout.addWidget(self.msg_info_box)

        # --- 2. Signal Table ---
        sig_hdr = QLabel("Signals")
        sig_hdr.setFont(QFont("", -1, QFont.Bold))
        sig_hdr.setStyleSheet("padding:2px 4px;background:#e9ecef;border-radius:3px")
        layout.addWidget(sig_hdr)

        self.sig_table = QTableWidget()
        self.sig_table.setColumnCount(8)
        self.sig_table.setHorizontalHeaderLabels([
            "Signal", "Start", "Len", "Order", "Factor", "Offset", "Unit", "Comment",
        ])
        h = self.sig_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)   # Signal name
        h.setSectionResizeMode(7, QHeaderView.Stretch)    # Comment
        for col, width in _SIG_TABLE_COL_WIDTHS.items():
            h.setSectionResizeMode(col, QHeaderView.Fixed)
            self.sig_table.setColumnWidth(col, width)

        self.sig_table.setFont(QFont("Consolas", 9))
        self.sig_table.setAlternatingRowColors(True)
        self.sig_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sig_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.sig_table)

        # --- 3. Value Definitions ---
        val_hdr = QLabel("Value Definitions")
        val_hdr.setFont(QFont("", -1, QFont.Bold))
        val_hdr.setStyleSheet("padding:2px 4px;background:#e9ecef;border-radius:3px")
        layout.addWidget(val_hdr)

        self.choices_text = QTextEdit()
        self.choices_text.setReadOnly(True)
        self.choices_text.setFont(QFont("Consolas", 9))
        self.choices_text.setMaximumHeight(100)
        self.choices_text.setPlaceholderText("Select a signal to see value definitions...")
        layout.addWidget(self.choices_text)

        # Click signal row → hiện value definitions
        self.sig_table.itemClicked.connect(self._on_sig_clicked)

        return w

    def _build_tab_status_bar(self) -> QHBoxLayout:
        """
        Tạo status bar riêng cho DBC tab (dưới cùng).

        Layout:
        [Files: 2/3 loaded | Networks: 1 | Messages: 45]    [cantools: OK]
        """
        bar = QHBoxLayout()

        self.status_label = QLabel("No DBC files loaded")
        self.status_label.setFont(QFont("Consolas", 9))
        self.status_label.setStyleSheet("color:#6c757d")
        bar.addWidget(self.status_label)

        bar.addStretch()

        # Trạng thái cantools: OK (xanh) hoặc NOT INSTALLED (đỏ)
        status_text = "cantools: OK" if _CANTOOLS_AVAILABLE else "cantools: NOT INSTALLED"
        status_color = "#0a6e3c" if _CANTOOLS_AVAILABLE else "#842029"
        self.cantools_label = QLabel(status_text)
        self.cantools_label.setStyleSheet(
            f"color:{status_color};font-weight:bold;font-size:11px"
        )
        bar.addWidget(self.cantools_label)

        return bar

    # ==========================================================
    # FILE OPERATIONS — Load, reload, remove DBC files
    # ==========================================================

    def _on_add_dbc(self) -> None:
        """
        Mở file dialog để chọn 1 hoặc nhiều DBC files.

        Filter hiện *.dbc trước, cũng cho phép All Files.
        Sau khi chọn → bắt đầu background load.
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select DBC File(s)", "",
            "DBC Files (*.dbc);;All Files (*)",
        )
        if paths:
            self._load_dbc_paths(paths)

    def _on_add_folder(self) -> None:
        """
        Mở folder dialog, tìm tất cả *.dbc files recursively.

        Dùng rglob("*.dbc") nên tìm cả trong subfolders.
        Nếu không tìm thấy file nào → hiện thông báo.
        """
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder Containing DBC Files",
        )
        if not folder:
            return

        paths = [str(p) for p in Path(folder).rglob("*.dbc")]
        if not paths:
            QMessageBox.information(
                self, "No DBCs Found",
                f"Không tìm thấy file .dbc trong:\n{folder}",
            )
            return

        self._load_dbc_paths(paths)

    def _load_dbc_paths(self, paths: List[str]) -> None:
        """
        Bắt đầu background load cho danh sách file paths.

        Luồng:
        1. Tạo DbcFile objects cho các file chưa load
        2. Skip files đã có trong danh sách (tránh duplicate)
        3. Lấy network name từ input (hoặc dùng tên file)
        4. Hiện progress bar
        5. Start DbcLoadThread

        Args:
            paths: Danh sách đường dẫn DBC files cần load
        """
        net_override = self.net_name_edit.text().strip()

        # Tạo DbcFile objects, skip duplicates
        new_files: List[DbcFile] = []
        existing_paths = {str(f.path) for f in self._dbc_files}

        for p in paths:
            if p in existing_paths:
                logger.info("Đã load rồi: %s", p)
                continue
            net_name = net_override if net_override else Path(p).stem
            dbc = DbcFile(p, network=net_name)
            new_files.append(dbc)
            self._dbc_files.append(dbc)

        if not new_files:
            self.status_label.setText("Tất cả files đã được load.")
            return

        # Hiện progress bar
        self.progress_bar.setRange(0, len(new_files))
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        # Start background thread
        self._load_thread = DbcLoadThread(new_files, parent=self)
        self._load_thread.progress.connect(self._on_load_progress)
        self._load_thread.file_loaded.connect(self._on_file_loaded)
        self._load_thread.finished.connect(self._on_load_finished)
        self._load_thread.start()

    def _on_load_progress(self, current: int, total: int, name: str) -> None:
        """
        Slot nhận progress từ DbcLoadThread.

        Cập nhật progress bar và status text.

        Args:
            current: File thứ mấy đang load (1-based)
            total:   Tổng số files cần load
            name:    Tên file đang load
        """
        self.progress_bar.setValue(current)
        self.status_label.setText(f"Loading {current}/{total}: {name}...")

    def _on_file_loaded(self, dbc: DbcFile, success: bool) -> None:
        """
        Slot gọi khi 1 DBC file đã parse xong.

        Tạo hoặc cập nhật Network object, thêm DbcFile vào network.

        Args:
            dbc:     DbcFile vừa load xong
            success: True nếu parse thành công
        """
        # Tạo network nếu chưa có
        if dbc.network not in self._networks:
            self._networks[dbc.network] = Network(dbc.network)

        net = self._networks[dbc.network]
        net.dbc_files.append(dbc)
        # [OPT-4] Invalidate cache vì vừa thêm file mới
        net.invalidate_cache()

        if success:
            logger.info("Loaded: %s → %d msgs", dbc.path, dbc.msg_count)
        else:
            logger.warning("Failed: %s: %s", dbc.path, dbc.error)

        # Cập nhật file tree ngay (để user thấy progress)
        self._refresh_file_tree()

    def _on_load_finished(self) -> None:
        """
        Slot gọi khi TẤT CẢ DBC files đã load xong.

        Ẩn progress bar, rebuild message tree, thông báo cho MainWindow.

        [OPT-2] CHỈ emit signal, KHÔNG gọi main.on_networks_changed() trực tiếp.
        MainWindow đã connect slot vào signal này rồi.
        Gọi thêm trực tiếp = handler chạy 2 lần = BUG.
        """
        self.progress_bar.hide()
        self._load_thread = None
        self._update_status()
        self._rebuild_msg_tree()

        # Thông báo cho MainWindow đồng bộ network registry
        # (MainWindow._on_networks_changed slot sẽ xử lý)
        self.networks_changed.emit()

    def _on_reload_selected(self) -> None:
        """
        Reload các DBC files đang được chọn trong file tree.

        Dùng khi file DBC đã bị sửa bên ngoài (e.g. trong Vector CANdb++)
        và cần re-parse để cập nhật signal definitions.
        """
        items = self.file_tree.selectedItems()

        # Thu thập DbcFile objects từ selection
        to_reload: List[DbcFile] = []
        for item in items:
            dbc = item.data(0, Qt.UserRole)
            if isinstance(dbc, DbcFile):
                to_reload.append(dbc)

        if not to_reload:
            self.status_label.setText("Chọn DBC file(s) để reload")
            return

        # Invalidate cache cho các networks bị ảnh hưởng
        affected_nets = {dbc.network for dbc in to_reload}
        for net_name in affected_nets:
            if net_name in self._networks:
                self._networks[net_name].invalidate_cache()

        # Start reload thread
        self.progress_bar.setRange(0, len(to_reload))
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        self._load_thread = DbcLoadThread(to_reload, parent=self)
        self._load_thread.progress.connect(self._on_load_progress)
        self._load_thread.file_loaded.connect(
            lambda _dbc, _ok: self._refresh_file_tree()
        )
        self._load_thread.finished.connect(self._on_load_finished)
        self._load_thread.start()

    def _on_remove_selected(self) -> None:
        """
        Xoá DBC files hoặc toàn bộ networks đang được chọn.

        Nếu chọn 1 Network node → xoá tất cả DBC files trong đó.
        Nếu chọn DBC file nodes → xoá từng file riêng.
        Sau khi xoá → cleanup empty networks → emit signal.

        [OPT-2] CHỈ emit signal, bỏ gọi trực tiếp main.on_networks_changed().
        """
        items = self.file_tree.selectedItems()

        # Thu thập files cần xoá
        to_remove: List[DbcFile] = []
        for item in items:
            data = item.data(0, Qt.UserRole)
            if isinstance(data, DbcFile):
                to_remove.append(data)
            elif isinstance(data, Network):
                # Chọn network node → xoá tất cả files trong đó
                to_remove.extend(data.dbc_files)

        if not to_remove:
            return

        # Xoá files khỏi danh sách chính và khỏi network
        for dbc in to_remove:
            if dbc in self._dbc_files:
                self._dbc_files.remove(dbc)

            for net in self._networks.values():
                if dbc in net.dbc_files:
                    net.dbc_files.remove(dbc)
                    # [OPT-4] Invalidate cache
                    net.invalidate_cache()

        # Cleanup: xoá networks rỗng (không còn DBC file nào)
        empty_nets = [k for k, v in self._networks.items() if not v.dbc_files]
        for k in empty_nets:
            del self._networks[k]

        # Refresh UI
        self._refresh_file_tree()
        self._rebuild_msg_tree()
        self._update_status()

        # [OPT-2] Chỉ emit signal, MainWindow tự xử lý
        self.networks_changed.emit()

    # ==========================================================
    # FILE TREE — Hiển thị danh sách DBC files theo network
    # ==========================================================

    def _refresh_file_tree(self) -> None:
        """
        Rebuild toàn bộ file tree widget.

        Cấu trúc 2 cấp:
        - Level 0 (Network): tên network, tổng msgs/sigs, nền xanh lá
        - Level 1 (DbcFile): tên file, msgs/sigs, status (OK/ERR)

        Mỗi item lưu data object (Network hoặc DbcFile) trong Qt.UserRole
        để có thể truy xuất khi click.
        """
        self.file_tree.clear()

        for net_name, net in self._networks.items():
            # Network node (level 0)
            net_item = QTreeWidgetItem([
                net_name,
                str(net.msg_count),
                str(net.sig_count),
                "Network",
            ])
            net_item.setData(0, Qt.UserRole, net)
            net_item.setFont(0, QFont("", -1, QFont.Bold))
            net_item.setForeground(0, QBrush(QColor("#0a3622")))
            net_item.setBackground(0, QBrush(QColor("#d1e7dd")))

            # DBC file nodes (level 1)
            for dbc in net.dbc_files:
                status = "OK" if dbc.loaded else ("ERR" if dbc.error else "...")
                color = "#0a6e3c" if dbc.loaded else "#842029"

                child = QTreeWidgetItem([
                    dbc.name,
                    str(dbc.msg_count),
                    str(dbc.sig_count),
                    status,
                ])
                child.setData(0, Qt.UserRole, dbc)
                child.setToolTip(0, str(dbc.path))
                child.setForeground(3, QBrush(QColor(color)))
                if dbc.error:
                    child.setToolTip(3, dbc.error)
                net_item.addChild(child)

            self.file_tree.addTopLevelItem(net_item)
            net_item.setExpanded(True)

    def _on_file_selected(self) -> None:
        """
        Slot khi selection thay đổi trong file tree.

        Rebuild message tree để filter theo network/file đang chọn.
        """
        self._rebuild_msg_tree()

    # ==========================================================
    # MESSAGE TREE — Hiển thị messages với search/filter
    # ==========================================================

    def _rebuild_msg_tree(self) -> None:
        """
        Rebuild toàn bộ message tree với filter hiện tại.

        [OPT-1] Hàm này được connect trực tiếp tới:
        - msg_search.textChanged
        - show_all_cb.stateChanged
        - file_tree.itemSelectionChanged
        Bỏ wrapper _filter_messages() trung gian.

        Logic filter:
        1. Nếu "All Networks" checked → hiện tất cả networks
        2. Nếu unchecked → chỉ hiện networks đang chọn trong file tree
        3. Search text filter theo tên message, hex ID, hoặc decimal ID
        """
        self.msg_tree.clear()

        search_query = self.msg_search.text().lower().strip()
        show_all = self.show_all_cb.isChecked()

        # Xác định networks nào sẽ hiển thị
        selected_nets: set = set()
        if not show_all:
            for item in self.file_tree.selectedItems():
                data = item.data(0, Qt.UserRole)
                if isinstance(data, Network):
                    selected_nets.add(data.name)
                elif isinstance(data, DbcFile):
                    selected_nets.add(data.network)

        # Duyệt từng network, tạo tree items
        for net_name, net in self._networks.items():
            # Filter theo network selection
            if not show_all and selected_nets and net_name not in selected_nets:
                continue

            # Network node (level 0)
            net_item = QTreeWidgetItem([net_name, "", "", "", "", ""])
            net_item.setFont(0, QFont("", -1, QFont.Bold))
            net_item.setForeground(0, QBrush(QColor("#052c65")))
            net_item.setBackground(0, QBrush(QColor("#cfe2ff")))

            # Message nodes (level 1)
            for msg in net.database.messages:
                # Search filter
                if search_query:
                    match = (
                        search_query in msg.name.lower()
                        or search_query in f"0x{msg.frame_id:x}"
                        or search_query in str(msg.frame_id)
                    )
                    if not match:
                        continue

                # Lấy metadata từ DBC attributes
                cycle = getattr(msg, "cycle_time", None) or ""
                send_type = getattr(msg, "send_type", None) or ""
                sig_count = len(msg.signals)

                msg_item = QTreeWidgetItem([
                    msg.name,
                    f"0x{msg.frame_id:03X}",
                    str(msg.length),
                    f"{cycle}ms" if cycle else "",
                    str(send_type),
                    str(sig_count),
                ])
                # Lưu message object + network name để truy xuất sau
                msg_item.setData(0, Qt.UserRole, msg)
                msg_item.setData(0, Qt.UserRole + 1, net_name)

                # Tô màu send type "Event" (vàng cảnh báo)
                if "event" in str(send_type).lower():
                    msg_item.setForeground(4, QBrush(QColor("#664d03")))

                net_item.addChild(msg_item)

            # Chỉ thêm network node nếu có message con
            if net_item.childCount() > 0:
                self.msg_tree.addTopLevelItem(net_item)
                net_item.setExpanded(True)

    # [OPT-1] Đã xoá _filter_messages() — wrapper thừa chỉ gọi _rebuild_msg_tree()

    # ==========================================================
    # DETAIL PANEL — Hiển thị thông tin message + signals
    # ==========================================================

    def _on_msg_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        """
        Slot khi click vào 1 message trong message tree.

        Hiển thị:
        1. Message info: name, frame ID, DLC, cycle time, send type, comment
        2. Signal table: tất cả signals với metadata
        3. Highlight signals có auto-generator (nền vàng)

        [OPT-5] Import SignalTypeDetector 1 lần trước vòng lặp,
        thay vì import trong mỗi iteration.

        Args:
            item: TreeWidgetItem được click
            col:  Cột được click (không dùng)
        """
        msg = item.data(0, Qt.UserRole)

        # Skip nếu click vào network node (không có signals)
        if msg is None or not hasattr(msg, "signals"):
            return

        # --- 1. Message Info ---
        cycle = getattr(msg, "cycle_time", None)
        send_type = getattr(msg, "send_type", None)
        comment = getattr(msg, "comment", None)

        info_lines = [
            f"Name    : {msg.name}",
            f"Frame ID: 0x{msg.frame_id:03X}  ({msg.frame_id})",
            f"DLC     : {msg.length}",
        ]
        if cycle:
            info_lines.append(f"Cycle   : {cycle} ms")
        if send_type:
            info_lines.append(f"SendType: {send_type}")
        if comment:
            info_lines.append(f"Comment : {comment}")
        self.msg_info_label.setText("\n".join(info_lines))

        # --- 2. Signal Table ---
        # [OPT-5] Import 1 lần, không import trong vòng lặp
        from ui.tab_send_can import SignalTypeDetector

        self.sig_table.setRowCount(len(msg.signals))
        for r, sig in enumerate(msg.signals):
            # Xác định byte order
            bo = "BE" if "big" in str(getattr(sig, "byte_order", "")).lower() else "LE"
            unit = getattr(sig, "unit", "") or ""
            sig_comment = getattr(sig, "comment", "") or ""

            # Helper tạo table item với alignment
            def _make_item(text, align=Qt.AlignLeft) -> QTableWidgetItem:
                it = QTableWidgetItem(str(text))
                it.setTextAlignment(align | Qt.AlignVCenter)
                it.setData(Qt.UserRole, sig)
                return it

            # Điền các cột
            self.sig_table.setItem(r, 0, _make_item(sig.name))
            self.sig_table.setItem(r, 1, _make_item(
                getattr(sig, "start", 0), Qt.AlignCenter,
            ))
            self.sig_table.setItem(r, 2, _make_item(
                getattr(sig, "length", 0), Qt.AlignCenter,
            ))
            self.sig_table.setItem(r, 3, _make_item(bo, Qt.AlignCenter))

            factor = getattr(sig, "scale", 1.0)
            offset = getattr(sig, "offset", 0.0)
            self.sig_table.setItem(r, 4, _make_item(factor, Qt.AlignRight))
            self.sig_table.setItem(r, 5, _make_item(offset, Qt.AlignRight))
            self.sig_table.setItem(r, 6, _make_item(unit, Qt.AlignCenter))
            self.sig_table.setItem(r, 7, _make_item(sig_comment))

            # Highlight signals có auto-generator (nền vàng #fff3cd)
            gen = SignalTypeDetector.detect(sig.name)
            if gen != "None":
                highlight = QBrush(QColor("#fff3cd"))
                for c in range(self.sig_table.columnCount()):
                    it = self.sig_table.item(r, c)
                    if it:
                        it.setBackground(highlight)

        # Clear value definitions
        self.choices_text.clear()

    def _on_sig_clicked(self, item: QTableWidgetItem) -> None:
        """
        Slot khi click vào 1 signal trong signal table.

        Hiển thị value definitions (choices) nếu có.
        Ví dụ: signal "GearPosition" có choices:
            0 → Park
            1 → Reverse
            2 → Neutral
            3 → Drive

        Args:
            item: TableWidgetItem được click (chứa signal object trong UserRole)
        """
        sig = item.data(Qt.UserRole)
        if not sig:
            return

        choices = getattr(sig, "choices", None)
        if choices:
            try:
                lines = [f"  {k} → {v}" for k, v in choices.items()]
                self.choices_text.setPlainText("\n".join(lines))
            except Exception:
                self.choices_text.setPlainText(str(choices))
        else:
            self.choices_text.setPlainText("(No value definitions)")

    # ==========================================================
    # EXPORT CSV — Xuất danh sách signals ra file CSV
    # ==========================================================

    def _on_export_csv(self) -> None:
        """
        Export tất cả signals từ các DBC đã load ra file CSV.

        Format CSV: 1 row = 1 signal, với metadata message + signal.
        Dùng UTF-8 encoding để hỗ trợ ký tự đặc biệt.

        Nếu chưa có DBC nào loaded → hiện thông báo và return.
        """
        if not self._dbc_files:
            self.status_label.setText("Chưa load DBC nào để export")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Signal List", "signals.csv",
            "CSV Files (*.csv)",
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(_CSV_HEADER)

                for net_name, net in self._networks.items():
                    for msg in net.database.messages:
                        cycle = getattr(msg, "cycle_time", "")
                        send_type = getattr(msg, "send_type", "")

                        for sig in msg.signals:
                            bo = (
                                "BE"
                                if "big" in str(getattr(sig, "byte_order", "")).lower()
                                else "LE"
                            )
                            writer.writerow([
                                net_name,
                                msg.name,
                                f"0x{msg.frame_id:03X}",
                                msg.length,
                                cycle or "",
                                send_type or "",
                                sig.name,
                                getattr(sig, "start", 0),
                                getattr(sig, "length", 0),
                                bo,
                                getattr(sig, "scale", 1.0),
                                getattr(sig, "offset", 0.0),
                                getattr(sig, "unit", "") or "",
                                getattr(sig, "comment", "") or "",
                            ])

            self.status_label.setText(f"Exported to {path}")
            logger.info("CSV exported: %s", path)

        except Exception as e:
            self.status_label.setText(f"Export failed: {e}")
            logger.error("CSV export failed: %s", e)

    # ==========================================================
    # STATUS — Cập nhật status label
    # ==========================================================

    def _update_status(self) -> None:
        """
        Cập nhật status label với thông tin hiện tại.

        Hiển thị: số files loaded / tổng, số networks, tổng messages.
        """
        n_files = len(self._dbc_files)
        n_loaded = sum(1 for f in self._dbc_files if f.loaded)
        n_msgs = sum(f.msg_count for f in self._dbc_files if f.loaded)
        n_nets = len(self._networks)

        self.status_label.setText(
            f"Files: {n_loaded}/{n_files} loaded | "
            f"Networks: {n_nets} | Messages: {n_msgs}"
        )