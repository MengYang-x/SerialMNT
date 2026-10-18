# SerialMNT 当前实现开发指南

> 文档版本：1.3  
> 软件版本：0.7.2  
> 对应基线提交：`cb6c23a`  
> 更新时间：2026-10-18  
> 适用读者：维护者、测试人员和参与扩展的开发者

## 1. 文档职责

本指南记录当前代码的实际结构、接口、数据流和运行约束，不代替产品规划文档：

- 产品范围与原始设计：`docs/implementation-plan.md`
- 当前用户操作：`docs/user-guide.md`
- 关键架构决策：`docs/adr/`
- 构建与发布：`docs/build-and-release.md`
- 当前行为最终真值：源码和自动化测试

实现、接口、线程模型、配置 schema 或日志格式发生变化时，必须在同一提交中更新本指南；用户可见行为变化还必须更新用户手册。

## 2. 总体架构

代码采用 `src` 布局，依赖方向如下：

```text
bootstrap -> ui -> application -> domain
     |           |            |
     +---------->+------------+-> infrastructure implements domain ports
```

- `domain` 只依赖 Python 标准库，定义不可变模型、状态、错误、编解码、缓冲和外部能力协议。
- `application` 编排会话、Worker、日志和状态，不依赖 PySide6。
- `infrastructure` 实现 `Transport`、`PortScanner`、`ConfigStore`、`LogSink` 和 `Clock` 协议。
- `ui` 依赖 PySide6 与 application 控制器，通过不可变快照和记录更新界面。
- `bootstrap` 组装真实对象并启动 Qt 应用。

更新检查沿用同一依赖方向：`domain` 负责版本比较和 `ReleaseInfo`，`application` 负责 `UpdateService`，`infrastructure` 负责 GitHub API 读取，`ui` 负责线程池调度和结果展示。

不能把串口 I/O、文件 I/O 或 Qt 类型放入 domain；也不能让 infrastructure 反向依赖 UI。

## 3. 关键公共模型与协议

### 3.1 `SerialConfig`

不可变串口配置，主要字段和默认值：

| 字段 | 默认值 | 约束 |
|---|---|---|
| `port` | 空字符串 | 未连接时可为空，连接前必须非空 |
| `baudrate` | `115200` | 正整数 |
| `bytesize` | `8` | `5`、`6`、`7`、`8`；界面下拉框仅提供 `8` |
| `parity` | `N` | `N`、`E`、`O`、`M`、`S` |
| `stopbits` | `1.0` | `1`、`1.5`、`2` |
| `flow_control` | `none` | `none`、`xonxoff`、`rtscts`、`dsrdtr` |
| `dtr` / `rts` | `True` | 布尔值 |
| `encoding` | `utf-8` | `utf-8`、`gb18030`、`ascii` |

`validate()` 检查不依赖连接状态的字段；`validate_for_connect()` 额外要求端口非空。

### 3.2 记录与快照

- `LogRecord` 保存 UTC 时间、方向、原始 `bytes`、文本编码、文本、HEX 和会话 ID。
- `LogRecord.from_bytes()` 是创建串口记录的标准入口，文本和 HEX 必须从原始字节派生。
- `SessionSnapshot` 保存会话 ID、标题、状态、配置、最后错误、RX/TX 字节数、累计淘汰记录数和可选的 `FileTransferSnapshot`。
- `FileTransferSnapshot` 保存 Ymodem 传输状态、文件名、文件大小、已确认字节、块序号、重试次数、耗时和说明；传输状态为 `waiting`、`sending`、`finishing`、`succeeded`、`failed` 或 `cancelled`。
- 回调跨线程传递时只使用不可变 `LogRecord` 和 `SessionSnapshot`。
- `SessionPreferences.view_mode` 保存每个标签的 `split`/`terminal` 视图模式，默认 `split`；旧配置缺少该字段时按 `split` 迁移。
- `SessionPreferences.line_ending` 保存发送换行策略，默认 `lf`；旧配置缺少该字段时按 `lf` 迁移，显式保存的 `none` 不会被改写。
- `SessionPreferences.highlight_enabled` 保存终端视图高亮开关，默认 `True`；旧配置缺少该字段时按开启迁移。
- `SessionPreferences.wrap_enabled` 保存接收区自动换行开关，默认 `True`；旧配置缺少该字段时按开启迁移。
- `SessionPreferences.show_line_number` 保存接收区行号开关，默认 `True`；旧配置缺少该字段时按开启迁移。
- `LogLevelRule` 定义日志级别的整词字段与颜色：`color` 为 `default`（跟随主题正文色）、语义键（`warning`/`error`/`pass`/`debug`）或 `#RRGGBB` 固定颜色。`DEFAULT_LOG_LEVEL_RULES` 按 `Error > Warning > Pass > Debug > Info` 排列，列表顺序即匹配优先级；`match_log_level()` 不区分大小写，只排除 ASCII 单词字符边界，因此 `information`、`ERROR_CODE` 不会误命中。

### 3.3 终端模型

- `domain/terminal.py` 提供独立的行式滚动缓冲终端模型，不依赖 Qt 与 pyserial。
- `TerminalScreen.feed(text)` 增量解析文本与 ANSI 转义序列，跨记录分片由内部待处理缓冲区拼接；`lines()`/`cursor` 返回只读快照供渲染，`reset()` 清空画面与解析状态。
- `TerminalStyle` 只保存语义样式（`fg`/`bg` 的 0-255 索引与 `bold`），由 UI 映射到当前主题，换主题无需重放数据；0-15 映射主题语义色，16-255 使用 xterm 调色板（16-231 色立方、232-255 灰度）；`TerminalCell` 带 `trailing` 标记双宽字符的右半格。
- 语义约定：LF 换行并回到行首，CR 仅回行首，BS 左移不擦除，HT 跳到下一个 8 列制表位；SGR 支持 0/1/22/30-37/39/40-47/49/90-97/100-107 与 `38;5;n`/`48;5;n` 的 256 色；`38;2;r;g;b`/`48;2;r;g;b` 真彩、斜体等未支持参数整组跳过，缺失后续参数时停止解析且不改动已有属性；CSI 支持 A/B/C/D/H/f/G 与 J/K，越界钳制。
- 未知、私有与带中间字节的序列整体吞掉；真彩、alternate screen、滚动区域和 OSC 不在范围内。
- 行数上限默认 5000，单行超过 500 列自动折行，超出上限裁剪最旧行。

