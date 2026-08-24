# Windows 部署

双击项目根目录的 `start.bat` 即可启动。脚本不会要求 Git，也不会把 Token、代理或
本地数据写入仓库。

脚本会自动完成以下检查：

1. 检查 Python 3.11 或更高版本。
2. 没有 Python 时显示官方下载地址和 `winget install Python.Python.3.12` 命令。
3. 创建项目专用 `.venv`，检查并修复 `pip`。
4. 按 `requirements.txt` 安装运行依赖；安装失败时显示可复制的手动命令。
5. 启动本地 Web 控制台 `http://127.0.0.1:8765/` 并自动打开浏览器。

当前阶段 Web 控制台只进行多行 `accessToken`、双代理池输入校验、JWT 邮箱识别和任务
摘要预览，不发起真实网络请求。命令行入口仍可用 `python -m gcash_linker`；未来新增
固定依赖时，只需将依赖写入 `requirements.txt`，启动脚本会自动安装。

命令行验证启动环境时可以执行：

```bat
start.bat --no-pause --check
```

如果系统没有 Python，请从 [python.org](https://www.python.org/downloads/windows/) 下载，
安装时勾选 `Add python.exe to PATH`、`pip` 和 `venv`。
