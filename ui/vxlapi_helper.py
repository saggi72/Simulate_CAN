"""
==============================================================
ui/vxlapi_helper.py — Vector XL vxlapi64.dll Path Resolver (Tối ưu hoá)
==============================================================

Thay đổi so với bản gốc:
- [OPT-1] Thêm type hints đầy đủ
- [OPT-2] Dùng set() tracking duplicates thay vì check path mỗi lần
- [OPT-3] Thêm docstring tiếng Việt
- [OPT-4] Early return nếu không phải Windows

Vấn đề:
    python-can tìm vxlapi64.dll CHỈ trong system PATH.
    Nếu DLL nằm trong project folder, python-can không tìm thấy
    → import can.interfaces.vector thất bại.

Giải pháp:
    Gọi patch_vxlapi_path() MỘT LẦN TRƯỚC `import can`:
        from ui.vxlapi_helper import patch_vxlapi_path
        patch_vxlapi_path()
        import can

Thứ tự tìm kiếm:
    1. extra_paths (caller cung cấp)
    2. Thư mục chứa main.py (script directory)
    3. Subfolder Lib/, lib/, dll/ trong project
    4. Thư mục cài đặt Vector XL Driver Library
"""

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Tên DLL cần tìm
_DLL_NAME = "vxlapi64.dll"

# Đường dẫn cài đặt Vector phổ biến trên Windows
_VECTOR_INSTALL_PATHS = [
    r"C:\Users\Public\Documents\Vector\XL Driver Library\bin",
    r"C:\Program Files\Vector XL Driver Library\bin",
    r"C:\Program Files (x86)\Vector XL Driver Library\bin",
]


def patch_vxlapi_path(extra_paths: Optional[List[str]] = None) -> List[str]:
    """
    Thêm folders chứa vxlapi64.dll vào DLL search path.
    Đã được tối ưu để nhận diện cả môi trường EXE và Development.
    """
    if sys.platform != "win32":
        logger.debug("[vxlapi] Không phải Windows — bỏ qua")
        return []

    # Build danh sách thư mục cần kiểm tra
    candidates: List[str] = list(extra_paths or [])

    # --- PHẦN SỬA ĐỔI ĐỂ TƯƠNG THÍCH EXE ---
    if hasattr(sys, '_MEIPASS'):
        # Nếu là EXE: DLL nằm trong thư mục tạm giải nén
        base_dir = Path(sys._MEIPASS)
        # Thêm thư mục Lib trong thư mục tạm
        candidates.append(str(base_dir / "Lib"))
        candidates.append(str(base_dir / "lib"))
    else:
        # Nếu là code .py: Lấy thư mục chứa file main.py
        # Dùng sys.argv[0] để đảm bảo lấy đúng entry point
        base_dir = Path(sys.argv[0]).resolve().parent

    candidates.extend([
        str(base_dir),
        str(base_dir / "Lib"),
        str(base_dir / "lib"),
        str(base_dir / "dll"),
    ])
    # ---------------------------------------

    # Đường dẫn cài đặt Vector tiêu chuẩn
    candidates.extend(_VECTOR_INSTALL_PATHS)

    added: List[str] = []
    patched_dirs: set = set()

    for folder in candidates:
        if not folder: continue
        p = Path(folder)
        dll = p / _DLL_NAME

        if not dll.exists():
            continue

        folder_str = str(p.absolute())
        if folder_str in patched_dirs:
            continue

        patched_dirs.add(folder_str)

        # Thêm vào Windows DLL search path (Cực kỳ quan trọng cho Python 3.8+)
        try:
            os.add_dll_directory(folder_str)
            logger.debug("[vxlapi] Added DLL directory: %s", folder_str)
        except (AttributeError, OSError, ValueError):
            pass

        # Thêm vào PATH để hỗ trợ các thư viện ctypes cũ
        current_path = os.environ.get("PATH", "")
        if folder_str not in current_path:
            os.environ["PATH"] = folder_str + os.pathsep + current_path

        added.append(str(dll))
        logger.info("[vxlapi] Found and Patched DLL: %s", dll)

    if not added:
        logger.warning("[vxlapi] Không tìm thấy vxlapi64.dll")

    return added