### 3.4 外部能力协议

`domain/ports.py` 定义：

- `Transport`：打开/关闭、读取、完整写入、DTR/RTS 和打开状态。
- `PortScanner`：返回不可变的 `PortInfo` 元组（`device` 设备名 + `description` 友好描述；描述缺失或与设备名重复时为空串）。
- `Clock`：返回带时区 UTC 时间和单调时钟。
- `ConfigStore`：加载和保存应用配置。
- `LogSink`：打开、写入、刷新和关闭日志。
- `ReleaseSource`：返回最新正式版本 `ReleaseInfo`，允许阻塞，失败时抛出 `UpdateCheckError`。

测试使用 Fake 实现替换这些协议，不依赖真实硬件。

### 3.5 Ymodem 文件发送模型

- `application/ymodem.py` 提供 `YmodemSender`，只依赖 `domain.Transport`、领域类型和注入的单调时钟，不依赖 Qt、pyserial 或 UI。
- 文件头固定为 SOH/128 字节，内容为 `basename NUL 十进制大小 NUL`，其余用 `0x00` 填充；数据块默认 STX/1024 字节，也可选 SOH/128 字节，最后一块用 `0x1A` 补齐。序号按 8 位回绕，CRC 使用 CRC-16/XMODEM（`binascii.crc_hqx`），高字节在前。
- 文件名只取 basename，并通过 NFKD 分解转为 ASCII：非 ASCII 字符被丢弃，空格和特殊字符替换为 `_`，保留扩展名；文件名无法放入 128 字节文件头时拒绝发送。
- 握手等待接收端 `C`；文件头后最多等待 0.5 s 的第二个 `C`，没有也继续发送数据，兼容不重发 `C` 的 bootloader。
- 每块等待 ACK；NAK 或超时重发同一块，默认最多 10 次。每个阶段均有有限超时，不存在无限等待。
- EOT 允许接收端先 NAK 再 ACK 或直接 ACK；EOT 确认后短等 `C` 并发送全零结束空块，结束空块未确认不回滚已全部确认的文件。
- 用户取消、Worker 停止、重试耗尽、接收端双 CAN、文件读取失败和传输 I/O 错误都会终止传输；取消或失败时尽力发送 8 个 CAN 字节。
- 传输期间协议字节由引擎消费，不进入接收缓冲、CSV/TXT 日志和 TX/RX 计数；只向界面发布不可变进度快照和开始/终态 `SYSTEM` 记录。

### 3.6 版本与更新检查模型

- `domain/updates.py` 提供 `parse_version()`、`normalize_version()`、`is_newer_version()` 和 `ReleaseInfo`；版本按三段整数比较，允许 Release tag 带一个 `v` 前缀。
- `UpdateService` 通过 `ReleaseSource.fetch_latest()` 获取最新正式版本，返回 `UpdateCheckResult`（`update_available`、`up_to_date` 或 `failed`）。可预期的 `UpdateCheckError` 转为失败结果，未预期异常由 UI 任务边界兜底。
- `GithubReleaseSource` 使用标准库 `urllib.request` 访问 `https://api.github.com/repos/MengYang-x/SerialMNT/releases/latest`，超时 5 秒，响应上限 256 KiB。只接受 HTTPS 和 HTTP 200；404、403、超时、网络错误和格式错误都转为 `UpdateCheckError`。
- 请求只带 `User-Agent: SerialMNT/<version>`、`Accept: application/vnd.github+json` 和 `X-GitHub-Api-Version: 2022-11-28`，不发送查询参数、Cookie、令牌、设备信息或唯一标识。
- 更新检查不修改 `AppConfig` schema，不下载、不执行、不修改任何程序文件。

## 4. 会话状态机

`SessionState` 的稳定值：

```text
disconnected -> connecting -> connected -> disconnecting -> disconnected
                      |                         |
                      +------> error <-----------+
```

`SessionStateMachine` 只允许以下显式转换：

- `connect()`：`disconnected/error -> connecting`
- `mark_connected()`：`connecting -> connected`
- `mark_connect_failed()`：`connecting -> error`
- `begin_disconnect()`：`connected -> disconnecting`
- `mark_disconnected()`：`disconnecting -> disconnected`
- `mark_cleanup_failed()`：`disconnecting -> error`
- `reset()`：`error -> disconnected`
- `close(force=False)`：`disconnected/error` 可关闭；活动状态必须显式 `force=True`

非法转换统一抛出 `InvalidStateTransitionError`，不得直接改写状态。

## 5. 线程、队列与并发边界

