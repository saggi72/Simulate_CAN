"""
==============================================================
main_window.py — CAN Tool — Main Application Window (FIXED)
==============================================================

🔧 FIX: SendCanTab không còn attribute `_bus` (đã chuyển vào BusManager)
       → đổi thành `_bus_mgr.is_connected` và `_bus_mgr.bus`
🔧 FIX: SendCanTab method là `_do_disconnect`, không phải `_disconnect`
"""

import logging
from typing import Dict

from PyQt5.QtWidgets import (
    QMainWindow, QTabWidget, QAction, QLabel,
    QMessageBox, QApplication
)
from PyQt5.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QKeySequence

from ui.tab_dbc import DbcTab, Network
from ui.tab_send_can import SendCanTab
from ui.tab_inject_can import InjectCanTab
from ui.tab_trace import TraceTab
from ui.tab_graphic import GraphicTab

logger = logging.getLogger(__name__)

# ==============================================================
# CONSTANTS
# ==============================================================

APP_NAME    = "CAN Tool "
APP_VERSION = "2.3"
ORG_NAME    = "CANToolDev"
SETTINGS_KEY = "MainWindow"

STATUS_UPDATE_MS = 1000

DEFAULT_WIDTH  = 1280
DEFAULT_HEIGHT = 800
MIN_WIDTH      = 1100
MIN_HEIGHT     = 700

# ==============================================================
# FONT SYSTEM
# ==============================================================

UI_FONT       = QFont("Segoe UI", 10)
UI_FONT_SMALL = QFont("Segoe UI", 9)

MONO_FONT       = QFont("Consolas", 10)
MONO_FONT_SMALL = QFont("Consolas", 9)

BOLD_FONT = QFont("Segoe UI", 10, QFont.Bold)


# ==============================================================
# MAIN WINDOW
# ==============================================================

