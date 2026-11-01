"""
==============================================================
ui/tab_graphic.py — Signal Watch / Monitor Tab
==============================================================

Tab giám sát signal realtime kiểu TSmaster "Signals" panel.
Cho phép user chọn signals từ DBC database, thêm vào bảng watch,
hiển thị giá trị realtime được decode từ bus.

Layout:
┌─────────────────────────────────────────────────────────────────┐
│  [▶ START] [■ STOP]   Filter:[________]   [Clear All]          │
├──────────────────────┬──────────────────────────────────────────┤
│  DBC Signal Browser  │  Signal Watch Table                     │
│  ┌────────────────┐  │  ┌────┬──────┬────────────┬─────┬─────┐ │
│  │ ▸ Network_1    │  │  │ ■  │ Chn  │ Name       │  Y  │Unit │ │
│  │   ▸ Msg_0x126  │  │  │────┼──────┼────────────┼─────┼─────│ │
│  │     Signal_A   │  │  │ ■  │CAN 1 │ BMS_SOC    │34.8 │ %   │ │
│  │     Signal_B   │  │  │ ■  │CAN 1 │ BMS_SOH    │32.0 │ %   │ │
│  │   ▸ Msg_0x1A0  │  │  │ ■  │CAN 2 │ VCU_TrqReq │150  │ Nm  │ │
│  │     Signal_C   │  │  │ ...│      │            │     │     │ │
│  └────────────────┘  │  └────┴──────┴────────────┴─────┴─────┘ │
│  [Add Selected >>]   │  Total: 3 signals  |  Update: 200ms     │
├──────────────────────┴──────────────────────────────────────────┤
│  All: 0  │  Updated: 0  │  Errors: 0  │  ● RUNNING             │
└─────────────────────────────────────────────────────────────────┘

Tích hợp:
- [INTEG-1] Lấy bus từ send_tab._bus (shared) để recv CAN frames
- [INTEG-2] Dùng main.networks cho DBC signal browser + decode
- [INTEG-3] Mỗi signal trong watch table có màu riêng (color indicator)
"""

import logging
import time
import collections
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QAbstractItemView, QMenu, QComboBox,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QMutex, QMutexLocker
from PyQt5.QtGui import QFont, QColor, QBrush

logger = logging.getLogger(__name__)

_CAN_AVAILABLE = False
try:
    import can
    _CAN_AVAILABLE = True
except ImportError:
    can = None


# ==============================================================
# CONSTANTS
# ==============================================================

_RECV_TIMEOUT_S = 0.004       # Timeout recv() (4ms)
_UI_REFRESH_MS  = 200         # Chu kỳ cập nhật table values
_STAT_REFRESH_MS = 500        # Chu kỳ cập nhật stats
_BATCH_SIZE     = 64          # Frames xử lý mỗi tick
_BUFFER_MAX     = 10_000      # Buffer tối đa

# WinError codes gây ra lỗi fatal (bus handle bị đóng)
_FATAL_WINERRORS = {6, 1, 5}   # Invalid handle, Incorrect function, Access denied
_FATAL_ERRNOS    = {9}          # EBADF

# Bảng màu cho signal indicators (xoay vòng)
_SIGNAL_COLORS = [
    QColor(0x1E, 0x88, 0xE5),
    QColor(0xE5, 0x39, 0x35),
    QColor(0x43, 0xA0, 0x47),
    QColor(0xFB, 0x8C, 0x00),
    QColor(0x8E, 0x24, 0xAA),
    QColor(0x00, 0x97, 0xA7),
    QColor(0xD8, 0x1B, 0x60),
    QColor(0x55, 0x6B, 0x2F),
    QColor(0x3F, 0x51, 0xB5),
    QColor(0xFF, 0x6F, 0x00),
    QColor(0x00, 0x69, 0x5C),
    QColor(0x6A, 0x1B, 0x9A),
]


# ==============================================================
# WATCH ENTRY — 1 signal đang được giám sát
# ==============================================================

@dataclass
class WatchEntry:
    """
    Đại diện 1 signal trong watch table.

    Attributes:
        network:      Tên network (từ DBC)
        message_name: Tên message chứa signal
        frame_id:     CAN arbitration ID
        signal_name:  Tên signal
        unit:         Đơn vị (từ DBC)
        comment:      Comment (từ DBC)
        color:        Màu indicator
        value:        Giá trị hiện tại (str hiển thị)
        raw_value:    Giá trị raw (số hoặc str)
        channel:      Channel nhận được
        last_update:  Timestamp lần cập nhật cuối
        update_count: Đếm số lần cập nhật
    """
    network: str
    message_name: str
    frame_id: int
    signal_name: str
    unit: str = ""
    comment: str = ""
    color: QColor = field(default_factory=lambda: QColor(0x1E, 0x88, 0xE5))
    value: str = "–"
    raw_value: Any = None
    channel: str = ""
    last_update: float = 0.0
    update_count: int = 0