- Qt 主线程负责所有窗口和控件操作。
- 每个连接创建一个标准库 `threading.Thread`，名称为 `serialmnt-serial-worker`，不是 `QThread`。
- Worker 默认读取块大小为 `8192` 字节，读取超时 `50 ms`，关闭超时 `1 s`。
- 发送队列最大 `256` 个写入任务；队列满时抛出 `SendQueueFullError`，不阻塞 UI。
- Ymodem 启动命令与普通写入共用同一个 FIFO 队列，保证已排队的普通数据先发完；`start_file_send()` 在锁内把传输标记为排队，之后的普通 `send()` 和 `set_line_state()` 抛 `FileTransferError`。协议引擎在 Worker 线程内同步运行并独占 `Transport`，不新增线程，也不与普通读取循环抢字节。
- 传输的读等待使用不超过 50 ms 的切片轮询，用户取消或 Worker 停止在切片边界生效；写超时按波特率计算（`长度 × 11 / 波特率 × 2`，最少 2 s），避免低波特率下误判超时。
- DTR/RTS 使用无界的小型控制队列；队列元素是固定布尔控制命令，不存在用户数据堆积风险；文件传输期间拒绝新的线路控制命令。
- `SessionController` 和 `SessionManager` 使用 `RLock` 保护共享状态。
- Worker 通过 `WorkerEventHandler` 回调 controller；UI 由 `QtSessionBridge` 把回调转换为 Qt 信号，并以 queued connection 投递到主线程。
- 控件不得读取 Worker 内部状态，只能读取 controller 的不可变快照或缓冲副本。
- 更新检查由 `ui/update_check.py` 的 `UpdateCheckRunner` 在独立 `QThreadPool` 中执行一次同步请求，HTTP 请求超时 5 秒；同一时刻只允许一个任务，结果经 queued signal 返回主线程。

## 6. 关键数据流

### 6.1 连接

1. UI 从连接面板构造并验证 `SerialConfig`。
2. `SessionManager` 检查进程内是否已有其他活动会话占用同一端口。
3. `SessionController` 把状态切换为 `connecting`，创建并启动 `SerialWorker`。
4. Worker 创建 `SerialTransport`、调用 `open()`、应用 DTR/RTS。
5. 成功时 controller 进入 `connected`，关闭旧日志（若存在），并按偏好创建本次自动日志。
6. 失败时进入 `error`，发布用户可读错误快照，界面允许修改参数后重试。

### 6.2 接收

1. Worker 循环读取最多 `8192` 字节，单次超时 `50 ms`。
2. `on_bytes_received()` 创建方向为 `rx` 的 `LogRecord`。
3. controller 同时写入有界内存缓冲和活动日志。
4. 记录通过回调发布，`QtSessionBridge` 将事件投递到 UI。
5. 接收面板根据暂停状态决定是否追加显示，并按文本/HEX 模式渲染。

### 6.3 发送

1. UI 将文本编码或解析 HEX，再追加所选 CR/LF；构造出的 payload 为空时不调用 controller，也不弹空数据错误。
2. controller 要求状态为 `connected`，把非空 `bytes` 放入 Worker 的固定大小写队列。
3. Worker 使用 `write_all()` 在写入超时内发送完整任务。
4. 成功后创建方向为 `tx` 的 `LogRecord`，进入和接收相同的缓冲、日志和 UI 数据流。

### 6.4 Ymodem 文件发送

1. UI 调用 `controller.start_file_send(path, block_size)`；controller 校验连接状态、8N1/无 XON-XOFF、文件存在且当前没有活动传输，构造 `waiting` 快照并追加一条开始记录。
2. controller 调用 Worker 的 `start_file_send()`；启动命令进入写队列，Worker 的传输阶段变为排队，后续普通发送被拒绝。
3. Worker 弹出启动命令后打开文件，调用 `YmodemSender.send()`；引擎自行读取 ACK/NAK/C/CAN，不与常规接收循环共享字节。
4. 引擎每收到一个数据块 ACK 就通过 `WorkerEventHandler.on_file_transfer()` 发布进度；进度按 `progress_interval` 节流，终态由 Worker 在释放传输锁后发布。
5. controller 在锁内更新 `_transfer`，终态追加一条 `SYSTEM` 记录，然后走既有快照回调链路；UI 的 `FileTransferPanel` 根据 `SessionSnapshot.transfer` 刷新进度、阶段和按钮。
6. 用户取消或断开连接只设置取消事件；Worker 在下一个读切片或帧边界停止，尽力发送 CAN 序列并发布 `cancelled` 终态。
7. 传输结束后 Worker 恢复正常读取循环；迟到的协议字节按普通 RX 处理，不按字节值过滤，避免误删设备随后输出的正常文本。

### 6.5 断开和退出

1. controller 状态切换到 `disconnecting`，先请求取消文件传输，再设置停止事件并等待最多 `1.5 s`；若传输仍在进行，等待上限放宽为 `2 × 帧写超时 + 0.5 s`（帧写超时为 `max(2 s, 1029 × 11 / 波特率 × 2)`），总上限 `5 s`，以覆盖无法中途打断的当前帧写入和随后尽力发送的取消序列。
2. Worker 退出循环，关闭 `Transport`，最后触发 `on_stopped()`。
3. controller 刷新并关闭活动日志，然后把状态改回 `disconnected`。
4. 超时则进入 `error` 并记录“等待串口线程退出超时”。
5. 关闭标签或退出程序时使用 `force=True`；退出前 Qt 主线程会等待 Worker 清理。

### 6.6 更新检查

1. `bootstrap.main()` 创建 `UpdateService(GithubReleaseSource(), current_version=__version__)`，`build_main_window()` 通过 `configure_update_service()` 注入；测试不传服务，不访问网络。
2. `MainWindow` 显示后约 3 秒触发一次静默检查；☰ 菜单“检查更新”触发手动检查。手动检查会接管正在进行的启动检查并优先反馈结果。
3. `UpdateCheckRunner` 在线程池中调用 `UpdateService.check()`，结果通过 queued signal 回到主线程。
4. 有新版本时弹窗显示当前/最新版本和纯文本更新说明；“前往下载”只打开 HTTPS 的 `github.com` 页面，不下载或执行任何文件。
5. 启动检查无更新或失败时静默；手动检查在状态栏显示“已是最新”或“检查失败”，消息 5 秒后消失。

