"""
ui/tab_send_can.py — Send CAN Tab (UI Layer).

"""

import json
import logging
from typing import Dict, List, Optional
from datetime import datetime
from collections import OrderedDict

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QPushButton, QComboBox, QLineEdit, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QListWidget,
    QListWidgetItem, QGroupBox, QHeaderView,
    QCheckBox, QAbstractItemView, QFileDialog,
)
from PyQt5.QtCore import Qt, QTimer, QMutex, QMutexLocker
from PyQt5.QtGui import QFont, QColor

from core.core import (
    CanType, MAX_DLC,
    UI_REFRESH_MS, STATS_UPDATE_MS,
    MAX_CYCLE_MS, DEFAULT_CYCLE_MS, DEFAULT_FD_DATA_BITRATE,
    MAX_LOG_LINES, LOG_BUFFER_MAX, LOG_BUFFER_KEEP,
    LOG_FLUSH_MAX_SHOW, LOG_BOX_HEIGHT,
    TX_LOG_ENABLED, RX_LOG_ENABLED,
    BAUD_RATES, FD_DATA_BITRATES,
    SIG_EDITOR_COL_WIDTHS, TX_TABLE_COL_WIDTHS,
    GENERATOR_OPTIONS,
    CrcAlgorithm, SignalTypeDetector,
)
from core.tx_entry import TxEntry
from core.threads import (
    TxWorkerThread, RxReaderThread, QuickScanThread, is_can_available,
)
from core.bus_manager import BusManager

logger = logging.getLogger(__name__)


