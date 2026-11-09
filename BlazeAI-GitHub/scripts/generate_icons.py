#!/usr/bin/env python3
"""Generate PNG, ICO and ICNS app icons using only the Python standard library.

Blaze's mark is sampled with 4 subpixels per output pixel and averaged, so the
repository does not need binary image tooling at build time.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "blaze_ai" / "assets"
SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)
SAMPLES = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))


def _rounded_rect_inside(x: float, y: float, left: float, top: float, w: float, h: float, r: float) -> bool:
    right = left + w
    bottom = top + h
    if x < left or x >= right or y < top or y >= bottom:
        return False
    cx = min(max(x, left + r), right - r)
    cy = min(max(y, top + r), bottom - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r**2


def _sample(px: float, py: float, size: float) -> tuple[int, int, int, int]:
    margin = size * 0.025
    radius = size * 0.235
    if not _rounded_rect_inside(px, py, margin, margin, size - 2 * margin, size - 2 * margin, radius):
        return (0, 0, 0, 0)
    center = size / 2
    ring_radius = size * 0.258
    ring_width = size * 0.034
    distance = math.hypot(px - center, py - center)
    if abs(distance - ring_radius) <= ring_width / 2:
        angle = math.degrees(math.atan2(py - center, px - center)) % 360
        if not (120 <= angle <= 155):
            return (244, 244, 244, 255)
    if distance <= size * 0.036:
        return (244, 244, 244, 255)
    if math.hypot(px - size * 0.712, py - size * 0.276) <= size * 0.062:
        return (255, 45, 45, 255)
    return (0, 0, 0, 255)


def render(size: int) -> bytes:
    out = bytearray()
    step = 1.0 / size
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for sx, sy in SAMPLES:
                sr, sg, sb, sa = _sample(x + sx, y + sy, size)
                r += sr * sa
                g += sg * sa
                b += sb * sa
                a += sa
            if a == 0:
                out.extend((0, 0, 0, 0))
            else:
                out.extend(
                    (
                        r // a,
                        g // a,
                        b // a,
                        a // len(SAMPLES),
                    )
                )
    _ = step
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw.extend(rgba[y * stride : (y + 1) * stride])
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def write_ico(path: Path, images: dict[int, bytes]) -> None:
    entries = []
    offset = 6 + 16 * len(images)
    for size, data in sorted(images.items()):
        dim = 0 if size >= 256 else size
        entries.append(
            struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        )
        offset += len(data)
    payload = struct.pack("<HHH", 0, 1, len(entries)) + b"".join(entries)
    payload += b"".join(data for _, data in sorted(images.items()))
    path.write_bytes(payload)


def write_icns(path: Path, pngs: dict[int, bytes]) -> None:
    chunks = []
    mapping = {
        16: b"icp4",
        32: b"icp5",
        64: b"icp6",
        128: b"ic07",
        256: b"ic08",
        512: b"ic09",
        1024: b"ic10",
    }
    for size, data in pngs.items():
        kind = mapping.get(size)
        if kind:
            chunks.append(kind + struct.pack(">I", len(data) + 8) + data)
    payload = b"icns" + struct.pack(">I", 8 + sum(len(chunk) for chunk in chunks)) + b"".join(chunks)
    path.write_bytes(payload)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    pngs = {}
    for size in SIZES:
        print(f"rendering {size}x{size}…", flush=True)
        pngs[size] = png(size, render(size))
        (ASSETS / f"icon_{size}.png").write_bytes(pngs[size])
    (ASSETS / "blaze_icon.png").write_bytes(pngs[1024])
    write_ico(ASSETS / "blaze_icon.ico", {k: v for k, v in pngs.items() if k <= 256})
    write_icns(ASSETS / "blaze_icon.icns", pngs)
    print(f"wrote icons to {ASSETS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