## 7. 缓冲、配置与日志实现

### 7.1 内存缓冲

`RecordBuffer` 默认同时限制 `100,000` 条记录和 `32 MiB` 原始字节。超过任一限制时从最旧记录开始淘汰；单条记录超过总字节上限时抛出 `BufferCapacityError`。`clear()` 只删除当前记录，不重置累计淘汰数。`ReceivePanel` 只渲染最近 `2000` 条并把隐藏条数写成提示行，`QPlainTextEdit` 文档块上限固定为 `2002`，避免整表重插阻塞主线程。

### 7.2 配置持久化

`JsonConfigStore` 实现 schema version `1`：

- Windows：`%APPDATA%\SerialMNT\settings.json`
- Linux：`${XDG_CONFIG_HOME:-$HOME/.config}/SerialMNT/settings.json`
- 保存前先把现有文件复制为 `.bak`，再写入 `.tmp`，执行 `fsync()` 后用原子替换提交。
- atomically 替换失败时抛出 `ConfigIOError`。
- 读取到不可解析或不受支持的内容时，复制为 `settings.corrupt.<UTC时间>.json` 并返回 `None`。
- 单个无效会话会被跳过，其他有效会话继续加载。
- `AppConfig.data_font_size` 保存全局数据字号（默认 `0` 表示跟随系统），有效值为 `0` 或 `8..28`；旧配置缺少该字段时按 `0` 迁移，非法值会被拒绝。
- `AppConfig.log_level_rules` 保存全局日志级别规则，默认使用内置五条；旧配置缺少该字段时按默认规则迁移，单条非法规则会被跳过，显式空数组表示关闭级别着色。
- `MainWindow` 使用 `500 ms` 单次定时器合并保存请求，保存前用最近一次成功的 `AppConfig` 与当前快照比较，完全相同时跳过写盘，并在退出时同步保存。标签标题取自 `SessionController.title`，可通过 `rename()` 修改（去空白后非空、最长 `MAX_SESSION_TITLE_LENGTH = 40`）；改名后广播快照并随 `SessionPreferences.title` 持久化。`view_mode` 也随标签保存；终端视图会暂存分栏比例，切回分栏时恢复。

### 7.3 自动日志

`LogService` 管理单个会话的连接期日志：

- 默认目录是 `~/Documents/SerialMNT/Logs`，可由用户覆盖。
- 连接成功时创建文件，断开时刷新并关闭。
- 文件名为 `SerialMNT_<清理后的端口>_YYYYMMDD_HHMMSS.<csv|txt>`；同名时追加数字后缀。
- 每累计 `256 KiB` 或经过 `1 s` 时刷新一次，具体取决于先达到的条件。
- 日志错误保存为会话错误，但不会中断串口 I/O。

CSV 输出使用 `utf-8-sig` 和 RFC 4180 字段：`timestamp`、`direction`、`encoding`、`text`、`hex`。TXT 使用 UTF-8，格式为 `[本地时间] 方向 [编码] 文本`。

## 8. UI 层职责

