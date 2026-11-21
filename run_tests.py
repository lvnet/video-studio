#!/usr/bin/env python3
"""一键运行全部测试（标准库 unittest，零外部依赖）。

每个测试类在独立子进程中运行，彻底隔离 HTTP 集成测试共享的模块级全局状态
（如 web.api_server 的 STATE / DATA_DIR）。多个测试类在同一进程里各自起服务、
改全局、再还原，时机交错会产生偶发竞态；按类分进程后从根上消除。

用法：
    python run_tests.py

返回码：0=全绿，1=有失败。CI / 推送前检查用。
"""

import subprocess
import sys
import unittest


def _iter_tests(suite):
    """递归展开 TestSuite，产出每一个 TestCase。"""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def main() -> int:
    suite = unittest.defaultTestLoader.discover(
        start_dir="tests",
        pattern="test_*.py",
        top_level_dir=".",
    )

    # 按「模块.类」分组：同一类的用例仍在同一进程内顺序执行，
    # 不同的类彼此完全隔离（各自独立进程、独立模块状态）。
    groups: dict[str, int] = {}
    for case in _iter_tests(suite):
        key = f"{type(case).__module__}.{type(case).__name__}"
        groups[key] = groups.get(key, 0) + 1

    total = sum(groups.values())
    print(f"发现 {total} 个测试用例，分 {len(groups)} 个测试类隔离运行\n")

    failed: list[tuple[str, str]] = []
    for key, count in groups.items():
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "-v", key],
                capture_output=True,
                text=True,
                timeout=180,
            )
            ok = proc.returncode == 0
            detail = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            ok = False
            detail = f"测试类运行超时（>180s）：{key}"
        print(f"[{'PASS' if ok else 'FAIL'}] {key} ({count})")
        if not ok:
            failed.append((key, detail))

    print("\n" + "=" * 50)
    if not failed:
        print(f"全部通过：{total} 个用例 / {len(groups)} 个类（独立进程隔离）")
        return 0

    print(f"失败 {len(failed)} 个测试类 / 共 {total} 个用例")
    for key, detail in failed:
        print(f"\n----- {key} -----\n{detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