# ==============================================================
# HELPER — Phân loại lỗi fatal vs transient
# ==============================================================

def _is_fatal_bus_error(exc: Exception) -> bool:
    """
    Trả về True nếu exception là lỗi fatal về bus handle.

    WinError 6 (ERROR_INVALID_HANDLE) xảy ra khi bus bị close
    bởi SendCanTab trong khi receiver thread vẫn đang recv().
    """
    winerror = getattr(exc, "winerror", None)
    errno_val = getattr(exc, "errno", None)
    err_str = str(exc).lower()

    if winerror in _FATAL_WINERRORS:
        return True
    if errno_val in _FATAL_ERRNOS:
        return True
    if any(kw in err_str for kw in ("invalid handle", "winerror 6", "bad file descriptor")):
        return True
    return False


# ==============================================================
# SIGNAL RECEIVER THREAD — Decode frames, cập nhật watch entries
# ==============================================================

class SignalReceiver(QThread):
    """
    Thread nhận CAN frames, decode signals, emit cập nhật.

    Dùng chung bus + mutex với SendCanTab và TraceTab.
    Chỉ decode signals có trong watch list (tối ưu hiệu năng).

    FIX [WinError 6]:
    - Phân biệt lỗi fatal (bus bị đóng) vs transient
    - Khi fatal → emit error_occurred + thoát loop ngay (không spam log)

    Signals:
        signals_updated(dict): {(frame_id, signal_name): decoded_value}
        error_occurred(str):   Lỗi recv
    """

    signals_updated = pyqtSignal(dict)
    error_occurred  = pyqtSignal(str)

    def __init__(
        self,
        bus,
        bus_mutex: QMutex,
        networks: Dict,
        watch_frame_ids: set,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bus          = bus
        self._mutex        = bus_mutex
        self._networks     = networks
        self._watch_fids   = watch_frame_ids
        self._running: bool = True

        self._batch: Dict[Tuple, Any] = {}
        self._last_emit: float = 0.0

    def stop(self) -> None:
        self._running = False

    def update_watch_ids(self, fids: set) -> None:
        """Cập nhật danh sách frame IDs cần theo dõi (gọi từ UI thread)."""
        self._watch_fids = fids

    def run(self) -> None:
        """
        Loop nhận frames, decode signals trong watch list.

        FIX: Phát hiện WinError 6 (invalid handle) → dừng ngay,
        không tiếp tục spam exception mỗi 4ms.
        """
        while self._running:
            try:
                with QMutexLocker(self._mutex):
                    if not self._running:
                        break
                    msg = self._bus.recv(timeout=_RECV_TIMEOUT_S)

                if msg is None:
                    self._try_emit_batch()
                    continue

                fid = msg.arbitration_id
                if fid not in self._watch_fids:
                    self._try_emit_batch()
                    continue

                decoded = self._decode_frame(fid, bytes(msg.data))
                if decoded:
                    for sig_name, val in decoded.items():
                        self._batch[(fid, sig_name)] = val

                self._try_emit_batch()

            except Exception as e:
                if not self._running:
                    break  # Đang dừng, bỏ qua

                if _is_fatal_bus_error(e):
                    # Bus handle bị đóng → dừng hẳn, không loop lại
                    logger.warning(
                        "SignalReceiver: bus handle invalid, stopping. (%s)", e
                    )
                    self._running = False
                    self.error_occurred.emit(str(e))
                    break
                else:
                    # Lỗi tạm thời → log và tiếp tục
                    logger.warning("SignalReceiver transient error: %s", e)

        # Emit batch còn lại trước khi thoát
        if self._batch:
            self.signals_updated.emit(dict(self._batch))
            self._batch.clear()

    def _try_emit_batch(self) -> None:
        """Emit batch nếu đủ thời gian (~50ms interval)."""
        now = time.time()
        if self._batch and (now - self._last_emit) > 0.05:
            self.signals_updated.emit(dict(self._batch))
            self._batch.clear()
            self._last_emit = now

    def _decode_frame(self, frame_id: int, data: bytes) -> Dict[str, Any]:
        """Decode signals từ CAN data bằng DBC database."""
        result: Dict[str, Any] = {}
        try:
            for net_name, net in self._networks.items():
                if not net or not getattr(net, "database", None):
                    continue
                md = net.database.get_message_by_frame_id(frame_id)
                if not md:
                    continue
                decoded = md.decode(data, decode_choices=True)
                for sig_name, val in decoded.items():
                    if isinstance(val, float):
                        result[sig_name] = round(val, 3)
                    else:
                        result[sig_name] = val
                return result
        except Exception:
            pass
        return result


# ==============================================================
# DEMO SIGNAL GENERATOR
# ==============================================================

class DemoSignalGenerator(QThread):
    """Tạo signal values giả khi không có hardware."""

    signals_updated = pyqtSignal(dict)
    error_occurred  = pyqtSignal(str)

    def __init__(self, watch_entries: List[WatchEntry], parent=None) -> None:
        super().__init__(parent)
        self._entries       = watch_entries
        self._running: bool = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        import random
        while self._running:
            batch: Dict[Tuple, Any] = {}
            for entry in self._entries:
                if entry.unit == "%":
                    val = round(random.uniform(0, 100), 1)
                elif entry.unit in ("A", "Nm"):
                    val = round(random.uniform(-200, 350), 1)
                elif entry.unit in ("rpm",):
                    val = round(random.uniform(0, 12000), 0)
                elif entry.unit in ("°C",):
                    val = round(random.uniform(20, 120), 1)
                elif entry.unit in ("km/h",):
                    val = round(random.uniform(0, 220), 1)
                elif entry.unit in ("V",):
                    val = round(random.uniform(300, 420), 1)
                elif entry.unit in ("°",):
                    val = round(random.uniform(-780, 780), 1)
                else:
                    val = round(random.uniform(0, 255), 2)
                batch[(entry.frame_id, entry.signal_name)] = val

            if batch:
                self.signals_updated.emit(batch)
            time.sleep(0.1)


# ==============================================================
# STYLE SHEET
# ==============================================================

STYLE = """
QWidget {
    background: #FFFFFF;
    color: #1a1a1a;
    font-family: 'Segoe UI', 'Helvetica Neue', 'Arial', sans-serif;
    font-size: 13px;
}
#topBar {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #F8F8F8, stop:1 #ECECEC);
    border-bottom: 1px solid #B8B8B8;
}
#btnStart {
    background: #E8F5E9; color: #1B5E20;
    border: 1px solid #4CAF50; border-radius: 4px;
    font-weight: 700; font-size: 13px;
    padding: 5px 20px; min-width: 76px;
}
#btnStart:hover { background: #C8E6C9; }
#btnStart:disabled { background: #E8E8E8; color: #A0A0A0; border-color: #C0C0C0; }
#btnStop {
    background: #FFEBEE; color: #B71C1C;
    border: 1px solid #E53935; border-radius: 4px;
    font-weight: 700; font-size: 13px;
    padding: 5px 20px; min-width: 76px;
}
#btnStop:hover { background: #FFCDD2; }
#btnStop:disabled { background: #E8E8E8; color: #A0A0A0; border-color: #C0C0C0; }
#filterEdit {
    background: white; border: 1px solid #B0B0B0; border-radius: 3px;
    padding: 3px 8px; font-size: 12px; min-width: 160px;
}
#filterEdit:focus { border: 2px solid #1E88E5; padding: 2px 7px; }
#dbcTree {
    background: #FFFFFF; alternate-background-color: #F5F7FA;
    border: 1px solid #D0D0D0; border-radius: 3px;
    font-family: 'Segoe UI', sans-serif; font-size: 12px;
    selection-background-color: #BBDEFB; selection-color: #0D47A1;
}
#dbcTree::item { height: 22px; padding: 1px 4px; }
#dbcTree::item:selected { background: #BBDEFB; }
#dbcTree::item:hover:!selected { background: #E3F2FD; }
#watchTable {
    background: #FFFFFF; alternate-background-color: #F8F9FA;
    border: 1px solid #D0D0D0; border-radius: 3px;
    font-family: 'Cascadia Mono', 'Consolas', 'Courier New', monospace;
    font-size: 12px;
    selection-background-color: #BBDEFB; selection-color: #0D47A1;
    gridline-color: #E8E8E8;
}
#watchTable::item { height: 24px; padding: 2px 4px; }
#watchTable::item:selected { background: #BBDEFB; }
QHeaderView::section {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #F8F8F8, stop:1 #E8E8E8);
    color: #333; border: none;
    border-right: 1px solid #C8C8C8; border-bottom: 2px solid #999;
    padding: 5px 6px;
    font-family: 'Segoe UI', sans-serif; font-size: 12px; font-weight: 600;
}
QHeaderView::section:hover { background: #E0E4E8; }
#bottomBar {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #F2F2F2, stop:1 #E8E8E8);
    border-top: 1px solid #C0C0C0;
}
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #C0C0C0; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #999; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: #C0C0C0; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QPushButton {
    border: 1px solid #B0B0B0; border-radius: 3px;
    padding: 4px 12px; background: #FAFAFA;
    font-family: 'Segoe UI', sans-serif; font-size: 12px; font-weight: 500;
}
QPushButton:hover { background: #EFEFEF; }
QPushButton:pressed { background: #E0E0E0; }
QComboBox {
    background: white; border: 1px solid #B0B0B0; border-radius: 3px;
    padding: 3px 8px; font-size: 12px;
}
"""

# Watch table column layout
_WATCH_COLUMNS = [
    ("",          28),
    ("Chn",       60),
    ("Message",  130),
    ("Signal",   160),
    ("Value",    100),
    ("Unit",      50),
    ("Comment",  200),
]
_WCOL_COLOR   = 0
_WCOL_CHN     = 1
_WCOL_MSG     = 2
_WCOL_SIG     = 3
_WCOL_VAL     = 4
_WCOL_UNIT    = 5
_WCOL_COMMENT = 6
_WNC = len(_WATCH_COLUMNS)


# ==============================================================
# GRAPHIC TAB — Signal Watch / Monitor
# ==============================================================

class GraphicTab(QWidget):
    """
    Tab giám sát signal realtime.

    FIX [WinError 6]:
    - SignalReceiver tự detect fatal bus error → emit error_occurred → _on_error → _stop()
    - Kết nối send_tab.bus_disconnected (nếu có) để dừng receiver khi bus bị đóng
    - Không spam log khi bus invalid

    MUTUAL EXCLUSION:
    - Emit monitor_started khi bắt đầu → TraceTab tự _stop()
    - Lắng nghe TraceTab.monitor_started → tự _stop() nếu đang chạy
    - Kết nối chéo được thực hiện ở MainWindow sau khi cả 2 tab được tạo
    """

    # Signal phát khi tab này bắt đầu monitor
    monitor_started = pyqtSignal()

    def __init__(self, main_window: "MainWindow") -> None:
        super().__init__()
        self.main = main_window

        self._running: bool = False
        self._receiver: Optional[QThread] = None
        self._color_idx: int = 0

        self._watch: Dict[Tuple[int, str], WatchEntry] = {}
        self._pending: collections.deque = collections.deque(maxlen=_BUFFER_MAX)

        self._n_updates: int = 0
        self._n_errors: int  = 0

        self._build_ui()
        self.setStyleSheet(STYLE)

        self._ui_timer = QTimer()
        self._ui_timer.setInterval(_UI_REFRESH_MS)
        self._ui_timer.timeout.connect(self._refresh_table_values)

        self._stat_timer = QTimer()
        self._stat_timer.setInterval(_STAT_REFRESH_MS)
        self._stat_timer.timeout.connect(self._refresh_stats)

        if hasattr(self.main, "networks_changed"):
            self.main.networks_changed.connect(self._refresh_dbc_browser)

        # FIX: Kết nối bus_disconnected từ SendCanTab (nếu tồn tại)
        self._connect_send_tab_signals()

    def _connect_send_tab_signals(self) -> None:
        """Kết nối signal bus_disconnected từ SendCanTab nếu có."""
        send_tab = getattr(self.main, "send_tab", None)
        if send_tab and hasattr(send_tab, "bus_disconnected"):
            try:
                send_tab.bus_disconnected.connect(self._on_bus_disconnected)
            except Exception:
                pass

    def stop_for_exclusive_mode(self) -> None:
        """
        Dừng tab này vì tab khác (TraceTab) vừa bắt đầu.
        Gọi từ MainWindow khi TraceTab emit monitor_started.
        """
        if self._running:
            logger.info("GraphicTab: stopped because TraceTab started.")
            self._stop()

    def _on_bus_disconnected(self) -> None:
        """
        Slot nhận thông báo bus đã bị đóng từ SendCanTab.

        Dừng receiver ngay lập tức trước khi WinError 6 xảy ra.
        """
        if self._running:
            logger.info("GraphicTab: bus disconnected, stopping receiver.")
            self._stop()

    # ==========================================================
    # BUILD UI
    # ==========================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._mk_top_bar())

        spl = QSplitter(Qt.Horizontal)
        spl.setStyleSheet("QSplitter::handle{background:#D0D0D0;width:2px;}")
        spl.addWidget(self._mk_browser_panel())
        spl.addWidget(self._mk_watch_panel())
        spl.setSizes([260, 780])
        spl.setStretchFactor(0, 0)
        spl.setStretchFactor(1, 1)
        root.addWidget(spl, 1)

        root.addWidget(self._mk_bottom_bar())

    def _mk_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(42)
        ly = QHBoxLayout(bar)
        ly.setContentsMargins(8, 5, 8, 5)
        ly.setSpacing(8)

        self._btn_start = QPushButton("START")
        self._btn_start.setObjectName("btnStart")
        self._btn_start.clicked.connect(self._start)
        ly.addWidget(self._btn_start)

        self._btn_stop = QPushButton("STOP")
        self._btn_stop.setObjectName("btnStop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop)
        ly.addWidget(self._btn_stop)

        ly.addSpacing(12)
        title = QLabel("Signal Watch")
        title.setStyleSheet(
            "color:#1a1a1a; font-size:14px; font-weight:700; "
            "font-family:'Segoe UI',sans-serif;"
        )
        ly.addWidget(title)
        ly.addStretch()

        ly.addWidget(QLabel("Filter:"))
        self._filter = QLineEdit()
        self._filter.setObjectName("filterEdit")
        self._filter.setPlaceholderText("Signal or message name...")
        self._filter.textChanged.connect(self._filter_dbc_browser)
        ly.addWidget(self._filter)

        ly.addSpacing(8)
        clr_btn = QPushButton("Clear All")
        clr_btn.setStyleSheet(
            "QPushButton{background:#FFF3E0;border:1px solid #FF9800;"
            "border-radius:3px;padding:4px 12px;font-weight:600;}"
            "QPushButton:hover{background:#FFE0B2;}"
        )
        clr_btn.clicked.connect(self._clear_all)
        ly.addWidget(clr_btn)

        return bar

    def _mk_browser_panel(self) -> QWidget:
        w = QWidget()
        ly = QVBoxLayout(w)
        ly.setContentsMargins(4, 4, 4, 4)
        ly.setSpacing(4)

        hdr = QLabel("DBC Signals")
        hdr.setStyleSheet(
            "font-weight:700; font-size:13px; color:#1565C0; "
            "padding:2px 4px; font-family:'Segoe UI',sans-serif;"
        )
        ly.addWidget(hdr)

        self._dbc_tree = QTreeWidget()
        self._dbc_tree.setObjectName("dbcTree")
        self._dbc_tree.setHeaderLabels(["Signal / Message / Network"])
        self._dbc_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._dbc_tree.setAlternatingRowColors(True)
        self._dbc_tree.setRootIsDecorated(True)
        self._dbc_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self._dbc_tree.setFont(QFont("Segoe UI", 11))
        self._dbc_tree.itemDoubleClicked.connect(
            lambda item, _: self._add_item_to_watch(item)
        )
        ly.addWidget(self._dbc_tree)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Selected >>")
        add_btn.setStyleSheet(
            "QPushButton{background:#E3F2FD;border:1px solid #1E88E5;"
            "border-radius:4px;padding:5px 14px;font-weight:600;color:#0D47A1;}"
            "QPushButton:hover{background:#BBDEFB;}"
        )
        add_btn.clicked.connect(self._add_selected_to_watch)
        btn_row.addWidget(add_btn)

        add_all_btn = QPushButton("Add All Msg >>")
        add_all_btn.setToolTip("Add tất cả signals của message đang chọn")
        add_all_btn.clicked.connect(self._add_all_msg_signals)
        btn_row.addWidget(add_all_btn)

        btn_row.addStretch()
        ly.addLayout(btn_row)

        return w

    def _mk_watch_panel(self) -> QWidget:
        w = QWidget()
        ly = QVBoxLayout(w)
        ly.setContentsMargins(4, 4, 4, 4)
        ly.setSpacing(4)

        hdr = QLabel("Watched Signals")
        hdr.setStyleSheet(
            "font-weight:700; font-size:13px; color:#1565C0; "
            "padding:2px 4px; font-family:'Segoe UI',sans-serif;"
        )
        ly.addWidget(hdr)

        self._watch_table = QTableWidget()
        self._watch_table.setObjectName("watchTable")
        self._watch_table.setColumnCount(_WNC)
        self._watch_table.setHorizontalHeaderLabels([c[0] for c in _WATCH_COLUMNS])
        self._watch_table.setAlternatingRowColors(True)
        self._watch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._watch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._watch_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._watch_table.customContextMenuRequested.connect(self._ctx_menu)
        self._watch_table.verticalHeader().setVisible(False)

        h = self._watch_table.horizontalHeader()
        for col, (_, width) in enumerate(_WATCH_COLUMNS):
            if col in (_WCOL_SIG, _WCOL_COMMENT):
                h.setSectionResizeMode(col, QHeaderView.Stretch)
            else:
                h.setSectionResizeMode(col, QHeaderView.Fixed)
                self._watch_table.setColumnWidth(col, width)

        ly.addWidget(self._watch_table)

        info_row = QHBoxLayout()
        self._info_lbl = QLabel("Total: 0 signals")
        self._info_lbl.setStyleSheet(
            "color:#666; font-size:11px; font-family:'Segoe UI',sans-serif;"
        )
        info_row.addWidget(self._info_lbl)
        info_row.addStretch()

        rm_btn = QPushButton("Remove Selected")
        rm_btn.setStyleSheet(
            "QPushButton{background:#FFEBEE;border:1px solid #EF5350;"
            "border-radius:3px;padding:3px 10px;font-size:11px;}"
            "QPushButton:hover{background:#FFCDD2;}"
        )
        rm_btn.clicked.connect(self._remove_selected)
        info_row.addWidget(rm_btn)
        ly.addLayout(info_row)

        return w

    def _mk_bottom_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("bottomBar")
        bar.setFixedHeight(28)
        ly = QHBoxLayout(bar)
        ly.setContentsMargins(8, 0, 8, 0)
        ly.setSpacing(0)

        def _sep() -> QFrame:
            f = QFrame()
            f.setFrameShape(QFrame.VLine)
            f.setStyleSheet("color:#C0C0C0;")
            f.setFixedSize(1, 18)
            return f

        def _lbl(text: str) -> QLabel:
            l = QLabel(text)
            l.setStyleSheet(
                "color:#444; font-size:12px; font-weight:500; "
                "padding:0 10px; font-family:'Segoe UI',sans-serif;"
            )
            return l

        self._watch_cnt_lbl  = _lbl("Signals: 0")
        self._update_cnt_lbl = _lbl("Updated: 0")
        self._err_cnt_lbl    = _lbl("Errors: 0")

        for widget in (self._watch_cnt_lbl, _sep(),
                       self._update_cnt_lbl, _sep(),
                       self._err_cnt_lbl, _sep()):
            ly.addWidget(widget)

        self._run_lbl = QLabel("■ STOPPED")
        self._run_lbl.setStyleSheet(
            "color:#C62828; font-size:12px; font-weight:700; "
            "padding:0 10px; font-family:'Segoe UI',sans-serif;"
        )
        ly.addWidget(self._run_lbl)
        ly.addStretch()

        return bar

    # ==========================================================
    # DBC BROWSER
    # ==========================================================

    def _refresh_dbc_browser(self) -> None:
        self._dbc_tree.clear()
        networks = getattr(self.main, "networks", {})

        net_font = QFont("Segoe UI", 11, QFont.Bold)
        msg_font = QFont("Segoe UI", 11)
        sig_font = QFont("Cascadia Mono", 11)
        sig_font.setStyleHint(QFont.Monospace)

        for net_name, net in networks.items():
            if not net or not getattr(net, "database", None):
                continue

            net_item = QTreeWidgetItem([net_name])
            net_item.setFont(0, net_font)
            net_item.setForeground(0, QBrush(QColor("#0D47A1")))

            for msg in net.database.messages:
                msg_text = f"{msg.name}  (0x{msg.frame_id:03X})  DLC:{msg.length}"
                msg_item = QTreeWidgetItem([msg_text])
                msg_item.setFont(0, msg_font)
                msg_item.setForeground(0, QBrush(QColor("#333")))
                msg_item.setData(0, Qt.UserRole, {
                    "type": "message",
                    "network": net_name,
                    "message": msg,
                })

                for sig in msg.signals:
                    unit    = getattr(sig, "unit", "") or ""
                    comment = getattr(sig, "comment", "") or ""
                    sig_text = f"{sig.name}"
                    if unit:
                        sig_text += f"  [{unit}]"

                    sig_item = QTreeWidgetItem([sig_text])
                    sig_item.setFont(0, sig_font)
                    sig_item.setForeground(0, QBrush(QColor("#1a1a1a")))
                    sig_item.setToolTip(0, comment if comment else sig.name)
                    sig_item.setData(0, Qt.UserRole, {
                        "type": "signal",
                        "network": net_name,
                        "message": msg,
                        "signal": sig,
                    })
                    msg_item.addChild(sig_item)

                net_item.addChild(msg_item)

            self._dbc_tree.addTopLevelItem(net_item)
            net_item.setExpanded(True)

    def _filter_dbc_browser(self, text: str) -> None:
        q = text.strip().lower()
        for ni in range(self._dbc_tree.topLevelItemCount()):
            net_item = self._dbc_tree.topLevelItem(ni)
            net_visible = False
            for mi in range(net_item.childCount()):
                msg_item = net_item.child(mi)
                msg_visible = False
                for si in range(msg_item.childCount()):
                    sig_item = msg_item.child(si)
                    if not q:
                        sig_item.setHidden(False)
                        msg_visible = True
                    else:
                        match = (
                            q in sig_item.text(0).lower()
                            or q in msg_item.text(0).lower()
                        )
                        sig_item.setHidden(not match)
                        if match:
                            msg_visible = True
                msg_item.setHidden(not msg_visible)
                if msg_visible:
                    net_visible = True
                    if q:
                        msg_item.setExpanded(True)
            net_item.setHidden(not net_visible)

    # ==========================================================
    # ADD SIGNALS TO WATCH
    # ==========================================================

    def _next_color(self) -> QColor:
        color = _SIGNAL_COLORS[self._color_idx % len(_SIGNAL_COLORS)]
        self._color_idx += 1
        return color

    def _add_item_to_watch(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data["type"] == "signal":
            self._add_signal_entry(data)
        elif data["type"] == "message":
            for i in range(item.childCount()):
                child_data = item.child(i).data(0, Qt.UserRole)
                if child_data and child_data["type"] == "signal":
                    self._add_signal_entry(child_data)

    def _add_signal_entry(self, data: Dict) -> None:
        msg      = data["message"]
        sig      = data["signal"]
        net_name = data["network"]

        key = (msg.frame_id, sig.name)
        if key in self._watch:
            return

        entry = WatchEntry(
            network=net_name,
            message_name=msg.name,
            frame_id=msg.frame_id,
            signal_name=sig.name,
            unit=getattr(sig, "unit", "") or "",
            comment=getattr(sig, "comment", "") or "",
            color=self._next_color(),
        )
        self._watch[key] = entry
        self._rebuild_watch_table()

    def _add_selected_to_watch(self) -> None:
        for item in self._dbc_tree.selectedItems():
            self._add_item_to_watch(item)

    def _add_all_msg_signals(self) -> None:
        for item in self._dbc_tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if not data:
                for mi in range(item.childCount()):
                    self._add_item_to_watch(item.child(mi))
            elif data["type"] == "message":
                self._add_item_to_watch(item)
            elif data["type"] == "signal":
                parent = item.parent()
                if parent:
                    self._add_item_to_watch(parent)

    # ==========================================================
    # WATCH TABLE
    # ==========================================================

    def _rebuild_watch_table(self) -> None:
        self._watch_table.setRowCount(len(self._watch))

        mono_font = QFont("Cascadia Mono", 11)
        mono_font.setStyleHint(QFont.Monospace)
        seg_font = QFont("Segoe UI", 11)

        for row, ((fid, sig_name), entry) in enumerate(self._watch.items()):
            color_item = QTableWidgetItem("")
            color_item.setBackground(QBrush(entry.color))
            color_item.setFlags(color_item.flags() & ~Qt.ItemIsSelectable)
            self._watch_table.setItem(row, _WCOL_COLOR, color_item)

            chn_item = QTableWidgetItem(entry.network)
            chn_item.setFont(seg_font)
            chn_item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
            self._watch_table.setItem(row, _WCOL_CHN, chn_item)

            msg_item = QTableWidgetItem(entry.message_name)
            msg_item.setFont(seg_font)
            self._watch_table.setItem(row, _WCOL_MSG, msg_item)

            sig_item = QTableWidgetItem(entry.signal_name)
            sig_item.setFont(QFont("Segoe UI", 11, QFont.Bold))
            sig_item.setForeground(QBrush(entry.color))
            self._watch_table.setItem(row, _WCOL_SIG, sig_item)

            val_item = QTableWidgetItem(entry.value)
            val_item.setFont(QFont("Cascadia Mono", 12, QFont.Bold))
            val_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val_item.setForeground(QBrush(QColor("#1a1a1a")))
            self._watch_table.setItem(row, _WCOL_VAL, val_item)

            unit_item = QTableWidgetItem(entry.unit)
            unit_item.setFont(seg_font)
            unit_item.setForeground(QBrush(QColor("#666")))
            unit_item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
            self._watch_table.setItem(row, _WCOL_UNIT, unit_item)

            cmt_item = QTableWidgetItem(entry.comment)
            cmt_item.setFont(QFont("Segoe UI", 10))
            cmt_item.setForeground(QBrush(QColor("#888")))
            self._watch_table.setItem(row, _WCOL_COMMENT, cmt_item)

        self._info_lbl.setText(f"Total: {len(self._watch)} signals")
        self._watch_cnt_lbl.setText(f"Signals: {len(self._watch)}")

    def _refresh_table_values(self) -> None:
        while self._pending:
            batch = self._pending.popleft()
            for (fid, sig_name), val in batch.items():
                key   = (fid, sig_name)
                entry = self._watch.get(key)
                if not entry:
                    continue
                entry.raw_value    = val
                entry.value        = self._format_value(val)
                entry.last_update  = time.time()
                entry.update_count += 1
                self._n_updates   += 1

        for row, entry in enumerate(self._watch.values()):
            val_item = self._watch_table.item(row, _WCOL_VAL)
            if val_item:
                old_text = val_item.text()
                new_text = entry.value
                val_item.setText(new_text)
                if old_text != new_text and entry.last_update > 0:
                    age = time.time() - entry.last_update
                    if age < 0.5:
                        val_item.setForeground(QBrush(entry.color))
                    else:
                        val_item.setForeground(QBrush(QColor("#1a1a1a")))

    @staticmethod
    def _format_value(val) -> str:
        if isinstance(val, str):
            return val
        if isinstance(val, float):
            if abs(val) >= 1000:
                return f"{val:,.1f}"
            v = f"{val:.3f}".rstrip("0").rstrip(".")
            return v if "." in v else f"{val:.1f}"
        if isinstance(val, int):
            return str(val)
        return str(val)

    # ==========================================================
    # REMOVE / CLEAR
    # ==========================================================

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._watch_table.selectedIndexes()},
                      reverse=True)
        keys = list(self._watch.keys())
        for r in rows:
            if r < len(keys):
                del self._watch[keys[r]]
        self._rebuild_watch_table()

    def _clear_all(self) -> None:
        self._watch.clear()
        self._color_idx = 0
        self._n_updates = 0
        self._n_errors  = 0
        self._rebuild_watch_table()

    def _ctx_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;border:1px solid #B0B0B0;}"
            "QMenu::item{padding:5px 18px;font-size:12px;}"
            "QMenu::item:selected{background:#BBDEFB;}"
        )
        menu.addAction("Remove Selected", self._remove_selected)
        menu.addAction("Clear All", self._clear_all)
        menu.exec_(self._watch_table.viewport().mapToGlobal(pos))

    # ==========================================================
    # START / STOP
    # ==========================================================

    def _start(self) -> None:
        """
        Bắt đầu giám sát signals.

        FIX: Kết nối error_occurred từ SignalReceiver → _on_error → _stop()
        đảm bảo khi WinError 6 xảy ra, UI cập nhật đúng trạng thái.
        """
        if self._running:
            return
        if not self._watch:
            self._run_lbl.setText("No signals to watch!")
            self._run_lbl.setStyleSheet(
                "color:#E65100; font-size:12px; font-weight:700; "
                "padding:0 10px; font-family:'Segoe UI',sans-serif;"
            )
            return

        self._refresh_dbc_browser()

        send_tab  = getattr(self.main, "send_tab", None)
        bus       = getattr(send_tab, "_bus", None) if send_tab else None
        bus_mutex = getattr(send_tab, "_bus_mutex", None) if send_tab else None
        networks  = getattr(self.main, "networks", {})

        watch_fids = {entry.frame_id for entry in self._watch.values()}

        if bus and bus_mutex and _CAN_AVAILABLE:
            self._receiver = SignalReceiver(
                bus=bus,
                bus_mutex=bus_mutex,
                networks=networks,
                watch_frame_ids=watch_fids,
                parent=self,
            )
        else:
            self._receiver = DemoSignalGenerator(
                watch_entries=list(self._watch.values()),
                parent=self,
            )

        self._receiver.signals_updated.connect(self._on_signals_updated)
        self._receiver.error_occurred.connect(self._on_error)
        self._receiver.start()

        self._running = True
        self.monitor_started.emit()   # → TraceTab sẽ tự _stop() nếu đang chạy
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._run_lbl.setText("● RUNNING")
        self._run_lbl.setStyleSheet(
            "color:#2E7D32; font-size:12px; font-weight:700; "
            "padding:0 10px; font-family:'Segoe UI',sans-serif;"
        )
        self._ui_timer.start()
        self._stat_timer.start()

    def _stop(self) -> None:
        """Dừng giám sát. An toàn khi gọi nhiều lần."""
        if not self._running:
            return

        self._running = False

        if self._receiver:
            try:
                self._receiver.signals_updated.disconnect(self._on_signals_updated)
                self._receiver.error_occurred.disconnect(self._on_error)
            except (TypeError, RuntimeError):
                pass
            self._receiver.stop()
            self._receiver.wait(2000)
            self._receiver = None

        self._ui_timer.stop()
        self._stat_timer.stop()
        self._refresh_table_values()

        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._run_lbl.setText("■ STOPPED")
        self._run_lbl.setStyleSheet(
            "color:#C62828; font-size:12px; font-weight:700; "
            "padding:0 10px; font-family:'Segoe UI',sans-serif;"
        )

    def _on_signals_updated(self, batch: Dict) -> None:
        self._pending.append(batch)

    def _on_error(self, msg: str) -> None:
        """
        Slot nhận error từ receiver.

        FIX: Gọi _stop() khi receiver báo fatal error (WinError 6),
        cập nhật UI trạng thái STOPPED thay vì để spam log.
        """
        self._n_errors += 1
        logger.warning("GraphicTab receiver error: %s", msg)
        # Tự dừng khi receiver gặp lỗi fatal
        if self._running:
            self._stop()

    def _refresh_stats(self) -> None:
        self._watch_cnt_lbl.setText(f"Signals: {len(self._watch)}")
        self._update_cnt_lbl.setText(f"Updated: {self._n_updates:,}")
        self._err_cnt_lbl.setText(f"Errors: {self._n_errors}")

    # ==========================================================
    # CLEANUP
    # ==========================================================

    def cleanup(self) -> None:
        """Dừng giám sát khi đóng app."""
        if self._running:
            self._stop()