- `MainWindow`：单行头部，不创建 `QMenuBar` 与 `QToolBar`；标签栏右上角角落控件依次承载“主题”下拉框、“字号”下拉框（两者高 `CONTROL_HEIGHT_PX`）和“☰”菜单（新建会话/关闭当前会话/字号/日志级别颜色/使用帮助/退出），菜单动作额外用 `addAction()` 挂到窗口以保留 `Ctrl+T/W/Q`；“+”新建按钮由 `_SessionTabBar` 承载——它是 `QTabBar` 子类，在 `__init__` 里经 `setTabBar()` 装入（必须先换标签栏再设置 `setTabsClosable()`/`setMovable()`，这两个属性不会随 `setTabBar()` 带到新标签栏），把按钮作为标签栏子控件贴在最后一个标签右边缘之后，并在 `tabLayoutChange()`（标签增删、改名）与 `resizeEvent()` 后重新定位，拖动标签只改变顺序、不改变各标签宽度之和，因此无需额外挂钩，按钮放不下时（标签溢出滚动、窗口过窄）隐藏，新建会话改走 ☰ 菜单或 `Ctrl+T`；标签栏通过事件过滤器处理双击重命名与右键菜单（重命名…/关闭当前会话/新建会话），菜单动作作用于被点击的标签，右键坐标取 `QContextMenuEvent.pos()/globalPos()`（该类没有 `position()`/`globalPosition()`），重命名对话框用 `dialog.findChild(QLineEdit)` 设置长度上限（`QInputDialog.lineEdit()` 在 PySide6 未暴露），弹菜单与弹对话框分别抽成 `_show_tab_menu()`/`_ask_session_title()` 便于测试替换；负责标签、窗口状态与配置保存（内容未变化时跳过写盘），窗口最小尺寸 900×600，启动时把普通窗口居中到主屏可用区域（恢复的几何只保留尺寸，最大化/全屏状态不覆盖；窗口大于可用区域时贴住左上角），主题变化后对每个 `SessionTab` 重新应用数据区字号并调用 `refresh_theme()`。
- `PageFadeTransition`：`MainWindow` 监听 `QTabWidget.currentChanged`，窗口可见时把上一页的 `grab()` 快照放进与当前页同尺寸、屏蔽鼠标事件的覆盖层；覆盖层用自绘 `paintEvent` 按动画进度同时调整 `QPainter` 透明度和水平偏移，在 360 ms 内按 `InOutSine` 沿标签切换方向平移 48 px；位移前段保持完全不透明，只在最后 35% 的位移进度内淡出，避免长时间半透明叠加，露出下方实时刷新的新页面。切到右侧标签时旧页向右滑出，切到左侧标签时向左滑出。这里刻意不用 `QGraphicsOpacityEffect`：自绘方式没有整页离屏合成，首帧就能稳定显示快照，平移也不做缩放，文字保持清晰。没有上一页或窗口未显示时直接硬切换；同一时间只保留一条覆盖层，快速连续切换先停止并隐藏上一条；覆盖层随页面容器销毁时丢弃引用，避免访问已释放控件。
- 更新检查入口：☰ 菜单“检查更新”仅在 `update_service` 已配置时启用；`configure_update_service()` 创建 `UpdateCheckRunner` 并启动 3 秒单次定时器。新版本对话框使用纯文本格式显示版本号，更新说明放在详情区；“前往下载”经 `is_allowed_release_url()` 校验 HTTPS 与 GitHub 主机后交给 `QDesktopServices.openUrl()`。
- `HelpWindow`：由 ☰ 菜单“使用帮助”（`F1`）打开的非模态帮助窗口，左侧 `QListWidget` 目录、右侧只读 `QTextBrowser` 正文，中间用 `QSplitter` 分隔；正文由 `setMarkdown()` 渲染随包分发的 `ui/assets/user-guide.md`（与 `docs/user-guide.md` 逐字节一致，由单元测试守卫），目录取 `headingLevel() == 2` 的标题块并用 `block.position()` 记录位置，选中目录项时把 `QTextCursor` 移到该位置后 `ensureCursorVisible()`；资源缺失时只显示一行降级提示而不抛错；`MainWindow._show_help()` 懒创建并复用单实例。
- `SessionTab`：用水平 `QSplitter` 组合左侧设置面板和右侧收发区；设置面板是 `QScrollArea`，内容控件挂在 `sidebar_content` 上并顶部对齐（末尾 `addStretch(1)`，多余高度留在滚动区背景而不是分组框内部），接收标题行最左侧是侧栏折叠按钮（`QPushButton`，与暂停/清屏共用描边样式），首行右侧依次是“发送设置”（`QToolButton` 菜单按钮）和“终端/分栏”（`QPushButton`）；终端视图隐藏 `SendPanel`、收起垂直分隔条，接收区末尾使用原始流内联输入；切回分栏恢复原比例，视图模式随标签持久化，底部状态条由 `state_indicator` 色点和 `status_label` 组成；快照标题变化时发出 `title_changed(str)`，由窗口据此刷新标签文本与 tooltip。
- 分组框内边距：`ConnectionPanel`、`FileTransferPanel` 和 `LogPanel` 的布局外边距均设为 0，内边距统一由 QSS `QGroupBox` 的 `padding` 提供；否则 Qt 默认布局外边距会与 QSS 内边距叠加 9 px，三个分组的左边缘也会错位。
- 下拉框滚轮：所有下拉框统一使用 `ui/no_wheel_combo.py` 的 `NoWheelComboBox`，`wheelEvent()` 直接忽略滚轮事件，避免滚动侧栏或页面时误改端口、波特率、主题、字号等选项；被忽略的事件继续向父控件传播，侧栏仍可正常滚动，展开列表内的滚动不受影响。`tests/unit/ui/test_no_wheel_combo.py` 用 AST 守卫禁止 UI 模块直接实例化 `QComboBox`。
- `ConnectionPanel`：端口刷新、配置校验、连接/断开、DTR/RTS 和文本编码选择；`encoding_combo` 提供 `UTF-8`/`GB18030`/`ASCII` 三选一，`build_config()` 读取其 `userData`，并与其余连接参数一同在连接中禁用。表单标签右对齐，端口集合不变时不重建下拉框以保留输入光标；`set_refresh_active()` 按标签可见性启停轮询，自动枚举周期为 30 s，端口行右侧 `refresh_button` 可手动立即枚举（连接中与端口控件一同禁用）；该按钮固定为 26×26，并把 `iconSize` 显式设为 22×22，因为 `QToolButton` 默认按样式表取 16 px 图标，在高分屏上过小不易辨认。下拉项显示「设备名 · 描述」并用 `userData` 存设备名，端口框只显示设备名；`current_port()` 解析真正的端口值，`describe_port()` 供状态栏显示带描述的文本，弹窗宽度按最长项自适应（上限 480 px）。
- `ReceivePanel`：接收区两行标题（首行为格式与操作，次行为时间戳、行号、自动滚动、自动换行、高亮）和内存缓冲显示；输出控件为 `TerminalOutput`，空文档时用 `setPlaceholderText("暂无数据")` 显示空态，终端视图下清空该占位文字以免与块状光标重叠。标题行控件在创建时就带上面板作为父对象：标题行布局挂载前还没有宿主控件，控件若此时没有父对象，`setVisible(True)` 会把它当成顶层窗口弹出，启动和新建会话时会闪出小窗。标题行固定为两行：首行是格式下拉框与暂停/清屏等操作，次行集中时间戳、行号、自动滚动、自动换行和高亮等显示选项；每行由 `QHBoxLayout` 加末尾 `stretch` 排列，所有控件始终可见，不再按宽度折叠或收进菜单。终端模式把 RX 文本喂给 `TerminalScreen` 后按网格渲染，不应用时间戳并隐藏时间戳和行号控件；首次进入终端视图时用最近 2000 条 RX 文本重建模型，切换回分栏时丢弃，暂停时继续喂模型但冻结渲染，清屏同时重置模型；草稿不写入记录缓冲或导出。取色走 `theme_manager.data_colors()` 与 `log_level_color()`，分栏视图按方向分层着色：时间戳用 `system` 灰、`RX`/`TX` 前缀用 `rx`/`tx` 方向色加粗、RX 正文按 `splitlines(keepends=True)` 逐行命中日志级别规则并取规则颜色、其余正文用 `text` 默认色（TX 与 HEX 模式按整条记录取色，TX 不受级别规则影响），`SYSTEM` 记录整行灰色；重绘前先清空文档，避免设置变更或主题切换重复追加记录；可通过 `add_leading_header_widget()`/`add_trailing_header_widget()` 在标题行首行两侧插入外部控件，并通过 `refresh_theme()` 响应主题切换。接收区查找由 `ui/find_bar.py` 的 `FindBar` 提供：`ReceivePanel` 用 `QShortcut`（`WidgetWithChildrenShortcut`）绑定 `Ctrl+F`，在 `TerminalOutput` 当前渲染的文档中做不区分大小写的纯文本匹配，命中项用文本选区标出并循环跳转，右侧显示“当前/总数”或“无匹配”。重建接收内容前调用 `suspend()` 清除选区，重建后再 `refresh()` 重新定位，避免终端视图因存在选区而延迟重建；关闭查找栏时清除选区、恢复跟随末尾并把焦点还给接收区（终端视图还给内联输入）。查找不访问会话缓冲，受 `_MAX_RENDERED_RECORDS` 渲染上限约束。
- 日志级别颜色：`ui/log_level_settings.py` 的 `LogLevelRulesDialog` 用两列表格编辑“字段—颜色”，支持添加、删除、设为默认颜色和恢复默认规则，双击颜色单元格打开 `QColorDialog`；确认时拒绝空字段和重复字段。`MainWindow.log_level_action`（☰ 菜单“日志级别颜色…”）是唯一入口，确认后经 `_apply_log_level_rules()` 遍历全部 `SessionTab` 调用 `set_log_level_rules()`，接收区随即重绘并随配置保存；新建会话也沿用当前全局规则。
- `SendPanel`：分栏视图使用两行标题（格式/换行/发送、历史/间隔/周期）、文本/HEX 编码和周期发送；编辑器使用等宽字体，标题行下拉框与按钮统一为 `CONTROL_HEIGHT_PX`。发送编辑器高度跟随内容（最多 `_EDITOR_MAX_LINES` 行，再多转为内部滚动），面板垂直方向取 `Maximum` 策略把多余高度让给接收区；高度按文档布局的块矩形累加得出，且必须等事件循环下一轮再量——`documentSizeChanged` 触发时块的行布局还没算完，量到的是空值。终端视图不再使用独立发送控件，`send_content()` 负责发送内联终端提交的单行文本；构造出的 payload 为空时直接视为成功无操作，不调用 controller。
- `TerminalOutput`：按 `TerminalScreen` 快照重建网格，把连续同样式单元格合并成运行段写入文档，块状光标用 `paintEvent` 叠加绘制；内联草稿渲染在模型光标处，支持回车提交、上下键历史、多行粘贴逐行发送。终端采用仅设备回显，提交成功后移除本地草稿，TX 记录不在终端视图渲染；画面只渲染最近 2000 行，更早的滚动历史由模型保留；分栏视图用 `append_segments()` 按 (文本, 颜色, 加粗) 片段追加一行，`append_text()` 保留为单色快捷方式。日志级别规则通过 `set_log_level_rules()` 注入，`_log_level_override()` 按整行文本匹配后覆盖整行前景（含 ANSI 显式前景色），级别颜色优先于地址/关键字高亮。
- 终端模式高亮：每行渲染前调用 `terminal_highlight.find_highlights()` 识别 IPv4/MAC（`address`）、URL/邮箱（`link`）与关键字 OK/SUCCESS/PASS（`success`）、ERROR/FAIL（`error`）、WARN（`warning`）；关键字大小写不敏感、整词匹配，命中区间按 URL→邮箱→MAC→IPv4→关键字先到先得、重叠跳过；只有前景色未被 ANSI 显式设置的单元格才用 `theme_manager.highlight_colors()` 的对应颜色覆盖，其它属性不变。接收标题行的“高亮”勾选框（仅终端视图可见、默认勾选）通过 `TerminalOutput.set_highlight_enabled()` 统一开关整层高亮，并随 `SessionPreferences.highlight_enabled` 持久化。
- `FileTransferPanel`：侧栏中的“Ymodem 发送”分组，位于“串口设置”和“日志与导出”之间，因此终端视图隐藏发送区时进度和取消仍然可见；包含只读文件路径、26×26 的“…”文件选择按钮、“1K 块”勾选框、进度条、百分比、阶段文本、可省略的指标文本和“发送文件/取消”按钮；“文件”标签与路径框、“…”按钮位于同一横向布局并从内容左边缘开始，文件行独占整行，不再与“1K 块”共用网格列而留下空白，按钮贴齐内容右边缘。`apply_snapshot()` 只根据 `SessionSnapshot.state` 和 `transfer` 更新控件；活动传输期间禁用文件选择和块大小，取消按一次后显示“正在取消…”并禁用；失败原因显示在阶段和指标文本中，同时由 controller 写入 `SYSTEM` 记录，不使用模态框。
- `SendPanel` 的 `set_transfer_active()` 在 Ymodem 传输期间停止周期发送并禁用发送/周期按钮，`send_content()` 直接返回 `False`，保留终端内联草稿且不产生错误框；`ConnectionPanel.set_transfer_active()` 禁用 DTR/RTS，避免传输中误复位设备；`SessionTab.request_close()` 在活动传输时使用包含“将中止传输”的确认文案。
- `LogPanel`：以“日志与导出”分组呈现自动日志设置、打开目录和导出当前缓冲；网格布局为“自动保存 + 格式”一行（格式下拉框与“自动保存”相邻，不再横跨到面板右端）、“目录”标签 + 路径输入框 + “…”选择按钮一行（整行从内容左边缘开始，标签紧贴输入框）、“打开目录 + 导出当前缓冲”一行（按 `3:4` 拉伸，导出更宽）、状态文本一行（文本为空时整行隐藏，不留空白），且不设行拉伸，因此面板贴合内容、最小宽度可放进侧栏最窄宽度；选择目录收进“…”按钮后省掉独立一行，该按钮为 `QToolButton` 并固定 `26×26`，与端口刷新按钮一致。长路径状态文本不参与最小宽度计算，并按标签宽度做中间省略，完整路径保留在 tooltip。
- `theme_manager`：`apply_theme()` 以 `style_sheet.tokens()` 为唯一取色来源，同时设置调色板（`Base` 取 `editor`、`Window` 取 `window`、`Highlight` 取 `accent_fill`，深色下额外指定禁用态文字）和全局 QSS，并按主题重设占位符文字颜色；`resolved_theme()`、`data_colors()`、`status_colors()`、`ansi_colors()` 和 `data_font()` 提供主题解析结果、数据区文字配色（`DataColors` 含正文 `text`、方向 `rx`/`tx` 与 `system`）、状态圆点配色（`StatusColors` 按已连接/错误/其余三态各给一个填充色）、ANSI 16 色和等宽字体；`ansi_color()` 把 0-255 索引映射为颜色（0-15 主题语义色，16-231 为 xterm 色立方，232-255 为灰度），`highlight_colors()` 提供终端模式高亮的地址、链接与成功/失败/警告关键字颜色，`log_level_color()` 把日志级别的 `default`/语义键解析为当前主题颜色并原样返回固定 `#RRGGBB`；`CONTROL_HEIGHT_PX = 24` 是标题行与顶栏控件的统一高度。
- `style_sheet`：`ThemeTokens` 保存浅色/深色两套设计令牌，`build_stylesheet()` 用令牌生成卡片、标签栏、下拉框、勾选框、滚动条、菜单等控件的 QSS。渲染数据区的 `editor` 必须与 palette `Base` 取同一令牌，否则对比度测试将不再反映真实渲染；QSS 刻意不声明 `font-family`/`font-size`，数据区等宽字体仍由 `data_font()` 在控件上设置。资源放在 `ui/assets/`，`assets_dir()` 兼容 PyInstaller 单文件解包目录，`app_icon()` 读取其中的 PNG 作为窗口图标；下拉箭头与勾选标记引用同目录的 SVG（`chevron-*.svg`、`check*.svg`），新增资源需同时登记到 `pyproject.toml` 的 `package-data` 与 `packaging/serialmnt.spec` 的 `datas`。
- `QtSessionBridge`：把 Worker 线程事件转换为 Qt 信号。
- 数据字号：`theme_manager.DATA_FONT_PRESETS` 与 `data_font(size)` 生成数据区字体（`size=0` 时保持系统默认），`ReceivePanel.set_data_font_size()`、`SendPanel.set_data_font_size()` 与 `SessionTab.set_data_font_size()` 逐层转发；头部“字号”下拉框默认“跟随系统”，改变时立即应用到所有标签页并持久化为 `AppConfig.data_font_size`。`MainWindow` 的“字号”动作（`Ctrl+=`/`Ctrl++` 放大、`Ctrl+-` 缩小、`Ctrl+0` 恢复默认，并挂在 ☰ 菜单的字号子菜单）经 `_zoom_font()` 按预设档位步进；`SessionTab` 在接收区与发送编辑区的 `viewport()` 上安装事件过滤器，把带 `Ctrl` 的滚轮事件转成 `font_zoom_requested(int)` 信号。全局样式表在控件挂载和主题切换时会重新 polish 并覆盖显式设置的字体，因此 `MainWindow` 在 `addTab()` 之后以及 `_theme_changed()` 里都会重新调用 `set_data_font_size()`，字号不会因换主题而回退。
- 自动换行：接收标题行的 `wrap_check`（默认开启）经 `ReceivePanel._settings_changed()` 调用 `TerminalOutput.set_wrap_enabled()`；开启时控件使用 `LineWrapMode.WidgetWidth` 与 `QTextOption.WrapMode.WrapAnywhere` 按宽度折行，关闭时恢复 `NoWrap`，分栏与终端视图共用同一控件，状态随 `SessionPreferences.wrap_enabled` 持久化。
- 接收行号：接收标题行的 `line_number_check`（默认开启，位于 `timestamp_check` 之后）经 `_record_segments()` 调用 `_numbered_segments()`，在分栏视图每个物理显示行前插入灰色 `{序号:>6} │ ` 前缀；一条记录正文含多个换行时逐行编号，关闭行号时不插入前缀也不消耗计数。序号只覆盖当前渲染窗口（最近 2000 条记录），`render_records()` 和清屏会把计数重置为 1；终端视图隐藏该控件且不插入行号。行号属于渲染文本，因此会随复制和查找命中，但不会写入内存缓冲、导出文件或自动日志；开关状态随 `SessionPreferences.show_line_number` 持久化。
- 接收区只读：分栏与终端视图都不允许修改接收内容，右键菜单固定只有“复制”“全选”（`TerminalOutput.contextMenuEvent()` 调用 `_build_context_menu()`，不提供剪切/删除/粘贴/撤销/重做）；终端模式下 `_handle_terminal_command()` 先处理 `Copy`，避免 `_constrain_cursor()` 清空草稿外选区导致复制失效，并把 `Paste` 先约束到输入草稿，防止粘贴写进日志正文。
- 接收区拖动选区：`TerminalOutput` 在左键按下期间暂停 `_scroll_after_update()` 的自动滚动与终端模式的画面重建，`mouseReleaseEvent()` 再经 `_on_selection_changed()` 补做，因为滚动到底用的 `moveCursor(End)` 会连带把选区锚点搬到文档末尾，这次滚动若落在按下瞬间，拖动就会从按下点一直选到文末；只用 `hasSelection()` 判断挡不住它，按下瞬间和按下清空旧选区时都还没有选区。判断由 `_mouse_held()` 给出：本控件按下的标记（`mousePressEvent()`/`mouseReleaseEvent()` 维护）与 `QApplication.mouseButtons()` 相与，因为终端视图从选区内部拖动会启动 Qt 内部拖拽，拖拽期间收不到释放事件，只看标记会让暂停自我锁死。
- 接收区跟随末尾：`_follow_tail` 记录新内容到达时是否滚动到底，`_scroll_after_update()` 只在它为真时执行滚动，因此用户滚离底部后点击接收区、清除选区或继续收到数据都不会跳回末尾。该状态只由用户滚动操作更新：`wheelEvent()`、非终端命令键的 `keyPressEvent()` 以及滚动条的 `actionTriggered`/`sliderMoved` 会按滚动条是否到达最大值同步；光标移动、选区和终端画面重建引起的程序化滚动不会改变它。终端视图每次整屏重建前记录滚动条位置，未跟随时在 `_place_input_cursor()` 把视图带到输入处后恢复原位置；切换视图与清屏会把状态重置为跟随。关闭查找栏时 `ReceivePanel._find_bar_closed()` 显式恢复跟随：分栏调用 `scroll_to_bottom()`，终端调用 `resume_follow()` 后聚焦内联输入。

