"""本地剧本生成引擎测试：产出必须结构化、可拆镜、确定性、合规、零外部依赖。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_studio.creation.story_engine import (
    _extract_elements,
    continue_script,
    generate_outlines,
    rewrite_text,
)
from video_studio.creation.script import import_script
from video_studio.compliance import check_prompt


class StoryEngineTest(unittest.TestCase):
    SYNOPSIS = "一个旅人十年后回到江边码头，寻找失散的故人林远。"

    def test_01_outlines_are_structured_and_splittable(self):
        outlines = generate_outlines(self.SYNOPSIS, 3)
        self.assertEqual(len(outlines), 3)
        for i, text in enumerate(outlines, 1):
            self.assertTrue(text.startswith(f"第{i}集"))
            self.assertIn("场景：", text)
            self.assertIn("：", text)  # 对白
            beats = import_script(text, "txt")
            self.assertGreaterEqual(len(beats), 2, f"第{i}集应能拆出至少2个镜头")
            self.assertTrue(all(b.is_complete for b in beats))

    def test_02_deterministic(self):
        self.assertEqual(generate_outlines(self.SYNOPSIS, 4), generate_outlines(self.SYNOPSIS, 4))

    def test_03_elements_extracted(self):
        el = _extract_elements("林远回到旧书店，遇见苏晚。")
        self.assertIn(el.hero, ("林远", "苏晚"))
        self.assertEqual(el.location, "旧书店")
        self.assertTrue(el.theme)
        # 地点命中梗概里的候选；无命中时也有默认值
        self.assertEqual(_extract_elements(self.SYNOPSIS).location, "江边码头")

    def test_04_all_original_and_compliant(self):
        # 生成内容必须通过合规守卫（零版权风险）
        for text in generate_outlines(self.SYNOPSIS, 5):
            self.assertTrue(check_prompt(text).passed)

    def test_05_rewrite_variants(self):
        base = generate_outlines(self.SYNOPSIS, 1)[0]
        for inst in ("精简", "扩写", "对白", "悬念", "口语", "幽默", "正式", "更幽默"):
            out = rewrite_text(base, inst)
            self.assertTrue(out.strip(), inst)
            self.assertNotEqual(out, "", inst)
        # 精简确实变短
        self.assertLessEqual(len(rewrite_text(base, "精简")), len(base))
        # 扩写确实变长
        self.assertGreater(len(rewrite_text(base, "扩写")), len(base))

    def test_06_continue_script(self):
        out = continue_script(self.SYNOPSIS, 2)
        self.assertIn("场景：", out)
        self.assertGreater(len(out), 40)

    def test_07_empty_synopsis_safe(self):
        outlines = generate_outlines("", 2)
        self.assertEqual(len(outlines), 2)
        self.assertTrue(all("场景：" in t for t in outlines))


if __name__ == "__main__":
    unittest.main()
