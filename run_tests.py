#!/usr/bin/env python3
"""一键运行全部测试（标准库 unittest，零外部依赖）。

用法：
    python run_tests.py

返回码：0=全绿，1=有失败。CI / 推送前检查用。
"""

import sys
import unittest


def main() -> int:
    # 从 tests/ 目录发现所有 test_*.py
    suite = unittest.defaultTestLoader.discover(
        start_dir="tests",
        pattern="test_*.py",
        top_level_dir=".",
    )
    count = suite.countTestCases()
    print(f"发现 {count} 个测试用例\n")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\n" + "=" * 50)
    if result.wasSuccessful():
        print(f"全部通过：{result.testsRun} 个用例")
        return 0
    print(f"失败 {len(result.failures)} / 错误 {len(result.errors)} / 共 {result.testsRun}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
