# ADR-0003：启动更新检查与 GitHub Releases 数据源

- 状态：Accepted（已实现）
- 日期：2026-10-18
- 决策范围：版本检查（不包含自动下载或自动安装）
- 关联需求：FR-023

## 背景

原始 V1 范围排除了自动更新。用户随后要求参考专业软件，在每次启动时根据软件版本号检查是否有新版本。该需求只涉及“检查并提示”，不涉及下载、替换或执行二进制，因此可以在不引入遥测、不破坏现有安全边界的前提下实现。

当前事实：

- 版本号唯一来源是 `src/serialmnt/__init__.py` 的 `__version__`，格式为 `MAJOR.MINOR.PATCH`。
- 应用启动入口是 `bootstrap.app.main()`；`MainWindow` 在主线程构建，串口 I/O 已通过 Worker 线程隔离。
- 仓库 `MengYang-x/SerialMNT` 公开可访问；项目尚未发布任何 GitHub Release。
- `pyproject.toml` 中的 `Repository` 地址 `Queqiongtao/SerialMNT` 当前返回 404，与实际远程仓库不一致。
- 运行时不依赖任何 HTTP 库，只有 PySide6 和 pyserial。

## 候选方案

### 更新源

1. GitHub Releases API（选用）：`GET https://api.github.com/repos/MengYang-x/SerialMNT/releases/latest`。无需自建服务，发布 Release 即生效；缺点是仓库尚无 Release，且部分地区访问 GitHub API 可能不稳定。
2. 仓库内静态清单：维护 `update.json`，字段可控，但每次发布需手动同步，且仍需托管。
3. 自建更新服务器：可控性最好，但增加运维、证书和可用性责任。

### 发现新版本后的动作

1. 仅提示并打开下载页（选用）：应用不接触二进制，风险最小。
2. 应用内下载并校验：需要下载、进度、取消、校验和磁盘处理。
3. 自动下载并替换：需要退出替换、回滚、签名与完整性验证，与当前“不自动更新”约束冲突。

### 并发模型

1. Qt 主线程同步请求：实现最简单，但会阻塞界面，违反现有约束。
2. `QThreadPool` + 标准库 `urllib.request`（选用）：网络实现不依赖 Qt，领域和应用层可脱离 UI 测试；线程池有界，HTTP 请求 5 秒超时。
3. `QNetworkAccessManager`：Qt 原生异步，但会把具体网络实现绑定到 Qt，且应用层协议难以保持同步可测试的形态。
4. 新增独立常驻线程：为一个低频请求增加生命周期管理，收益不足。

## 决策

采用：GitHub Releases API + 仅提示并打开下载页 + 启动后台检查 + ☰ 菜单手动检查；不新增配置项。

### 版本规则

- 当前版本从 `serialmnt.__version__` 读取，必须匹配 `^\d+\.\d+\.\d+$`。
- Release tag 允许一个可选的 `v` 前缀；解析后必须得到三段十进制数字。
- 按整数元组比较，禁止字符串比较（`0.10.0` 必须大于 `0.9.9`）。
- 最新版本小于或等于当前版本时视为已是最新；非法 tag 视为检查失败。
- 不支持预发布版本、构建元数据或第四段版本号。

### 领域层

新增 `domain/updates.py`：

```python
@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    release_url: str
    notes: str = ""
    published_at: datetime | None = None


def parse_version(text: str) -> tuple[int, int, int]: ...


def is_newer_version(current: str, latest: str) -> bool: ...
```

- `ReleaseInfo.__post_init__` 校验版本格式，并要求 `release_url` 使用 HTTPS。
- `domain/ports.py` 增加协议：

```python
@runtime_checkable
class ReleaseSource(Protocol):
    def fetch_latest(self) -> ReleaseInfo: ...
```

- `domain/errors.py` 增加 `ErrorCode.UPDATE_CHECK_FAILED` 和 `UpdateCheckError(DomainError)`；错误信息面向用户，可恢复，建议检查网络后重试。

### 应用层

新增 `application/update_service.py`：

```python
class UpdateStatus(StrEnum):
    UPDATE_AVAILABLE = "update_available"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    status: UpdateStatus
    current_version: str
    release: ReleaseInfo | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None


class UpdateService:
    def __init__(self, source: ReleaseSource, *, current_version: str) -> None: ...

    def check(self) -> UpdateCheckResult: ...
```

