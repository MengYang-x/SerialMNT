"""测试会话页面切换的交叉淡入淡出过渡。"""

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QStackedWidget, QWidget

from serialmnt.ui.page_transition import PageFadeTransition

_OVERLAY_NAME = "pageTransitionOverlay"


def _stack(qtbot: object) -> tuple[QStackedWidget, QWidget, QWidget]:
    stack = QStackedWidget()
    stack.resize(240, 140)
    first = QWidget()
    first.setStyleSheet("background: red;")
    second = QWidget()
    second.setStyleSheet("background: blue;")
    stack.addWidget(first)
    stack.addWidget(second)
    qtbot.addWidget(stack)
    stack.show()
    return stack, first, second


def _overlays(page: QWidget) -> list[QWidget]:
    parent = page.parentWidget()
    if parent is None:
        return []
    return [
        child
        for child in parent.children()
        if isinstance(child, QWidget) and child.objectName() == _OVERLAY_NAME
    ]


def test_switch_overlays_outgoing_snapshot_and_fades_out(qtbot: object) -> None:
    stack, first, second = _stack(qtbot)
    transition = PageFadeTransition(duration_ms=40)
    stack.setCurrentWidget(second)

    transition.play(first, second)

    overlays = _overlays(second)
    assert len(overlays) == 1
    overlay = overlays[0]
    assert overlay.isVisible()
    assert overlay.geometry() == second.geometry()
    assert overlay.property("progress") == 1.0

    qtbot.waitUntil(lambda: not _overlays(second), timeout=2000)


def test_rapid_switch_keeps_only_latest_overlay(qtbot: object) -> None:
    stack, first, second = _stack(qtbot)
    third = QWidget()
    stack.addWidget(third)
    transition = PageFadeTransition(duration_ms=1000)
    stack.setCurrentWidget(second)

    transition.play(first, second)
    transition.play(second, third)

    visible = [label for label in _overlays(second) if label.isVisible()]
    assert len(visible) == 1
    assert visible[0].geometry() == third.geometry()

    transition.stop()
    assert not any(label.isVisible() for label in _overlays(second))


def _render_overlay(overlay: QWidget) -> QImage:
    """把覆盖层渲染到新页面颜色的底图上，避免依赖窗口合成时序。"""
    image = QImage(overlay.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#0000ff"))
    overlay.render(image)
    return image


def test_slide_direction_exposes_new_page_from_opposite_edge(qtbot: object) -> None:
    stack, first, second = _stack(qtbot)
    transition = PageFadeTransition(duration_ms=1000)
    stack.setCurrentWidget(second)

    transition.play(first, second, direction=1)
    overlay = next(label for label in _overlays(second) if label.isVisible())
    overlay.set_progress(0.5)
    right_slide = _render_overlay(overlay)
    right = overlay.width() - 4
    middle = overlay.height() // 2
    assert right_slide.pixelColor(4, middle).name() == "#0000ff"
    assert right_slide.pixelColor(right, middle).name() == "#ff0000"

    transition.play(first, second, direction=-1)
    overlay = next(label for label in _overlays(second) if label.isVisible())
    overlay.set_progress(0.5)
    left_slide = _render_overlay(overlay)
    assert left_slide.pixelColor(right, middle).name() == "#0000ff"
    assert left_slide.pixelColor(4, middle).name() == "#ff0000"

    transition.stop()


def test_switch_without_outgoing_page_skips_transition(qtbot: object) -> None:
    stack, _first, second = _stack(qtbot)
    transition = PageFadeTransition(duration_ms=1000)
    stack.setCurrentWidget(second)

    transition.play(None, second)

    assert _overlays(second) == []
