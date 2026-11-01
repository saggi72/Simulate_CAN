"""
==============================================================
ui/tab_inject_can.py — Inject CAN Tab
==============================================================

Man-in-the-middle CAN message injection tool.

Flow:
1. Chọn TX Channel (source) và RX Channel (dest) — 2 channels riêng biệt
2. Load DBC → browse messages
3. Chọn message cần inject → edit signal values
4. Connect cả 2 channels
5. Start: đọc TẤT CẢ messages từ TX channel
   - Message KHÔNG nằm trong inject list → forward nguyên bản
   - Message CÓ trong inject list → override signals → forward
6. Stop: dừng inject loop

Generator types đặc biệt cho Inject:
- "Passthrough": giữ nguyên giá trị gốc từ source (mặc định)
- "None":        user set giá trị cố định (override)
- Các generator khác: Alive Counter, Gen Counter, Ramp, Random, Gen Checksum

Imports TẤT CẢ từ core/ — KHÔNG import từ ui.tab_send_can.
"""

import json
import logging
import random
import sys
from typing import Dict, List, Optional
from datetime import datetime
from collections import OrderedDict

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QComboBox, QLineEdit, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QListWidget,
    QListWidgetItem, QGroupBox, QHeaderView,
    QCheckBox, QAbstractItemView,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QMutex, QMutexLocker
from PyQt5.QtGui import QFont, QColor

# === Core imports — tất cả logic từ core ===
from core.core import (
    CanType, MAX_DLC,
    UI_REFRESH_MS, STATS_UPDATE_MS,
    MAX_CYCLE_MS, DEFAULT_CYCLE_MS, DEFAULT_FD_DATA_BITRATE,
    MAX_LOG_LINES, LOG_BUFFER_MAX, LOG_BUFFER_KEEP,
    LOG_FLUSH_MAX_SHOW, LOG_BOX_HEIGHT,
    BAUD_RATES, FD_DATA_BITRATES,
    EXTENDED_ID_THRESHOLD,
    SIG_EDITOR_COL_WIDTHS, TX_TABLE_COL_WIDTHS,
    GENERATOR_OPTIONS,
    CrcAlgorithm, SignalEncoder, SignalTypeDetector,
    AliveCounter, GenCounter, get_signal_byte_set,
)
from core.tx_entry import TxEntry
from core.threads import (
    TxWorkerThread, RxReaderThread, QuickScanThread, is_can_available,
)
from core.bus_manager import BusManager

logger = logging.getLogger(__name__)

# Lazy import: python-can chỉ load khi user click Scan/Connect
_can_module = None
_can_checked = False


def _get_can():
    global _can_module, _can_checked
    if not _can_checked:
        try:
            import can
            _can_module = can
        except ImportError:
            _can_module = None
        _can_checked = True
    return _can_module


# ==============================================================
# INJECT-SPECIFIC CONSTANTS
# ==============================================================

INJECT_TICK_MS = 1            # Inject loop nhanh hơn SendCAN (1ms vs 10ms)
RX_TIMEOUT_S = 0.001          # recv timeout trên source channel (1ms)
PASSTHROUGH_LOG = False        # Log passthrough messages (mặc định tắt)
INJECT_LOG = True              # Log injected messages

# Generator options cho Inject (thêm "Passthrough")
INJECT_GENERATOR_OPTIONS = [
    "None", "Passthrough", "Gen Checksum", "Gen Counter",
    "Alive Counter", "Ramp", "Random",
]


# ==============================================================
# INJECT WORKER THREAD
# ==============================================================

