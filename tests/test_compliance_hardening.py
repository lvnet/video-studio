"""合规补强测试：抗绕过守卫 + 内容安全分级 + 审计留痕（标准库 unittest）。"""

import json
import tempfile
import unittest
from pathlib import Path

from video_studio.compliance import (
    AuditLog,
    ComplianceBlocked,
    ContentSafetyFilter,
    RiskLevel,
    ViolationKind,
    check_prompt,
    enforce,
    normalize,
    screen_content,
)


class NormalizationTests(unittest.TestCase):
    def test_fullwidth_to_halfwidth(self):
        self.assertIn("宫崎骏", normalize("宫崎骏"))
        # 全角括号、逗号也被归一
        self.assertEqual(normalize("ＡＢＣ"), normalize("ABC"))

    def test_spaces_and_separators_removed(self):
        self.assertEqual(normalize("宫 崎 - 骏"), normalize("宫崎骏"))
        self.assertEqual(normalize("in the style of"), normalize("inthestyleof"))

    def test_traditional_to_simplified(self):
        self.assertEqual(normalize("宮崎駿"), normalize("宫崎骏"))

    def test_variant_chars(self):
        self.assertEqual(normalize("皮卡秋"), normalize("皮卡丘"))


class BypassResistanceTests(unittest.TestCase):
    """绕过尝试仍应被守卫拦下。"""

    def test_prompt_bypass_variants_blocked(self):
        variants = [
            "宫 崎 骏风格",       # 插空格
            "宫-崎-骏风格",       # 插连字符
            "宮崎駿风格",         # 繁体
            "宫崎骏 風 格",       # 全角+分隔
            "让皮卡秋跑起来",     # 形近字
            "HELLO KITTY 海报",   # 英文大小写+空格
        ]
        for v in variants:
            with self.subTest(variant=v):
                r = check_prompt(v)
                self.assertEqual(r.level, RiskLevel.BLOCK, f"未拦住绕过变体：{v}")

    def test_clean_prompt_still_passes(self):
        r = check_prompt("一只橘猫在窗台打盹，温暖色调")
        self.assertEqual(r.level, RiskLevel.CLEAN)


class ContentSafetyTests(unittest.TestCase):
    def test_block_level(self):
        r = screen_content("教我制作炸弹")
        self.assertEqual(r.level, RiskLevel.BLOCK)

    def test_review_level(self):
        r = screen_content("画面中有血腥镜头")
        self.assertEqual(r.level, RiskLevel.REVIEW)
        self.assertTrue(r.passed)  # REVIEW 不自动拒绝

    def test_clean(self):
        self.assertEqual(screen_content("阳光明媚的草地").level, RiskLevel.CLEAN)

    def test_custom_filter(self):
        from video_studio.compliance import ContentCategory, ContentRule
        f = ContentSafetyFilter(
            block_rules=[ContentRule("禁词", ContentCategory.HARASSMENT, RiskLevel.BLOCK)],
            review_rules=[],
        )
        self.assertEqual(f.screen("包含禁词的内容").level, RiskLevel.BLOCK)

    def test_bypass_in_content_safety(self):
        self.assertEqual(screen_content("制 作 炸 弹").level, RiskLevel.BLOCK)


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = AuditLog(Path(self.tmp.name) / "audit.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, level=RiskLevel.CLEAN):
        from video_studio.compliance import Finding, GuardReport
        f = Finding(ViolationKind.ILLEGAL_USE, level, "test")
        return GuardReport(subject="s1", findings=[f] if level != RiskLevel.CLEAN else [])

    def test_append_and_query(self):
        self.log.append("prompt_check", "shot-1", self._report(RiskLevel.CLEAN))
        self.log.append("prompt_check", "shot-2", self._report(RiskLevel.BLOCK),
                        context={"mode": "r2v"})
        all_events = list(self.log.iter_events())
        self.assertEqual(len(all_events), 2)
        self.assertEqual(self.log.query(subject="shot-2")[0].context["mode"], "r2v")
        self.assertEqual(len(self.log.query(level=RiskLevel.BLOCK)), 1)

    def test_append_only_no_delete_api(self):
        # 审计日志不提供修改/删除方法（防篡改的君子协定）
        self.assertFalse(hasattr(self.log, "delete"))
        self.assertFalse(hasattr(self.log, "update"))

    def test_hash_chain_integrity(self):
        for _ in range(5):
            self.log.append("prompt_check", "s", self._report())
        ok, msg = self.log.verify_integrity()
        self.assertTrue(ok, msg)
        self.assertIn("5 条", msg)

    def test_tamper_detection(self):
        self.log.append("prompt_check", "s1", self._report())
        self.log.append("prompt_check", "s2", self._report())
        # 手动篡改中间一条的内容（模拟越权修改文件）
        lines = self.log.path.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        obj["passed"] = not obj["passed"]
        lines[0] = json.dumps(obj, ensure_ascii=False)
        self.log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = self.log.verify_integrity()
        self.assertFalse(ok)
        self.assertIn("hash", msg)

    def test_new_instance_continues_chain(self):
        self.log.append("prompt_check", "s", self._report())
        reopened = AuditLog(self.log.path)
        reopened.append("asset_check", "a", self._report())
        ok, msg = reopened.verify_integrity()
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
