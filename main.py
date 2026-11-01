"""
==============================================================
main.py — CAN Tool — Entry Point (YASUO SPLASH EDITION)
==============================================================
"""

import faulthandler
import sys
import argparse
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

# ==============================================================
# 1. KHỞI TẠO HỆ THỐNG & FIX CONSOLE
# ==============================================================
IS_WINDOWED = sys.stdout is None

if not IS_WINDOWED:
    faulthandler.enable(all_threads=True)

APP_NAME = "CAN Tool (CAN 2.0, CAN FD)"
APP_VERSION = "2.1.2"
LOG_FILENAME = "can_tool.log"

_BANNER = r"""
  ██████╗ █████╗ ███╗   ██╗    ████████╗ ██████╗  ██████╗ ██╗
 ██╔════╝██╔══██╗████╗  ██║    ╚══██╔══╝██╔═══██╗██╔═══██╗██║
 ██║     ███████║██╔██╗ ██║       ██║   ██║   ██║██║   ██║██║
 ██║     ██╔══██║██║╚██╗██║       ██║   ██║   ██║██║   ██║██║
 ╚██████╗██║  ██║██║ ╚████║       ██║   ╚██████╔╝╚██████╔╝███████╗
  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═══╝       ╚═╝    ╚═════╝  ╚═════╝ ╚══════╝
"""

# ==============================================================
# 2. CẤU HÌNH ĐƯỜNG DẪN & LOGGING
# ==============================================================
def get_resource_path(relative_path: str) -> str:
    """ Lấy đường dẫn tài nguyên tuyệt đối (Hỗ trợ đóng gói EXE) """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

if hasattr(sys, '_MEIPASS'):
    sys.path.append(sys._MEIPASS)

