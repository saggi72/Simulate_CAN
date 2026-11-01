"""
core/bus_manager.py — CAN Bus connection manager.
🔧 FIX: Thread-safe disconnect, atomic state transitions

Encapsulate toàn bộ logic kết nối / ngắt:
- Vector (xlSetApplConfig, force TX output, diagnostics)
- PEAK, SocketCAN, Virtual
- Thread-safe state machine (connecting, connected, disconnecting)

Không import Qt widgets. Giao tiếp qua callback log_fn(str).
CAN FD: fd=True + data_bitrate trong bus kwargs.
"""

import logging
import sys
import time
from typing import Callable, Dict, Optional
from enum import Enum

from PyQt5.QtCore import QMutex, QMutexLocker

from core.core import DEFAULT_FD_DATA_BITRATE, EXTENDED_ID_THRESHOLD

logger = logging.getLogger(__name__)

# Lazy import: tránh DLL conflict khi app khởi động
_can_module = None
_can_checked = False


def _get_can():
    """Lazy import python-can."""
    global _can_module, _can_checked
    if not _can_checked:
        try:
            import can
            _can_module = can
        except ImportError:
            _can_module = None
        _can_checked = True
    return _can_module

LogFn = Callable[[str], None]


class BusState(Enum):
    """🔧 State machine cho bus lifecycle."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"


class BusManager:
    """
    Quản lý lifecycle CAN bus connection.

    🔧 FIX:
    - State machine (DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTING)
    - Thread-safe connect/disconnect
    - Sync disconnect với active threads

    Usage:
        mgr = BusManager(log_fn=print)
        bus = mgr.connect(hw_info, baud=500000, is_fd=False)
        mgr.disconnect()
    """

    def __init__(self, log_fn: Optional[LogFn] = None) -> None:
        self.bus = None
        self.bus_mutex = QMutex()
        self._state = BusState.DISCONNECTED
        self._state_mutex = QMutex()
        self._log = log_fn or logger.info

    @property
    def is_connected(self) -> bool:
        with QMutexLocker(self._state_mutex):
            return self._state == BusState.CONNECTED

    def _set_state(self, new_state: BusState) -> None:
        """🔧 Atomic state transition."""
        with QMutexLocker(self._state_mutex):
            old_state = self._state
            self._state = new_state
            logger.debug(f"BusManager state: {old_state.value} → {new_state.value}")

    # ----------------------------------------------------------
    # CONNECT / DISCONNECT
    # ----------------------------------------------------------

    def connect(
        self,
        hw: Dict,
        baud: int,
        is_fd: bool = False,
        fd_data_bitrate: int = DEFAULT_FD_DATA_BITRATE,
        app_name: str = "",
    ) -> Optional[object]:
        """
        Kết nối CAN bus.

        🔧 FIX:
        - Atomic state transition: DISCONNECTED → CONNECTING → CONNECTED
        - Release state lock TRƯỚC I/O operations
        - Return bus object nếu thành công, None nếu fail

        Returns bus object nếu thành công, None nếu fail.
        """
        with QMutexLocker(self._state_mutex):
            if self._state != BusState.DISCONNECTED:
                self._log(f"ERROR: Cannot connect while state={self._state.value}")
                return None
            self._state = BusState.CONNECTING

        can = _get_can()
        if can is None:
            self._log("ERROR: python-can not installed")
            self._set_state(BusState.DISCONNECTED)
            return None

        try:
            kwargs: Dict = {"bitrate": baud}
            if is_fd:
                kwargs["fd"] = True
                kwargs["data_bitrate"] = fd_data_bitrate

            iface = hw["interface"]
            ch = hw["channel"]

            if iface == "vector":
                self.bus = self._connect_vector(hw, ch, kwargs, app_name)
            elif iface == "pcan":
                self.bus = can.interface.Bus(
                    interface="pcan", channel=str(ch), **kwargs,
                )
            else:
                self.bus = can.interface.Bus(
                    interface=iface, channel=ch, **kwargs,
                )

            if self.bus:
                fd_tag = " [CAN FD]" if is_fd else ""
                self._log(f"Connected: {hw['label']} @ {baud}{fd_tag}")
                self._set_state(BusState.CONNECTED)
                return self.bus
            else:
                raise RuntimeError("Bus object is None")

        except Exception as e:
            self._log(f"Connect FAILED: {e}")
            self._safe_shutdown()
            self._set_state(BusState.DISCONNECTED)
            return None

    def disconnect(self) -> None:
        """
        Ngắt kết nối an toàn.

        🔧 FIX:
        - Atomic state: CONNECTED → DISCONNECTING → DISCONNECTED
        - Notify waiting threads
        - Ensure no in-flight operations
        """
        with QMutexLocker(self._state_mutex):
            if self._state == BusState.DISCONNECTED:
                return
            self._state = BusState.DISCONNECTING

        # 🔧 Give threads time to see DISCONNECTING state and exit gracefully
        time.sleep(0.05)

        self._safe_shutdown()
        self._set_state(BusState.DISCONNECTED)
        self._log("Disconnected")

    def _safe_shutdown(self) -> None:
        """🔧 Atomic bus shutdown with mutex."""
        with QMutexLocker(self.bus_mutex):
            if self.bus:
                try:
                    self.bus.shutdown()
                except Exception as e:
                    logger.warning(f"Bus shutdown exception: {e}")
                finally:
                    self.bus = None

    # ----------------------------------------------------------
    # DIRECT SEND (Event one-shot từ UI thread)
    # ----------------------------------------------------------

    def send_direct(self, entry, is_fd: bool = False) -> bool:
        """
        Gửi 1 message trực tiếp. Returns True nếu OK.

        🔧 FIX: Kiểm tra bus trước lock, double-check inside lock
        """
        can = _get_can()

        # Quick check
        if not self.is_connected or self.bus is None or can is None:
            return False

        try:
            use_fd = entry.is_fd or is_fd
            msg = can.Message(
                arbitration_id=entry.frame_id,
                data=bytes(entry.data[:entry.dlc]),
                is_extended_id=entry.frame_id > EXTENDED_ID_THRESHOLD,
                is_fd=use_fd,
                bitrate_switch=entry.brs if use_fd else False,
                dlc=entry.dlc,
            )

            # 🔧 Lock scope hẹp, double-check bus
            with QMutexLocker(self.bus_mutex):
                if self.bus is None:
                    return False
                self.bus.send(msg)

            entry.tx_count += 1
            return True
        except Exception as e:
            self._log(f"[TX ERR] {entry.name}: {e}")
            return False

    # ----------------------------------------------------------
    # VECTOR-SPECIFIC
    # ----------------------------------------------------------

    def _connect_vector(
        self, hw: Dict, ch: int, kwargs: Dict, app_name: str,
    ) -> Optional[object]:
        can = _get_can()
        vector_kwargs = {**kwargs, "receive_own_messages": True}

        if app_name:
            self._vector_set_appl_config(app_name, hw, ch)

        bus = None
        if app_name:
            try:
                bus = can.interface.Bus(
                    interface="vector", channel=ch,
                    app_name=app_name, **vector_kwargs,
                )
                self._log(f"Bus opened with app_name='{app_name}'")
            except Exception as e1:
                self._log(f"App '{app_name}' failed ({e1}), trying direct...")
                bus = can.interface.Bus(
                    interface="vector", channel=ch, **vector_kwargs,
                )
                self._log("Bus opened without app_name")
        else:
            bus = can.interface.Bus(
                interface="vector", channel=ch, **vector_kwargs,
            )
            self._log("Bus opened without app_name")

        if bus:
            self.bus = bus
            self._vector_force_tx_output()
            self._vector_log_channel_info()

        return bus

    def _vector_set_appl_config(self, app_name: str, hw: Dict, ch: int) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            vxlapi = ctypes.windll.LoadLibrary("vxlapi64.dll")
            status = vxlapi.xlSetApplConfig(
                ctypes.c_char_p(app_name.encode()),
                ctypes.c_uint(ch),
                ctypes.c_uint(hw.get("hw_type", 0)),
                ctypes.c_uint(hw.get("hw_index", 0)),
                ctypes.c_uint(hw.get("hw_channel", ch)),
                ctypes.c_uint(1),  # XL_BUS_TYPE_CAN
            )
            if status == 0:
                self._log(f"xlSetApplConfig OK: Ch{ch}")
            else:
                self._log(f"xlSetApplConfig warning (status={status})")
        except Exception as e:
            self._log(f"xlSetApplConfig skipped: {e}")

    def _vector_force_tx_output(self) -> None:
        if sys.platform != "win32" or not self.bus:
            return
        try:
            import ctypes
            vxlapi = ctypes.windll.LoadLibrary("vxlapi64.dll")

            port = self._get_attr("port_handle", "_port_handle", "handle")
            mask = self._get_attr(
                "mask", "_channel_mask", "channel_mask",
                "permission_mask", "_permission_mask",
            )
            if port is None or mask is None:
                self._log("Vector handles not found")
                return

            perm = self._get_attr("permission_mask", "_permission_mask")
            if perm is not None and perm == 0:
                self._log("WARNING: TX permission NOT granted!")
                return

            status = vxlapi.xlCanSetChannelOutput(
                ctypes.c_long(port),
                ctypes.c_ulonglong(mask),
                ctypes.c_int(1),  # XL_OUTPUT_MODE_NORMAL
            )
            if status == 0:
                self._log("Vector TX output: NORMAL mode")
            else:
                self._log(f"xlCanSetChannelOutput status={status}")
        except Exception as e:
            self._log(f"xlCanSetChannelOutput: {e}")

    def _vector_log_channel_info(self) -> None:
        if not self.bus:
            return
        try:
            parts = []
            for attr, label in [
                ("channel_info", "Info"),
                ("port_handle", "Port"), ("_port_handle", "Port"),
                ("permission_mask", "PermMask"), ("_permission_mask", "PermMask"),
            ]:
                if hasattr(self.bus, attr):
                    val = getattr(self.bus, attr)
                    if isinstance(val, int) and "Mask" in label:
                        parts.append(f"{label}=0x{val:X}")
                    else:
                        parts.append(f"{label}={val}")
            if parts:
                self._log(f"Vector: {', '.join(parts)}")

            perm = self._get_attr("permission_mask", "_permission_mask")
            if perm is not None:
                if perm == 0:
                    self._log("*** WARNING: permission_mask=0 → NO TX! ***")
                else:
                    self._log(f"TX permission OK (mask=0x{perm:X})")
        except Exception as e:
            self._log(f"Channel info: {e}")

    def _get_attr(self, *names) -> Optional[int]:
        """Tìm attribute trên bus object (python-can versions khác tên)."""
        for name in names:
            if hasattr(self.bus, name):
                return getattr(self.bus, name)
        return None