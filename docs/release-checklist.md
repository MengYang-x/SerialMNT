# SerialMNT 发布检查清单

## 0.6.0 发布记录

- 发布对象：`v0.6.0`，合并发布 0.3.1、0.4.0、0.5.0 和 0.6.0 的变更。
- 已执行：`scripts/build.ps1` 的文档同步、版本同步、Ruff、612 项 pytest 和 PyInstaller 构建全部通过。
- 已执行：`SerialMNT.exe` 构建成功（48123376 字节），并完成本机离屏冷启动 10 秒冒烟。
- 已发布：`v0.6.0` tag 与 GitHub Release（非草稿、非预发布），Windows 单文件和 SHA-256 已上传，`/releases/latest` 返回 v0.6.0。
- 未执行：GitHub Actions 未产生运行记录（工作流状态为 active，但仓库无任何历史运行）；已按 CI 同等命令在本地完成文档同步、版本同步、提交信息和测试校验。
- 未执行：Linux 构建、桌面环境与干净测试机验收、真实串口和两小时稳定性测试。

## 代码和文档

- [x] 版本号与 `docs/release-notes-<version>.md` 一致，且 `scripts/check_version_sync.py` 通过。
- [x] `README.md`、`LICENSE`、`THIRD_PARTY_NOTICES.md` 完整。
- [x] `docs/user-guide.md`、`docs/developer-guide.md` 和 `docs/README.md` 完整。
- [x] `AGENT.md`、实施计划和架构决策记录已同步，提交信息与文档使用简体中文。
- [ ] 本地 Hook 已启用，文档同步和版本同步检查通过。
- [ ] 提交信息中文检查（本地 `commit-msg` 钩子或 CI）通过。
- [ ] GitHub Actions 文档同步、版本同步、Ruff 和 pytest 门禁通过。
- [x] 工作区干净，目标提交已推送。

## 自动化质量门禁

- [x] `ruff format --check .` 通过。
- [x] `ruff check .` 通过。
- [x] `pytest` 通过。
- [x] 更新检查相关单元和 UI 测试通过，测试使用 Fake 源且不访问真实网络。
- [x] domain 分支覆盖率不低于 90%。
- [x] application 分支覆盖率不低于 90%。
- [x] 标准 wheel 和 sdist 构建成功。

## Windows 验收

- [x] `scripts/build.bat` 在干净环境成功。
- [x] 生成 `SerialMNT.exe` 和 SHA-256。
- [ ] 单文件在允许执行本地产物的干净 Windows 测试机启动。
- [ ] 无缺失 Qt 插件。
- [ ] 窗口、任务栏与 `SerialMNT.exe` 图标显示正确。
- [ ] 配置文件可在用户 AppData 创建并恢复标签。
- [ ] 完成真实串口收发、拔插、错误和两小时稳定性测试。

## Linux 验收

- [ ] 在 Ubuntu 22.04 执行 `scripts/build.sh`。
- [ ] 单文件具有执行权限并可冷启动。
- [ ] Qt `xcb` 平台插件加载正常。
- [ ] 窗口图标正确，浅色与深色外观无破版且控件文字对比度可读。
- [ ] 完成串口权限、权限错误提示和两小时稳定性测试。

## 发布产物

- [ ] Windows 单文件和 SHA-256 已生成。
- [ ] Linux 单文件和 SHA-256 已生成。
- [x] 已创建 `vX.Y.Z` tag 和 GitHub Release（非草稿、非预发布），正文与用户手册版本记录一致。
- [ ] Windows/Linux 单文件和 SHA-256 已上传到对应 GitHub Release。
- [ ] `/releases/latest` 返回当前版本；旧版本启动能看到更新提示，当前版本启动无提示。
- [ ] 断网启动时更新检查静默失败且串口功能正常；手动检查显示失败提示。
- [x] 发布说明列出已知限制。
- [x] 发布说明注明 Windows/Linux 必须分别构建。