- `UpdateService.check()` 只捕获 `UpdateCheckError` 并转为 `FAILED`；未预期异常不在此层吞掉，由 UI 任务边界捕获、记录并转为失败结果。
- 不修改 `AppConfig`、`SessionPreferences` 或配置 schema。

### 基础设施层

新增 `infrastructure/github_release_source.py`：

```python
class GithubReleaseSource:
    def __init__(
        self,
        repository: str = "MengYang-x/SerialMNT",
        *,
        timeout: float = 5.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None: ...

    def fetch_latest(self) -> ReleaseInfo: ...
```

请求约束：

- `GET https://api.github.com/repos/{repository}/releases/latest`。
- 请求头：`User-Agent: SerialMNT/<version>`、`Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-11-28`。
- 超时 5 秒；不发送查询参数、Cookie、令牌、设备信息或唯一标识。
- 只接受 HTTPS 响应；最终 URL 不是 HTTPS 时按失败处理。
- 最多读取 256 KiB；超过上限按失败处理，避免无界内存。
- 只接受 HTTP 200；404（尚无 Release）、403（限流）、超时、TLS 错误、网络错误、JSON 或字段非法都转为 `UpdateCheckError`。
- `/releases/latest` 本身已排除草稿和预发布，仍额外校验 `draft` 和 `prerelease` 字段。
- 输出 `ReleaseInfo(version, html_url, body, published_at)`；`html_url` 不是 HTTPS 时拒绝。
- 不自动重试、不跟随非 HTTPS 跳转；代理行为沿用 `urllib` 的系统默认设置。

### UI 层

新增 `ui/update_check.py`：

- `UpdateCheckRunner(QObject)` 持有 `QThreadPool`（最大 1 个线程）和 `finished(UpdateCheckResult)` 信号。
- 每次检查启动一个 `QRunnable`；运行中再次手动触发时直接返回，不排队。
- 任务顶层捕获所有异常：`UpdateCheckError` 转为失败结果，其他异常记录 `logging.exception` 并转为 `INTERNAL_ERROR` 失败结果。
- 结果通过 Qt queued signal 回到主线程；网络任务不触碰任何控件。

`MainWindow` 修改：

- 构造参数增加 `update_service: UpdateService | None = None`；测试传 `None`，不发起网络请求。
- 服务不为 `None` 时，构造完成后 `QTimer.singleShot(3000, ...)` 启动一次静默检查。
- ☰ 菜单在“使用帮助”之后增加“检查更新”动作；手动检查期间状态栏显示“正在检查更新…”，并禁用该动作。
- 结果处理：
  - 有新版本：弹 `QMessageBox`，标题“发现新版本”，正文包含当前/最新版本，`setDetailedText()` 显示纯文本更新说明，按钮“前往下载”和“稍后”。
  - 已是最新：启动时静默；手动检查时状态栏显示“当前已是最新版本 X”，5 秒后消失。
  - 失败：启动时静默；手动检查时状态栏显示“检查更新失败，请检查网络后重试”，5 秒后消失。
- “前往下载”再次校验 URL 为 HTTPS 且主机为 `github.com`，然后调用 `QDesktopServices.openUrl()`；打开失败时用可复制的消息框给出 URL。
- 更新说明一律按纯文本渲染（`Qt.TextFormat.PlainText`），禁止 HTML 和远程图片，防止 Release 内容被当作富文本执行或追踪。
- 每次启动最多一次自动检查；手动检查可再次触发，但同一时刻只允许一个任务。

### Bootstrap

- `main()` 创建 `UpdateService(GithubReleaseSource(), current_version=__version__)` 并传给 `build_main_window(update_service=...)`。
- `build_main_window()` 的 `update_service` 默认 `None`，保证现有测试和纯 UI 构造不访问网络。

## 后果

优点：

- 不新增第三方依赖（`urllib`、`ssl` 属于标准库）。
- 网络 I/O 不在主线程，启动不被阻塞；失败不影响串口功能。
- 不下载、不执行、不修改程序文件，安全边界清晰。
- 不修改配置 schema，不引入遥测或唯一标识。
- 领域、应用和基础设施层可脱离 Qt 测试。

约束与风险：