class SendCanTab(QWidget):


    CRC_VARIANTS = list(CrcAlgorithm.VARIANTS.keys())

    def __init__(self, main: "MainWindow") -> None:
        # ==========================================================
        # 🐛 DEBUG: Init startup với step-by-step logging
        # ==========================================================
        print("[SendCanTab INIT] STEP 0: Entering __init__")
        logger.info("[SendCanTab INIT] STEP 0: Entering __init__")

        try:
            print("[SendCanTab INIT] STEP 1: super().__init__()")
            super().__init__()
            print("[SendCanTab INIT] STEP 1: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 1 FAILED: {e}")
            logger.exception("STEP 1 super().__init__() failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 2: Assign self.main")
            self.main = main
            print(f"[SendCanTab INIT] STEP 2: OK (main={type(main).__name__})")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 2 FAILED: {e}")
            logger.exception("STEP 2 assign main failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 3: Init state (queue, mutex, edit_fid)")
            # State (init TRƯỚC _build_ui vì không cần widgets)
            self._tx_queue: OrderedDict[int, TxEntry] = OrderedDict()
            self._queue_mutex = QMutex()
            self._current_edit_fid: int = -1
            print("[SendCanTab INIT] STEP 3: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 3 FAILED: {e}")
            logger.exception("STEP 3 init state failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 4: Init thread placeholders")
            # Threads (chỉ khai báo, KHÔNG start)
            self._tx_worker: Optional[TxWorkerThread] = None
            self._rx_thread: Optional[RxReaderThread] = None
            self._scan_thread: Optional[QuickScanThread] = None
            self._hw_channels: List[Dict] = []

            self._bus = None  # Sẽ được gán ở _do_connect()
            self._bus_mutex = QMutex()  # Dùng chung với BusManager

            print("[SendCanTab INIT] STEP 4: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 4 FAILED: {e}")
            logger.exception("STEP 4 init thread placeholders failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 5: Init stats & log state")
            # Stats & Log state (init TRƯỚC _build_ui)
            self._stats: Dict[str, int] = {"tx": 0, "rx": 0, "err": 0}
            self._log_buffer: List[str] = []
            self._log_line_count: int = 0
            self._log_dropped: int = 0
            self._tx_logging: bool = TX_LOG_ENABLED
            self._rx_logging: bool = RX_LOG_ENABLED
            print("[SendCanTab INIT] STEP 5: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 5 FAILED: {e}")
            logger.exception("STEP 5 init stats/log failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 6: Build UI (_build_ui)")
            # === Build UI TRƯỚC — tạo tất cả widgets ===
            self._build_ui()
            print("[SendCanTab INIT] STEP 6: OK (UI built)")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 6 FAILED: {e}")
            logger.exception("STEP 6 _build_ui() failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 7: Create BusManager")
            # === Sau _build_ui: mọi thứ cần widgets (log_box, stats_label) ===
            # BusManager dùng self._log callback → cần log_box đã tồn tại
            self._bus_mgr = BusManager(log_fn=self._log)
            print("[SendCanTab INIT] STEP 7: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 7 FAILED: {e}")
            logger.exception("STEP 7 BusManager creation failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 8: Setup timers (ui_timer, stats_timer)")
            # Timers: start SAU _build_ui vì callback cần widgets
            self._ui_timer = QTimer()
            self._ui_timer.timeout.connect(self._ui_refresh)
            self._stats_timer = QTimer()
            self._stats_timer.timeout.connect(self._update_stats)
            self._stats_timer.start(STATS_UPDATE_MS)
            print("[SendCanTab INIT] STEP 8: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 8 FAILED: {e}")
            logger.exception("STEP 8 timers setup failed")
            raise

        try:
            print("[SendCanTab INIT] STEP 9: Connect signals (networks_changed, destroyed)")
            if hasattr(self.main, "networks_changed"):
                self.main.networks_changed.connect(self._refresh_networks)
                print("[SendCanTab INIT] STEP 9: networks_changed connected")
            else:
                print("[SendCanTab INIT] STEP 9: main has no networks_changed (skip)")
            self.destroyed.connect(self._cleanup)
            print("[SendCanTab INIT] STEP 9: OK")
        except Exception as e:
            print(f"[SendCanTab INIT] STEP 9 FAILED: {e}")
            logger.exception("STEP 9 signal connections failed")
            raise

        print("[SendCanTab INIT] ✓ ALL STEPS COMPLETE — __init__ finished OK")
        logger.info("[SendCanTab INIT] ✓ ALL STEPS COMPLETE")

    # ==========================================================
    # CLEANUP
    # ==========================================================

    def _cleanup(self) -> None:
        """
        🔧 CHANGE: Stop TOÀN BỘ threads trước shutdown
        """
        # Stop all activity
        self._on_stop()

        # Stop RX thread
        if self._rx_thread:
            if self._rx_thread.isRunning():
                self._rx_thread.stop()
                self._rx_thread.wait()
            self._rx_thread = None

        # Stop TX worker (double-check)
        if self._tx_worker:
            if self._tx_worker.isRunning():
                self._tx_worker.stop()
                self._tx_worker.wait()
            self._tx_worker = None

        # Disconnect bus (nó gọi _safe_shutdown())
        self._bus_mgr.disconnect()

        logger.debug("SendCanTab cleaned up")

    # ==========================================================
    # UI BUILD
    # ==========================================================

    def _build_ui(self) -> None:
        # 🐛 DEBUG: Sub-steps của _build_ui
        print("  [_build_ui] STEP 6.1: Create root QVBoxLayout")
        root = QVBoxLayout(self)
        root.setContentsMargins(3, 3, 3, 3)
        root.setSpacing(2)

        print("  [_build_ui] STEP 6.2: Build connect bar")
        root.addLayout(self._build_connect_bar())

        print("  [_build_ui] STEP 6.3: Create splitter")
        splitter = QSplitter(Qt.Horizontal)

        print("  [_build_ui] STEP 6.4: Build DBC browser (left panel)")
        splitter.addWidget(self._build_dbc_browser())

        print("  [_build_ui] STEP 6.5: Build right panel (signal editor + TX queue + log)")
        splitter.addWidget(self._build_right_panel())

        print("  [_build_ui] STEP 6.6: Finalize splitter")
        splitter.setSizes([220, 800])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)
        print("  [_build_ui] STEP 6 COMPLETE")

    def _build_connect_bar(self) -> QHBoxLayout:
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

        # Channel
        bar.addWidget(QLabel("Channel:"))
        self.ch_combo = QComboBox()
        self.ch_combo.setMinimumWidth(180)
        bar.addWidget(self.ch_combo)

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

        self.send_btn = QPushButton("SEND")
        self.send_btn.setStyleSheet("QPushButton{font-weight:bold;background:#d1e7dd;}")
        self.send_btn.clicked.connect(self._on_send)
        bar.addWidget(self.send_btn)

        self.stop_btn = QPushButton("STOP")
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

        self.add_btn = QPushButton("Add Selected >>")
        self.add_btn.clicked.connect(self._add_selected_to_queue)
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

        # TX Queue — thêm cột BRS cho CAN FD
        qg = QGroupBox("TX Queue")
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
        for text, slot in [
            ("Remove", self._remove_from_queue),
            ("Clear All", self._clear_queue),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            br.addWidget(b)
        br.addStretch()
        for text, slot in [
            ("Export...", self._export_queue),
            ("Import...", self._import_queue),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            br.addWidget(b)
        ql.addLayout(br)
        qg.setLayout(ql)
        lay.addWidget(qg)

        # Stats + Log
        lb = QHBoxLayout()
        self.stats_label = QLabel("TX:0 RX:0 Err:0")
        self.stats_label.setFont(QFont("Consolas", 9, QFont.Bold))
        lb.addWidget(self.stats_label)
        lb.addStretch()

        self.tx_log_cb = QCheckBox("TX Log")
        self.tx_log_cb.setChecked(self._tx_logging)
        self.tx_log_cb.stateChanged.connect(
            lambda s: setattr(self, "_tx_logging", s == Qt.Checked))
        lb.addWidget(self.tx_log_cb)

        self.rx_log_cb = QCheckBox("RX Log")
        self.rx_log_cb.setChecked(self._rx_logging)
        self.rx_log_cb.stateChanged.connect(
            lambda s: setattr(self, "_rx_logging", s == Qt.Checked))
        lb.addWidget(self.rx_log_cb)

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
                    self._add_dbc_msg_to_queue(msg)
                    return

    # ==========================================================
    # BROWSER → QUEUE
    # ==========================================================

    def _on_browser_msg_clicked(self, item) -> None:
        msg = item.data(Qt.UserRole)
        if msg:
            self._show_dbc_msg_signals(msg)

    def _on_browser_msg_dblclick(self, item) -> None:
        msg = item.data(Qt.UserRole)
        if msg:
            self._add_dbc_msg_to_queue(msg)

    def _add_selected_to_queue(self) -> None:
        for item in self.msg_list.selectedItems():
            msg = item.data(Qt.UserRole)
            if msg:
                self._add_dbc_msg_to_queue(msg)

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

            gc = QComboBox()
            gc.addItems(GENERATOR_OPTIONS)
            gc.setCurrentText(SignalTypeDetector.detect(sig.name))
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
    # TX QUEUE MANAGEMENT
    # ==========================================================

    def _add_dbc_msg_to_queue(self, dbc_msg) -> None:
        if dbc_msg.frame_id in self._tx_queue:
            self._log(f"Already in queue: {dbc_msg.name}")
            self._load_tx_entry_to_editor(dbc_msg.frame_id)
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
            signals.append({
                "name": getattr(sig, "name", ""),
                "start": getattr(sig, "start", 0),
                "length": getattr(sig, "length", 0),
                "byte_order": bo,
                "factor": getattr(sig, "scale", 1.0),
                "offset": getattr(sig, "offset", 0.0),
                "raw": 0,
                "choices": choices,
                "generator": SignalTypeDetector.detect(sig.name),
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

        stype_raw = getattr(dbc_msg, "send_type", None)
        if stype_raw:
            st = str(stype_raw).lower()
            if "event" in st and "cyclic" in st:
                entry.send_mode = "Cyclic+Event"
            elif "event" in st or "ifactive" in st:
                entry.send_mode = "Event"

        with QMutexLocker(self._queue_mutex):
            self._tx_queue[dbc_msg.frame_id] = entry

        self._refresh_tx_table()
        self._load_tx_entry_to_editor(dbc_msg.frame_id)
        self._log(
            f"Added: {dbc_msg.name} (0x{dbc_msg.frame_id:X}) "
            f"DLC:{dbc_msg.length} Cycle:{entry.cycle_ms}ms "
            f"Mode:{entry.send_mode}" + (" [FD]" if is_fd else ""))

    def _load_tx_entry_to_editor(self, frame_id: int) -> None:
        entry = self._tx_queue.get(frame_id)
        if not entry:
            return
        self._current_edit_fid = frame_id
        fd_tag = " [FD]" if entry.is_fd else ""
        self.msg_header.setText(
            f"{entry.name} (0x{entry.frame_id:X})  DLC:{entry.dlc}"
            f"{fd_tag}  [TX Queue - editable]")

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
            gc.addItems(GENERATOR_OPTIONS)
            gc.setCurrentText(sig.get("generator", "None"))
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
        entry = self._tx_queue.get(self._current_edit_fid)
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
        entry = self._tx_queue.get(self._current_edit_fid)
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
        entry = self._tx_queue.get(self._current_edit_fid)
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
    # TX TABLE
    # ==========================================================

    def _refresh_tx_table(self) -> None:
        self.tx_table.setRowCount(len(self._tx_queue))
        for row, (fid, e) in enumerate(self._tx_queue.items()):
            # Name (read-only)
            ni = QTableWidgetItem(e.name)
            ni.setFlags(ni.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 0, ni)
            # ID
            ii = QTableWidgetItem(f"0x{e.frame_id:X}")
            ii.setFlags(ii.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 1, ii)
            # DLC
            di = QTableWidgetItem(str(e.dlc))
            di.setFlags(di.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 2, di)
            # Cycle
            cs = QSpinBox()
            cs.setRange(1, MAX_CYCLE_MS)
            cs.setValue(e.cycle_ms)
            cs.setSuffix("ms")
            cs.valueChanged.connect(lambda v, f=fid: self._set_cycle(f, v))
            self.tx_table.setCellWidget(row, 3, cs)
            # Mode
            mc = QComboBox()
            mc.addItems(TxEntry.MODES)
            mc.setCurrentText(e.send_mode)
            mc.currentTextChanged.connect(lambda m, f=fid: self._set_mode(f, m))
            self.tx_table.setCellWidget(row, 4, mc)
            # Data
            dti = QTableWidgetItem(e.get_data_hex())
            dti.setFlags(dti.flags() & ~Qt.ItemIsEditable)
            self.tx_table.setItem(row, 5, dti)
            # BRS checkbox
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
            cl = QHBoxLayout(cw)
            cl.addWidget(cb)
            cl.setAlignment(Qt.AlignCenter)
            cl.setContentsMargins(0, 0, 0, 0)
            self.tx_table.setCellWidget(row, 7, cw)
            # Row color
            color = QColor(220, 255, 220) if e.active else QColor(240, 240, 240)
            for c in range(self.tx_table.columnCount()):
                it = self.tx_table.item(row, c)
                if it:
                    it.setBackground(color)

    def _refresh_tx_data_col(self, frame_id: int) -> None:
        for row, (fid, e) in enumerate(self._tx_queue.items()):
            if fid == frame_id:
                it = self.tx_table.item(row, 5)
                if it:
                    it.setText(e.get_data_hex())
                break

    def _on_queue_row_clicked(self, item) -> None:
        row = item.row()
        keys = list(self._tx_queue.keys())
        if row < len(keys):
            self._load_tx_entry_to_editor(keys[row])

    def _remove_from_queue(self) -> None:
        rows = {it.row() for it in self.tx_table.selectedItems()}
        keys = list(self._tx_queue.keys())
        with QMutexLocker(self._queue_mutex):
            for r in sorted(rows, reverse=True):
                if r < len(keys):
                    del self._tx_queue[keys[r]]
        self._refresh_tx_table()

    def _clear_queue(self) -> None:
        with QMutexLocker(self._queue_mutex):
            self._tx_queue.clear()
        self._refresh_tx_table()
        self._current_edit_fid = -1
        self._log("Queue cleared")

    def _set_cycle(self, fid, v):
        if fid in self._tx_queue: self._tx_queue[fid].cycle_ms = v

    def _set_mode(self, fid, m):
        if fid in self._tx_queue: self._tx_queue[fid].send_mode = m

    def _set_active(self, fid, s):
        if fid in self._tx_queue: self._tx_queue[fid].active = (s == Qt.Checked)

    def _set_brs(self, fid, s):
        if fid in self._tx_queue: self._tx_queue[fid].brs = (s == Qt.Checked)

    # ==========================================================
    # EXPORT / IMPORT
    # ==========================================================

    def _export_queue(self) -> None:
        if not self._tx_queue:
            self._log("Nothing to export"); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export TX Queue", "tx_queue.json", "JSON (*.json)")
        if not path:
            return
        try:
            data = [e.to_dict() for e in self._tx_queue.values()]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._log(f"Exported {len(data)} message(s)")
        except Exception as e:
            self._log(f"Export failed: {e}")

    def _import_queue(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import TX Queue", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            count = 0
            with QMutexLocker(self._queue_mutex):
                for d in data:
                    entry = TxEntry.from_dict(d)
                    if entry.frame_id not in self._tx_queue:
                        self._tx_queue[entry.frame_id] = entry
                        count += 1
            self._refresh_tx_table()
            self._log(f"Imported {count} message(s)")
        except Exception as e:
            self._log(f"Import failed: {e}")

    # ==========================================================
    # HW CONNECTION — delegates to BusManager
    # ==========================================================

    def _scan_hw(self) -> None:
        """
        🔧 CRITICAL FIX: Scan ĐỒNG BỘ trong main thread, KHÔNG dùng QThread.

        Lý do: QuickScanThread + Qt event loop cleanup gây crash
        STATUS_STACK_BUFFER_OVERRUN (0xC0000409) trên Windows khi
        `_run_startup` auto-call `_scan_hw` — race condition giữa
        QThread.deleteLater và finished signal dispatch.

        Hardware scan chỉ mất vài chục ms, chạy đồng bộ không block UI
        đáng kể. Đơn giản hóa lifecycle → hết crash.
        """
        import traceback
        print("[_scan_hw] CALLED — stacktrace:")
        traceback.print_stack()
        logger.info("[_scan_hw] CALLED (synchronous)")

        if not is_can_available():
            self._log("ERROR: python-can not installed")
            return

        self.scan_btn.setEnabled(False)

        # 🔧 Import scan logic và chạy đồng bộ
        try:
            print("[_scan_hw] Running synchronous scan...")
            channels = self._do_scan_sync()
            print(f"[_scan_hw] Scan finished: {len(channels)} channels")
        except Exception as e:
            print(f"[_scan_hw] Scan failed: {e}")
            logger.exception("Scan failed")
            channels = []
        finally:
            self.scan_btn.setEnabled(True)

        # Gọi trực tiếp handler
        self._on_scan_done(channels)
        print("[_scan_hw] DONE")

    def _do_scan_sync(self) -> List[Dict]:
        """
        🔧 Scan hardware channels đồng bộ (trong main thread).
        Copy từ QuickScanThread.run() nhưng không dùng QThread.
        """
        import sys
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
        print(f"[_on_scan_done] CALLED with {len(channels)} channels")
        logger.info(f"[_on_scan_done] {len(channels)} channels found")
        self._hw_channels = channels
        self.ch_combo.clear()
        for ch in channels:
            label = ch["label"] + (" [FD]" if ch.get("supports_fd") else "")
            self.ch_combo.addItem(label, ch)
        self._log(f"Scan: {len(channels)} channel(s)")
        print("[_on_scan_done] DONE")

    def _toggle_connect(self) -> None:
        if self._bus_mgr.is_connected:
            self._do_disconnect()
        else:
            self._do_connect()

    def _do_connect(self) -> None:
        hw = self.ch_combo.currentData()
        if not hw:
            self._log("ERROR: No channel selected"); return
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            self._log("ERROR: Invalid baud rate"); return

        bus = self._bus_mgr.connect(
            hw=hw, baud=baud,
            is_fd=self._is_fd_mode(),
            fd_data_bitrate=self._get_data_bitrate(),
            app_name=self.app_name_edit.text().strip(),
        )
        if not bus:
            return

        self._bus = bus
        self._bus_mutex = self._bus_mgr.bus_mutex  # Dùng chung mutex
        logger.info(f"[_do_connect] _bus exposed: {self._bus is not None}")

        # 🔧 FIX: Nếu RxReaderThread cũ đang chạy, stop nó trước
        if self._rx_thread and self._rx_thread.isRunning():
            self._rx_thread.stop()
            self._rx_thread.wait()

        self._rx_thread = RxReaderThread(bus, self._bus_mgr.bus_mutex, parent=self)
        self._rx_thread.message_received.connect(self._on_rx)
        self._rx_thread.error_occurred.connect(self._on_rx_err)

        # 🔧 CRUCIAL: Chỉ gọi .start() tại đây, không ở constructor
        self._rx_thread.start()

        self.connect_btn.setText("Disconnect")
        self.connect_btn.setStyleSheet("QPushButton{font-weight:bold;background:#d1e7dd;}")

        mode = "CAN FD" if self._is_fd_mode() else "CAN 2.0"
        self.conn_label.setText(f"Connected: {hw['label']} [{mode}]")
        self.conn_label.setStyleSheet("color:green;font-weight:bold;")

    def _do_disconnect(self) -> None:
        """
        🔧 CHANGE: Stop threads TRƯỚC disconnect bus
        Order: Stop TX Worker → Wait → Stop RX Thread → Wait → Disconnect Bus
        """
        # Step 1: Stop TX worker (nếu đang chạy)
        if self._tx_worker:
            self._tx_worker.stop()
            self._tx_worker.wait()
            self._tx_worker = None

        # Step 2: Stop RX reader (nếu đang chạy)
        if self._rx_thread:
            self._rx_thread.stop()
            self._rx_thread.wait()
            self._rx_thread = None

        self._bus = None

        # Step 3: GIỜ MỚI disconnect bus (threads đã exit)
        self._bus_mgr.disconnect()

        # Update UI
        self.connect_btn.setText("Connect")
        self.connect_btn.setStyleSheet("QPushButton{font-weight:bold;background:#e3f2fd;}")
        self.conn_label.setText("Disconnected")
        self.conn_label.setStyleSheet("color:red;font-weight:bold;")

    # ==========================================================
    # SEND / STOP
    # ==========================================================

    def _on_send(self) -> None:
        if not self._bus_mgr.is_connected:
            self._log("ERROR: Not connected"); return
        if self._tx_worker and self._tx_worker.isRunning():
            self._on_stop()

        active = [e for e in self._tx_queue.values() if e.active]
        if not active:
            self._log("WARNING: No active messages"); return

        for e in active:
            e.reset_counters()

        # Event initial send
        for e in active:
            if e.send_mode in ("Event", "Cyclic+Event"):
                e.tick_generators(is_event_send=True)
                if self._bus_mgr.send_direct(e, self._is_fd_mode()):
                    self._stats["tx"] += 1
                else:
                    self._stats["err"] += 1

        # Start TX worker
        self._tx_worker = TxWorkerThread(
            self._bus_mgr.bus, self._bus_mgr.bus_mutex,
            self._tx_queue, self._queue_mutex,
            is_fd=self._is_fd_mode(), parent=self,
        )
        self._tx_worker.stats_updated.connect(self._on_worker_stats)
        self._tx_worker.error_occurred.connect(self._on_worker_error)
        self._tx_worker.start()

        self._ui_timer.start(UI_REFRESH_MS)
        self.send_btn.setStyleSheet("QPushButton{font-weight:bold;background:#a8e6a3;}")
        self._log(f"TX STARTED: {len(active)} message(s)")

    def _on_stop(self) -> None:
        """
        🔧 CHANGE: Chắc chắn worker thread stop trước exit
        """
        self._ui_timer.stop()

        # 🔧 FIX: Wait for worker to finish BEFORE setting button state
        if self._tx_worker:
            self._tx_worker.stop()  # Signal to stop
            self._tx_worker.wait()  # Wait for exit
            self._tx_worker = None

        self.send_btn.setStyleSheet("QPushButton{font-weight:bold;background:#d1e7dd;}")
        self._flush_log_buffer()
        self._log("TX STOPPED")

    def _on_worker_stats(self, tx, err):
        self._stats["tx"] += tx
        self._stats["err"] += err

    def _on_worker_error(self, msg):
        self._log(f"[TX ERR] {msg}")

    # ==========================================================
    # UI REFRESH
    # ==========================================================

    def _ui_refresh(self) -> None:
        if self._current_edit_fid >= 0:
            entry = self._tx_queue.get(self._current_edit_fid)
            if entry:
                self._update_signal_table_live(entry)
                self._update_data_preview(entry)
        for fid, entry in self._tx_queue.items():
            if entry.active:
                self._refresh_tx_data_col(fid)
        self._flush_log_buffer()

    def _update_signal_table_live(self, entry: TxEntry) -> None:
        self.sig_table.blockSignals(True)
        try:
            for r, sig in enumerate(entry.signals):
                if sig.get("generator", "None") == "None":
                    continue
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
    # RX
    # ==========================================================

    def _on_rx(self, msg) -> None:
        self._stats["rx"] += 1
        if self._rx_logging:
            ds = " ".join(f"{b:02X}" for b in msg.data)
            fd = " [FD]" if getattr(msg, "is_fd", False) else ""
            self._log_buffered(f"[RX] 0x{msg.arbitration_id:03X} DLC:{msg.dlc}{fd} {ds}")

    def _on_rx_err(self, err):
        self._stats["err"] += 1
        self._log(f"[RX ERR] {err}")

    # ==========================================================
    # STATS / LOG
    # ==========================================================

    def _update_stats(self) -> None:
        self.stats_label.setText(
            f"TX:{self._stats['tx']} RX:{self._stats['rx']} Err:{self._stats['err']}")

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_box.append(f"[{ts}] {msg}")
        self._log_line_count += 1
        self._trim_log()
        logger.debug("SendCAN: %s", msg)

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