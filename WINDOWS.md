# Windows 部署

双击项目根目录的 `start.bat` 即可启动。脚本不会要求 Git，也不会把 Token、代理或
本地数据写入仓库。

脚本会自动完成以下检查：

1. 检查 Python 3.11 或更高版本。
2. 没有 Python 时显示官方下载地址和 `winget install Python.Python.3.12` 命令。
3. 检查 Node.js 20 或更高版本；缺失时显示官网和 `winget` 安装命令。
4. 创建项目专用 `.venv`，检查并修复 `pip`。
5. 按 `requirements.txt` 安装运行依赖；安装失败时显示可复制的手动命令。
6. 启动本地 Web 控制台 `http://127.0.0.1:8765/` 并自动打开浏览器。

Web 控制台会执行 PH/PHP 自定义 Checkout、促销更新、零元校验、GCash 二维码生成、
扫码授权轮询和支付回跳。命令行入口 `python -m gcash_linker` 只用于输入校验，真实流程
统一从 Web 控制台启动。

`accessToken`、账单出口代理池、促销出口代理池、并发数和重试数会保存在当前浏览器的
本地存储中，刷新或关闭浏览器后重新打开仍会恢复。任务摘要和二维码保存在当前用户的
本机应用数据目录；任务文件不保存 Token 和代理。不要在公共电脑或共享浏览器中输入
真实 Token。

当代理供应商不允许国内公网 IP 直接连接时，先确保 Clash/VPN 在
`127.0.0.1:7897` 提供 SOCKS5 或 mixed-port。程序只检测这个固定端口：可用时组成
“本机代理 → HTTP 供应商代理 → 目标站”的代理链，不可用时直接连接供应商代理，且
不会读取其他系统代理端口。两种方式的最终出口都是输入框中的供应商代理。

命令行验证启动环境时可以执行：

```bat
start.bat --no-pause --check
```

如果系统没有 Python，请从 [python.org](https://www.python.org/downloads/windows/) 下载，
安装时勾选 `Add python.exe to PATH`、`pip` 和 `venv`。

如果系统没有 Node.js，请从 [nodejs.org](https://nodejs.org/en/download) 安装 LTS 版本。
