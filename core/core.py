
"""
core/core.py

CAN / CAN FD core utilities.

Features:
- CAN classic + CAN FD
- CRC-8 / CRC-16 / CRC-32
- Alive Counter / Gen Counter
- Intel + Motorola signal packing
- DBC-style Motorola bit numbering
- CAN FD DLC conversion
- Validation-safe encode/decode
"""

from __future__ import annotations

import re
import zlib

from abc import ABC, abstractmethod
from enum import IntEnum, auto
from typing import Dict, List, Optional, Set


# ==============================================================
# CAN TYPES
# ==============================================================

class CanType(IntEnum):
    CLASSIC = auto()
    FD = auto()


MAX_DLC: Dict[CanType, int] = {
    CanType.CLASSIC: 8,
    CanType.FD: 64,
}


# ==============================================================
# CAN FD DLC MAP
# ==============================================================

CAN_FD_DLC_MAP: Dict[int, int] = {
    0: 0,
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
    8: 8,
    9: 12,
    10: 16,
    11: 20,
    12: 24,
    13: 32,
    14: 48,
    15: 64,
}

CAN_FD_LEN_TO_DLC: Dict[int, int] = {
    v: k for k, v in CAN_FD_DLC_MAP.items()
}

CAN_FD_VALID_SIZES = sorted(CAN_FD_LEN_TO_DLC.keys())


def normalize_fd_length(length: int) -> int:

    if length < 0 or length > 64:
        raise ValueError("CAN FD payload must be 0..64 bytes")

    for size in CAN_FD_VALID_SIZES:
        if length <= size:
            return size

    return 64


def fd_len_to_dlc(length: int) -> int:
    return CAN_FD_LEN_TO_DLC[normalize_fd_length(length)]


# ==============================================================
# PROTOCOL
# ==============================================================

DEFAULT_FD_DATA_BITRATE = 2_000_000
EXTENDED_ID_THRESHOLD = 0x7FF


# ==============================================================
# TIMING
# ==============================================================

TICK_MS = 10
UI_REFRESH_MS = 200
STATS_UPDATE_MS = 500

DEFAULT_CYCLE_MS = 100
MAX_CYCLE_MS = 60_000


# ==============================================================
# LOGGING
# ==============================================================

MAX_LOG_LINES = 2000
LOG_BUFFER_MAX = 500
LOG_BUFFER_KEEP = 200
LOG_FLUSH_MAX_SHOW = 50

TX_LOG_ENABLED = True
RX_LOG_ENABLED = False


# ==============================================================
# BAUDRATES
# ==============================================================

BAUD_RATES = [
    "125000",
    "250000",
    "500000",
    "1000000",
]

FD_DATA_BITRATES = [
    "1000000",
    "2000000",
    "4000000",
    "5000000",
    "8000000",
]


# ==============================================================
# UI
# ==============================================================

LOG_BOX_HEIGHT = 140

SIG_EDITOR_COL_WIDTHS: Dict[int, int] = {
    1: 60,
    2: 100,
    3: 110,
    4: 50,
    5: 55,
}

TX_TABLE_COL_WIDTHS: Dict[int, int] = {
    1: 60,
    2: 35,
    3: 70,
    4: 95,
    6: 45,
}


# ==============================================================
# GENERATORS
# ==============================================================

GENERATOR_OPTIONS = [
    "None",
    "Gen Checksum",
    "Gen Counter",
    "Alive Counter",
    "Ramp",
    "Random",
]


# ==============================================================
# CRC
# ==============================================================

