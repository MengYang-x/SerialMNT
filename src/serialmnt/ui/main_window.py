"""SerialMNT 主窗口。"""

from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QContextMenuEvent,
    QDesktopServices,
    QKeySequence,
    QMouseEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabBar,
    QTabWidget,
    QToolButton,
    QWidget,
)

from serialmnt import __version__
from serialmnt.application import (
    MAX_SESSION_TITLE_LENGTH,
    SessionManager,
    UpdateCheckResult,
    UpdateService,
    UpdateStatus,
)
from serialmnt.domain import (
    DEFAULT_LOG_LEVEL_RULES,
    AppConfig,
    ConfigIOError,
    ConfigStore,
    DomainError,
    LogLevelRule,
    PortInfo,
    SerialConfig,
    SessionPreferences,
    SessionState,
    Theme,
)
from serialmnt.ui.app_icon import app_icon
from serialmnt.ui.help_window import HelpWindow
from serialmnt.ui.log_level_settings import LogLevelRulesDialog
from serialmnt.ui.no_wheel_combo import NoWheelComboBox
from serialmnt.ui.qt_bridge import QtSessionBridge
from serialmnt.ui.session_tab import _SIDEBAR_DEFAULT_WIDTH, SessionTab
from serialmnt.ui.theme_manager import (
    CONTROL_HEIGHT_PX,
    DATA_FONT_PRESETS,
    apply_theme,
    data_font,
)
from serialmnt.ui.update_check import UpdateCheckRunner, is_allowed_release_url

_THEMES: tuple[tuple[str, Theme], ...] = (
    ("跟随系统", "system"),
    ("浅色", "light"),
    ("深色", "dark"),
)


