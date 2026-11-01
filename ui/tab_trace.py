"""
==============================================================
ui/tab_trace.py — CAN / CAN FD Trace Tab (Tối ưu hoá)
==============================================================

Tích hợp với SendCanTab:
- [INTEG-1] Lấy bus object từ send_tab._bus để nhận RX messages
- [INTEG-2] Dùng chung bus_mutex với send_tab để thread-safe
- [INTEG-3] Lấy TX frames từ send_tab (receive_own_messages=True trên Vector)
- [INTEG-4] DBC decode sử dụng main.networks (đã load ở DbcTab)

FIX [WinError 6]:
- [FIX-1] TraceReceiver phân biệt fatal bus error vs transient error
          → WinError 6/1/5 → emit error_occurred + break loop ngay
          → Không spam hàng trăm dòng log mỗi giây
- [FIX-2] TraceTab._on_err chỉ gọi _stop() (không restart)
- [FIX-3] Kết nối send_tab.bus_disconnected để proactive stop
          trước khi WinError 6 xảy ra
- [FIX-4] Guard kiểm tra self._running trước recv() trong mutex
"""

import logging
import time
import csv
import collections
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QComboBox, QLineEdit,
    QTreeWidget, QTreeWidgetItem, QHeaderView,
    QTextEdit, QAbstractItemView, QCheckBox,
    QFileDialog, QToolButton, QMenu, QFrame,
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

_RECV_TIMEOUT_S     = 0.004
_FPS_EMA_ALPHA      = 0.3
_MAX_HISTORY_ITEMS  = 50_000
_FILTER_DEBOUNCE_MS = 150
_BATCH_SIZE         = 64
_PAUSE_BUFFER_MAX   = 10_000
_UI_TICK_MS         = 50
_SCROLL_TICK_MS     = 100
_STAT_TICK_MS       = 400

# FIX: WinError codes gây ra lỗi fatal (bus handle bị đóng)
_FATAL_WINERRORS = {6, 1, 5}   # Invalid handle, Incorrect function, Access denied
_FATAL_ERRNOS    = {9}          # EBADF


# ==============================================================
# HELPER — Phân loại lỗi fatal vs transient
# ==============================================================

def _is_fatal_bus_error(exc: Exception) -> bool:
    """
    Trả về True nếu exception là lỗi fatal về bus handle.

    WinError 6 (ERROR_INVALID_HANDLE) xảy ra khi bus bị close
    bởi SendCanTab trong khi receiver thread vẫn đang recv().
    """
    winerror  = getattr(exc, "winerror", None)
    errno_val = getattr(exc, "errno", None)
    err_str   = str(exc).lower()

    if winerror in _FATAL_WINERRORS:
        return True
    if errno_val in _FATAL_ERRNOS:
        return True
    if any(kw in err_str for kw in ("invalid handle", "winerror 6", "bad file descriptor")):
        return True
    return False


# ==============================================================
# DATA CLASS
# ==============================================================

@dataclass
class CanFrame:
    """
    Đại diện 1 CAN/CAN FD frame nhận được hoặc gửi đi.
    """
    timestamp: float
    abs_time: float
    network_name: str
    channel: str
    frame_id: int
    dlc: int
    data: bytes
    direction: str
    is_error: bool = False
    is_fd: bool = False
    is_remote: bool = False
    message_name: str = ""
    node: str = ""
    msg_type: str = "Data"
    fps: float = 0.0
    signals: Dict[str, Any] = field(default_factory=dict)
    signal_units: Dict[str, str] = field(default_factory=dict)
    signal_comments: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    count: int = 1
    cycle_ms: float = 0.0


# ==============================================================
# RECEIVER THREAD
# ==============================================================

