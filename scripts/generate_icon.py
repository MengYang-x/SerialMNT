"""从代码生成应用图标（PNG + 多尺寸 ICO）。

图标是程序化绘制的几何图形，不依赖外部素材：圆角渐变底上叠加一条
方波折线构成的 Serial 首字母 S，同时表达串口数字信号与收发方向。
修改图形后重新运行本脚本，把生成结果与脚本一起提交：

    .\.venv\Scripts\python.exe scripts\generate_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "serialmnt" / "ui" / "assets"
ICO_SIZES = (16, 32, 48, 256)
ICO_MAX_EDGE = 256
CORNER_RADIUS = 56.0
STROKE_WIDTH = 26.0


def _draw_icon(size: int) -> QImage:
    """绘制圆角渐变底 + 由方波折线构成的 Serial 首字母 S。"""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    scale = size / 256.0
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0.0, 0.0, float(size), float(size))
        gradient.setColorAt(0.0, QColor("#2563EB"))
        gradient.setColorAt(1.0, QColor("#1E3A8A"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(
            QRectF(0.0, 0.0, float(size), float(size)),
            CORNER_RADIUS * scale,
            CORNER_RADIUS * scale,
        )

        pen = QPen(QColor("#FFFFFF"))
        pen.setWidthF(STROKE_WIDTH * scale)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        path = QPainterPath()
        path.moveTo(184.0 * scale, 64.0 * scale)
        path.lineTo(72.0 * scale, 64.0 * scale)
        path.lineTo(72.0 * scale, 128.0 * scale)
        path.lineTo(184.0 * scale, 128.0 * scale)
        path.lineTo(184.0 * scale, 192.0 * scale)
        path.lineTo(72.0 * scale, 192.0 * scale)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    finally:
        painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("无法编码 PNG 图标。")
    return bytes(buffer.data())


def _ico_edge(size: int) -> int:
    """ICO 目录项用 0 表示 256 像素。"""
    return 0 if size >= ICO_MAX_EDGE else size


def _write_ico(path: Path, images: tuple[tuple[int, bytes], ...]) -> None:
    """写出 ICO 容器；每个尺寸以 PNG 编码条目存储。"""
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        edge = _ico_edge(size)
        entries += struct.pack(
            "<BBBBHHII", edge, edge, 0, 0, 1, 32, len(data), offset + len(payload)
        )
        payload += data
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + bytes(entries) + bytes(payload))


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = _draw_icon(256)
    if not master.save(str(ASSETS / "serialmnt.png"), "PNG"):
        raise RuntimeError("无法写出 PNG 图标。")
    _write_ico(
        ASSETS / "serialmnt.ico",
        tuple((size, _png_bytes(_draw_icon(size))) for size in ICO_SIZES),
    )
    del app
    print(f"wrote {ASSETS / 'serialmnt.png'}")
    print(f"wrote {ASSETS / 'serialmnt.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
