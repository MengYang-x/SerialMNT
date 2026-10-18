"""会话页面切换时的方向性滑动淡出过渡。"""

from PySide6.QtCore import Property, QEasingCurve, QObject, QPropertyAnimation, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QWidget

_FADE_DURATION_MS = 360
_SLIDE_DISTANCE_PX = 48
# 旧页在整个位移进度中保持完全不透明，只有最后这一段进度才淡出。
_FADE_OUT_PORTION = 0.35
_OVERLAY_OBJECT_NAME = "pageTransitionOverlay"
_HIDDEN_PROGRESS = 0.0
_VISIBLE_PROGRESS = 1.0


class _SnapshotOverlay(QWidget):
    """按进度平移并淡化旧页面快照的覆盖层。

    直接以 ``QPainter`` 绘制 pixmap，不经过 ``QGraphicsOpacityEffect``：
    没有整页离屏合成，首帧就能稳定显示快照，平移时也不做缩放，文字保持清晰。
    """

    def __init__(
        self,
        snapshot: QPixmap,
        parent: QWidget,
        *,
        slide_distance: int,
    ) -> None:
        super().__init__(parent)
        self._snapshot = snapshot
        self._slide_distance = slide_distance
        self._progress = _VISIBLE_PROGRESS
        self.setObjectName(_OVERLAY_OBJECT_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def get_progress(self) -> float:
        """返回快照当前可见比例，1 表示完全可见。"""
        return self._progress

    def set_progress(self, value: float) -> None:
        """更新可见比例并请求重绘。"""
        self._progress = value
        self.update()

    progress = Property(float, get_progress, set_progress)

    def paintEvent(self, event: QPaintEvent) -> None:
        traveled = _VISIBLE_PROGRESS - self._progress
        offset = round(self._slide_distance * traveled)
        opacity = min(_VISIBLE_PROGRESS, self._progress / _FADE_OUT_PORTION)
        painter = QPainter(self)
        painter.setOpacity(opacity)
        painter.drawPixmap(self.rect().translated(offset, 0), self._snapshot)


class PageFadeTransition(QObject):
    """让旧页面沿标签切换方向滑出并淡出，露出实时的新页面。

    标签切换完成后旧页面已被隐藏，但 ``grab()`` 仍能渲染出完整快照。快照放在
    页面容器内与新页面同尺寸、屏蔽鼠标事件的覆盖层上：沿水平方向缓动平移一个
    短距离，前段保持完全不透明，最后一段进度才淡出，新页面在下方逐步显现且
    保持实时刷新。

    同一时间只保留一条覆盖层；快速连续切换会先停止上一条动画并隐藏旧覆盖层。
    覆盖层不接收鼠标事件，过渡期间新页面即可正常操作。
    """

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        duration_ms: int = _FADE_DURATION_MS,
    ) -> None:
        super().__init__(parent)
        self._duration_ms = max(duration_ms, 1)
        self._overlay: _SnapshotOverlay | None = None
        self._animation: QPropertyAnimation | None = None

    def play(
        self,
        outgoing: QWidget | None,
        incoming: QWidget,
        *,
        direction: int = -1,
    ) -> None:
        """从旧页面快照滑出到新页面；direction 为 1 时向右滑出，-1 时向左。"""
        self.stop()
        overlay = self._build_overlay(outgoing, incoming, direction)
        if overlay is None:
            return
        animation = QPropertyAnimation(overlay, b"progress", self)
        animation.setDuration(self._duration_ms)
        animation.setStartValue(_VISIBLE_PROGRESS)
        animation.setEndValue(_HIDDEN_PROGRESS)
        animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        animation.finished.connect(self.stop)
        overlay.destroyed.connect(self._overlay_destroyed)
        self._overlay = overlay
        self._animation = animation
        overlay.show()
        overlay.raise_()
        animation.start()

    def stop(self) -> None:
        """停止动画并移除覆盖层。"""
        animation = self._animation
        overlay = self._overlay
        self._animation = None
        self._overlay = None
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()

    def _build_overlay(
        self,
        outgoing: QWidget | None,
        incoming: QWidget,
        direction: int,
    ) -> _SnapshotOverlay | None:
        """按新页面位置创建旧页快照覆盖层，没有可用快照时返回 None。"""
        if outgoing is None or outgoing is incoming or incoming.parentWidget() is None:
            return None
        snapshot = outgoing.grab()
        if snapshot.isNull():
            return None
        slide_distance = _SLIDE_DISTANCE_PX if direction >= 0 else -_SLIDE_DISTANCE_PX
        overlay = _SnapshotOverlay(
            snapshot,
            incoming.parentWidget(),
            slide_distance=slide_distance,
        )
        overlay.setGeometry(incoming.geometry())
        return overlay

    def _overlay_destroyed(self, overlay: QObject) -> None:
        """覆盖层随窗口销毁时丢弃引用，避免后续访问已释放控件。"""
        if self._overlay is overlay:
            self._overlay = None
