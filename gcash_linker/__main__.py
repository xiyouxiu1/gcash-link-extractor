"""阶段一命令行入口：校验输入并显示脱敏摘要，不发起网络请求。"""
from __future__ import annotations

import argparse
import sys

from .inputs import InputValidationError, parse_batch_request


def _read_multiline(label: str) -> str:
    print(label)
    print("每行输入一个值，完成后按 Ctrl+Z 再回车。")
    lines: list[str] = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GCash Link Extractor")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查运行环境，不读取输入",
    )
    args = parser.parse_args(argv)

    if args.check:
        print("环境检查通过：输入层已就绪，当前不会发起网络请求。")
        return 0

    print("GCash Link Extractor")
    print("当前阶段只校验输入并显示脱敏摘要，不会发起网络请求。")
    try:
        request = parse_batch_request(
            _read_multiline("请输入 accessToken："),
            _read_multiline("请输入 Checkout 代理池："),
            _read_multiline("请输入第二代理池："),
        )
    except InputValidationError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    print("输入校验通过：")
    for key, value in request.redacted_summary().items():
        print(f"  {key}: {value}")
    print("当前版本尚未接入真实 GCash 协议执行器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