def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    fmt = "[%(asctime)s] %(levelname)-8s %(name)-20s %(message)s"
    handlers: List[logging.Handler] = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    base_dir = Path(sys.executable).parent if hasattr(sys, '_MEIPASS') else Path(__file__).parent
    log_path = base_dir / LOG_FILENAME

    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8", delay=True))
    except OSError as e:
        if sys.stdout: print(f"Logging Error: {e}")

    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S", handlers=handlers)
    for noisy in ("can", "cantools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

# ==============================================================
# 3. GIAO DIỆN (STYLESHEET)
# ==============================================================
def _global_stylesheet() -> str:
    return """
    QMainWindow { background: #f8f9fa; }
    QTabWidget::pane { border: 1px solid #dee2e6; border-top: none; }
    QTabBar::tab {
        padding: 6px 18px; font-size: 13px; font-weight: 500;
        border: 1px solid #dee2e6; border-bottom: none;
        border-radius: 4px 4px 0 0; background: #e9ecef;
        color: #495057; margin-right: 2px;
    }
    QTabBar::tab:selected { background: #ffffff; color: #212529; border-bottom: 1px solid #ffffff; }
    QGroupBox { font-weight: bold; border: 1px solid #dee2e6; border-radius: 4px; margin-top: 8px; padding-top: 4px; }
    QLineEdit, QComboBox, QSpinBox { border: 1px solid #ced4da; border-radius: 3px; padding: 2px 6px; background: white; }
    QPushButton { border: 1px solid #ced4da; border-radius: 4px; padding: 4px 12px; background: white; }
    QPushButton:hover { background: #e9ecef; }
    """

# (Các hàm _load_startup_dbcs và _import_startup_queue giữ nguyên như bản cũ)
def _load_startup_dbcs(main_window, dbc_paths: List[str], net_names: List[str]):
    tab = main_window.dbc_tab
    for i, path_str in enumerate(dbc_paths):
        p = Path(path_str)
        if p.exists() and p.suffix.lower() == ".dbc":
            net = net_names[i] if i < len(net_names) else p.stem
            tab.net_name_edit.setText(net)
            tab._load_dbc_paths([str(p)])

def _import_startup_queue(main_window, queue_path: str):
    p = Path(queue_path)
    if not p.exists(): return
    tab = main_window.send_tab
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        from ui.tab_send_can import TxEntry
        for d in data:
            entry = TxEntry.from_dict(d)
            if entry.frame_id not in tab._tx_queue:
                tab._tx_queue[entry.frame_id] = entry
        tab._refresh_tx_table()
    except Exception as e:
        logging.getLogger("main").error(f"Queue Import Error: {e}")

# ==============================================================
# 4. HÀM MAIN VỚI SPLASH SCREEN
# ==============================================================
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="can_tool")
    parser.add_argument("--dbc", action="append", default=[])
    parser.add_argument("--network", action="append", default=[])
    parser.add_argument("--import-queue")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--no-hw-scan", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level)
    logger = logging.getLogger("main")

    # --- CẤU HÌNH DPI & HỆ THỐNG ---
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    # ==============================================================
    # 🔧 ĐOẠN FIX CHUẨN: MONKEY-PATCH PHƯƠNG THỨC TÌM DLL CỦA PYTHON-CAN
    # ==============================================================
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        # Nếu PyInstaller nhét mọi thứ vào _internal, ta phải ưu tiên trỏ vào đó
        internal_dir = os.path.join(exe_dir, "_internal")
        base_dir = internal_dir if os.path.exists(internal_dir) else exe_dir
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    pcan_dll_path = os.path.join(base_dir, "Lib", "PCANBasic.dll")

    if os.path.exists(pcan_dll_path):
        try:
            import ctypes
            import ctypes.util

            # 1. Nạp cứng DLL vào Windows Process Memory trước
            pcan_lib = ctypes.windll.LoadLibrary(pcan_dll_path)

            # 2. Bẫy hàm tìm kiếm thư viện của hệ thống (Bypass hoàn toàn System32)
            orig_find_library = ctypes.util.find_library

            def patched_find_library(name):
                if name and "pcanbasic" in name.lower():
                    return pcan_dll_path
                return orig_find_library(name)

            ctypes.util.find_library = patched_find_library

            # 3. Đè hàm load thư viện nội bộ của python-can
            import can.interfaces.pcan.pcan as pcan_module
            if hasattr(pcan_module, 'pcanlib'):
                pcan_module.pcanlib = pcan_lib

            logger.info(f"⚡ Đã ép nạp thành công PCANBasic.dll từ: {pcan_dll_path}")
        except Exception as e:
            logger.error(f"❌ Lỗi nạp bẫy thư viện PCAN dll: {e}", exc_info=True)

        # Đồng bộ PATH môi trường cho các tiến trình con
        lib_path = os.path.join(base_dir, "Lib")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(lib_path)
            except Exception:
                pass
        os.environ["PATH"] = lib_path + os.path.pathsep + os.environ["PATH"]
    else:
        logger.warning(f"⚠️ Không tìm thấy file PCANBasic.dll tại: {pcan_dll_path}")
    # ==============================================================

    try:
        from PyQt5.QtCore import Qt, QCoreApplication, QTimer
        from PyQt5.QtWidgets import QApplication, QSplashScreen
        from PyQt5.QtGui import QPixmap, QPainter, QColor, QFont
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except:
        pass

    app = QApplication(sys.argv)
    app.setStyleSheet(_global_stylesheet())

    # --- 1. XỬ LÝ LOGO (SCALE NHỎ LẠI) ---
    logo_path = get_resource_path(os.path.join("Image", "Logo.png"))
    base_pixmap = QPixmap(logo_path)

    # Scale logo nhỏ lại (ví dụ rộng 400px, giữ nguyên tỉ lệ)
    # SmoothTransformation giúp ảnh không bị vỡ khi thu nhỏ
    scaled_pixmap = base_pixmap.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    splash = QSplashScreen(scaled_pixmap, Qt.WindowStaysOnTopHint)
    splash.show()

    # --- 2. HÀM VẼ THANH PROGRESS BAR ---
    def update_splash_loading(progress):
        # Tạo canvas từ ảnh đã scale để vẽ đè lên
        canvas = scaled_pixmap.copy()
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)

        # Cấu hình thanh Progress Bar
        bar_height = 6
        bar_margin = 40
        bar_width = canvas.width() - (bar_margin * 2)
        bar_x = bar_margin
        bar_y = canvas.height() - 40  # Vị trí cách đáy ảnh 40px

        # Vẽ nền thanh Bar (Màu xám trong suốt)
        painter.setBrush(QColor(50, 50, 50, 150))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(bar_x, bar_y, bar_width, bar_height, 3, 3)

        # Vẽ phần tiến độ (Màu xanh Cyan Yasuo)
        painter.setBrush(QColor(0, 255, 255))
        current_w = int(bar_width * (progress / 100))
        painter.drawRoundedRect(bar_x, bar_y, current_w, bar_height, 3, 3)

        # Vẽ chữ phần trăm
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(bar_x, bar_y - 10, f"Hasagi! Loading... {progress}%")

        painter.end()
        splash.setPixmap(canvas)
        app.processEvents()  # Cập nhật giao diện ngay lập tức

    # --- 3. GIẢ LẬP TIẾN TRÌNH LOADING (0-100%) ---
    # Chạy vòng lặp để tạo hiệu ứng thanh loading chạy
    for p in range(0, 101):
        update_splash_loading(p)
        # Tốc độ loading (0.01s mỗi bước => Tổng ~1 giây)
        import time
        time.sleep(0.01)

    # Patch Vector DLL (Chạy ngầm trong khi load)
    try:
        from ui.vxlapi_helper import patch_vxlapi_path
        patch_vxlapi_path()
    except Exception as e:
        logger.debug(f"vxlapi patch skip: {e}")

    # --- 4. KHỞI TẠO CỬA SỔ CHÍNH ---
    from main_window import MainWindow
    window = MainWindow()

    def show_main_and_finish_splash():
        window.show()
        splash.finish(window)  # Tắt splash khi MainWindow đã hiện diện

    # Đợi 300ms sau khi đạt 100% để chuyển cảnh mượt mà
    QTimer.singleShot(300, show_main_and_finish_splash)

    def _delayed_init():
        if args.dbc:
            _load_startup_dbcs(window, args.dbc, args.network)
        if args.import_queue:
            QTimer.singleShot(1000, lambda: _import_startup_queue(window, args.import_queue))
        if not args.no_hw_scan:
            try:
                window.send_tab._scan_hw()
            except:
                pass

    # Khởi tạo các cấu hình CAN sau khi app đã mở
    QTimer.singleShot(500, _delayed_init)

    logger.info("Application ready")
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()