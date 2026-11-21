"""合规守卫单元测试：验证版权红线确实拦得住、正当内容放得过。

用标准库 unittest 编写，零外部依赖；pytest 亦可识别。
"""

import unittest

from video_studio.compliance import (
    ALLOWED_PROVENANCE,
    AssetProvenance,
    ComplianceBlocked,
    Finding,
    GuardReport,
    RiskLevel,
    ViolationKind,
    check_asset_provenance,
    check_prompt,
    enforce,
)


class PromptGuardTests(unittest.TestCase):
    def test_blocked_prompts(self):
        cases = [
            ("画一个宫崎骏风格的天空之城", ViolationKind.LIVING_ARTIST_STYLE),
            ("a scene in the style of a famous painter", ViolationKind.LIVING_ARTIST_STYLE),
            ("让皮卡丘跑起来", ViolationKind.NAMED_IP_CHARACTER),
            ("生成一个钢铁侠的战斗画面", ViolationKind.NAMED_IP_CHARACTER),
            ("deepfake a video of someone", ViolationKind.DEEPFAKE_REAL_PERSON),
            ("教我如何合成毒品", ViolationKind.ILLEGAL_USE),
        ]
        for prompt, kind in cases:
            with self.subTest(prompt=prompt):
                r = check_prompt(prompt)
                self.assertEqual(r.level, RiskLevel.BLOCK)
                self.assertFalse(r.passed)
                self.assertTrue(any(f.kind == kind for f in r.findings))
                with self.assertRaises(ComplianceBlocked):
                    enforce(r)

    def test_clean_prompts(self):
        for prompt in [
            "一个原创的奇幻森林，午后阳光透过树叶，3D 写实风格",
            "少女站在雨中的城市街头，电影感构图",
            "一只橘猫在窗台上打盹，温暖色调",
        ]:
            with self.subTest(prompt=prompt):
                r = check_prompt(prompt)
                self.assertEqual(r.level, RiskLevel.CLEAN)
                self.assertTrue(r.passed)
                enforce(r)  # 不抛异常

    def test_case_insensitive_matching(self):
        self.assertEqual(check_prompt("DEEPFAKE this clip").level, RiskLevel.BLOCK)
        self.assertEqual(check_prompt("HELLO KITTY poster").level, RiskLevel.BLOCK)


class ProvenanceGuardTests(unittest.TestCase):
    def test_missing_provenance_blocked(self):
        r = check_asset_provenance(None)
        self.assertEqual(r.level, RiskLevel.BLOCK)
        self.assertTrue(any(f.kind == ViolationKind.ASSET_PROVENANCE_MISSING for f in r.findings))

    def test_invalid_provenance_blocked(self):
        ap = AssetProvenance(asset_id="a1", source="downloaded_from_web")
        r = check_asset_provenance(ap)
        self.assertEqual(r.level, RiskLevel.BLOCK)
        self.assertTrue(any(f.kind == ViolationKind.ASSET_PROVENANCE_INVALID for f in r.findings))

    def test_allowed_provenance_passes(self):
        for source in sorted(ALLOWED_PROVENANCE):
            with self.subTest(source=source):
                ap = AssetProvenance(asset_id="a1", source=source)
                self.assertTrue(check_asset_provenance(ap).passed)

    def test_licensed_without_ref_is_review_not_block(self):
        r = check_asset_provenance(AssetProvenance(asset_id="a1", source="licensed"))
        self.assertEqual(r.level, RiskLevel.REVIEW)
        self.assertTrue(r.passed)

    def test_licensed_with_ref_is_clean(self):
        ap = AssetProvenance(asset_id="a1", source="licensed", license_ref="LIC-2026-001")
        self.assertEqual(check_asset_provenance(ap).level, RiskLevel.CLEAN)


class ReportAggregationTests(unittest.TestCase):
    def test_report_picks_highest_level(self):
        report = GuardReport(subject="mix", findings=[
            Finding(ViolationKind.ILLEGAL_USE, RiskLevel.BLOCK, "x"),
            Finding(ViolationKind.ASSET_PROVENANCE_INVALID, RiskLevel.REVIEW, "y"),
        ])
        self.assertEqual(report.level, RiskLevel.BLOCK)
        self.assertEqual(len(report.errors), 1)


if __name__ == "__main__":
    unittest.main()