class TraceReceiver(QThread):
    """
    Thread nhận CAN messages từ bus hardware.

    FIX [WinError 6]:
    - Phát hiện WinError 6 → emit error_occurred + break loop ngay
    - Guard self._running trước khi gọi recv() trong mutex
    - Không spam hàng trăm lần exception mỗi giây
    """

    frame_received = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        bus,
        bus_mutex: QMutex,
        channel_label: str,
        networks: Dict,
        start_time: float,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bus          = bus
        self._mutex        = bus_mutex
        self._channel      = channel_label
        self._networks     = networks
        self._start_time   = start_time
        self._running: bool = True

        self._fps_ts: Dict[int, float]   = {}
        self._fps_ema: Dict[int, float]  = {}
        self._counters: Dict[int, int]   = {}

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        """
        Loop nhận messages.

        FIX: Phân biệt WinError fatal vs lỗi tạm thời.
        Khi fatal → break ngay, không tiếp tục loop.
        """
        while self._running:
            try:
                with QMutexLocker(self._mutex):
                    # FIX: Guard kiểm tra trước khi recv()
                    if not self._running:
                        break
                    msg = self._bus.recv(timeout=_RECV_TIMEOUT_S)

                if msg is None:
                    continue

                now = time.time()
                fid = msg.arbitration_id

                self._counters[fid] = self._counters.get(fid, 0) + 1

                prev = self._fps_ts.get(fid, now)
                dt   = now - prev
                if dt > 0:
                    instant_fps = 1.0 / dt
                    old_ema     = self._fps_ema.get(fid, instant_fps)
                    fps = _FPS_EMA_ALPHA * instant_fps + (1 - _FPS_EMA_ALPHA) * old_ema
                    self._fps_ema[fid] = fps
                else:
                    fps = self._fps_ema.get(fid, 0.0)
                self._fps_ts[fid] = now

                if msg.is_error_frame:
                    direction = "Error"
                    msg_type  = "Error"
                elif msg.is_remote_frame:
                    direction = "Rx"
                    msg_type  = "Remote"
                else:
                    direction = "Rx"
                    msg_type  = "Data"

                frame = CanFrame(
                    timestamp=now,
                    abs_time=now - self._start_time,
                    network_name=self._channel,
                    channel=self._channel,
                    frame_id=fid,
                    dlc=msg.dlc,
                    data=bytes(msg.data),
                    direction=direction,
                    is_error=msg.is_error_frame,
                    is_fd=getattr(msg, "is_fd", False),
                    is_remote=msg.is_remote_frame,
                    msg_type=msg_type,
                    fps=round(fps, 1),
                    count=self._counters[fid],
                    cycle_ms=round(dt * 1000, 1) if dt > 0 else 0.0,
                )

                self._decode(frame)
                self.frame_received.emit(frame)

            except Exception as e:
                if not self._running:
                    break  # Đang dừng, bỏ qua exception

                if _is_fatal_bus_error(e):
                    # FIX: Bus handle bị đóng → dừng hẳn, không loop lại
                    logger.warning(
                        "TraceReceiver: bus handle invalid, stopping. (%s)", e
                    )
                    self._running = False
                    self.error_occurred.emit(str(e))
                    break  # ← Thoát loop, KHÔNG tiếp tục
                else:
                    # Lỗi tạm thời → log và tiếp tục
                    logger.warning("TraceReceiver transient error: %s", e)

    def _decode(self, frame: CanFrame) -> None:
        """DBC decode frame."""
        try:
            for net_name, net in self._networks.items():
                if not net or not getattr(net, "database", None):
                    continue
                md = net.database.get_message_by_frame_id(frame.frame_id)
                if not md:
                    continue

                frame.message_name = md.name
                frame.description  = getattr(md, "comment", "") or ""
                frame.network_name = net_name

                senders = getattr(md, "senders", None) or []
                if not senders:
                    sn = getattr(md, "sendnode", None) or getattr(md, "sender", None)
                    if sn:
                        senders = [sn] if isinstance(sn, str) else list(sn)
                frame.node = ", ".join(str(s) for s in senders) if senders else ""

                for s in md.signals:
                    frame.signal_units[s.name]    = getattr(s, "unit", "") or ""
                    frame.signal_comments[s.name] = getattr(s, "comment", "") or ""

                if frame.data:
                    try:
                        decoded = md.decode(frame.data, decode_choices=True)
                        for sig_name, val in decoded.items():
                            if isinstance(val, float):
                                frame.signals[sig_name] = round(val, 3)
                            else:
                                frame.signals[sig_name] = val
                    except Exception:
                        pass

                return

        except Exception as e:
            logger.debug("DBC decode 0x%X: %s", frame.frame_id, e)


# ==============================================================
# COLUMN LAYOUT
# ==============================================================

COLUMNS = [
    ("Absolute Time", 115),
    ("Counter",        68),
    ("Chn",            56),
    ("Identifier",     76),
    ("FPS",            46),
    ("Message Name",  140),
    ("Node",           72),
    ("Type",           52),
    ("Dir",            38),
    ("DLC",            34),
    ("00", 36), ("01", 36), ("02", 36), ("03", 36),
    ("04", 36), ("05", 36), ("06", 36), ("07", 36),
]

COL_ABSTIME = 0
COL_COUNTER = 1
COL_CHN     = 2
COL_ID      = 3
COL_FPS     = 4
COL_MSGNAME = 5
COL_NODE    = 6
COL_TYPE    = 7
COL_DIR     = 8
COL_DLC     = 9
COL_B0      = 10

NC = len(COLUMNS)

