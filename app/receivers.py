"""接続中の映像デバイスを列挙し、受信器（設定のデバイス名に一致するもの）を探す。"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

import cv2

log = logging.getLogger(__name__)


@dataclass
class Receiver:
    index: int          # cv2.VideoCapture に渡すデバイス番号
    backend: int        # cv2.CAP_DSHOW など
    name: str           # デバイス名
    path: str = ""      # デバイスパス（USBポートの区別に使う）


def default_backend() -> int:
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_V4L2


def has_device_names() -> bool:
    """デバイスを開かずに名前付きで列挙できるか（自動検索はこのときだけ常時行う）。"""
    try:
        import cv2_enumerate_cameras  # noqa: F401
        return True
    except ImportError:
        return False


def signature(receivers: list[Receiver]) -> tuple:
    """受信器の組み合わせが変わったかを比べるための値。"""
    return tuple((r.index, r.name, r.path) for r in receivers)


def list_devices(backend: int | None = None) -> list[Receiver]:
    """名前付きでデバイスを列挙する。cv2-enumerate-cameras が無ければ番号だけで探す。"""
    backend = default_backend() if backend is None else backend
    try:
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError:
        log.warning("cv2-enumerate-cameras is not available. Probing device numbers 0-9 without names")
        return _probe_without_names(backend)

    devices = []
    for info in enumerate_cameras(backend):
        idx = int(info.index)
        # ライブラリの index にはバックエンドの番号(700, 1400 など)が足されている場合がある
        if idx >= 100:
            idx = idx % 100
        devices.append(Receiver(index=idx, backend=int(getattr(info, "backend", backend)),
                                name=str(info.name), path=str(getattr(info, "path", "") or "")))
    return devices


def _probe_without_names(backend: int) -> list[Receiver]:
    found = []
    for i in range(10):
        cap = cv2.VideoCapture(i, backend)
        ok = cap.isOpened()
        cap.release()
        if ok:
            found.append(Receiver(index=i, backend=backend, name=f"Camera {i}"))
    return found


def find_receivers(device_names: list[str], max_count: int = 4,
                   devices: list[Receiver] | None = None, verbose: bool = True) -> list[Receiver]:
    """設定のデバイス名（部分一致）に合うデバイスを最大 max_count 台返す。
    並び順はデバイスパス→番号順にして、毎回同じ ch に割り当たるようにする。"""
    devices = list_devices() if devices is None else devices
    patterns = [p.lower() for p in device_names if p and p.strip()]
    matched = [d for d in devices if any(p in d.name.lower() for p in patterns)]
    matched.sort(key=lambda d: (d.path or "~", d.index))
    for d in devices if verbose else []:
        log.info("device %d: %s %s %s", d.index, d.name, d.path,
                 "(receiver)" if d in matched else "")
    return matched[:max_count]
