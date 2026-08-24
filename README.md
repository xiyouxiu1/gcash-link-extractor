# GCash Link Extractor

GCash提链工具,自动生成登录态二维码,,自动检测支付是否到账

## 项目定位

本项目只面向用户自有的 GCash 账号和自有的代理资源，目标是提供一个可审计的、
支持批量 `accessToken` 输入的 GCash 链接提取工具。

## 输入约定

- `accessToken`：每行一个，支持多行输入；空行会忽略，重复值会去重。
- `checkout_proxy_pool`：每行一个完整代理 URL。
- `promotion_proxy_pool`：每行一个完整代理 URL。
- 两个代理池独立轮询，不能把代理凭据写进代码、日志、测试 fixture 或仓库。

代理只接受带协议的 URL，例如 `http://host:port`、`https://host:port`、
`socks5://host:port` 或 `socks5h://host:port`。不接受未定义含义的
`host:port:user:password` 裸格式，避免不同供应商的解释不一致。


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
