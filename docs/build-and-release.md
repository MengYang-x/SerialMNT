# SerialMNT 构建与发布

## 适用环境

- Windows 10/11 x64。
- Ubuntu 22.04 x64 或兼容的 Linux x64 环境。
- Python 3.10.x。
- 已按 README 创建 `.venv` 并安装 `[dev,build]` 依赖。

Windows 和 Linux 必须在对应操作系统上分别构建，不能交叉生成单文件程序。

## Windows 构建

双击 `scripts\build.bat`，或在终端运行：

```powershell
.\scripts\build.bat
```

`scripts\build.bat` 只是转调同目录的 `build.ps1`，两者等价；也可以直接调用 PowerShell 脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

脚本依次执行：

1. 文档同步检查。
2. 版本同步检查（版本号、文档版本头和用户手册版本记录）。
3. Ruff 格式检查。
4. Ruff 静态检查。
5. pytest 全部测试。
6. PyInstaller 单文件构建。
7. 生成 SHA-256 校验文件。

产物：

```text
dist/SerialMNT.exe
dist/SerialMNT.exe.sha256
```

## Linux 构建

在 Ubuntu 22.04 或兼容环境运行：

```bash
chmod +x scripts/build.sh
./scripts/build.sh
```

产物：

```text
dist/SerialMNT
dist/SerialMNT.sha256
```

## 文档同步与 CI

首次贡献前启用本地提交 Hook：

```powershell
git config core.hooksPath .githooks
```

提交时 Hook 检查暂存变更与受影响文档是否属于同一提交。构建脚本会检查当前工作区的文档同步和版本同步；GitHub Actions 会在 push 和 pull request 上重新检查提交范围、版本同步，并运行 Ruff 与 pytest。CI 不构建或发布单文件产物，Windows/Linux 构建仍必须在对应系统执行。

## 运行依赖

Windows 单文件目标机器无需安装 Python。Linux 单文件仍依赖常见图形和系统运行库；若启动时报 Qt 平台插件错误，请根据发行版安装 XCB 相关系统库。

## 单文件行为

- 首次启动会解压到系统临时目录，启动速度慢于普通源码运行。
- 临时目录不可执行或磁盘空间不足时可能无法启动。
- 程序不包含自动下载或自动替换式更新、代码签名和遥测功能；启动更新检查只查询 GitHub Releases 并提示新版本。
- 发布时必须同时提供对应的 SHA-256 校验值。

## GitHub Release 与更新检查

更新检查读取 `https://api.github.com/repos/MengYang-x/SerialMNT/releases/latest`。每次正式发布必须：

1. 确认 `src/serialmnt/__init__.py` 的 `__version__`、三处文档版本头和用户手册版本记录一致，并运行 `scripts/check_version_sync.py`。
2. 创建 `vMAJOR.MINOR.PATCH` 注释 tag。
3. 以该 tag 创建 GitHub Release，标题使用 `SerialMNT X.Y.Z`，正文填写用户可见变更；Release 不得保存为草稿或标记为预发布。
4. 上传 Windows/Linux 单文件和对应 SHA-256 文件。
5. 访问 `/releases/latest` 确认返回该版本；用旧版本启动确认出现更新提示，用当前版本启动并手动检查确认“已是最新”。

没有正式 Release 时，更新检查会静默跳过，不影响串口功能。

## 图标与样式资源

应用图标与控件样式资源位于 `src/serialmnt/ui/assets/`：

- `serialmnt.png` / `serialmnt.ico`：窗口图标；Windows 单文件构建会同时把 ICO 写入可执行文件图标。
- `chevron-*.svg`、`check*.svg`：下拉箭头与勾选标记（含禁用态），由全局 QSS 引用。

这些文件通过 `packaging/serialmnt.spec` 的 `datas` 打进单文件产物（解包路径为 `serialmnt/ui/assets`），并在 `pyproject.toml` 的 `package-data` 中声明，保证源码安装同样可用。修改图标图形后运行 `.\.venv\Scripts\python.exe scripts\generate_icon.py` 重新生成 PNG/ICO，并与脚本一起提交。

## 当前 Windows 构建记录

2026-10-18 已按 `scripts/build.ps1` 的相同步骤成功生成 SerialMNT 0.7.0：

- 文件：`SerialMNT.exe`
- 大小：48126558 字节
- SHA-256：`a2a64f2efe79a001988ee55507c6ddd210fcc0b6f3778415bf0241ac031d98e8`
- 已在本机以 `QT_QPA_PLATFORM=offscreen` 冷启动 10 秒，进程未退出；桌面窗口、任务栏图标、配置恢复和真实串口仍需在目标测试机验收。
