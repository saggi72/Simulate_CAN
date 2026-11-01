"""
core/threads.py — Worker threads cho CAN communication.
🔧 FIX: Kiểm tra bus tồn tại, graceful shutdown, state machine
🔧 FIX: QuickScanThread.finished → scan_complete (tránh override QThread.finished)

- TxWorkerThread:  Tick engine gửi cyclic messages (start/stop explicit)
- RxReaderThread:  Nhận CAN messages liên tục (không auto-start)
- QuickScanThread: Quét hardware channels

CAN FD ready: is_fd + bitrate_switch trong can.Message.
"""

import time
import logging
from typing import Dict, List, Optional
from collections import OrderedDict

from PyQt5.QtCore import QThread, pyqtSignal, QMutex, QMutexLocker

from core.core import TICK_MS, UI_REFRESH_MS, EXTENDED_ID_THRESHOLD
from core.tx_entry import TxEntry

logger = logging.getLogger(__name__)

# Lazy import: python-can chỉ load khi thực sự cần
_can_module = None
_can_checked = False


def _get_can():
    """Lazy import python-can. Trả về module hoặc None."""
    global _can_module, _can_checked
    if not _can_checked:
        try:
            import can
            _can_module = can
        except ImportError:
            _can_module = None
        _can_checked = True
    return _can_module


def is_can_available() -> bool:
    return _get_can() is not None


# ==============================================================
# TX WORKER THREAD
# ==============================================================