class CrcAlgorithm:

    VARIANTS = {

        # ======================================================
        # CRC8 SAE J1850
        # poly=0x1D init=0xFF xor=0xFF
        # ======================================================

        "CRC-8/SAE-J1850": {
            "poly": 0x1D,
            "init": 0xFF,
            "xor_out": 0xFF,
            "width": 8,
            "reflected": False,
        },

        # ======================================================
        # AUTOSAR CRC8H2F
        # ======================================================

        "CRC-8/AUTOSAR": {
            "poly": 0x2F,
            "init": 0xFF,
            "xor_out": 0xFF,
            "width": 8,
            "reflected": False,
        },

        # ======================================================
        # CRC16
        # ======================================================

        "CRC-16/CCITT-FALSE": {
            "poly": 0x1021,
            "init": 0xFFFF,
            "xor_out": 0x0000,
            "width": 16,
            "reflected": False,
        },

        # ======================================================
        # CRC32 Ethernet
        # ======================================================

        "CRC-32/ETHERNET": {
            "poly": 0x04C11DB7,
            "init": 0xFFFFFFFF,
            "xor_out": 0xFFFFFFFF,
            "width": 32,
            "reflected": True,
        },

        # ======================================================
        # XOR
        # ======================================================

        "XOR-8": {
            "poly": None,
            "init": 0x00,
            "xor_out": 0x00,
            "width": 8,
            "reflected": False,
        },
    }

    _ALIASES = {
        "CRC-8H2F": "CRC-8/AUTOSAR",
        "CRC-16/AUTOSAR": "CRC-16/CCITT-FALSE",
    }

    _tables: Dict[tuple, List[int]] = {}

    @staticmethod
    def _reflect(value: int, width: int) -> int:

        result = 0

        for i in range(width):
            if value & (1 << i):
                result |= 1 << (width - 1 - i)

        return result

    @classmethod
    def _get_table(
        cls,
        poly: int,
        width: int,
        reflected: bool,
    ) -> List[int]:

        key = (poly, width, reflected)

        if key in cls._tables:
            return cls._tables[key]

        table = [0] * 256
        mask = (1 << width) - 1

        if reflected:

            poly = cls._reflect(poly, width)

            for i in range(256):

                crc = i

                for _ in range(8):

                    if crc & 1:
                        crc = (crc >> 1) ^ poly
                    else:
                        crc >>= 1

                table[i] = crc & mask

        else:

            top_bit = 1 << (width - 1)

            for i in range(256):

                crc = i << (width - 8)

                for _ in range(8):

                    if crc & top_bit:
                        crc = ((crc << 1) ^ poly) & mask
                    else:
                        crc = (crc << 1) & mask

                table[i] = crc

        cls._tables[key] = table
        return table

    @classmethod
    def compute(
        cls,
        data: bytes,
        variant: str = "CRC-8/SAE-J1850",
    ) -> int:

        variant = cls._ALIASES.get(variant, variant)

        # Ethernet CRC32
        if variant == "CRC-32/ETHERNET":
            return zlib.crc32(data) & 0xFFFFFFFF

        cfg = cls.VARIANTS.get(
            variant,
            cls.VARIANTS["CRC-8/SAE-J1850"],
        )

        width = cfg["width"]
        reflected = cfg["reflected"]

        # XOR
        if cfg["poly"] is None:

            crc = cfg["init"]

            for b in data:
                crc ^= b

            return (crc ^ cfg["xor_out"]) & (
                (1 << width) - 1
            )

        table = cls._get_table(
            cfg["poly"],
            width,
            reflected,
        )

        mask = (1 << width) - 1

        crc = cfg["init"] & mask

        if reflected:

            for b in data:
                crc = (
                    table[(crc ^ b) & 0xFF]
                    ^ (crc >> 8)
                ) & mask

        else:

            if width == 8:

                for b in data:
                    crc = table[(crc ^ b) & 0xFF]

            else:

                shift = width - 8

                for b in data:

                    idx = (
                        ((crc >> shift) ^ b) & 0xFF
                    )

                    crc = (
                        table[idx]
                        ^ ((crc << 8) & mask)
                    ) & mask

        return (crc ^ cfg["xor_out"]) & mask

    @classmethod
    def width_of(cls, variant: str) -> int:

        variant = cls._ALIASES.get(variant, variant)

        cfg = cls.VARIANTS.get(variant)

        if cfg is None:
            return 8

        return cfg["width"]


# ==============================================================
# COUNTERS
# ==============================================================

class BaseCounter(ABC):

    def __init__(self, bit_length: int):

        if bit_length <= 0:
            raise ValueError("bit_length must be > 0")

        self._bit_length = bit_length
        self._max_raw = (1 << bit_length) - 1
        self._current = 0

    @abstractmethod
    def next(self) -> int:
        ...

    def reset(self) -> None:
        self._current = 0

    def peek(self) -> int:
        return self._current


class AliveCounter(BaseCounter):

    def __init__(
        self,
        bit_length: int = 4,
        invalid_value: Optional[int] = None,
    ):

        super().__init__(bit_length)

        self._invalid = (
            invalid_value
            if invalid_value is not None
            else self._max_raw
        )

    def next(self) -> int:

        if self._current >= self._invalid:
            self._current = 0

        value = self._current

        self._current += 1

        if self._current >= self._invalid:
            self._current = 0

        return value


class GenCounter(BaseCounter):

    def next(self) -> int:

        value = self._current

        self._current = (
            self._current + 1
        ) & self._max_raw

        return value