界面组件只通过 controller 的公共方法执行动作；跨线程 UI 更新必须经过 queued signal 或 Qt 定时器，禁止直接从 Worker 修改控件。

## 9. 错误模型

所有领域错误继承 `DomainError`，携带稳定 `ErrorCode`、用户消息、可恢复标志和建议。`SessionSnapshot.last_error` 使用不可变 `UserFacingError`。主要分类包括：

- 输入与 HEX/文本编码错误。
- 非法状态转换、缓冲容量和发送队列满。
- 端口不存在、占用、权限不足和传输 I/O 错误。
- 日志与配置文件 I/O 错误。
- Ymodem 传输错误（`FILE_TRANSFER_ERROR`）：文件不可读、文件名过长、串口参数不兼容、重试耗尽、对端取消和传输忙。协议失败不写入 `SessionSnapshot.last_error`，只体现在 `FileTransferSnapshot` 和 `SYSTEM` 记录中，串口连接保持可用；真实 `TransportIOError` 仍走既有致命错误和断开路径。
- 更新检查错误（`UPDATE_CHECK_FAILED`）：网络不可达、超时、限流或响应格式非法；启动时静默，手动检查在状态栏提示检查失败。
- 未预期内部错误。

底层 `pyserial` 异常在 `SerialTransport` 边界映射为领域错误；UI 不显示未处理的 traceback。

