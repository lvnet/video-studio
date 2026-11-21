#!/usr/bin/env python3
"""一键启动 AI 短剧工坊工作台（零第三方依赖）。

用法：
    python start.py                 # 默认 127.0.0.1:8765
    python start.py --port 9000     # 指定端口
    python start.py --no-browser    # 不自动打开浏览器

启动后浏览器访问 http://127.0.0.1:8765 ，点右上角「载入示例」即可体验全流程。
按 Ctrl+C 停止。所有算力为桩，不产生真实费用。
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description="启动 AI 短剧工坊工作台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    from web.api_server import run

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        # 延迟开浏览器，等服务起来
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print("=" * 52)
    print("  AI 短剧工坊 · 工作台")
    print(f"  访问地址：{url}")
    print("  首次使用：点右上角「载入示例」")
    print("  停止服务：Ctrl+C")
    print("=" * 52)
    run(args.host, args.port)


if __name__ == "__main__":
    main()