# ==============================================================
# MOTOROLA BIT WALKER
# ==============================================================

def _motorola_next_bit(bit_index: int) -> int:

    bit_in_byte = bit_index % 8

    if bit_in_byte == 0:
        return bit_index + 15

    return bit_index - 1


# ==============================================================
# SIGNAL BYTE SET
# ==============================================================

def get_signal_byte_set(
    start: int,
    length: int,
    byte_order: str = "little_endian",
) -> Set[int]:

    result: Set[int] = set()

    if byte_order == "big_endian":

        bit = start

        for _ in range(length):

            result.add(bit // 8)
            bit = _motorola_next_bit(bit)

    else:

        for i in range(length):
            result.add((start + i) // 8)

    return result


# ==============================================================
# SIGNAL ENCODER
# ==============================================================

class SignalEncoder:

    @staticmethod
    def encode(
        data: List[int],
        start: int,
        length: int,
        raw: int,
        byte_order: str = "little_endian",
    ) -> None:

        raw &= (1 << length) - 1

        if byte_order == "big_endian":
            SignalEncoder._encode_motorola(
                data,
                start,
                length,
                raw,
            )
        else:
            SignalEncoder._encode_intel(
                data,
                start,
                length,
                raw,
            )

    @staticmethod
    def decode(
        data: List[int],
        start: int,
        length: int,
        byte_order: str = "little_endian",
    ) -> int:

        if byte_order == "big_endian":
            return SignalEncoder._decode_motorola(
                data,
                start,
                length,
            )

        return SignalEncoder._decode_intel(
            data,
            start,
            length,
        )

    # ==========================================================
    # INTEL
    # ==========================================================

    @staticmethod
    def _encode_intel(
        data: List[int],
        start: int,
        length: int,
        raw: int,
    ) -> None:

        for i in range(length):

            bit = (raw >> i) & 1

            bit_index = start + i

            byte_pos = bit_index // 8
            bit_pos = bit_index % 8

            if bit:
                data[byte_pos] |= (1 << bit_pos)
            else:
                data[byte_pos] &= ~(1 << bit_pos)

    @staticmethod
    def _decode_intel(
        data: List[int],
        start: int,
        length: int,
    ) -> int:

        raw = 0

        for i in range(length):

            bit_index = start + i

            byte_pos = bit_index // 8
            bit_pos = bit_index % 8

            if data[byte_pos] & (1 << bit_pos):
                raw |= (1 << i)

        return raw

    # ==========================================================
    # MOTOROLA
    # ==========================================================

    @staticmethod
    def _encode_motorola(
        data: List[int],
        start: int,
        length: int,
        raw: int,
    ) -> None:

        bit_index = start

        for i in range(length):

            bit_val = (
                raw >> (length - 1 - i)
            ) & 1

            byte_pos = bit_index // 8
            bit_pos = bit_index % 8

            if bit_val:
                data[byte_pos] |= (1 << bit_pos)
            else:
                data[byte_pos] &= ~(1 << bit_pos)

            bit_index = _motorola_next_bit(bit_index)

    @staticmethod
    def _decode_motorola(
        data: List[int],
        start: int,
        length: int,
    ) -> int:

        raw = 0

        bit_index = start

        for i in range(length):

            byte_pos = bit_index // 8
            bit_pos = bit_index % 8

            if data[byte_pos] & (1 << bit_pos):

                raw |= (
                    1 << (length - 1 - i)
                )

            bit_index = _motorola_next_bit(bit_index)

        return raw


# ==============================================================
# SIGNAL TYPE DETECTOR
# ==============================================================

class SignalTypeDetector:

    _RULES = [

        (
            [
                re.compile(r"checksum", re.I),
                re.compile(r"chksum", re.I),
                re.compile(r"\bcrc\b", re.I),
            ],
            "Gen Checksum",
        ),

        (
            [
                re.compile(r"alive.?counter", re.I),
                re.compile(r"rolling.?counter", re.I),
                re.compile(r"\balv\b", re.I),
            ],
            "Alive Counter",
        ),

        (
            [
                re.compile(r"msg.?cnt", re.I),
                re.compile(r"message.?counter", re.I),
            ],
            "Gen Counter",
        ),
    ]

    @staticmethod
    def detect(name: str) -> str:

        for patterns, result in SignalTypeDetector._RULES:

            for pattern in patterns:

                if pattern.search(name):
                    return result

        return "None"