class InjectWorkerThread(QThread):
    """
    Worker thread: đọc từ source bus → inject/modify → forward sang dest bus.

    Flow mỗi iteration:
    1. Recv message từ source_bus (TX channel)
    2. Check: message ID có trong inject_entries?
       - CÓ  → override signals đã config → encode → forward
       - KHÔNG → forward nguyên bản (passthrough)
    3. Send message sang dest_bus (RX channel)

    Signals:
        stats_updated(int, int, int): (forwarded, injected, errors)
        error_occurred(str):          Error message
    """

    stats_updated = pyqtSignal(int, int, int)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        source_bus,
        dest_bus,
        source_mutex: QMutex,
        dest_mutex: QMutex,
        inject_entries: OrderedDict,
        entries_mutex: QMutex,
        is_fd: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._source = source_bus
        self._dest = dest_bus
        self._source_mutex = source_mutex
        self._dest_mutex = dest_mutex
        self._inject_entries = inject_entries
        self._entries_mutex = entries_mutex
        self._is_fd = is_fd
        self._running: bool = False

        self._fwd_batch: int = 0
        self._inj_batch: int = 0
        self._err_batch: int = 0

    def run(self) -> None:
        import time

        self._running = True
        stats_interval = UI_REFRESH_MS / 1000.0
        last_stats = time.perf_counter()

        while self._running:
            t0 = time.perf_counter()
            msg = None

            try:
                with QMutexLocker(self._source_mutex):
                    msg = self._source.recv(timeout=RX_TIMEOUT_S)

                if msg is not None:
                    self._process_message(msg)

            except Exception as e:
                if self._running:
                    self._err_batch += 1
                    self.error_occurred.emit(f"Recv error: {e}")

            # Emit batched stats
            now = time.perf_counter()
            if now - last_stats >= stats_interval:
                if self._fwd_batch or self._inj_batch or self._err_batch:
                    self.stats_updated.emit(
                        self._fwd_batch, self._inj_batch, self._err_batch,
                    )
                    self._fwd_batch = 0
                    self._inj_batch = 0
                    self._err_batch = 0
                last_stats = now

            # Minimal sleep nếu không có message
            if msg is None:
                remaining = INJECT_TICK_MS / 1000.0 - (time.perf_counter() - t0)
                if remaining > 0.0001:
                    time.sleep(remaining)

    def _process_message(self, msg) -> None:
        """Xử lý 1 message: inject hoặc passthrough."""
        # Snapshot inject entry
        with QMutexLocker(self._entries_mutex):
            entry = self._inject_entries.get(msg.arbitration_id)

        if entry is not None and entry.active:
            self._inject_message(msg, entry)
        else:
            self._forward(msg)
            self._fwd_batch += 1

    def _inject_message(self, msg, entry: TxEntry) -> None:
        """
        Inject mode: copy data gốc → override signals → forward.

        Thứ tự:
        1. Copy data từ message gốc
        2. Passthrough signals: đọc raw từ data gốc (để UI hiện)
        3. Override signals: encode user-set / auto-generated values
        4. Checksum: tính CRC sau cùng
        5. Forward modified message
        """
        # Copy data gốc, đảm bảo đủ dài
        inject_data = list(msg.data)
        while len(inject_data) < entry.dlc:
            inject_data.append(0)

        # Pass 1: Xử lý tất cả signals (trừ Checksum)
        for sig in entry.signals:
            gen = sig.get("generator", "None")

            if gen == "Passthrough":
                # Đọc giá trị gốc từ source message để UI hiển thị
                sig["raw"] = SignalEncoder.decode(
                    inject_data, sig["start"], sig["length"],
                    sig.get("byte_order", "little_endian"),
                )
                continue

            if gen == "Gen Checksum":
                continue  # Tính sau cùng

            # Auto-generators
            if gen == "Alive Counter":
                alv = entry._alive_counters.get(sig["name"])
                if alv is None:
                    alv = AliveCounter(bit_length=sig["length"])
                    entry._alive_counters[sig["name"]] = alv
                sig["raw"] = alv.next()

            elif gen == "Gen Counter":
                gc = entry._gen_counters.get(sig["name"])
                if gc is None:
                    gc = GenCounter(bit_length=sig["length"])
                    entry._gen_counters[sig["name"]] = gc
                sig["raw"] = gc.next()

            elif gen == "Ramp":
                max_val = (1 << sig["length"]) - 1
                sig["raw"] = (sig["raw"] + 1) & max_val

            elif gen == "Random":
                max_val = (1 << sig["length"]) - 1
                sig["raw"] = random.randint(0, max_val)

            # gen == "None": giữ sig["raw"] đã set bởi user

            # Encode signal vào inject_data
            SignalEncoder.encode(
                inject_data, sig["start"], sig["length"],
                sig["raw"], sig.get("byte_order", "little_endian"),
            )

        # Pass 2: Checksum sau cùng
        for sig in entry.signals:
            if sig.get("generator") == "Gen Checksum":
                chk_bytes = get_signal_byte_set(
                    sig["start"], sig["length"],
                    sig.get("byte_order", "little_endian"),
                )
                crc_input = [
                    inject_data[i] for i in range(entry.dlc)
                    if i not in chk_bytes
                ]
                chk = CrcAlgorithm.compute(
                    bytes(crc_input), entry.crc_variant,
                )
                chk &= (1 << sig["length"]) - 1
                sig["raw"] = chk
                SignalEncoder.encode(
                    inject_data, sig["start"], sig["length"],
                    chk, sig.get("byte_order", "little_endian"),
                )

        # Cập nhật entry.data cho UI preview
        entry.data[:entry.dlc] = inject_data[:entry.dlc]

        # Forward modified message
        can = _get_can()
        use_fd = entry.is_fd or getattr(msg, "is_fd", False)
        modified = can.Message(
            arbitration_id=msg.arbitration_id,
            data=bytes(inject_data[:msg.dlc]),
            is_extended_id=msg.is_extended_id,
            is_fd=use_fd,
            bitrate_switch=entry.brs if use_fd else False,
            dlc=msg.dlc,
        )
        self._forward(modified)
        self._inj_batch += 1
        entry.tx_count += 1

    def _forward(self, msg) -> None:
        """Forward message sang destination bus."""
        try:
            with QMutexLocker(self._dest_mutex):
                self._dest.send(msg)
        except Exception as e:
            self._err_batch += 1
            self.error_occurred.emit(
                f"Forward error (0x{msg.arbitration_id:03X}): {e}"
            )

    def stop(self) -> None:
        self._running = False
        self.wait(3000)


# ==============================================================
# INJECT CAN TAB
# ==============================================================

