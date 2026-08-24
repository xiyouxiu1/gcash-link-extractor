# GCash Link Extractor

GCash提链工具,自动生成登录态二维码,,自动检测支付是否到账

## 项目定位

本项目只面向用户自有的 GCash 账号和自有的代理资源，目标是提供一个可审计的、
支持批量 `accessToken` 输入的 GCash 链接提取工具。

## 输入约定

- `accessToken`：每行一个，支持多行输入；空行会忽略，重复值会去重。
- `billing_exit_proxy_pool`：每行一个完整代理 URL，用于账单出口。
- `promotion_exit_proxy_pool`：每行一个完整代理 URL，用于促销出口。
- 两个代理池独立轮询，不能把代理凭据写进代码、日志、测试 fixture 或仓库。

代理支持带协议的 URL，例如 `http://host:port`、`https://host:port`、
`socks5://host:port` 或 `socks5h://host:port`。常见的 `host:port` 和
`host:port:user:password` 裸格式按 HTTP 代理解析；SOCKS 代理必须显式填写协议。

程序只检测固定地址 `socks5h://127.0.0.1:7897`：该端口提供 SOCKS5 或 mixed-port
时，自动组成“本机 Clash/VPN → HTTP 供应商代理 → 目标站”的代理链；未监听或不支持
SOCKS5 时直接连接供应商代理。本机代理只负责连到供应商入口，目标站看到的最终出口
仍是输入框中的供应商代理。

## 运行

Windows 双击 `start.bat`。脚本会检查 Python 3.11+、Node.js 20+，创建 `.venv`、
安装依赖并打开 `http://127.0.0.1:8765/`。

任务会在同一个 HTTP Session、同一个出口和同一个浏览器画像内依次执行自定义 Checkout、
加载 Checkout 页面上下文、PH 账单校验、零元确认、GCash 二维码生成、扫码授权轮询、
支付回跳及成功页同步。第二个促销出口输入仍为兼容字段，但源项目 GCash 链路不会在中途
切换代理，以免丢失 Cookie、apsessionId 和授权状态。浏览器会记住三组输入及并发/重试设置；
任务摘要和二维码保存在本机，持久化文件不包含 Token 和代理。


## 参考项目与致谢

本项目独立实现，借鉴以下公开项目在协议流程、任务编排、代理池和工程实践方面的
思路与经验：

- [paypal-agreement-protocol](https://github.com/1537271403/paypal-agreement-protocol)
- [pay153-checkout-link](https://github.com/1537271403/pay153-checkout-link)
- [gpt-auto-register](https://github.com/Regert888/gpt-auto-register)
- [upstream-ratio-watch](https://github.com/Regert888/upstream-ratio-watch)

当前版本没有复制上述仓库的源代码。第三方项目的名称、代码和其他受保护内容仍归
原作者所有；如果未来引入第三方代码，必须按对应文件的原许可证单独保留版权和许可
声明，不能把本项目许可证扩展到第三方代码。许可证审计时，`gpt-auto-register`
声明为 AGPL-3.0，其余三个仓库未发现根目录许可证文件，因此本项目仅引用公开链接和
思路，不把它们视为可直接复制的代码来源。

## 许可证

本项目新增代码采用 [0BSD](LICENSE)（Zero-Clause BSD）许可证：在适用法律允许的
范围内，可自由使用、复制、修改、合并、发布、再许可和销售，无需承担署名或开源义务。
第三方代码（如未来引入）不受本条款覆盖，仍以其自身许可证为准。