class TxWorkerThread(QThread):
    """
    Tick engine gửi CAN messages trong worker thread.

    🔧 FIX:
    - Kiểm tra bus != None trước mỗi send
    - Graceful stop with timeout
    - Cleanup entries khi bus bị shutdown

    Signals:
        stats_updated(int, int):  (tx_count, err_count) batch
        error_occurred(str):      Lỗi TX
        message_sent(int, bytes): frame_id + data cho TX log
    """

    stats_updated = pyqtSignal(int, int)
    error_occurred = pyqtSignal(str)
    message_sent = pyqtSignal(int, bytes)

    def __init__(
        self,
        bus,
        bus_mutex: QMutex,
        tx_queue: OrderedDict,
        queue_mutex: QMutex,
        is_fd: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bus = bus
        self._bus_mutex = bus_mutex
        self._tx_queue = tx_queue
        self._queue_mutex = queue_mutex
        self._global_is_fd = is_fd
        self._running: bool = False  # 🔧 Không chạy cho tới khi explicitly start()
        self._tx_batch: int = 0
        self._err_batch: int = 0

    def run(self) -> None:
        self._running = True
        tick_s = TICK_MS / 1000.0
        stats_interval = UI_REFRESH_MS / 1000.0
        last_stats = time.perf_counter()

        logger.debug(f"TxWorkerThread started")

        while self._running:
            t0 = time.perf_counter()

            # 🔧 Kiểm tra bus tồn tại TRƯỚC snapshot
            if self._bus is None:
                self._running = False
                break

            # Snapshot entries (mutex-protected)
            with QMutexLocker(self._queue_mutex):
                entries = list(self._tx_queue.values())

            for entry in entries:
                if not self._running:
                    break
                if not entry.active:
                    continue
                if entry.should_send(TICK_MS):
                    entry.tick_generators()
                    self._send_one(entry)

            # Emit batched stats định kỳ
            now = time.perf_counter()
            if now - last_stats >= stats_interval:
                if self._tx_batch or self._err_batch:
                    self.stats_updated.emit(self._tx_batch, self._err_batch)
                    self._tx_batch = 0
                    self._err_batch = 0
                last_stats = now

            # Sleep remaining tick
            remaining = tick_s - (time.perf_counter() - t0)
            if remaining > 0.001:
                time.sleep(remaining)

        logger.debug(f"TxWorkerThread stopped")

    def _send_one(self, entry: TxEntry) -> None:
        try:
            # 🔧 Kiểm tra bus trước khi send
            if self._bus is None:
                return

            can = _get_can()
            use_fd = entry.is_fd or self._global_is_fd
            msg = can.Message(
                arbitration_id=entry.frame_id,
                data=bytes(entry.data[:entry.dlc]),
                is_extended_id=entry.frame_id > EXTENDED_ID_THRESHOLD,
                is_fd=use_fd,
                bitrate_switch=entry.brs if use_fd else False,
                dlc=entry.dlc,
            )

            # 🔧 Locking scope hẹp, kiểm tra lại bus tại lock
            with QMutexLocker(self._bus_mutex):
                if self._bus is not None:
                    self._bus.send(msg)
                else:
                    return  # Bus ngắt kết nối trong lock

            entry.tx_count += 1
            self._tx_batch += 1
            self.message_sent.emit(entry.frame_id, bytes(entry.data[:entry.dlc]))

        except Exception as e:
            self._err_batch += 1
            logger.warning(f"TX error {entry.name}: {e}")
            self.error_occurred.emit(f"{entry.name}: {e}")

    def stop(self) -> None:
        """Gracefully stop worker thread."""
        self._running = False
        # 🔧 Timeout 3s, sau đó force-kill
        if not self.wait(3000):
            logger.warning("TxWorkerThread did not finish in 3s, terminating...")
            self.terminate()
            self.wait(1000)


# ==============================================================
# RX READER THREAD
# ==============================================================

class RxReaderThread(QThread):
    """
    Nhận CAN messages liên tục (CAN 2.0 + FD).

    🔧 FIX:
    - Không auto-start, chỉ start khi explicitly call
    - Kiểm tra bus != None trước mỗi recv
    - Exit gracefully nếu bus được ngắt

    Signals:
        message_received(object): Received CAN message
        error_occurred(str):      Error message
    """

    message_received = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, bus, bus_mutex: QMutex, parent=None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._mutex = bus_mutex
        self._running: bool = False  # 🔧 Không chạy cho tới khi explicitly start()

    def run(self) -> None:
        self._running = True
        logger.debug(f"RxReaderThread started")

        while self._running:
            try:
                # 🔧 Kiểm tra bus tồn tại
                if self._bus is None:
                    self._running = False
                    break

                with QMutexLocker(self._mutex):
                    msg = self._bus.recv(timeout=0.05)

                if msg is not None:
                    self.message_received.emit(msg)

            except Exception as e:
                if self._running:
                    logger.error(f"RX error: {e}")
                    self.error_occurred.emit(str(e))
                # 🔧 Không break, tiếp tục loop (có thể recovery)
                time.sleep(0.1)

        logger.debug(f"RxReaderThread stopped")

    def stop(self) -> None:
        """Gracefully stop reader thread."""
        self._running = False
        # 🔧 Timeout 3s
        if not self.wait(3000):
            logger.warning("RxReaderThread did not finish in 3s, terminating...")
            self.terminate()
            self.wait(1000)

    def set_bus(self, bus) -> None:
        """🔧 Update bus reference safely."""
        with QMutexLocker(self._mutex):
            self._bus = bus


# ==============================================================
# HW SCANNER THREAD
# ==============================================================

class QuickScanThread(QThread):
    """Quét hardware channels: Vector → PEAK → SocketCAN → Virtual.

    🔧 CRITICAL FIX:
    Signal đã được rename từ `finished` → `scan_complete` vì
    `QThread` đã có built-in signal `finished` (no args). Override
    nó với `pyqtSignal(list)` gây xung đột C++ vtable → native crash
    với STATUS_STACK_BUFFER_OVERRUN (0xC0000409) khi Qt framework
    cố emit finished() để cleanup thread.
    """

    # 🔧 RENAMED: finished → scan_complete
    scan_complete = pyqtSignal(list)

    def run(self) -> None:
        channels: List[Dict] = []
        can = _get_can()

        if can is None:
            self.scan_complete.emit(channels)
            return

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
        except Exception:
            pass

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
        except Exception:
            pass

        # --- SocketCAN ---
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

        # 🔧 RENAMED
        self.scan_complete.emit(channels)