class InjectCanTab(QWidget):
    """
    Tab inject CAN messages — man-in-the-middle.

    Đọc traffic từ TX channel (source), inject/modify signals
    cho messages đã chọn, forward TẤT CẢ sang RX channel (dest).
    """

    CRC_VARIANTS = list(CrcAlgorithm.VARIANTS.keys())

    def __init__(self, main: "MainWindow") -> None:
        print("[InjectCanTab INIT] STEP 0: Entering __init__")
        logger.info("[InjectCanTab INIT] STEP 0")
        super().__init__()
        self.main = main

        # State (init TRƯỚC _build_ui — không cần widgets)
        self._inject_entries: OrderedDict[int, TxEntry] = OrderedDict()
        self._entries_mutex = QMutex()
        self._current_edit_fid: int = -1

        # Threads (chỉ khai báo, KHÔNG start)
        self._inject_worker: Optional[InjectWorkerThread] = None
        self._hw_channels: List[Dict] = []

        # 2 buses: source (TX) + dest (RX)
        self._source_bus = None
        self._dest_bus = None
        self._source_mutex = QMutex()
        self._dest_mutex = QMutex()

        # Stats & Log state
        self._stats: Dict[str, int] = {"fwd": 0, "inj": 0, "err": 0}
        self._log_buffer: List[str] = []
        self._log_line_count: int = 0
        self._log_dropped: int = 0
        self._inject_logging: bool = INJECT_LOG
        self._passthrough_logging: bool = PASSTHROUGH_LOG

        # === Build UI TRƯỚC — tạo tất cả widgets ===
        self._build_ui()

        # === Sau _build_ui: timers cần widgets đã tồn tại ===
        self._ui_timer = QTimer()
        self._ui_timer.timeout.connect(self._ui_refresh)
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(STATS_UPDATE_MS)

        if hasattr(self.main, "networks_changed"):
            self.main.networks_changed.connect(self._refresh_networks)
        self.destroyed.connect(self._cleanup)
        print("[InjectCanTab INIT] ✓ DONE")
        logger.debug("InjectCanTab initialized")

    # ==========================================================
    # CLEANUP
    # ==========================================================

    def _cleanup(self) -> None:
        self._on_stop()
        self._disconnect()

    # ==========================================================
    # HELPERS
    # ==========================================================

    def _is_fd_mode(self) -> bool:
        return self.type_combo.currentText() == "CAN FD"

    def _get_data_bitrate(self) -> int:
        try:
            return int(self.data_baud_combo.currentText())
        except ValueError:
            return DEFAULT_FD_DATA_BITRATE

    # ==========================================================
    # UI BUILD
    # ==========================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(3, 3, 3, 3)
        root.setSpacing(2)
        root.addLayout(self._build_connect_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_dbc_browser())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([220, 800])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

    def _build_connect_bar(self) -> QHBoxLayout:
        """2 channel combos (TX source + RX dest) + CAN FD support."""
        bar = QHBoxLayout()
        bar.setSpacing(4)

        # CAN Type
        bar.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["CAN 2.0", "CAN FD"])
        self.type_combo.currentTextChanged.connect(self._on_can_type_changed)
        bar.addWidget(self.type_combo)

        # Arbitration Baud
        bar.addWidget(QLabel("Baud:"))
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText("500000")
        self.baud_combo.setEditable(True)
        bar.addWidget(self.baud_combo)

        # Data Baud (CAN FD only — ẩn mặc định)
        self.data_baud_label = QLabel("DataBaud:")
        self.data_baud_combo = QComboBox()
        self.data_baud_combo.addItems(FD_DATA_BITRATES)
        self.data_baud_combo.setCurrentText(str(DEFAULT_FD_DATA_BITRATE))
        self.data_baud_combo.setEditable(True)
        bar.addWidget(self.data_baud_label)
        bar.addWidget(self.data_baud_combo)
        self.data_baud_label.setVisible(False)
        self.data_baud_combo.setVisible(False)

        # TX Channel (Source)
        bar.addWidget(QLabel("Chn TX:"))
        self.tx_ch_combo = QComboBox()
        self.tx_ch_combo.setMinimumWidth(160)
        self.tx_ch_combo.setToolTip("Source — đọc CAN traffic từ đây")
        bar.addWidget(self.tx_ch_combo)

        # RX Channel (Dest)
        bar.addWidget(QLabel("Chn RX:"))
        self.rx_ch_combo = QComboBox()
        self.rx_ch_combo.setMinimumWidth(160)
        self.rx_ch_combo.setToolTip("Dest — forward traffic sang đây")
        bar.addWidget(self.rx_ch_combo)

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._scan_hw)
        bar.addWidget(self.scan_btn)

        # App name (Vector)
        bar.addWidget(QLabel("App:"))
        self.app_name_edit = QLineEdit("MS_Tool")
        self.app_name_edit.setMaximumWidth(100)
        bar.addWidget(self.app_name_edit)

        bar.addStretch()

        # CRC
        bar.addWidget(QLabel("CRC:"))
        self.crc_combo = QComboBox()
        self.crc_combo.addItems(self.CRC_VARIANTS)
        bar.addWidget(self.crc_combo)

        # Buttons
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMinimumWidth(90)
        self.connect_btn.setStyleSheet("QPushButton{font-weight:bold;background:#e3f2fd;}")
        self.connect_btn.clicked.connect(self._toggle_connect)
        bar.addWidget(self.connect_btn)

        self.start_btn = QPushButton("Start")
        self.start_btn.setStyleSheet("QPushButton{font-weight:bold;background:#d1e7dd;}")
        self.start_btn.clicked.connect(self._on_start)
        bar.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setStyleSheet("QPushButton{font-weight:bold;background:#f8d7da;}")
        self.stop_btn.clicked.connect(self._on_stop)
        bar.addWidget(self.stop_btn)

        return bar

    def _on_can_type_changed(self, text: str) -> None:
        is_fd = (text == "CAN FD")
        self.data_baud_label.setVisible(is_fd)
        self.data_baud_combo.setVisible(is_fd)

    def _build_dbc_browser(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)

        lay.addWidget(QLabel("Network"))
        self.net_combo = QComboBox()
        self.net_combo.currentTextChanged.connect(self._load_dbc_messages)
        lay.addWidget(self.net_combo)

        lay.addWidget(QLabel("Messages"))
        self.msg_list = QListWidget()
        self.msg_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.msg_list.itemClicked.connect(self._on_browser_msg_clicked)
        self.msg_list.itemDoubleClicked.connect(self._on_browser_msg_dblclick)
        lay.addWidget(self.msg_list)

        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self._add_selected_to_inject)
        lay.addWidget(self.add_btn)
        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(3)

        # Search
        sr = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search message/signal or 0xID...")
        self.search_edit.textChanged.connect(self._on_search)
        sr.addWidget(self.search_edit)
        sa = QPushButton("Add")
        sa.clicked.connect(self._add_search_result)
        sr.addWidget(sa)
        lay.addLayout(sr)

        # Signal Editor
        sg = QGroupBox("Signal Editor")
        sl = QVBoxLayout()
        self.msg_header = QLabel("Select or add a message")
        self.msg_header.setFont(QFont("", -1, QFont.Bold))
        sl.addWidget(self.msg_header)

        self.sig_table = QTableWidget()
        self.sig_table.setColumnCount(7)
        self.sig_table.setHorizontalHeaderLabels([
            "Signal", "Raw", "Physical", "Generator", "Start", "Length", "Comment",
        ])
        h = self.sig_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in SIG_EDITOR_COL_WIDTHS.items():
            h.setSectionResizeMode(col, QHeaderView.Fixed)
            self.sig_table.setColumnWidth(col, width)
        h.setSectionResizeMode(6, QHeaderView.Stretch)
        self.sig_table.cellChanged.connect(self._on_sig_cell_changed)
        sl.addWidget(self.sig_table)

        self.data_preview = QLineEdit()
        self.data_preview.setReadOnly(True)
        self.data_preview.setFont(QFont("Consolas", 10))
        sl.addWidget(self.data_preview)
        sg.setLayout(sl)
        lay.addWidget(sg)

        # Inject List (TX Queue) — thêm cột BRS cho CAN FD
        qg = QGroupBox("Inject List")
        ql = QVBoxLayout()
        self.tx_table = QTableWidget()
        self.tx_table.setColumnCount(8)
        self.tx_table.setHorizontalHeaderLabels([
            "Message", "ID", "DLC", "Cycle(ms)", "Mode", "Data", "BRS", "Active",
        ])
        th = self.tx_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in TX_TABLE_COL_WIDTHS.items():
            if col < 8:
                th.setSectionResizeMode(col, QHeaderView.Fixed)
                self.tx_table.setColumnWidth(col, width)
        th.setSectionResizeMode(5, QHeaderView.Stretch)
        th.setSectionResizeMode(6, QHeaderView.Fixed)
        self.tx_table.setColumnWidth(6, 35)
        th.setSectionResizeMode(7, QHeaderView.Fixed)
        self.tx_table.setColumnWidth(7, 45)
        self.tx_table.itemClicked.connect(self._on_queue_row_clicked)
        ql.addWidget(self.tx_table)

        br = QHBoxLayout()
        for text, slot in [("Remove", self._remove_from_inject), ("Clear All", self._clear_inject)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            br.addWidget(b)
        br.addStretch()
        ql.addLayout(br)
        qg.setLayout(ql)
        lay.addWidget(qg)

        # Stats + Log
        lb = QHBoxLayout()
        self.stats_label = QLabel("Fwd:0 Inj:0 Err:0")
        self.stats_label.setFont(QFont("Consolas", 9, QFont.Bold))
        lb.addWidget(self.stats_label)
        lb.addStretch()

        self.inj_log_cb = QCheckBox("Inject Log")
        self.inj_log_cb.setChecked(self._inject_logging)
        self.inj_log_cb.stateChanged.connect(
            lambda s: setattr(self, "_inject_logging", s == Qt.Checked))
        lb.addWidget(self.inj_log_cb)

        self.pass_log_cb = QCheckBox("Passthrough Log")
        self.pass_log_cb.setChecked(self._passthrough_logging)
        self.pass_log_cb.stateChanged.connect(
            lambda s: setattr(self, "_passthrough_logging", s == Qt.Checked))
        lb.addWidget(self.pass_log_cb)

        self.conn_label = QLabel("Disconnected")
        self.conn_label.setStyleSheet("color:red;font-weight:bold;")
        lb.addWidget(self.conn_label)

        cl = QPushButton("Clear")
        cl.setMaximumWidth(50)
        cl.clicked.connect(self._clear_log)
        lb.addWidget(cl)
        lay.addLayout(lb)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(LOG_BOX_HEIGHT)
        self.log_box.setFont(QFont("Consolas", 9))
        lay.addWidget(self.log_box)

        return w

    # ==========================================================
    # NETWORK / DBC
    # ==========================================================

    def _refresh_networks(self) -> None:
        cur = self.net_combo.currentText()
        self.net_combo.clear()
        if hasattr(self.main, "networks"):
            self.net_combo.addItems(self.main.networks.keys())
        if cur:
            self.net_combo.setCurrentText(cur)

    def _load_dbc_messages(self, network: str) -> None:
        self.msg_list.clear()
        if not network or not hasattr(self.main, "networks"):
            return
        net = self.main.networks.get(network)
        if not net or not getattr(net, "database", None):
            return
        for msg in net.database.messages:
            cycle = getattr(msg, "cycle_time", None)
            send_type = getattr(msg, "send_type", None)
            extra = f"DLC:{msg.length}"
            if cycle:
                extra += f"  {cycle}ms"
            if send_type:
                extra += f"  [{send_type}]"
            text = f"[0x{msg.frame_id:03X}] {msg.name} ({extra})"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, msg)
            self.msg_list.addItem(item)

    def _on_search(self, text: str) -> None:
        q = text.lower().strip()
        for i in range(self.msg_list.count()):
            item = self.msg_list.item(i)
            item.setHidden(bool(q) and q not in item.text().lower())

    def _add_search_result(self) -> None:
        for i in range(self.msg_list.count()):
            item = self.msg_list.item(i)
            if not item.isHidden():
                msg = item.data(Qt.UserRole)
                if msg:
                    self._add_dbc_msg_to_inject(msg)
                    return

    # ==========================================================
    # BROWSER → INJECT LIST
    # ==========================================================

    def _on_browser_msg_clicked(self, item) -> None:
        msg = item.data(Qt.UserRole)
        if msg:
            self._show_dbc_msg_signals(msg)

    def _on_browser_msg_dblclick(self, item) -> None:
        msg = item.data(Qt.UserRole)
        if msg:
            self._add_dbc_msg_to_inject(msg)

    def _add_selected_to_inject(self) -> None:
        for item in self.msg_list.selectedItems():
            msg = item.data(Qt.UserRole)
            if msg:
                self._add_dbc_msg_to_inject(msg)

    def _show_dbc_msg_signals(self, dbc_msg) -> None:
        """Preview DBC message signals (read-only)."""
        cycle = getattr(dbc_msg, "cycle_time", None)
        header = f"{dbc_msg.name} (0x{dbc_msg.frame_id:X})  DLC:{dbc_msg.length}"
        if cycle:
            header += f"  Cycle:{cycle}ms"
        self.msg_header.setText(header)
        self._current_edit_fid = -1

        self.sig_table.blockSignals(True)
        self.sig_table.setRowCount(len(dbc_msg.signals))
        for r, sig in enumerate(dbc_msg.signals):
            ni = QTableWidgetItem(getattr(sig, "name", ""))
            ni.setFlags(ni.flags() & ~Qt.ItemIsEditable)
            self.sig_table.setItem(r, 0, ni)
            self.sig_table.setItem(r, 1, QTableWidgetItem("0"))

            choices = {}
            if hasattr(sig, "choices") and sig.choices:
                try:
                    choices = dict(sig.choices)
                except Exception:
                    pass
            if choices:
                combo = QComboBox()
                for rv, txt in choices.items():
                    combo.addItem(f"[{rv}] {txt}", rv)
                self.sig_table.setCellWidget(r, 2, combo)
            else:
                self.sig_table.setItem(r, 2, QTableWidgetItem(
                    str(getattr(sig, "offset", 0))))

            # Generator mặc định: Passthrough cho Inject, auto-detect cho special
            gc = QComboBox()
            gc.addItems(INJECT_GENERATOR_OPTIONS)
            auto = SignalTypeDetector.detect(sig.name)
            gc.setCurrentText("Passthrough" if auto == "None" else auto)
            self.sig_table.setCellWidget(r, 3, gc)

            for col, attr in [(4, "start"), (5, "length")]:
                it = QTableWidgetItem(str(getattr(sig, attr, 0)))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.sig_table.setItem(r, col, it)
            ci = QTableWidgetItem(getattr(sig, "comment", "") or "")
            ci.setFlags(ci.flags() & ~Qt.ItemIsEditable)
            self.sig_table.setItem(r, 6, ci)

        self.sig_table.blockSignals(False)
        self.data_preview.setText(
            f"[0x{dbc_msg.frame_id:X}] DLC:{dbc_msg.length} DATA: "
            + "00 " * dbc_msg.length)

    # ==========================================================
    # INJECT LIST MANAGEMENT
    # ==========================================================

    def _add_dbc_msg_to_inject(self, dbc_msg) -> None:
        """Tạo TxEntry với mặc định Passthrough cho non-special signals."""
        if dbc_msg.frame_id in self._inject_entries:
            self._log(f"Already in inject list: {dbc_msg.name}")
            self._load_entry_to_editor(dbc_msg.frame_id)
            return

        signals: List[Dict] = []
        for sig in dbc_msg.signals:
            bo = "little_endian"
            if hasattr(sig, "byte_order") and "big" in str(sig.byte_order).lower():
                bo = "big_endian"
            choices = {}
            if hasattr(sig, "choices") and sig.choices:
                try:
                    choices = dict(sig.choices)
                except Exception:
                    pass

            auto_gen = SignalTypeDetector.detect(sig.name)
            if auto_gen == "None":
                auto_gen = "Passthrough"

            signals.append({
                "name": getattr(sig, "name", ""),
                "start": getattr(sig, "start", 0),
                "length": getattr(sig, "length", 0),
                "byte_order": bo,
                "factor": getattr(sig, "scale", 1.0),
                "offset": getattr(sig, "offset", 0.0),
                "raw": 0,
                "choices": choices,
                "generator": auto_gen,
                "comment": getattr(sig, "comment", "") or "",
            })

        is_fd = self._is_fd_mode() or dbc_msg.length > MAX_DLC[CanType.CLASSIC]
        entry = TxEntry(
            name=dbc_msg.name, frame_id=dbc_msg.frame_id,
            dlc=dbc_msg.length, network=self.net_combo.currentText(),
            signals=signals, crc_variant=self.crc_combo.currentText(),
            is_fd=is_fd, brs=is_fd,
        )
        dbc_cycle = getattr(dbc_msg, "cycle_time", None)
        entry.cycle_ms = int(dbc_cycle) if dbc_cycle and dbc_cycle > 0 else DEFAULT_CYCLE_MS
        entry.send_mode = "Cyclic"

        with QMutexLocker(self._entries_mutex):
            self._inject_entries[dbc_msg.frame_id] = entry

        self._refresh_tx_table()
        self._load_entry_to_editor(dbc_msg.frame_id)
        self._log(f"Added to inject: {dbc_msg.name} (0x{dbc_msg.frame_id:X})"
                  + (" [FD]" if is_fd else ""))

    def _load_entry_to_editor(self, frame_id: int) -> None:
        """Load inject entry vào signal editor (editable)."""
        entry = self._inject_entries.get(frame_id)
        if not entry:
            return
        self._current_edit_fid = frame_id
        fd_tag = " [FD]" if entry.is_fd else ""
        self.msg_header.setText(
            f"{entry.name} (0x{entry.frame_id:X})  DLC:{entry.dlc}"
            f"{fd_tag}  [Inject - editable]")

        self.sig_table.blockSignals(True)
        self.sig_table.setRowCount(len(entry.signals))
        for r, sig in enumerate(entry.signals):
            ni = QTableWidgetItem(sig["name"])
            ni.setFlags(ni.flags() & ~Qt.ItemIsEditable)
            self.sig_table.setItem(r, 0, ni)
            self.sig_table.setItem(r, 1, QTableWidgetItem(str(sig["raw"])))

            if sig.get("choices"):
                combo = QComboBox()
                for rv, txt in sig["choices"].items():
                    combo.addItem(f"[{rv}] {txt}", rv)
                for idx in range(combo.count()):
                    if combo.itemData(idx) == sig["raw"]:
                        combo.setCurrentIndex(idx)
                        break
                combo.currentIndexChanged.connect(
                    lambda _, row=r: self._on_choice_changed(row))
                self.sig_table.setCellWidget(r, 2, combo)
            else:
                phys = sig["raw"] * sig["factor"] + sig["offset"]
                self.sig_table.setItem(r, 2, QTableWidgetItem(str(round(phys, 3))))

            gc = QComboBox()
            gc.addItems(INJECT_GENERATOR_OPTIONS)
            gc.setCurrentText(sig.get("generator", "Passthrough"))
            gc.currentTextChanged.connect(
                lambda val, row=r: self._on_gen_changed(row, val))
            self.sig_table.setCellWidget(r, 3, gc)

            for col, key in [(4, "start"), (5, "length")]:
                it = QTableWidgetItem(str(sig[key]))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.sig_table.setItem(r, col, it)
            ci = QTableWidgetItem(sig["comment"])
            ci.setFlags(ci.flags() & ~Qt.ItemIsEditable)
            self.sig_table.setItem(r, 6, ci)

        self.sig_table.blockSignals(False)
        self._update_data_preview(entry)

    # ==========================================================
    # SIGNAL EDITING
    # ==========================================================

    def _on_sig_cell_changed(self, row: int, col: int) -> None:
        if self._current_edit_fid < 0:
            return
        entry = self._inject_entries.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals):
            return
        sig = entry.signals[row]

        if col == 1:
            item = self.sig_table.item(row, 1)
            if not item:
                return
            try:
                raw = int(item.text())
            except ValueError:
                return
            max_val = (1 << sig["length"]) - 1
            raw = max(0, min(raw, max_val))
            sig["raw"] = raw
            if not sig.get("choices"):
                phys = raw * sig["factor"] + sig["offset"]
                self.sig_table.blockSignals(True)
                pi = self.sig_table.item(row, 2)
                if pi:
                    pi.setText(str(round(phys, 3)))
                self.sig_table.blockSignals(False)

        elif col == 2 and not sig.get("choices"):
            item = self.sig_table.item(row, 2)
            if not item:
                return
            try:
                phys = float(item.text())
            except ValueError:
                return
            if sig["factor"] != 0:
                raw = int(round((phys - sig["offset"]) / sig["factor"]))
                max_val = (1 << sig["length"]) - 1
                raw = max(0, min(raw, max_val))
                sig["raw"] = raw
                self.sig_table.blockSignals(True)
                ri = self.sig_table.item(row, 1)
                if ri:
                    ri.setText(str(raw))
                self.sig_table.blockSignals(False)
        else:
            return

        entry.encode_all_signals()
        self._update_data_preview(entry)
        self._refresh_tx_data_col(entry.frame_id)

    def _on_choice_changed(self, row: int) -> None:
        if self._current_edit_fid < 0:
            return
        entry = self._inject_entries.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals):
            return
        combo = self.sig_table.cellWidget(row, 2)
        if not combo:
            return
        sig = entry.signals[row]
        sig["raw"] = combo.currentData()
        self.sig_table.blockSignals(True)
        ri = self.sig_table.item(row, 1)
        if ri:
            ri.setText(str(sig["raw"]))
        self.sig_table.blockSignals(False)
        entry.encode_all_signals()
        self._update_data_preview(entry)
        self._refresh_tx_data_col(entry.frame_id)

    def _on_gen_changed(self, row: int, val: str) -> None:
        if self._current_edit_fid < 0:
            return
        entry = self._inject_entries.get(self._current_edit_fid)
        if not entry or row >= len(entry.signals):
            return
        entry.signals[row]["generator"] = val
        sig = entry.signals[row]
        entry.ensure_counter(sig["name"], val, sig["length"])

    def _update_data_preview(self, entry: TxEntry) -> None:
        fd = " [FD]" if entry.is_fd else ""
        self.data_preview.setText(
            f"[0x{entry.frame_id:X}] DLC:{entry.dlc}{fd} DATA: {entry.get_data_hex()}")

    # ==========================================================
    # INJECT LIST TABLE
    # ==========================================================

    def _refresh_tx_table(self) -> None:
        self.tx_table.setRowCount(len(self._inject_entries))
        for row, (fid, e) in enumerate(self._inject_entries.items()):
            ni = QTableWidgetItem(e.name)
            ni.setFlags(ni.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 0, ni)

            ii = QTableWidgetItem(f"0x{e.frame_id:X}")
            ii.setFlags(ii.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 1, ii)

            di = QTableWidgetItem(str(e.dlc))
            di.setFlags(di.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 2, di)

            # Cycle/Mode: chỉ hiển thị (inject không dùng cycle)
            cs = QSpinBox()
            cs.setRange(1, MAX_CYCLE_MS)
            cs.setValue(e.cycle_ms)
            cs.setSuffix("ms")
            cs.setEnabled(False)
            self.tx_table.setCellWidget(row, 3, cs)

            mc = QComboBox()
            mc.addItems(TxEntry.MODES)
            mc.setCurrentText(e.send_mode)
            mc.setEnabled(False)
            self.tx_table.setCellWidget(row, 4, mc)

            dti = QTableWidgetItem(e.get_data_hex())
            dti.setFlags(dti.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 5, dti)

            # BRS checkbox (CAN FD)
            brs_cb = QCheckBox()
            brs_cb.setChecked(e.brs)
            brs_cb.setEnabled(e.is_fd)
            brs_cb.setToolTip("Bit Rate Switch (CAN FD)")
            brs_cb.stateChanged.connect(lambda s, f=fid: self._set_brs(f, s))
            brs_w = QWidget()
            brs_l = QHBoxLayout(brs_w)
            brs_l.addWidget(brs_cb)
            brs_l.setAlignment(Qt.AlignCenter)
            brs_l.setContentsMargins(0, 0, 0, 0)
            self.tx_table.setCellWidget(row, 6, brs_w)

            # Active checkbox
            cb = QCheckBox()
            cb.setChecked(e.active)
            cb.stateChanged.connect(lambda s, f=fid: self._set_active(f, s))
            cw = QWidget()
            cl_ = QHBoxLayout(cw)
            cl_.addWidget(cb)
            cl_.setAlignment(Qt.AlignCenter)
            cl_.setContentsMargins(0, 0, 0, 0)
            self.tx_table.setCellWidget(row, 7, cw)

            color = QColor(220, 255, 220) if e.active else QColor(240, 240, 240)
            for c in range(self.tx_table.columnCount()):
                it = self.tx_table.item(row, c)
                if it:
                    it.setBackground(color)

    def _refresh_tx_data_col(self, frame_id: int) -> None:
        for row, (fid, e) in enumerate(self._inject_entries.items()):
            if fid == frame_id:
                it = self.tx_table.item(row, 5)
                if it:
                    it.setText(e.get_data_hex())
                break

    def _on_queue_row_clicked(self, item) -> None:
        row = item.row()
        keys = list(self._inject_entries.keys())
        if row < len(keys):
            self._load_entry_to_editor(keys[row])

    def _remove_from_inject(self) -> None:
        rows = {it.row() for it in self.tx_table.selectedItems()}
        keys = list(self._inject_entries.keys())
        with QMutexLocker(self._entries_mutex):
            for r in sorted(rows, reverse=True):
                if r < len(keys):
                    del self._inject_entries[keys[r]]
        self._refresh_tx_table()

    def _clear_inject(self) -> None:
        with QMutexLocker(self._entries_mutex):
            self._inject_entries.clear()
        self._refresh_tx_table()
        self._current_edit_fid = -1
        self._log("Inject list cleared")

    def _set_active(self, fid, s):
        if fid in self._inject_entries:
            self._inject_entries[fid].active = (s == Qt.Checked)

    def _set_brs(self, fid, s):
        if fid in self._inject_entries:
            self._inject_entries[fid].brs = (s == Qt.Checked)

    # ==========================================================
    # HW CONNECTION — 2 channels
    # ==========================================================

    def _scan_hw(self) -> None:
        """
        🔧 CRITICAL FIX: Scan ĐỒNG BỘ, KHÔNG dùng QThread.
        Giống như SendCanTab — loại bỏ crash 0xC0000409 do
        QuickScanThread lifecycle.
        """
        print("[InjectCanTab._scan_hw] CALLED (synchronous)")
        logger.info("[InjectCanTab._scan_hw] CALLED")

        if not is_can_available():
            self._log("ERROR: python-can not installed")
            return

        self.scan_btn.setEnabled(False)
        try:
            channels = self._do_scan_sync()
            print(f"[InjectCanTab._scan_hw] Found {len(channels)} channels")
        except Exception as e:
            print(f"[InjectCanTab._scan_hw] Scan failed: {e}")
            logger.exception("Inject scan failed")
            channels = []
        finally:
            self.scan_btn.setEnabled(True)

        self._on_scan_done(channels)

    def _do_scan_sync(self) -> List[Dict]:
        """Scan hardware channels đồng bộ."""
        channels: List[Dict] = []
        try:
            import can
        except ImportError:
            return channels

        # --- Vector ---
        try:
            from can.interfaces.vector.xldefine import XL_HardwareType as XL
            for hw in can.detect_available_configs(interfaces=["vector"]):
                ch = int(hw.get("channel", 0))
                ht = hw.get("hw_type", "")
                try:
                    model = XL(ht).name.replace("XL_HWTYPE_", "")
                except Exception:
                    model = str(ht)
                channels.append({
                    "label": f"Vector {model} Ch{ch + 1}",
                    "interface": "vector",
                    "channel": ch,
                    "hw_type": hw.get("hw_type", 0),
                    "hw_index": hw.get("hw_index", 0),
                    "hw_channel": hw.get("hw_channel", ch),
                    "supports_fd": True,
                })
        except Exception as e:
            logger.debug(f"Vector scan skipped: {e}")

        # --- PEAK ---
        try:
            for hw in can.detect_available_configs(interfaces=["pcan"]):
                ch = hw.get("channel", "")
                channels.append({
                    "label": f"PEAK {ch}",
                    "interface": "pcan",
                    "channel": str(ch),
                    "supports_fd": True,
                })
        except Exception as e:
            logger.debug(f"PCAN scan skipped: {e}")

        # --- SocketCAN (Linux only) ---
        if sys.platform != "win32":
            try:
                import subprocess
                result = subprocess.run(
                    ["ip", "link", "show", "type", "can"],
                    capture_output=True, text=True, timeout=3,
                )
                for line in result.stdout.splitlines():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        iface = parts[1].strip()
                        if iface:
                            channels.append({
                                "label": f"SocketCAN {iface}",
                                "interface": "socketcan",
                                "channel": iface,
                                "supports_fd": True,
                            })
            except Exception:
                pass

        # --- Virtual ---
        for i in range(3):
            channels.append({
                "label": f"Virtual Ch{i}",
                "interface": "virtual",
                "channel": i,
                "supports_fd": True,
            })

        return channels

    def _on_scan_done(self, channels: List[Dict]) -> None:
        self._hw_channels = channels
        self.tx_ch_combo.clear()
        self.rx_ch_combo.clear()
        for ch in channels:
            label = ch["label"] + (" [FD]" if ch.get("supports_fd") else "")
            self.tx_ch_combo.addItem(label, ch)
            self.rx_ch_combo.addItem(label, ch)
        if len(channels) >= 2:
            self.tx_ch_combo.setCurrentIndex(0)
            self.rx_ch_combo.setCurrentIndex(1)
        self._log(f"Scan: {len(channels)} channel(s)")

    def _toggle_connect(self) -> None:
        if self._source_bus or self._dest_bus:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        """Kết nối 2 channels: source (TX) + dest (RX)."""
        if _get_can() is None:
            self._log("ERROR: python-can not installed")
            return

        tx_hw = self.tx_ch_combo.currentData()
        rx_hw = self.rx_ch_combo.currentData()
        if not tx_hw or not rx_hw:
            self._log("ERROR: Select both TX and RX channels")
            return

        # Warn nếu cùng physical channel
        if (tx_hw["interface"] == rx_hw["interface"]
                and tx_hw["channel"] == rx_hw["channel"]
                and tx_hw["interface"] != "virtual"):
            self._log("WARNING: TX and RX are the same physical channel!")

        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            self._log("ERROR: Invalid baud rate")
            return

        is_fd = self._is_fd_mode()
        kwargs: Dict = {"bitrate": baud}
        if is_fd:
            kwargs["fd"] = True
            kwargs["data_bitrate"] = self._get_data_bitrate()

        try:
            self._source_bus = self._open_bus(tx_hw, kwargs, "TX Source")
            if not self._source_bus:
                return
            self._dest_bus = self._open_bus(rx_hw, kwargs, "RX Dest")
            if not self._dest_bus:
                self._safe_shutdown_bus(self._source_bus)
                self._source_bus = None
                return

            self.connect_btn.setText("Disconnect")
            self.connect_btn.setStyleSheet("QPushButton{font-weight:bold;background:#d1e7dd;}")
            mode = "CAN FD" if is_fd else "CAN 2.0"
            self.conn_label.setText(f"TX:{tx_hw['label']} → RX:{rx_hw['label']} [{mode}]")
            self.conn_label.setStyleSheet("color:green;font-weight:bold;")
            self._log(f"Connected: TX={tx_hw['label']}, RX={rx_hw['label']} @ {baud}"
                      + (f" [FD data:{self._get_data_bitrate()}]" if is_fd else ""))

        except Exception as e:
            self._log(f"Connect FAILED: {e}")
            self._shutdown_buses()

    def _open_bus(self, hw: Dict, kwargs: Dict, label: str):
        """Mở 1 CAN bus channel."""
        can = _get_can()
        if can is None:
            self._log(f"{label}: python-can not available")
            return None
        try:
            iface = hw["interface"]
            ch = hw["channel"]
            app_name = self.app_name_edit.text().strip()

            if iface == "vector":
                bus_kwargs = {**kwargs, "receive_own_messages": True}
                if app_name:
                    try:
                        bus = can.interface.Bus(
                            interface="vector", channel=ch,
                            app_name=app_name, **bus_kwargs)
                    except Exception:
                        bus = can.interface.Bus(
                            interface="vector", channel=ch, **bus_kwargs)
                else:
                    bus = can.interface.Bus(
                        interface="vector", channel=ch, **bus_kwargs)
            elif iface == "pcan":
                bus = can.interface.Bus(interface="pcan", channel=str(ch), **kwargs)
            else:
                bus = can.interface.Bus(interface=iface, channel=ch, **kwargs)

            self._log(f"{label} opened: {hw['label']}")
            return bus
        except Exception as e:
            self._log(f"{label} open FAILED: {e}")
            return None

    def _disconnect(self) -> None:
        self._on_stop()
        self._shutdown_buses()
        self.connect_btn.setText("Connect")
        self.connect_btn.setStyleSheet("QPushButton{font-weight:bold;background:#e3f2fd;}")
        self.conn_label.setText("Disconnected")
        self.conn_label.setStyleSheet("color:red;font-weight:bold;")
        self._log("Disconnected")

    def _shutdown_buses(self) -> None:
        self._safe_shutdown_bus(self._source_bus)
        self._safe_shutdown_bus(self._dest_bus)
        self._source_bus = None
        self._dest_bus = None

    @staticmethod
    def _safe_shutdown_bus(bus) -> None:
        if bus:
            try:
                bus.shutdown()
            except Exception:
                pass

    # ==========================================================
    # START / STOP INJECT
    # ==========================================================

    def _on_start(self) -> None:
        if not self._source_bus or not self._dest_bus:
            self._log("ERROR: Not connected (need both TX and RX channels)")
            return
        if self._inject_worker and self._inject_worker.isRunning():
            self._on_stop()

        # Reset counters
        for entry in self._inject_entries.values():
            entry.reset_counters()

        is_fd = self._is_fd_mode()
        self._inject_worker = InjectWorkerThread(
            source_bus=self._source_bus,
            dest_bus=self._dest_bus,
            source_mutex=self._source_mutex,
            dest_mutex=self._dest_mutex,
            inject_entries=self._inject_entries,
            entries_mutex=self._entries_mutex,
            is_fd=is_fd, parent=self,
        )
        self._inject_worker.stats_updated.connect(self._on_worker_stats)
        self._inject_worker.error_occurred.connect(self._on_worker_error)
        self._inject_worker.start()

        self._ui_timer.start(UI_REFRESH_MS)
        self.start_btn.setStyleSheet("QPushButton{font-weight:bold;background:#a8e6a3;}")

        inj_count = sum(1 for e in self._inject_entries.values() if e.active)
        self._log(f"INJECT STARTED: {inj_count} rule(s), others passthrough")

    def _on_stop(self) -> None:
        self._ui_timer.stop()
        if self._inject_worker:
            self._inject_worker.stop()
            self._inject_worker = None
        self.start_btn.setStyleSheet("QPushButton{font-weight:bold;background:#d1e7dd;}")
        self._flush_log_buffer()
        self._log("INJECT STOPPED")

    def _on_worker_stats(self, fwd, inj, err):
        self._stats["fwd"] += fwd
        self._stats["inj"] += inj
        self._stats["err"] += err

    def _on_worker_error(self, msg):
        self._log(f"[ERR] {msg}")

    # ==========================================================
    # UI REFRESH
    # ==========================================================

    def _ui_refresh(self) -> None:
        if self._current_edit_fid >= 0:
            entry = self._inject_entries.get(self._current_edit_fid)
            if entry:
                self._update_signal_table_live(entry)
                self._update_data_preview(entry)
        for fid, entry in self._inject_entries.items():
            if entry.active:
                self._refresh_tx_data_col(fid)
        self._flush_log_buffer()

    def _update_signal_table_live(self, entry: TxEntry) -> None:
        """Cập nhật Raw/Physical cho TẤT CẢ signals (kể cả Passthrough)."""
        self.sig_table.blockSignals(True)
        try:
            for r, sig in enumerate(entry.signals):
                ri = self.sig_table.item(r, 1)
                if ri:
                    ri.setText(str(sig["raw"]))
                if not sig.get("choices"):
                    phys = sig["raw"] * sig["factor"] + sig["offset"]
                    pi = self.sig_table.item(r, 2)
                    if pi:
                        pi.setText(str(round(phys, 3)))
        finally:
            self.sig_table.blockSignals(False)

    # ==========================================================
    # STATS / LOG
    # ==========================================================

    def _update_stats(self) -> None:
        self.stats_label.setText(
            f"Fwd:{self._stats['fwd']} Inj:{self._stats['inj']} Err:{self._stats['err']}")

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_box.append(f"[{ts}] {msg}")
        self._log_line_count += 1
        self._trim_log()
        logger.debug("InjectCAN: %s", msg)

    def _log_buffered(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._log_buffer.append(f"[{ts}] {msg}")
        if len(self._log_buffer) > LOG_BUFFER_MAX:
            dropped = len(self._log_buffer) - LOG_BUFFER_KEEP
            self._log_dropped += dropped
            self._log_buffer = self._log_buffer[-LOG_BUFFER_KEEP:]

    def _flush_log_buffer(self) -> None:
        if not self._log_buffer:
            return
        lines = self._log_buffer
        self._log_buffer = []
        if self._log_dropped > 0:
            lines.insert(0, f"  ... ({self._log_dropped} lines dropped)")
            self._log_dropped = 0
        if len(lines) > LOG_FLUSH_MAX_SHOW:
            skipped = len(lines) - LOG_FLUSH_MAX_SHOW
            lines = lines[-LOG_FLUSH_MAX_SHOW:]
            lines.insert(0, f"  ... ({skipped} lines skipped)")
        self.log_box.append("\n".join(lines))
        self._log_line_count += len(lines)
        self._trim_log()

    def _trim_log(self) -> None:
        if self._log_line_count > MAX_LOG_LINES:
            cur = self.log_box.textCursor()
            cur.movePosition(cur.Start)
            for _ in range(MAX_LOG_LINES // 2):
                cur.movePosition(cur.Down, cur.KeepAnchor)
            cur.removeSelectedText()
            self._log_line_count -= MAX_LOG_LINES // 2

    def _clear_log(self) -> None:
        self.log_box.clear()
        self._log_buffer.clear()
        self._log_line_count = 0
        self._log_dropped = 0