## 10. 质量门禁与文档同步

标准本地命令：

```powershell
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe scripts\check_version_sync.py
.\.venv\Scripts\python.exe -m pytest
```

版本号与变更记录规则见 `AGENT.md` §2.4。`check_version_sync.py` 校验 `src/serialmnt/__init__.py` 的 `__version__`、三处文档的“软件版本”头、`user-guide.md` 第 10 章条目以及随包手册副本是否一致；CI 和构建脚本都会执行该检查。修改带 `> 文档版本：` 头的文档时还必须递增其文档版本，`check_docs_sync.py` 会强制校验。

启用本地 Hook（`pre-commit` 文档同步与 `commit-msg` 提交信息校验）：

```powershell
git config core.hooksPath .githooks
```

手动检查模式：

```powershell
.\.venv\Scripts\python.exe scripts\check_docs_sync.py --staged
.\.venv\Scripts\python.exe scripts\check_docs_sync.py --base origin/main
.\.venv\Scripts\python.exe scripts\check_docs_sync.py --working-tree
.\.venv\Scripts\python.exe scripts\check_commit_message.py
.\.venv\Scripts\python.exe scripts\check_commit_message.py --base origin/main
.\.venv\Scripts\python.exe scripts\check_commit_message.py .git/COMMIT_EDITMSG
```

