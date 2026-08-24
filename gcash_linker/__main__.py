"""命令行环境检查与输入校验入口。"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
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
        missing = [
            name
            for name in ("curl_cffi", "qrcode", "PIL")
            if importlib.util.find_spec(name) is None
        ]
        if shutil.which("node") is None:
            missing.append("Node.js 20+")
        if missing:
            print(f"环境检查失败：缺少 {', '.join(missing)}", file=sys.stderr)
            return 1
        print("环境检查通过：GCash 协议依赖与 Node.js 已就绪。")
        return 0

    print("GCash Link Extractor")
    print("此命令只校验输入；完整提链请运行 start.bat 使用 Web 控制台。")
    try:
        request = parse_batch_request(
            _read_multiline("请输入 accessToken："),
            _read_multiline("请输入账单出口代理池："),
            _read_multiline("请输入促销出口代理池："),
        )
    except InputValidationError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    print("输入校验通过：")
    for key, value in request.redacted_summary().items():
        print(f"  {key}: {value}")
    print("输入校验完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