class _SessionTabBar(QTabBar):
    """把“新建会话”按钮紧贴在最后一个会话标签右侧的标签栏。

    QTabBar 只在标签内提供 ``setTabButton`` 插槽，而标签右侧的关闭按钮已被占用，
    因此按钮改为标签栏的子控件手动定位：标签增删、改名和标签栏缩放都会改变末位标签
    的右边缘，统一在这些时机重新摆放（拖动重排不改变各标签宽度之和，无需处理）。
    放不下时隐藏按钮，避免压住标签或滚动箭头，此时仍可通过 ☰ 菜单和 ``Ctrl+T``
    新建会话。
    """

    def __init__(self, new_button: QToolButton, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._new_button = new_button
        new_button.setParent(self)

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self._place_new_button()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._place_new_button()

    def _place_new_button(self) -> None:
        hint = self._new_button.sizeHint()
        count = self.count()
        left = self.tabRect(count - 1).right() + 1 if count else 0
        if left + hint.width() > self.width():
            self._new_button.setVisible(False)
            return
        self._new_button.setVisible(True)
        self._new_button.setGeometry(
            left,
            max((self.height() - hint.height()) // 2, 0),
            hint.width(),
            hint.height(),
        )


class MainWindow(QMainWindow):
    """管理多标签会话的顶层窗口。"""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        port_provider: Callable[[], tuple[PortInfo, ...]],
        config_store: ConfigStore | None = None,
        initial_config: AppConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_manager = session_manager
        self._port_provider = port_provider
        self._config_store = config_store
        self._last_saved: AppConfig | None = None
        self._session_counter = 0
        self._update_check_manual = False
        self._is_closing = False
        self._update_runner: UpdateCheckRunner | None = None
        self._theme: Theme = initial_config.theme if initial_config is not None else "system"
        self._data_font_size = initial_config.data_font_size if initial_config is not None else 0
        self._log_level_rules = (
            initial_config.log_level_rules
            if initial_config is not None
            else DEFAULT_LOG_LEVEL_RULES
        )
        self._sidebar_visible = initial_config.sidebar_visible if initial_config else True
        self._sidebar_width = (
            initial_config.sidebar_width if initial_config else _SIDEBAR_DEFAULT_WIDTH
        )
        self._content_splitter_state = (
            initial_config.content_splitter_state if initial_config else None
        )
        self._help_window: HelpWindow | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_config)
        self.tabs = QTabWidget()
        self.new_tab_button = QToolButton()
        self.new_tab_button.setText("+")
        self.new_tab_button.setAutoRaise(True)
        self.new_tab_button.setToolTip("新建会话 (Ctrl+T)")
        self.new_tab_button.clicked.connect(self._new_session)
        # 必须先换标签栏再设置属性：setTabBar 不会把关闭按钮和拖动属性带到新标签栏。
        self.tabs.setTabBar(_SessionTabBar(self.new_tab_button))
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)
        self.setWindowTitle(f"SerialMNT {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        # 状态栏只承载临时消息：提前创建并隐藏，消息清空后再次隐藏，
        # 避免检查更新等操作在窗口底部留下一条永久空白。
        self._status_bar = self.statusBar()
        self._status_bar.messageChanged.connect(self._status_message_changed)
        self._status_bar.hide()
        self._build_actions()
        self._restore_or_create_sessions(initial_config)
        self._center_on_primary_screen()
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(3000)
        self._update_timer.timeout.connect(self._auto_check_updates)

    def _status_message_changed(self, message: str) -> None:
        """按临时消息有无显示状态栏，消息超时或清空后让出底部空间。"""
        self._status_bar.setVisible(bool(message))

    def _build_actions(self) -> None:
        new_action = QAction("新建会话", self)
        new_action.setShortcut("Ctrl+T")
        new_action.triggered.connect(self._new_session)
        close_action = QAction("关闭当前会话", self)
        close_action.setShortcut("Ctrl+W")
        close_action.triggered.connect(lambda: self._close_tab(self.tabs.currentIndex()))
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)

        self._build_font_actions()

        self.help_action = QAction("使用帮助", self)
        self.help_action.setShortcut(QKeySequence("F1"))
        self.help_action.triggered.connect(self._show_help)

        self.check_update_action = QAction("检查更新", self)
        self.check_update_action.setEnabled(False)
        self.check_update_action.triggered.connect(self._manual_check_updates)

        self.log_level_action = QAction("日志级别颜色…", self)
        self.log_level_action.triggered.connect(self._open_log_level_settings)

        self.main_menu = QMenu(self)
        self.main_menu.addAction(new_action)
        self.main_menu.addAction(close_action)
        self.main_menu.addMenu(self.font_menu)
        self.main_menu.addAction(self.log_level_action)
        self.main_menu.addSeparator()
        self.main_menu.addAction(self.help_action)
        self.main_menu.addAction(self.check_update_action)
        self.main_menu.addSeparator()
        self.main_menu.addAction(exit_action)
        for action in (
            new_action,
            close_action,
            exit_action,
            self.help_action,
            self.font_zoom_in_action,
            self.font_zoom_out_action,
            self.font_reset_action,
        ):
            # 去掉菜单栏和工具栏后，动作必须挂到窗口上，快捷键才会继续生效。
            self.addAction(action)

        self.menu_button = QToolButton()
        self.menu_button.setText("☰")
        self.menu_button.setAutoRaise(True)
        self.menu_button.setToolTip("新建会话 / 关闭当前会话 / 字号 / 使用帮助 / 检查更新 / 退出")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setMenu(self.main_menu)

        self.theme_combo = self._build_theme_combo()

        self.font_combo = self._build_font_combo()

        self.tabs.setDocumentMode(True)
        self._header_tools = QWidget()
        header_layout = QHBoxLayout(self._header_tools)
        header_layout.setContentsMargins(2, 0, 4, 0)
        header_layout.setSpacing(4)
        header_layout.addWidget(QLabel("主题"))
        header_layout.addWidget(self.theme_combo)
        header_layout.addWidget(QLabel("字号"))
        header_layout.addWidget(self.font_combo)
        header_layout.addWidget(self.menu_button)
        self.tabs.setCornerWidget(self._header_tools, Qt.Corner.TopRightCorner)
        # 设置角落控件会重新挂载父级并隐藏控件，必须显式显示。
        self._header_tools.show()
        self.tabs.tabBar().installEventFilter(self)

    def _build_font_combo(self) -> QComboBox:
        """构造全局字号下拉框，首项为跟随系统默认。"""
        combo = NoWheelComboBox()
        combo.addItem("默认", 0)
        for size in DATA_FONT_PRESETS:
            combo.addItem(str(size), size)
        index = combo.findData(self._data_font_size)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.setFixedHeight(CONTROL_HEIGHT_PX)
        combo.setMinimumWidth(64)
        combo.setToolTip("接收区与发送编辑器的数据字号")
        combo.currentIndexChanged.connect(self._font_size_changed)
        return combo

    def _build_theme_combo(self) -> QComboBox:
        """构造全局主题下拉框。"""
        combo = NoWheelComboBox()
        for label, value in _THEMES:
            combo.addItem(label, value)
        index = combo.findData(self._theme)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.setFixedHeight(CONTROL_HEIGHT_PX)
        combo.setMinimumWidth(88)
        combo.setToolTip("跟随系统 / 浅色 / 深色")
        combo.currentIndexChanged.connect(self._theme_changed)
        return combo

    def _build_font_actions(self) -> None:
        """构造字号缩放动作与“字号”子菜单。"""
        self.font_zoom_in_action = QAction("放大", self)
        self.font_zoom_in_action.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        self.font_zoom_in_action.triggered.connect(lambda: self._zoom_font(1))
        self.font_zoom_out_action = QAction("缩小", self)
        self.font_zoom_out_action.setShortcut("Ctrl+-")
        self.font_zoom_out_action.triggered.connect(lambda: self._zoom_font(-1))
        self.font_reset_action = QAction("恢复默认", self)
        self.font_reset_action.setShortcut("Ctrl+0")
        self.font_reset_action.triggered.connect(self._reset_font_size)
        self.font_menu = QMenu("字号", self)
        self.font_menu.addAction(self.font_zoom_in_action)
        self.font_menu.addAction(self.font_zoom_out_action)
        self.font_menu.addAction(self.font_reset_action)

    def _show_help(self) -> None:
        """打开或前置使用帮助窗口。"""
        if self._help_window is None:
            self._help_window = HelpWindow(self)
        self._help_window.show()
        self._help_window.raise_()
        self._help_window.activateWindow()

    def _open_log_level_settings(self) -> None:
        """打开全局日志级别颜色设置，确认后立即应用到所有会话。"""
        dialog = LogLevelRulesDialog(self._log_level_rules, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_log_level_rules(dialog.rules())

    def _apply_log_level_rules(self, rules: tuple[LogLevelRule, ...]) -> None:
        self._log_level_rules = tuple(rules)
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionTab):
                widget.set_log_level_rules(self._log_level_rules)
        self._schedule_save()

    def configure_update_service(self, service: UpdateService | None) -> None:
        """由组合根注入更新服务并启动一次启动检查。

        未配置服务时“检查更新”菜单项保持禁用，测试和纯 UI 构造不会访问
        网络；重复配置不会创建第二个检查任务。
        """
        if service is None or self._update_runner is not None:
            return
        self._update_runner = UpdateCheckRunner(service, self)
        self._update_runner.finished.connect(self._on_update_checked)
        self.check_update_action.setEnabled(True)
        self._update_timer.start()

    def _auto_check_updates(self) -> None:
        """启动后的静默检查，无更新或失败时不打扰用户。"""
        self._start_update_check(manual=False)

    def _manual_check_updates(self, _checked: bool = False) -> None:
        """☰ 菜单入口，结果需要在状态栏明确反馈。"""
        self._start_update_check(manual=True)

    def _start_update_check(self, *, manual: bool) -> None:
        if self._update_runner is None:
            return
        if manual:
            # 手动检查优先接管正在进行的启动检查，其结果也按手动检查反馈。
            self._update_timer.stop()
            self._update_check_manual = True
            self.check_update_action.setEnabled(False)
            self.statusBar().showMessage("正在检查更新…")
        if not self._update_runner.start() and not manual:
            return

    def _on_update_checked(self, result: UpdateCheckResult) -> None:
        if self._is_closing:
            return
        manual = self._update_check_manual
        self._update_check_manual = False
        self.check_update_action.setEnabled(True)
        if result.status is UpdateStatus.UPDATE_AVAILABLE and result.release is not None:
            self._present_update(result)
            return
        if not manual:
            return
        if result.status is UpdateStatus.UP_TO_DATE:
            self.statusBar().showMessage(f"当前已是最新版本 {result.current_version}", 5000)
        else:
            self.statusBar().showMessage("检查更新失败，请检查网络后重试。", 5000)

    def _build_update_dialog(self, result: UpdateCheckResult) -> tuple[QMessageBox, QPushButton]:
        """构造新版本对话框，供展示和测试分别使用。"""
        release = result.release
        if release is None:
            raise ValueError("发现新版本时必须提供发布信息。")
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setIcon(QMessageBox.Icon.Information)
        # 更新说明来自外部服务，禁止按富文本解析或加载远程图片。
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"当前版本：{result.current_version}\n最新版本：{release.version}")
        if release.published_at is not None:
            published = release.published_at.astimezone().strftime("%Y-%m-%d")
            box.setInformativeText(f"发布时间：{published}")
        if release.notes.strip():
            box.setDetailedText(release.notes)
        download_button = box.addButton("前往下载", QMessageBox.ButtonRole.AcceptRole)
        later_button = box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(later_button)
        box.setEscapeButton(later_button)
        return box, download_button

    def _present_update(self, result: UpdateCheckResult) -> None:
        box, download_button = self._build_update_dialog(result)
        box.exec()
        if box.clickedButton() is download_button and result.release is not None:
            self._open_release_page(result.release.release_url)

    def _open_release_page(self, url: str) -> None:
        """只把受信任的 GitHub HTTPS 页面交给系统浏览器。"""
        if not is_allowed_release_url(url):
            QMessageBox.warning(self, "无法打开下载页", f"下载地址不受支持：\n{url}")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.information(self, "无法打开下载页", f"请手动访问：\n{url}")

    def _center_on_primary_screen(self) -> None:
        """启动时把普通窗口放到主屏可用区域的中央。

        恢复的窗口几何只保留尺寸，位置每次重新计算；最大化或全屏时保持
        用户上次的状态。窗口大于可用区域时贴住左上角，避免标题栏移出屏幕。
        """
        if self.isMaximized() or self.isFullScreen():
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        left = max(available.x() + (available.width() - self.width()) // 2, available.x())
        top = max(available.y() + (available.height() - self.height()) // 2, available.y())
        self.move(left, top)

    def _new_session(self, _checked: bool = False) -> None:
        """标签栏“+”按钮和“☰”菜单共用的零参数入口。

        QAction 与 QToolButton 的信号会带上一个布尔状态，直接连接 ``new_session``
        会把它当成 ``preferences`` 传入并报错，因此在此显式吞掉该参数。
        """
        self.new_session()

    def _restore_or_create_sessions(self, config: AppConfig | None) -> None:
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self._theme)
        if config is None or not config.sessions:
            self.new_session()
            return
        if config.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(config.window_geometry.encode("ascii")))
        if config.window_state:
            self.restoreState(QByteArray.fromBase64(config.window_state.encode("ascii")))
        for preferences in config.sessions:
            # DTR/RTS 不恢复上次保存的状态：每次启动都从未勾选开始。
            self.new_session(
                replace(preferences, config=replace(preferences.config, dtr=False, rts=False))
            )
        self.tabs.setCurrentIndex(config.active_session_index)

    def new_session(self, preferences: SessionPreferences | None = None) -> SessionTab:
        """创建并激活新的断开状态会话。"""
        self._session_counter += 1
        config = preferences.config if preferences is not None else SerialConfig(port="")
        title = preferences.title if preferences is not None else f"会话 {self._session_counter}"
        bridge = QtSessionBridge()
        controller = self._session_manager.create_session(
            config=config,
            title=title,
            on_snapshot=bridge.publish_snapshot,
            on_record=bridge.publish_record,
        )
        if preferences is not None:
            controller.update_log_preferences(
                enabled=preferences.auto_log_enabled,
                log_format=preferences.auto_log_format,
                directory=preferences.auto_log_directory,
            )
        tab = SessionTab(
            controller=controller,
            bridge=bridge,
            port_provider=self._port_provider,
            preferences=preferences,
            log_level_rules=self._log_level_rules,
        )
        current_tab = self.tabs.currentWidget()
        if isinstance(current_tab, SessionTab):
            layout_state = current_tab.layout_state()
        else:
            layout_state = (
                self._sidebar_visible,
                self._sidebar_width,
                self._content_splitter_state,
            )
        tab.apply_layout_state(
            visible=layout_state[0],
            width=layout_state[1],
            content_splitter_state=layout_state[2],
        )
        tab.font_zoom_requested.connect(self._zoom_font)
        tab.preferences_changed.connect(self._schedule_save)
        index = self.tabs.addTab(tab, controller.title)
        self.tabs.setTabToolTip(index, controller.title)
        tab.title_changed.connect(
            lambda title, widget=tab: self._on_tab_title_changed(widget, title)
        )
        self.tabs.setCurrentIndex(index)
        # 全局样式表会在挂载时重新 polish 控件并覆盖之前设置的字体，必须在其后重新应用。
        tab.set_data_font_size(self._data_font_size)
        self._schedule_save()
        return tab

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """在标签栏上处理双击重命名与右键菜单。"""
        bar = self.tabs.tabBar()
        if watched is bar:
            if isinstance(event, QMouseEvent) and event.button() is Qt.MouseButton.LeftButton:
                if event.type() is QEvent.Type.MouseButtonDblClick:
                    index = bar.tabAt(event.position().toPoint())
                    if index >= 0:
                        self._prompt_rename(index)
                        return True
            elif isinstance(event, QContextMenuEvent):
                index = bar.tabAt(event.pos())
                if index >= 0:
                    self.tabs.setCurrentIndex(index)
                    self._show_tab_menu(index, event.globalPos())
                    return True
        return super().eventFilter(watched, event)

    def _show_tab_menu(self, index: int, global_pos: QPoint) -> None:
        """在指定位置弹出标签菜单（独立方法便于测试替换）。"""
        self._build_tab_menu(index).exec(global_pos)

    def _build_tab_menu(self, index: int) -> QMenu:
        """构造标签右键菜单，动作作用于指定标签。"""
        menu = QMenu(self)
        rename_action = menu.addAction("重命名…")
        rename_action.triggered.connect(lambda: self._prompt_rename(index))
        close_action = menu.addAction("关闭当前会话")
        close_action.triggered.connect(lambda: self._close_tab(index))
        menu.addSeparator()
        new_action = menu.addAction("新建会话")
        new_action.triggered.connect(self._new_session)
        return menu

    def _ask_session_title(self, initial: str) -> str | None:
        """弹出名称输入框，取消时返回 None。"""
        dialog = QInputDialog(self)
        dialog.setWindowTitle("重命名会话")
        dialog.setLabelText("名称")
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.setTextValue(initial)
        # QInputDialog.lineEdit() 在 PySide6 未暴露，改用 findChild 取内部输入框。
        editor = dialog.findChild(QLineEdit)
        if editor is not None:
            editor.setMaxLength(MAX_SESSION_TITLE_LENGTH)
            editor.selectAll()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.textValue()

    def _prompt_rename(self, index: int) -> None:
        """对指定标签发起重命名，失败时提示校验信息。"""
        widget = self.tabs.widget(index)
        if not isinstance(widget, SessionTab):
            return
        title = self._ask_session_title(widget.controller.title)
        if title is None or not title.strip():
            return
        try:
            widget.controller.rename(title)
        except DomainError as exc:
            QMessageBox.warning(self, "重命名失败", exc.message)

    def _on_tab_title_changed(self, tab: SessionTab, title: str) -> None:
        """快照标题变化后刷新标签文本、tooltip 并触发保存。"""
        index = self.tabs.indexOf(tab)
        if index < 0:
            return
        self.tabs.setTabText(index, title)
        self.tabs.setTabToolTip(index, title)
        self._schedule_save()

    def _close_tab(self, index: int) -> None:
        if index < 0:
            return
        widget = self.tabs.widget(index)
        if not isinstance(widget, SessionTab):
            return
        if not widget.request_close():
            return
        self._session_manager.remove_session(widget.controller.session_id)
        self.tabs.removeTab(index)
        self._schedule_save()

    def _theme_changed(self) -> None:
        selected = self.theme_combo.currentData()
        if selected not in {"system", "light", "dark"}:
            return
        self._theme = selected
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self._theme)
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionTab):
                # apply_theme() 重新应用样式表会重置数据区字体，主题切换后必须补一次。
                widget.set_data_font_size(self._data_font_size)
                widget.refresh_theme()
        self._schedule_save()

    def _font_size_changed(self) -> None:
        size = self.font_combo.currentData()
        if not isinstance(size, int):
            return
        self._data_font_size = size
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionTab):
                widget.set_data_font_size(size)
        self._schedule_save()

    def _zoom_font(self, step: int) -> None:
        """按预设档位放大或缩小全局字号，到达边界后保持不变。"""
        presets = DATA_FONT_PRESETS
        size = self.font_combo.currentData()
        if isinstance(size, int) and size > 0:
            effective = size
        else:
            system_size = data_font().pointSize()
            effective = system_size if system_size > 0 else presets[0]
        if step > 0:
            candidates = [value for value in presets if value > effective]
            target = candidates[0] if candidates else presets[-1]
        else:
            candidates = [value for value in presets if value < effective]
            target = candidates[-1] if candidates else presets[0]
        index = self.font_combo.findData(target)
        if index >= 0:
            self.font_combo.setCurrentIndex(index)

    def _reset_font_size(self) -> None:
        """恢复为跟随系统的默认字号。"""
        index = self.font_combo.findData(0)
        if index >= 0:
            self.font_combo.setCurrentIndex(index)

    def _schedule_save(self) -> None:
        if self._config_store is not None:
            self._save_timer.start()

    def _save_config(self) -> None:
        if self._config_store is None:
            return
        active_tab = self.tabs.currentWidget()
        if isinstance(active_tab, SessionTab):
            self._sidebar_visible, self._sidebar_width, self._content_splitter_state = (
                active_tab.layout_state()
            )
        preferences: list[SessionPreferences] = []
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionTab):
                preferences.append(widget.to_preferences())
        config = AppConfig(
            schema_version=1,
            theme=self._theme,
            window_geometry=bytes(self.saveGeometry().toBase64()).decode("ascii"),
            window_state=bytes(self.saveState().toBase64()).decode("ascii"),
            active_session_index=max(self.tabs.currentIndex(), 0),
            sessions=tuple(preferences),
            sidebar_visible=self._sidebar_visible,
            sidebar_width=self._sidebar_width,
            content_splitter_state=self._content_splitter_state,
            data_font_size=self._data_font_size,
            log_level_rules=self._log_level_rules,
        )
        if config == self._last_saved:
            return
        try:
            self._config_store.save(config)
            self._last_saved = config
        except ConfigIOError as exc:
            self.statusBar().showMessage(exc.message, 5000)

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭窗口前确认活动会话并保存配置。"""
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionTab) and widget.controller.state in {
                SessionState.CONNECTED,
                SessionState.CONNECTING,
                SessionState.DISCONNECTING,
            }:
                answer = QMessageBox.question(
                    self,
                    "退出 SerialMNT",
                    "仍有活动串口会话，确定全部关闭并退出吗？",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                break
        self._is_closing = True
        self._update_timer.stop()
        self._save_timer.stop()
        self._save_config()
        self._session_manager.close_all(force=True)
        event.accept()