- 仓库必须开始发布 GitHub Release；没有 Release 时所有检查都会静默失败。
- `api.github.com` 在某些网络环境下不可达或被限流；启动检查和手动检查都可能失败，按设计只做非阻塞提示。
- GitHub 匿名 API 限流为每小时 60 次/IP；每次启动一次请求，正常使用不会触发。
- 不支持代理设置、预发布通道、增量更新、跳过版本或关闭自动检查；这些能力需要后续需求确认。
- 单文件程序退出时，通常会等待正在进行的 HTTP 请求结束（请求超时 5 秒）；该等待有上限，不会无限阻塞退出。
- `pyproject.toml` 中的仓库地址需要修正为 `MengYang-x/SerialMNT`，否则文档与实际发布源不一致。

## 验证

单元测试：

- 版本解析：合法三段、`v` 前缀、空串、两段、四段、非数字、预发布后缀、前后空白。
- 比较：相等、升/降、`0.10.0` 与 `0.9.9`、大版本差异。
- `ReleaseInfo`：非法版本、非 HTTPS URL 被拒绝。
- `UpdateService`：新版本、已最新、旧版本、`UpdateCheckError`、未预期异常不被吞掉。
- `GithubReleaseSource`：注入 Fake opener，断言 URL、请求头、超时、200 解析、`v` 前缀、空 body、404/403/超时/坏 JSON/缺字段/超大响应/非 HTTPS URL。
- UI：启动时只触发一次、无更新静默、失败静默、手动检查状态栏文案、更新弹窗只出现一次、“前往下载”调用 `openUrl`、运行中不重复启动。
- 所有测试使用 Fake source/opener，不访问真实网络。

实现后手动验证：

- 发布一个测试 Release（例如 `v0.3.0`），确认 `/releases/latest` 返回正确 tag。
- 用旧版本启动，确认延迟约 3 秒后弹窗且界面不卡顿。
- 用当前版本启动，确认无弹窗；点“检查更新”看到“已是最新”。
- 断网启动，确认无弹窗、串口功能正常；手动检查看到失败提示。
- 检查 Release 说明中的 HTML/图片不被渲染，下载按钮只打开 `https://github.com/...`。
- 在 PyInstaller 单文件中冷启动验证 `urllib`/`ssl` 可用。

## 实施清单

代码：

- `src/serialmnt/domain/updates.py`（新增）
- `src/serialmnt/domain/errors.py`（`UPDATE_CHECK_FAILED`、`UpdateCheckError`）
- `src/serialmnt/domain/ports.py`（`ReleaseSource`）
- `src/serialmnt/domain/__init__.py`（导出）
- `src/serialmnt/application/update_service.py`（新增）
- `src/serialmnt/application/__init__.py`（导出）
- `src/serialmnt/infrastructure/github_release_source.py`（新增）
- `src/serialmnt/infrastructure/__init__.py`（导出）
- `src/serialmnt/ui/update_check.py`（新增）
- `src/serialmnt/ui/main_window.py`（菜单、启动调度、状态栏、弹窗）
- `src/serialmnt/bootstrap/app.py`（组装）
- 测试：`tests/unit/domain/test_updates.py`、`tests/unit/application/test_update_service.py`、`tests/unit/infrastructure/test_github_release_source.py`、`tests/unit/ui/test_update_check.py`（新增）

版本与文档（同一实现提交）：

- `src/serialmnt/__init__.py`：`0.2.3 → 0.3.0`。
- `docs/README.md`、`docs/user-guide.md`、`docs/developer-guide.md` 版本头。
- `docs/user-guide.md` 第 10 章新增 V0.3.0 条目。
- `src/serialmnt/ui/assets/user-guide.md` 与 `docs/user-guide.md` 逐字节同步。
- `docs/user-guide.md`：新增“检查更新”使用说明，更新“已知限制”。
- `docs/developer-guide.md`：新增模块、协议、线程与错误说明。
- `docs/build-and-release.md`、`docs/release-checklist.md`：打 tag、创建 GitHub Release、产物与更新检查验证。
- `pyproject.toml`：修正 `Repository` 地址为 `https://github.com/MengYang-x/SerialMNT`，与更新源一致。
- 本 ADR 状态从 Proposed 更新为 Accepted（实现完成后）。

不在本次范围：

- 自动下载、SHA-256 校验、自动替换、回滚、增量更新、预发布通道、跳过版本、自动检查开关、代理设置和遥测。