class MainWindow(QMainWindow):

    networks_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()

        self._settings = QSettings(ORG_NAME, APP_NAME)
        self.networks: Dict[str, Network] = {}

        self._build_ui()
        self._build_menu()
        self._build_status_bar()
        self._restore_geometry()

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._update_status_bar)
        self._status_timer.start(STATUS_UPDATE_MS)

        logger.info("%s v%s started", APP_NAME, APP_VERSION)

    # ==========================================================
    # UI BUILD
    # ==========================================================

    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.setFont(UI_FONT)

        self.tab_widget = QTabWidget()
        self.tab_widget.setTabPosition(QTabWidget.North)
        self.tab_widget.setDocumentMode(True)
        self.tab_widget.setFont(UI_FONT)
        self.setCentralWidget(self.tab_widget)

        # --- Tabs ---
        self.dbc_tab = DbcTab(main=self)
        self.dbc_tab.networks_changed.connect(self._on_networks_changed)
        self.tab_widget.addTab(self.dbc_tab, "DBC")

        self.send_tab = SendCanTab(main=self)
        self.tab_widget.addTab(self.send_tab, "Send CAN")

        self.inject_tab = InjectCanTab(main=self)
        self.tab_widget.addTab(self.inject_tab, "Inject CAN")

        self.trace_tab = TraceTab(main_window=self)
        self.tab_widget.addTab(self.trace_tab, "Trace")

        self.graphic_tab = GraphicTab(main_window=self)
        self.tab_widget.addTab(self.graphic_tab, "Graphic")

        # ----------------------------------------------------------
        # MUTUAL EXCLUSION — khi 1 tab START → tab còn lại tự STOP
        # ----------------------------------------------------------
        self.trace_tab.monitor_started.connect(
            self.graphic_tab.stop_for_exclusive_mode
        )
        self.graphic_tab.monitor_started.connect(
            self.trace_tab.stop_for_exclusive_mode
        )

        self.tab_widget.currentChanged.connect(self._on_tab_changed)

    # ==========================================================
    # MENU
    # ==========================================================

    def _build_menu(self) -> None:
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")

        open_dbc = QAction("&Open DBC File(s)…", self)
        open_dbc.setShortcut(QKeySequence.Open)
        open_dbc.triggered.connect(self._menu_open_dbc)
        file_menu.addAction(open_dbc)

        open_folder = QAction("Open DBC &Folder…", self)
        open_folder.triggered.connect(self._menu_open_folder)
        file_menu.addAction(open_folder)

        file_menu.addSeparator()

        import_queue = QAction("&Import TX Queue…", self)
        import_queue.triggered.connect(self._menu_import_queue)
        file_menu.addAction(import_queue)

        export_queue = QAction("&Export TX Queue…", self)
        export_queue.triggered.connect(self._menu_export_queue)
        file_menu.addAction(export_queue)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # View
        view_menu   = mb.addMenu("&View")
        tab_labels  = ["DBC", "Send CAN", "Inject CAN", "Trace", "Graphic"]

        for i, label in enumerate(tab_labels):
            act = QAction(f"Switch to {label}", self)
            act.setShortcut(f"Ctrl+{i+1}")
            act.triggered.connect(
                lambda _checked, idx=i: self.tab_widget.setCurrentIndex(idx)
            )
            view_menu.addAction(act)

        view_menu.addSeparator()

        clear_send_log = QAction("Clear Send CAN Log", self)
        clear_send_log.triggered.connect(lambda: self.send_tab._clear_log())
        view_menu.addAction(clear_send_log)

        clear_inject_log = QAction("Clear Inject CAN Log", self)
        clear_inject_log.triggered.connect(
            lambda: self.inject_tab._clear_log()
        )
        view_menu.addAction(clear_inject_log)

        # Tools
        tools_menu = mb.addMenu("&Tools")

        scan_hw = QAction("&Scan Hardware", self)
        scan_hw.triggered.connect(lambda: self.send_tab._scan_hw())
        tools_menu.addAction(scan_hw)

        tools_menu.addSeparator()

        export_csv = QAction("Export Signal List (CSV)…", self)
        export_csv.triggered.connect(lambda: self.dbc_tab._on_export_csv())
        tools_menu.addAction(export_csv)

        # Help
        help_menu = mb.addMenu("&Help")

        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    # ==========================================================
    # STATUS BAR
    # ==========================================================

    def _build_status_bar(self) -> None:
        self.statusBar()

        self._status_net_label = QLabel("Networks: 0")
        self._status_net_label.setFont(MONO_FONT_SMALL)
        self.statusBar().addWidget(self._status_net_label)

        self.statusBar().addWidget(self._make_separator())

        self._status_msg_label = QLabel("Messages: 0")
        self._status_msg_label.setFont(MONO_FONT_SMALL)
        self.statusBar().addWidget(self._status_msg_label)

        self.statusBar().addWidget(self._make_separator())

        self._status_tx_label = QLabel("TX: 0")
        self._status_tx_label.setFont(MONO_FONT_SMALL)
        self._status_tx_label.setStyleSheet(
            "color:#0a6e3c; font-weight:bold"
        )
        self.statusBar().addWidget(self._status_tx_label)

        self.statusBar().addWidget(self._make_separator())

        self._status_inj_label = QLabel("INJ: 0")
        self._status_inj_label.setFont(MONO_FONT_SMALL)
        self._status_inj_label.setStyleSheet(
            "color:#6f42c1; font-weight:bold"
        )
        self.statusBar().addWidget(self._status_inj_label)

        self.statusBar().addWidget(self._make_separator())

        self._status_conn_label = QLabel("Disconnected")
        self._status_conn_label.setFont(BOLD_FONT)
        self._status_conn_label.setStyleSheet(
            "color:#842029; font-weight:bold"
        )
        self.statusBar().addPermanentWidget(self._status_conn_label)

    @staticmethod
    def _make_separator() -> QLabel:
        sep = QLabel("|")
        sep.setStyleSheet("color:#ced4da; padding:0 4px")
        return sep

    # ==========================================================
    # NETWORK SYNC
    # ==========================================================

    def _on_networks_changed(self) -> None:
        self.networks = dict(self.dbc_tab.networks)
        self.networks_changed.emit()

        if hasattr(self.send_tab, "_refresh_networks"):
            self.send_tab._refresh_networks()
        if hasattr(self.inject_tab, "_refresh_networks"):
            self.inject_tab._refresh_networks()

    # ==========================================================
    # STATUS UPDATE
    # ==========================================================

    def _update_status_bar(self) -> None:
        # 🐛 DEBUG: Wrap toàn bộ trong try để bắt mọi lỗi
        try:
            n_nets = len(self.networks)
            n_msgs = sum(net.msg_count for net in self.networks.values())

            self._status_net_label.setText(f"Networks: {n_nets}")
            self._status_msg_label.setText(f"Messages: {n_msgs}")

            # Send CAN stats
            tx_count = self.send_tab._stats.get("tx", 0)
            self._status_tx_label.setText(f"TX: {tx_count}")

            # Inject CAN stats
            inj_count = self.inject_tab._stats.get("inj", 0)
            self._status_inj_label.setText(f"INJ: {inj_count}")

            # Connection status — show both Send and Inject
            # 🔧 FIX: SendCanTab.bus đã chuyển vào BusManager
            parts = []
            if self.send_tab._bus_mgr.is_connected:
                ch = self.send_tab.ch_combo.currentText()
                parts.append(f"Send:{ch}")
            if self.inject_tab._source_bus:
                parts.append("Inject:Active")

            if parts:
                self._status_conn_label.setText(" | ".join(parts))
                self._status_conn_label.setStyleSheet(
                    "color:#0a6e3c; font-weight:bold"
                )
            else:
                self._status_conn_label.setText("Disconnected")
                self._status_conn_label.setStyleSheet(
                    "color:#842029; font-weight:bold"
                )
        except Exception as e:
            print(f"[_update_status_bar] ERROR: {e}")
            logger.exception("Status bar update failed")

    # ==========================================================
    # TAB CHANGE
    # ==========================================================

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tab_widget.widget(index)
        if widget is self.send_tab:
            self.send_tab._refresh_networks()
        elif widget is self.inject_tab:
            self.inject_tab._refresh_networks()

    # ==========================================================
    # MENU ACTIONS
    # ==========================================================

    def _menu_open_dbc(self) -> None:
        self.tab_widget.setCurrentWidget(self.dbc_tab)
        self.dbc_tab._on_add_dbc()

    def _menu_open_folder(self) -> None:
        self.tab_widget.setCurrentWidget(self.dbc_tab)
        self.dbc_tab._on_add_folder()

    def _menu_import_queue(self) -> None:
        self.tab_widget.setCurrentWidget(self.send_tab)
        self.send_tab._import_queue()

    def _menu_export_queue(self) -> None:
        self.tab_widget.setCurrentWidget(self.send_tab)
        self.send_tab._export_queue()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> v{APP_VERSION}<br><br>"
            "CAN Bus Analysis & Transmission Tool<br>"
            "Send CAN Tool by SangNM5"

        )

    # ==========================================================
    # SETTINGS
    # ==========================================================

    def _restore_geometry(self) -> None:
        geom  = self._settings.value(f"{SETTINGS_KEY}/geometry")
        state = self._settings.value(f"{SETTINGS_KEY}/state")

        if geom:
            self.restoreGeometry(geom)
        else:
            self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)
            self._center_on_screen()

        if state:
            self.restoreState(state)

    def _save_geometry(self) -> None:
        self._settings.setValue(
            f"{SETTINGS_KEY}/geometry", self.saveGeometry()
        )
        self._settings.setValue(
            f"{SETTINGS_KEY}/state", self.saveState()
        )

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width()  - self.width())  // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    # ==========================================================
    # CLOSE
    # ==========================================================

    def closeEvent(self, event) -> None:
        reply = QMessageBox.question(
            self,
            "Exit",
            "Stop and disconnect hardware?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )

        if reply == QMessageBox.Cancel:
            event.ignore()
            return

        # Hiển thị trạng thái đang đóng trên status bar để người dùng biết
        self.statusBar().showMessage("Closing application and releasing hardware...")

        # 1. Dừng các Timer để tránh gọi UI khi các object đang bị hủy
        if hasattr(self, '_status_timer'):
            self._status_timer.stop()

        # 2. Danh sách các bước cleanup để chạy vòng lặp (giảm thiểu crash)
        # Thứ tự: Dừng luồng xử lý trước -> Ngắt kết nối phần cứng sau
        cleanup_tasks = [
            (self.graphic_tab, 'cleanup'),
            (self.trace_tab, 'cleanup'),
            (self.send_tab, '_on_stop'),
            (self.inject_tab, '_on_stop')
        ]

        for tab, method_name in cleanup_tasks:
            try:
                method = getattr(tab, method_name, None)
                if method:
                    method()
            except Exception as e:
                logger.debug(f"Cleanup {method_name} failed: {e}")

        # 3. Ngắt kết nối Hardware (Quan trọng nhất để không treo Driver Vector)
        try:
            # Kiểm tra sự tồn tại của _bus_mgr và trạng thái kết nối
            if hasattr(self.send_tab, '_bus_mgr') and self.send_tab._bus_mgr.is_connected:
                # Ngắt kết nối Bus chính
                self.send_tab._do_disconnect()
        except Exception as e:
            logger.error(f"Disconnect main bus failed: {e}")

        try:
            # Ngắt kết nối Inject bus nếu có
            if hasattr(self.inject_tab, '_disconnect'):
                self.inject_tab._disconnect()
        except Exception as e:
            logger.error(f"Disconnect inject bus failed: {e}")

        # 4. Lưu lại cấu hình giao diện
        try:
            self._save_geometry()
        except Exception:
            pass

        # Chấp nhận đóng ứng dụng
        logger.info("Application closed safely.")
        event.accept()