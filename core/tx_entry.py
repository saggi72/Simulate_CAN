"""
core/tx_entry.py — TX Message Entry (data model).

TxEntry đại diện 1 message trong TX queue.
Không phụ thuộc UI — chỉ dùng core.core.

CAN FD ready: is_fd, brs flags, data buffer lên tới 64 bytes.
"""

import random
from typing import Dict, List

from core.core import (
    CanType, MAX_DLC, DEFAULT_CYCLE_MS,
    CrcAlgorithm, AliveCounter, GenCounter,
    SignalEncoder, get_signal_byte_set,
)


class TxEntry:
    """
    Đại diện 1 CAN message trong TX queue.

    Generator types:
    - None:          User điều khiển raw value
    - Gen Checksum:  CRC tự tính
    - Gen Counter:   Full range 0→max→0
    - Alive Counter: Skip invalid value (VCS_37)
    - Ramp:          Tăng 1 mỗi tick, wrap
    - Random:        Random mỗi tick

    Attributes:
        name, frame_id, dlc, network, data, signals,
        cycle_ms, send_mode, active, tx_count,
        is_fd, brs, crc_variant
    """

    MODES = ["Cyclic", "Event", "Cyclic+Event"]

    def __init__(
        self,
        name: str,
        frame_id: int,
        dlc: int,
        network: str,
        signals: List[Dict],
        crc_variant: str = "CRC-8/SAE-J1850",
        is_fd: bool = False,
        brs: bool = False,
    ) -> None:
        self.name = name
        self.frame_id = frame_id
        self.dlc = dlc
        self.network = network
        self.is_fd = is_fd
        self.brs = brs

        # Buffer theo DLC, minimum 8 (CAN 2.0) hoặc 64 (FD)
        min_size = MAX_DLC[CanType.FD] if is_fd else MAX_DLC[CanType.CLASSIC]
        self.data: List[int] = [0] * max(dlc, min_size)

        self.signals = signals
        self.cycle_ms: int = DEFAULT_CYCLE_MS
        self.send_mode: str = "Cyclic"
        self.active: bool = True
        self.tx_count: int = 0
        self.crc_variant = crc_variant

        # Tick accumulator
        self._tick_acc: int = 0

        # Counter objects
        self._alive_counters: Dict[str, AliveCounter] = {}
        self._gen_counters: Dict[str, GenCounter] = {}

        # Khởi tạo counters
        for sig in self.signals:
            gen = sig.get("generator", "None")
            if gen == "Alive Counter":
                self._alive_counters[sig["name"]] = AliveCounter(
                    bit_length=sig["length"],
                )
            elif gen == "Gen Counter":
                self._gen_counters[sig["name"]] = GenCounter(
                    bit_length=sig["length"],
                )

        # Guard flag cho Cyclic+Event double-increment
        self._event_sent_this_tick: bool = False

    # ----------------------------------------------------------
    # COUNTER MANAGEMENT
    # ----------------------------------------------------------

    def reset_counters(self) -> None:
        """Reset TẤT CẢ counters về 0. Gọi khi bắt đầu TX session."""
        for alv in self._alive_counters.values():
            alv.reset()
        for gc in self._gen_counters.values():
            gc.reset()
        self._tick_acc = 0
        self._event_sent_this_tick = False

    def ensure_counter(self, sig_name: str, gen_type: str, bit_length: int) -> None:
        """Đảm bảo counter object tồn tại (gọi khi user đổi generator type)."""
        if gen_type == "Alive Counter" and sig_name not in self._alive_counters:
            self._alive_counters[sig_name] = AliveCounter(bit_length=bit_length)
        elif gen_type == "Gen Counter" and sig_name not in self._gen_counters:
            self._gen_counters[sig_name] = GenCounter(bit_length=bit_length)

    # ----------------------------------------------------------
    # TICK / SEND LOGIC
    # ----------------------------------------------------------

    def should_send(self, tick_ms: int) -> bool:
        """Accumulator-based: trả True khi đủ cycle period."""
        if self.send_mode == "Event":
            return False

        self._tick_acc += tick_ms
        if self._tick_acc >= self.cycle_ms:
            self._tick_acc -= self.cycle_ms
            if self._tick_acc >= self.cycle_ms:
                self._tick_acc = 0  # Chống drift tích luỹ
            return True
        return False

    def tick_generators(self, is_event_send: bool = False) -> None:
        """Chạy auto-generators cho tất cả signals, rồi encode."""
        if is_event_send:
            self._event_sent_this_tick = True

        for sig in self.signals:
            gen = sig.get("generator", "None")

            if gen == "Alive Counter":
                alv = self._alive_counters.get(sig["name"])
                if alv is None:
                    alv = AliveCounter(bit_length=sig["length"])
                    self._alive_counters[sig["name"]] = alv
                sig["raw"] = alv.next()

            elif gen == "Gen Counter":
                gc = self._gen_counters.get(sig["name"])
                if gc is None:
                    gc = GenCounter(bit_length=sig["length"])
                    self._gen_counters[sig["name"]] = gc
                sig["raw"] = gc.next()

            elif gen == "Ramp":
                max_val = (1 << sig["length"]) - 1
                sig["raw"] = (sig["raw"] + 1) & max_val

            elif gen == "Random":
                max_val = (1 << sig["length"]) - 1
                sig["raw"] = random.randint(0, max_val)

        self.encode_all_signals()

    # ----------------------------------------------------------
    # ENCODE / CHECKSUM
    # ----------------------------------------------------------

    def encode_all_signals(self) -> None:
        """Encode signals thường trước, checksum SAU CÙNG."""
        # Bước 1: Non-checksum
        for sig in self.signals:
            if sig.get("generator") == "Gen Checksum":
                continue
            SignalEncoder.encode(
                self.data, sig["start"], sig["length"],
                sig["raw"], sig.get("byte_order", "little_endian"),
            )
        # Bước 2: Checksum
        for sig in self.signals:
            if sig.get("generator") == "Gen Checksum":
                chk = self._compute_checksum(sig)
                sig["raw"] = chk
                SignalEncoder.encode(
                    self.data, sig["start"], sig["length"],
                    chk, sig.get("byte_order", "little_endian"),
                )

    def _compute_checksum(self, chk_sig: Dict) -> int:
        """Tính CRC LOẠI TRỪ checksum bytes."""
        chk_bytes = get_signal_byte_set(
            chk_sig["start"],
            chk_sig["length"],
            chk_sig.get("byte_order", "little_endian"),
        )
        crc_input = [
            self.data[i] for i in range(self.dlc) if i not in chk_bytes
        ]
        crc = CrcAlgorithm.compute(bytes(crc_input), self.crc_variant)
        return crc & ((1 << chk_sig["length"]) - 1)

    # ----------------------------------------------------------
    # SERIALIZATION
    # ----------------------------------------------------------

    def to_dict(self) -> Dict:
        """Serialize ra dict cho JSON export."""
        return {
            "name": self.name,
            "frame_id": self.frame_id,
            "dlc": self.dlc,
            "network": self.network,
            "cycle_ms": self.cycle_ms,
            "send_mode": self.send_mode,
            "active": self.active,
            "is_fd": self.is_fd,
            "brs": self.brs,
            "crc_variant": self.crc_variant,
            "signals": [
                {
                    "name": s["name"],
                    "start": s["start"],
                    "length": s["length"],
                    "byte_order": s["byte_order"],
                    "factor": s["factor"],
                    "offset": s["offset"],
                    "raw": s["raw"],
                    "generator": s.get("generator", "None"),
                    "comment": s.get("comment", ""),
                }
                for s in self.signals
            ],
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "TxEntry":
        """Deserialize từ dict (JSON import)."""
        entry = cls(
            name=d["name"],
            frame_id=d["frame_id"],
            dlc=d["dlc"],
            network=d.get("network", ""),
            signals=d["signals"],
            crc_variant=d.get("crc_variant", "CRC-8/SAE-J1850"),
            is_fd=d.get("is_fd", False),
            brs=d.get("brs", False),
        )
        entry.cycle_ms = d.get("cycle_ms", DEFAULT_CYCLE_MS)
        entry.send_mode = d.get("send_mode", "Cyclic")
        entry.active = d.get("active", True)
        return entry

    def get_data_hex(self) -> str:
        """Hex string của data payload."""
        return " ".join(f"{self.data[i]:02X}" for i in range(self.dlc))