C_TX   = QColor(0,   70, 200)
C_RX   = QColor(10,  10,  10)
C_ERR  = QColor(200,  0,   0)
C_SIG  = QColor(70,  70,  70)
C_SIGV = QColor(0,   90, 170)
C_NODE = QColor(120, 60, 160)


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
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #F8F8F8, stop:1 #ECECEC);
    border-bottom: 1px solid #B8B8B8;
}
#iconBar {
    background: #F5F5F5;
    border-bottom: 1px solid #D8D8D8;
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
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 3px 6px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    font-weight: 500;
    color: #444;
}
QToolButton:hover { background: #E0E0E0; border-color: #B0B0B0; }
QToolButton:pressed { background: #D0D0D0; }
#settingsBtn {
    background: #FAFAFA;
    border: 1px solid #B0B0B0;
    border-radius: 3px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
}
#settingsBtn:hover { background: #EFEFEF; }
#filterEdit {
    background: white;
    border: 1px solid #B0B0B0;
    border-radius: 3px;
    padding: 3px 8px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    min-width: 160px;
}
#filterEdit:focus { border: 2px solid #1E88E5; padding: 2px 7px; }
#channelCombo {
    background: white;
    border: 1px solid #B0B0B0;
    border-radius: 3px;
    padding: 3px 8px;
    font-size: 12px;
    min-width: 88px;
}
QTreeWidget {
    background: #FFFFFF;
    alternate-background-color: #F5F7FA;
    border: none;
    outline: 0;
    font-family: 'Cascadia Mono', 'Consolas', 'DejaVu Sans Mono', 'Courier New', monospace;
    font-size: 12px;
    selection-background-color: #BBDEFB;
    selection-color: #0D47A1;
}
QTreeWidget::item {
    height: 22px;
    padding: 1px 3px;
    border-bottom: 1px solid #EEEEEE;
}
QTreeWidget::item:selected { background: #BBDEFB; color: #0D47A1; }
QTreeWidget::item:hover:!selected { background: #E3F2FD; }
QHeaderView::section {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #F8F8F8, stop:1 #E8E8E8);
    color: #333;
    border: none;
    border-right: 1px solid #C8C8C8;
    border-bottom: 2px solid #999;
    padding: 5px 6px;
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 12px;
    font-weight: 600;
}
QHeaderView::section:first { padding-left: 10px; }
QHeaderView::section:hover { background: #E0E4E8; }
#descPanel {
    background: #FAFBFC;
    border-left: 1px solid #D0D0D0;
}
#descText {
    background: #FAFBFC;
    border: none;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    color: #222;
    line-height: 1.5;
}
#bottomBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #F2F2F2, stop:1 #E8E8E8);
    border-top: 1px solid #C0C0C0;
}
#statLabel {
    color: #444;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    font-weight: 500;
}
#runLabel {
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    font-weight: 700;
}
QComboBox {
    background: white;
    border: 1px solid #B0B0B0;
    border-radius: 3px;
    padding: 3px 8px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
    min-width: 78px;
}
QComboBox:hover { border-color: #777; }
QComboBox QAbstractItemView {
    background: white;
    selection-background-color: #BBDEFB;
    border: 1px solid #B0B0B0;
    font-size: 12px;
}
QComboBox::drop-down { border: none; width: 20px; }
QCheckBox {
    color: #333;
    spacing: 6px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #888;
    border-radius: 3px;
    background: white;
}
QCheckBox::indicator:checked { background: #1E88E5; border-color: #1565C0; }
QLabel {
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 12px;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #C0C0C0; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #999; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: #C0C0C0; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #999; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


# ==============================================================
# TRACE TAB
# ==============================================================

class TraceTab(QWidget):
    """
    Tab Trace CAN / CAN FD — kiểu TSmaster / CANoe.

    Chỉ trace từ real hardware bus, không có demo mode.

    FIX [WinError 6]:
    - TraceReceiver tự detect WinError 6 → emit error_occurred → _on_err → _stop()
    - Kết nối send_tab.bus_disconnected để dừng proactive
    - _on_err không restart, chỉ dừng sạch

    MUTUAL EXCLUSION:
    - Emit monitor_started khi bắt đầu → GraphicTab tự _stop()
    - Lắng nghe GraphicTab.monitor_started → tự _stop() nếu đang chạy
    - Kết nối chéo được thực hiện ở MainWindow sau khi cả 2 tab được tạo
    """

    # Signal phát khi tab này bắt đầu trace
    monitor_started = pyqtSignal()

    def __init__(self, main_window: "MainWindow") -> None:
        super().__init__()
        self.main = main_window

        self._running: bool      = False
        self._paused: bool       = False
        self._scroll_lock: bool  = False
        self._start_time: float  = 0.0
        self._receiver: Optional[QThread] = None

        self._normal_map: Dict[Tuple, QTreeWidgetItem] = {}
        self._n_frames: int = 0
        self._n_rx: int     = 0
        self._n_tx: int     = 0
        self._n_err: int    = 0

        self._tx_frame_ids: set = set()

        self._pending: collections.deque     = collections.deque(maxlen=_PAUSE_BUFFER_MAX)
        self._pause_buffer: collections.deque = collections.deque(maxlen=_PAUSE_BUFFER_MAX)

        self._seen_channels: List[str] = []

        self._sort_col: int  = -1
        self._sort_asc: bool = True

        self._is_can_fd: bool  = False
        self._ts_mode: str     = "absolute"

        self._build_ui()
        self.setStyleSheet(STYLE)

        self._ui_timer = QTimer()
        self._ui_timer.setInterval(_UI_TICK_MS)
        self._ui_timer.timeout.connect(self._process_pending)

        self._scroll_timer = QTimer()
        self._scroll_timer.setInterval(_SCROLL_TICK_MS)
        self._scroll_timer.timeout.connect(self._do_autoscroll)

        self._stat_timer = QTimer()
        self._stat_timer.setInterval(_STAT_TICK_MS)
        self._stat_timer.timeout.connect(self._refresh_stats)

        self._filter_timer = QTimer()
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(_FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_combined_filter)

        # FIX: Kết nối bus_disconnected từ SendCanTab
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
        Dừng tab này vì tab khác (GraphicTab) vừa bắt đầu.
        Gọi từ MainWindow khi GraphicTab emit monitor_started.
        """
        if self._running:
            logger.info("TraceTab: stopped because GraphicTab started.")
            self._stop()

    def _on_bus_disconnected(self) -> None:
        """
        Slot nhận thông báo bus đã bị đóng từ SendCanTab.

        Dừng receiver ngay lập tức trước khi WinError 6 xảy ra.
        """
        if self._running:
            logger.info("TraceTab: bus disconnected, stopping receiver.")
            self._stop()

    # ==========================================================
    # BUILD UI
    # ==========================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._mk_top_bar())
        root.addWidget(self._mk_icon_bar())

        spl = QSplitter(Qt.Horizontal)
        spl.setStyleSheet("QSplitter::handle{background:#D0D0D0;width:2px;}")
        spl.addWidget(self._mk_tree_panel())
        spl.addWidget(self._mk_desc_panel())
        spl.setSizes([960, 280])
        spl.setStretchFactor(0, 1)
        spl.setStretchFactor(1, 0)
        root.addWidget(spl, 1)
        root.addWidget(self._mk_bottom_bar())

    def _mk_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(40)
        ly = QHBoxLayout(bar)
        ly.setContentsMargins(6, 4, 6, 4)
        ly.setSpacing(6)

        self._btn_start = QPushButton("START")
        self._btn_start.setObjectName("btnStart")
        self._btn_start.clicked.connect(self._start)
        ly.addWidget(self._btn_start)

        self._btn_stop = QPushButton("STOP")
        self._btn_stop.setObjectName("btnStop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop)
        ly.addWidget(self._btn_stop)

        ly.addSpacing(8)
        title = QLabel("CAN / CAN FD Trace")
        title.setStyleSheet(
            "color:#1a1a1a; font-size:14px; font-weight:700; "
            "letter-spacing:0.3px; font-family:'Segoe UI',sans-serif;"
        )
        ly.addWidget(title)
        ly.addStretch()

        ly.addWidget(QLabel("Channel:"))
        self._ch_combo = QComboBox()
        self._ch_combo.setObjectName("channelCombo")
        self._ch_combo.addItem("All")
        self._ch_combo.setFixedWidth(95)
        self._ch_combo.currentTextChanged.connect(
            lambda _: self._apply_combined_filter()
        )
        ly.addWidget(self._ch_combo)

        ly.addSpacing(6)

        self._set_btn = QPushButton("Settings")
        self._set_btn.setObjectName("settingsBtn")
        self._set_btn.setFixedWidth(80)
        self._set_btn.clicked.connect(self._settings_menu)
        ly.addWidget(self._set_btn)

        ly.addWidget(QLabel("Filter"))
        self._filter = QLineEdit()
        self._filter.setObjectName("filterEdit")
        self._filter.setPlaceholderText("ID / name / node / channel...")
        self._filter.textChanged.connect(lambda _: self._filter_timer.start())
        ly.addWidget(self._filter)

        return bar

    def _mk_icon_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("iconBar")
        bar.setFixedHeight(34)
        ly = QHBoxLayout(bar)
        ly.setContentsMargins(4, 2, 4, 2)
        ly.setSpacing(1)

        btns = [
            ("Save",  "Save CSV",        self._export_csv),
            ("Copy",  "Copy row",         self._copy_row),
            ("Clear", "Clear trace",      self._clear),
            ("|",     None,               None),
            ("Pause", "Pause/Resume",     self._pause_resume),
            ("Lock",  "Scroll lock",      self._scroll_lock_toggle),
            ("|",     None,               None),
            ("Exp",   "Expand all",       lambda: self._tree.expandAll()),
            ("Col",   "Collapse all",     lambda: self._tree.collapseAll()),
        ]
        for ico, tip, cb in btns:
            if ico == "|":
                sep = QFrame()
                sep.setFrameShape(QFrame.VLine)
                sep.setStyleSheet("color:#C8C8C8;")
                sep.setFixedSize(1, 20)
                ly.addWidget(sep)
            else:
                b = QToolButton()
                b.setText(ico)
                b.setToolTip(tip or "")
                b.setFixedHeight(24)
                b.setMinimumWidth(40)
                if cb:
                    b.clicked.connect(cb)
                ly.addWidget(b)

        ly.addSpacing(8)
        ly.addWidget(QLabel("View:"))

        self._view = QComboBox()
        self._view.addItems(["Normal"])
        self._view.setFixedWidth(85)
        self._view.currentTextChanged.connect(lambda _: self._clear())
        ly.addWidget(self._view)

        ly.addSpacing(6)
        self._autoscroll = QCheckBox("Auto scroll")
        self._autoscroll.setChecked(True)
        ly.addWidget(self._autoscroll)

        ly.addStretch()

        self._hw_lbl  = QLabel("HW: –")
        self._dbc_lbl = QLabel("DBC: –")
        for lbl in (self._hw_lbl, self._dbc_lbl):
            lbl.setStyleSheet(
                "color:#777; font-size:11px; font-weight:500; "
                "padding:0 6px; font-family:'Segoe UI',sans-serif;"
            )
            ly.addWidget(lbl)

        return bar

    def _mk_tree_panel(self) -> QWidget:
        w = QWidget()
        ly = QVBoxLayout(w)
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(0)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(NC)
        self._tree.setHeaderLabels([c[0] for c in COLUMNS])
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._ctx_menu)
        self._tree.currentItemChanged.connect(self._on_sel_changed)

        hdr = self._tree.header()
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setSectionsClickable(True)
        hdr.sectionClicked.connect(self._on_header_clicked)
        for i, (_, w2) in enumerate(COLUMNS):
            self._tree.setColumnWidth(i, w2)
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)

        ly.addWidget(self._tree)
        return w

    def _mk_desc_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("descPanel")
        ly = QVBoxLayout(panel)
        ly.setContentsMargins(6, 4, 6, 4)
        ly.setSpacing(3)

        hdr = QLabel("Description")
        hdr.setStyleSheet(
            "color:#1565C0; font-weight:700; font-size:13px; "
            "font-family:'Segoe UI',sans-serif; "
            "border-bottom:1px solid #C0C0C0; padding-bottom:4px;"
        )
        ly.addWidget(hdr)

        self._desc = QTextEdit()
        self._desc.setObjectName("descText")
        self._desc.setReadOnly(True)
        ly.addWidget(self._desc, 1)

        return panel

    def _mk_bottom_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("bottomBar")
        bar.setFixedHeight(28)
        ly = QHBoxLayout(bar)
        ly.setContentsMargins(6, 0, 6, 0)
        ly.setSpacing(0)

        def _sep() -> QFrame:
            f = QFrame()
            f.setFrameShape(QFrame.VLine)
            f.setStyleSheet("color:#C0C0C0;")
            f.setFixedSize(1, 18)
            return f

        def _lbl(text: str) -> QLabel:
            l = QLabel(text)
            l.setObjectName("statLabel")
            l.setStyleSheet(
                "color:#444; font-size:12px; font-weight:500; "
                "padding:0 10px; font-family:'Segoe UI',sans-serif;"
            )
            return l

        self._tab_lbl = _lbl("Tab 1")
        self._cnt_lbl = _lbl("All: 0")
        self._rx_lbl  = _lbl("Rx: 0")
        self._tx_lbl  = _lbl("Tx: 0")
        self._err_lbl = _lbl("Err: 0")

        for widget in (self._tab_lbl, _sep(), self._cnt_lbl, _sep(),
                       self._rx_lbl, _sep(), self._tx_lbl, _sep(),
                       self._err_lbl, _sep()):
            ly.addWidget(widget)

        self._run_lbl = QLabel("STOPPED")
        self._run_lbl.setObjectName("runLabel")
        self._run_lbl.setStyleSheet(
            "color:#C62828; font-size:12px; font-weight:700; "
            "padding:0 10px; font-family:'Segoe UI',sans-serif;"
        )
        ly.addWidget(self._run_lbl)
        ly.addStretch()

        return bar

    # ==========================================================
    # START / STOP
    # ==========================================================

    def _start(self) -> None:
        """
        Bắt đầu trace chỉ khi có real hardware bus.

        Nếu không có bus → không start, chỉ log warning.

        NOTE: Cần SendCanTab._do_connect() gán self._bus + self._bus_mutex
        để TraceTab có thể truy cập chúng (xem SENDCAN_TAB_PATCH.md)
        """
        if self._running:
            return

        self._update_hw_dbc_labels()
        self._start_time = time.time()
        networks = getattr(self.main, "networks", {})

        send_tab  = getattr(self.main, "send_tab", None)
        bus       = getattr(send_tab, "_bus", None) if send_tab else None
        bus_mutex = getattr(send_tab, "_bus_mutex", None) if send_tab else None

        if bus and bus_mutex and _CAN_AVAILABLE:
            ch_label = "CAN 1"
            if hasattr(send_tab, "ch_combo"):
                ch_label = send_tab.ch_combo.currentText() or "CAN 1"

            self._tx_frame_ids.clear()
            if hasattr(send_tab, "_tx_queue"):
                for fid in send_tab._tx_queue.keys():
                    self._tx_frame_ids.add(fid)

            self._receiver = TraceReceiver(
                bus=bus,
                bus_mutex=bus_mutex,
                channel_label=ch_label,
                networks=networks,
                start_time=self._start_time,
                parent=self,
            )
            self._hw_lbl.setText(f"HW: {ch_label}")
            logger.info("Trace started: using SendCanTab bus (%s)", ch_label)

            self._receiver.frame_received.connect(self._on_frame_received)
            self._receiver.error_occurred.connect(self._on_err)
            self._receiver.start()

            self._running = True
            self.monitor_started.emit()

            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(True)
            self._run_lbl.setText("● RUNNING")
            self._run_lbl.setStyleSheet(
                "color:#2E7D32; font-size:12px; font-weight:700; "
                "padding:0 10px; font-family:'Segoe UI',sans-serif;"
            )
            self._ui_timer.start()
            self._scroll_timer.start()
            self._stat_timer.start()
        else:
            logger.warning("Trace START: No hardware bus connected. Check SendCanTab.")
            self._hw_lbl.setText("HW: Not Connected")

    def _stop(self) -> None:
        """Dừng trace. An toàn khi gọi nhiều lần."""
        if not self._running:
            return

        self._running = False

        if self._receiver:
            try:
                self._receiver.frame_received.disconnect(self._on_frame_received)
                self._receiver.error_occurred.disconnect(self._on_err)
            except (TypeError, RuntimeError):
                pass
            self._receiver.stop()
            self._receiver.wait(2000)
            self._receiver = None

        self._ui_timer.stop()
        self._scroll_timer.stop()
        self._stat_timer.stop()
        self._process_pending()

        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._run_lbl.setText("■ STOPPED")
        self._run_lbl.setStyleSheet(
            "color:#C62828; font-size:12px; font-weight:700; "
            "padding:0 10px; font-family:'Segoe UI',sans-serif;"
        )

    # ==========================================================
    # FRAME HANDLING
    # ==========================================================

    def _on_frame_received(self, frame: CanFrame) -> None:
        if frame.frame_id in self._tx_frame_ids and frame.direction == "Rx":
            frame.direction = "Tx"

        if self._paused:
            self._pause_buffer.append(frame)
        else:
            self._pending.append(frame)

    def _process_pending(self) -> None:
        processed = 0
        while self._pending and processed < _BATCH_SIZE:
            frame = self._pending.popleft()
            self._ingest_frame(frame)
            processed += 1

    def _ingest_frame(self, frame: CanFrame) -> None:
        self._n_frames += 1
        if frame.is_error:
            self._n_err += 1
        elif frame.direction == "Tx":
            self._n_tx += 1
        else:
            self._n_rx += 1

        if frame.channel and frame.channel not in self._seen_channels:
            self._seen_channels.append(frame.channel)
            self._seen_channels.sort()
            self._ch_combo.blockSignals(True)
            cur = self._ch_combo.currentText()
            self._ch_combo.clear()
            self._ch_combo.addItem("All")
            self._ch_combo.addItems(self._seen_channels)
            idx = self._ch_combo.findText(cur)
            self._ch_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self._ch_combo.blockSignals(False)

        mode = self._view.currentText()
        key  = (frame.network_name, frame.frame_id)

        if mode == "Normal":
            if key in self._normal_map:
                self._update_item(self._normal_map[key], frame)
            else:
                item = self._mk_item(frame)
                self._tree.addTopLevelItem(item)
                self._normal_map[key] = item
                self._apply_visibility(item, frame)
        else:
            if self._tree.topLevelItemCount() >= _MAX_HISTORY_ITEMS:
                self._tree.takeTopLevelItem(0)
            item = self._mk_item(frame)
            self._tree.addTopLevelItem(item)
            self._apply_visibility(item, frame)

    def _on_err(self, msg: str) -> None:
        """
        Slot nhận error từ receiver.

        FIX: Chỉ gọi _stop() — không restart, không spam log.
        TraceReceiver đã tự dừng trước khi emit signal này.
        """
        logger.warning("TraceTab receiver stopped: %s", msg)
        if self._running:
            self._stop()

    # ==========================================================
    # TREE ITEMS
    # ==========================================================

    def _mk_item(self, frame: CanFrame) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        self._fill_item(item, frame)
        self._add_signal_children(item, frame)
        item.setData(0, Qt.UserRole, frame)
        return item

    def _fill_item(self, item: QTreeWidgetItem, frame: CanFrame) -> None:
        fg = QBrush(
            C_TX if frame.direction == "Tx"
            else C_ERR if frame.is_error
            else C_RX
        )
        t  = frame.abs_time
        hh = int(t // 3600)
        mm = int((t % 3600) // 60)
        ss = t % 60
        ts = f"{hh}:{mm:02d}:{ss:09.6f}" if hh else f"{mm:02d}:{ss:09.6f}"

        b    = list(frame.data) + [None] * 8
        vals = [
            ts,
            str(frame.count),
            frame.channel,
            f"{frame.frame_id:X}",
            str(int(frame.fps)) if frame.fps > 0 else "",
            frame.message_name or f"0x{frame.frame_id:X}",
            frame.node,
            frame.msg_type,
            frame.direction,
            str(frame.dlc),
        ] + [
            f"{b[i]:02X}" if i < frame.dlc and b[i] is not None else ""
            for i in range(8)
        ]

        mono = QFont("Cascadia Mono", 11)
        mono.setStyleHint(QFont.Monospace)
        for col, v in enumerate(vals):
            item.setText(col, v)
            item.setForeground(col, fg)
            item.setFont(col, mono)

        item.setForeground(COL_NODE, QBrush(C_NODE))

        center_cols = (
            COL_COUNTER, COL_FPS, COL_DLC, COL_NODE,
            COL_TYPE, COL_DIR, COL_CHN,
        ) + tuple(range(COL_B0, COL_B0 + 8))
        for col in center_cols:
            item.setTextAlignment(col, Qt.AlignCenter | Qt.AlignVCenter)

    def _update_item(self, item: QTreeWidgetItem, frame: CanFrame) -> None:
        old_frame = item.data(0, Qt.UserRole)
        self._fill_item(item, frame)
        item.setData(0, Qt.UserRole, frame)

        old_keys = set(old_frame.signals.keys()) if isinstance(old_frame, CanFrame) else set()
        new_keys = set(frame.signals.keys())
        if old_keys != new_keys:
            item.takeChildren()
            self._add_signal_children(item, frame)
        else:
            self._update_signal_children(item, frame)

    def _add_signal_children(self, parent: QTreeWidgetItem, frame: CanFrame) -> None:
        if not frame.signals:
            return
        seg_font  = QFont("Segoe UI", 11)
        mono_font = QFont("Cascadia Mono", 11)
        mono_font.setStyleHint(QFont.Monospace)

        for sig, val in frame.signals.items():
            child = QTreeWidgetItem()
            unit  = frame.signal_units.get(sig, "")
            v_str = self._fmt_sig_val(val)
            display = f"{v_str} {unit}".strip() if unit else v_str

            child.setText(COL_ABSTIME, f"   {sig}")
            child.setFont(COL_ABSTIME, seg_font)
            child.setForeground(COL_ABSTIME, QBrush(C_SIG))

            child.setText(COL_FPS, display)
            child.setFont(COL_FPS, mono_font)
            child.setForeground(COL_FPS, QBrush(C_SIGV))
            child.setTextAlignment(COL_FPS, Qt.AlignLeft | Qt.AlignVCenter)

            child.setData(0, Qt.UserRole, frame)
            child.setData(0, Qt.UserRole + 1, sig)
            parent.addChild(child)

    def _update_signal_children(self, parent: QTreeWidgetItem, frame: CanFrame) -> None:
        sig_list = list(frame.signals.items())
        for idx in range(min(parent.childCount(), len(sig_list))):
            child    = parent.child(idx)
            sig, val = sig_list[idx]
            unit     = frame.signal_units.get(sig, "")
            v_str    = self._fmt_sig_val(val)
            display  = f"{v_str} {unit}".strip() if unit else v_str
            child.setText(COL_ABSTIME, f"   {sig}")
            child.setText(COL_FPS, display)
            child.setData(0, Qt.UserRole, frame)

    @staticmethod
    def _fmt_sig_val(val) -> str:
        if isinstance(val, str):
            return val
        if isinstance(val, float):
            v = f"{val:.3f}".rstrip("0").rstrip(".")
            return v if "." in v else f"{val:.1f}"
        return str(val)

    # ==========================================================
    # SORT
    # ==========================================================

    def _on_header_clicked(self, logical_index: int) -> None:
        if logical_index == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = logical_index
            self._sort_asc = True

        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        self._tree.sortByColumn(logical_index, order)
        hdr = self._tree.header()
        hdr.setSortIndicator(logical_index, order)
        hdr.setSortIndicatorShown(True)

    # ==========================================================
    # FILTER
    # ==========================================================

    def _apply_visibility(self, item: QTreeWidgetItem, frame: CanFrame) -> None:
        ch_filter = self._ch_combo.currentText()
        t         = self._filter.text().strip().lower()
        ch_ok     = (ch_filter == "All" or frame.channel == ch_filter)
        txt_ok    = not t or self._text_matches(frame, t)
        item.setHidden(not (ch_ok and txt_ok))

    def _apply_combined_filter(self) -> None:
        ch_filter = self._ch_combo.currentText()
        t         = self._filter.text().strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            item  = self._tree.topLevelItem(i)
            frame = item.data(0, Qt.UserRole)
            if not frame:
                continue
            ch_ok  = (ch_filter == "All" or frame.channel == ch_filter)
            txt_ok = not t or self._text_matches(frame, t)
            item.setHidden(not (ch_ok and txt_ok))

    @staticmethod
    def _text_matches(frame: CanFrame, t: str) -> bool:
        return (
            t in frame.message_name.lower()
            or t in f"{frame.frame_id:x}"
            or t in f"0x{frame.frame_id:x}"
            or t in frame.channel.lower()
            or t in frame.node.lower()
            or t in frame.network_name.lower()
        )

    # ==========================================================
    # SELECTION → Description panel
    # ==========================================================

    def _on_sel_changed(self, cur, prev) -> None:
        if cur is None:
            return
        frame = cur.data(0, Qt.UserRole)
        if not isinstance(frame, CanFrame):
            return

        lines: List[str] = []

        if frame.message_name:
            lines.append(f"<b style='color:#1565C0'>{frame.message_name}</b>")
            node_html = (
                f"&nbsp;&nbsp;Node: <b style='color:#7B1FA2'>{frame.node}</b>"
                if frame.node else ""
            )
            lines.append(
                f"<span style='color:#666'>ID: 0x{frame.frame_id:X}"
                f"&nbsp;&nbsp;DLC: {frame.dlc}"
                f"&nbsp;&nbsp;{frame.channel}"
                f"&nbsp;&nbsp;Dir: {frame.direction}"
                f"{node_html}</span>"
            )
            lines.append("")

        if frame.description:
            for ln in frame.description.split("\n"):
                lines.append(f"<span style='color:#333'>{ln}</span>")
            lines.append("")

        if frame.signals:
            lines.append("<b style='color:#555'>Signals:</b>")
            for sig, val in frame.signals.items():
                unit  = frame.signal_units.get(sig, "")
                cmt   = frame.signal_comments.get(sig, "")
                v_str = self._fmt_sig_val(val)
                u     = f" {unit}" if unit else ""
                lines.append(
                    f"&nbsp;&nbsp;<span style='color:#0D47A1'>{sig}</span>"
                    f" = <span style='color:#1B5E20'><b>{v_str}{u}</b></span>"
                )
                if cmt:
                    lines.append(
                        f"&nbsp;&nbsp;&nbsp;&nbsp;"
                        f"<span style='color:#9E9E9E;font-style:italic'>{cmt}</span>"
                    )

        self._desc.setHtml("<br>".join(lines))

    # ==========================================================
    # TIMERS
    # ==========================================================

    def _do_autoscroll(self) -> None:
        if self._autoscroll.isChecked() and not self._scroll_lock:
            n = self._tree.topLevelItemCount()
            if n > 0:
                self._tree.scrollToItem(self._tree.topLevelItem(n - 1))

    def _refresh_stats(self) -> None:
        self._cnt_lbl.setText(f"All: {self._n_frames:,}")
        self._rx_lbl.setText(f"Rx: {self._n_rx:,}")
        self._tx_lbl.setText(f"Tx: {self._n_tx:,}")
        self._err_lbl.setText(f"Err: {self._n_err}")

    # ==========================================================
    # ACTIONS
    # ==========================================================

    def _clear(self) -> None:
        self._tree.clear()
        self._normal_map.clear()
        self._pending.clear()
        self._pause_buffer.clear()
        self._n_frames = self._n_rx = self._n_tx = self._n_err = 0
        self._seen_channels.clear()
        self._ch_combo.blockSignals(True)
        self._ch_combo.clear()
        self._ch_combo.addItem("All")
        self._ch_combo.blockSignals(False)
        self._refresh_stats()
        self._desc.clear()

    def _pause_resume(self) -> None:
        self._paused = not self._paused
        if not self._paused:
            while self._pause_buffer:
                self._pending.append(self._pause_buffer.popleft())

    def _scroll_lock_toggle(self) -> None:
        self._scroll_lock = not self._scroll_lock

    def _copy_row(self) -> None:
        from PyQt5.QtWidgets import QApplication
        item = self._tree.currentItem()
        if item:
            QApplication.clipboard().setText(
                "\t".join(item.text(c) for c in range(NC))
            )

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Trace", "", "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([c[0] for c in COLUMNS] + ["Signal Name", "Signal Value"])
                for i in range(self._tree.topLevelItemCount()):
                    item = self._tree.topLevelItem(i)
                    row  = [item.text(c) for c in range(NC)]
                    w.writerow(row + ["", ""])
                    for j in range(item.childCount()):
                        child    = item.child(j)
                        sig_name = child.data(0, Qt.UserRole + 1) or child.text(COL_ABSTIME).strip()
                        sig_val  = child.text(COL_FPS)
                        w.writerow(
                            [child.text(c) for c in range(NC)]
                            + [sig_name, sig_val]
                        )
        except Exception as e:
            logger.error("CSV export failed: %s", e)

    def _settings_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;border:1px solid #BDBDBD;}"
            "QMenu::item{padding:5px 18px;}"
            "QMenu::item:selected{background:#CCE5FF;}"
        )
        act20 = menu.addAction("CAN 2.0")
        act20.setCheckable(True)
        act20.setChecked(not self._is_can_fd)
        act20.triggered.connect(lambda: setattr(self, "_is_can_fd", False))

        actfd = menu.addAction("CAN FD")
        actfd.setCheckable(True)
        actfd.setChecked(self._is_can_fd)
        actfd.triggered.connect(lambda: setattr(self, "_is_can_fd", True))

        menu.addSeparator()

        act_abs = menu.addAction("Timestamp: Absolute")
        act_abs.setCheckable(True)
        act_abs.setChecked(self._ts_mode == "absolute")
        act_abs.triggered.connect(lambda: setattr(self, "_ts_mode", "absolute"))

        act_rel = menu.addAction("Timestamp: Relative")
        act_rel.setCheckable(True)
        act_rel.setChecked(self._ts_mode == "relative")
        act_rel.triggered.connect(lambda: setattr(self, "_ts_mode", "relative"))

        menu.addSeparator()
        menu.addAction("Reset Sort",   self._reset_sort)
        menu.addSeparator()
        menu.addAction("Export CSV...", self._export_csv)

        menu.exec_(self._set_btn.mapToGlobal(self._set_btn.rect().bottomLeft()))

    def _reset_sort(self) -> None:
        self._sort_col = -1
        self._sort_asc = True
        self._tree.header().setSortIndicatorShown(False)

    def _ctx_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;border:1px solid #BDBDBD;}"
            "QMenu::item{padding:5px 18px;}"
            "QMenu::item:selected{background:#CCE5FF;}"
        )
        menu.addAction("Copy Row",           self._copy_row)
        menu.addAction("Filter by this ID",  self._filter_sel_id)
        menu.addAction("Filter by this Node", self._filter_sel_node)
        menu.addSeparator()
        menu.addAction("Expand All",   self._tree.expandAll)
        menu.addAction("Collapse All", self._tree.collapseAll)
        menu.addSeparator()
        menu.addAction("Clear Trace", self._clear)
        menu.exec_(self._tree.viewport().mapToGlobal(pos))

    def _filter_sel_id(self) -> None:
        item = self._tree.currentItem()
        if item:
            frame = item.data(0, Qt.UserRole)
            if isinstance(frame, CanFrame):
                self._filter.setText(f"{frame.frame_id:X}")

    def _filter_sel_node(self) -> None:
        item = self._tree.currentItem()
        if item:
            frame = item.data(0, Qt.UserRole)
            if isinstance(frame, CanFrame) and frame.node:
                self._filter.setText(frame.node)

    def _update_hw_dbc_labels(self) -> None:
        send_tab  = getattr(self.main, "send_tab", None)
        has_bus   = bool(getattr(send_tab, "_bus", None)) if send_tab else False
        dbc_count = len(getattr(self.main, "networks", {}))
        self._hw_lbl.setText("HW: Connected" if has_bus else "HW: Demo")
        self._dbc_lbl.setText(f"DBC: {dbc_count} net" if dbc_count else "DBC: –")

    # ==========================================================
    # CLEANUP
    # ==========================================================

    def cleanup(self) -> None:
        """Dừng trace nếu đang chạy. Gọi từ MainWindow khi đóng app."""
        if self._running:
            self._stop()