规则位于 `docs/doc-sync-rules.json`。每次变更都必须在同一提交中更新或新增至少一个文档，纯测试变更可豁免但须在提交说明中注明理由，`AGENT.md` 变更必须同步 `implementation-plan.md`。具体映射：UI 变更必须至少同步用户手册或开发指南；domain/application 变更必须同步开发指南或 ADR；基础设施变更必须同步开发指南或用户手册；构建和项目元数据变更必须同步对应技术文档。未配置运行时路径会直接失败。

`scripts/check_commit_message.py` 校验提交信息是否使用 Conventional Commits 类型前缀并包含简体中文标题与正文，合并与回滚自动提交会被跳过。该检查是启发式门禁，只验证中文字符存在，语义质量仍由人工审查保证。

GitHub Actions 在 push 和 pull request 上执行同一文档同步与提交信息检查、Ruff 和离屏 pytest。CI 用于防止 `--no-verify` 等本地绕过；本地 Hook 用于尽早反馈，但机器检查只能证明相关文档发生了变更，不能代替人工核对语义准确性。

## 11. 扩展清单

新增或修改功能时依次执行：

1. 阅读 `implementation-plan.md`、相关 ADR、源码、测试和用户手册。
2. 明确接口、状态转换、线程边界、错误和资源上限。
3. 先增加失败测试，再实现最小改动。
4. 同步更新 `developer-guide.md`；存在用户可见变化时更新 `user-guide.md`。
5. 涉及公共接口、数据格式、线程模型或发布方式时新增或更新 ADR。
6. 运行 Ruff、相关单测、集成测试和文档同步检查。
7. 打包或真实硬件相关变化按发布清单完成对应验收。
