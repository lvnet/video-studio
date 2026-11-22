"""创作层测试：剧本拆镜 + 权威提示词 Schema（标准库 unittest）。"""

import json
import tempfile
import unittest
from pathlib import Path

from video_studio.compliance import ComplianceBlocked
from video_studio.creation import (
    RuleRegistry,
    beats_to_shots,
    expand_prompt,
    get_provider,
    import_script,
    missing_fields_report,
    normalize_screenplay_text,
    parse_json,
    parse_markdown,
    parse_txt,
    validate_bindings,
)
from video_studio.models import Asset, Episode, RuleSet, Shot


class ScriptImportTests(unittest.TestCase):
    def test_parse_txt(self):
        text = (
            "场景：雨夜天台\n"
            "主角缓缓走上天台，环顾四周。\n"
            "小明：这里就是约定的地方吗？\n"
            "\n"
            "场景：狭窄走廊\n"
            "灯光忽明忽暗，脚步声回荡。\n"
        )
        beats = parse_txt(text)
        self.assertEqual(len(beats), 2)
        self.assertEqual(beats[0].location, "雨夜天台")
        self.assertTrue("小明" in beats[0].dialogue)
        self.assertTrue("走上天台" in beats[0].action)
        self.assertEqual(beats[1].location, "狭窄走廊")

    def test_parse_json(self):
        data = {"beats": [
            {"location": "咖啡馆", "action": "两人对坐", "dialogue": "A：你好"},
            {"location": "", "action": "起身离开", "dialogue": ""},
        ]}
        beats = parse_json(json.dumps(data, ensure_ascii=False))
        self.assertEqual(len(beats), 2)
        self.assertEqual(beats[0].location, "咖啡馆")

    def test_parse_markdown(self):
        md = (
            "# 第一场\n场景：公园\n他在长椅上等待。\n\n"
            "---\n# 第二场\n场景：车站\n列车进站。\n"
        )
        beats = parse_markdown(md)
        self.assertGreaterEqual(len(beats), 2)

    def test_import_dispatch(self):
        self.assertEqual(len(import_script("小明：你好", "txt")), 1)
        with self.assertRaises(ValueError):
            import_script("x", "docx")

    def test_beats_to_shots_and_missing_report(self):
        ep = Episode(id="e1", project_id="p1", index=1)
        beats = parse_txt("场景：天台\n小明：你好\n")
        shots = beats_to_shots(ep, beats)
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0].episode_id, "e1")
        self.assertEqual(shots[0].duration, 5.0)
        report = missing_fields_report(shots)
        self.assertEqual(len(report), 1)
        self.assertIn("action", report[0]["missing"])

    def test_normalize_screenplay_text(self):
        """创作案正文展示前清洗：去掉 Markdown 标记，保留可读剧本，仍可拆镜。"""
        raw = (
            "- **时长**：2分40秒\n"
            "- **本集任务**：立钩子\n\n"
            "**开场钩子**\n\n"
            "#### 场景 1　夜 / 傅氏宴会厅 / 金色灯光\n\n"
            "苏晚站在角落。\n\n"
            "**司仪** *（高亢）*：让我们恭喜傅总——\n\n"
            "> 镜头：中近景压低机位。**局部强调**。\n\n"
            "**下集预告：**她救了他。**\n"
        )
        clean = normalize_screenplay_text(raw)
        # 所有 Markdown 标记消失
        for junk in ("**", "####", "> 镜头", "- **", "开场钩子", "下集预告", "时长", "本集任务"):
            self.assertNotIn(junk, clean)
        # 关键内容以干净格式保留
        self.assertIn("场景：傅氏宴会厅", clean)
        self.assertIn("司仪（高亢）：让我们恭喜傅总——", clean)
        self.assertIn("镜头：中近景压低机位。局部强调。", clean)
        # 规范化后仍可拆镜、无空镜头
        beats = parse_txt(clean)
        self.assertTrue(beats)
        self.assertTrue(all(b.action or b.dialogue for b in beats))

    def test_creative_screenplay_format(self):
        """真实短剧创作案：制作标注丢弃、场景归位、对白/镜头调度各归其位。"""
        text = (
            "- **时长**：2分40秒\n"
            "- **主要场景**：傅氏宴会厅\n"
            "- **本集任务**：立钩子\n\n"
            "**开场钩子 · 0-6 秒**\n\n"
            "特写：一枚钻戒被推进无名指。\n\n"
            "#### 场景 1　夜 / 傅氏宴会厅 / 金色灯光\n\n"
            "苏晚站在角落，手里端着酒盘。\n\n"
            "**司仪** *（高亢）*：让我们恭喜傅总——\n\n"
            "> 镜头：中近景压低机位，从酒盘边缘往上看。\n\n"
            "**集尾钩子**\n\n"
            "**下集预告：**她救了他母亲。**\n"
        )
        beats = parse_txt(text)
        blob = "\n".join((b.action + b.dialogue) for b in beats)
        # 制作标注全部丢弃
        for junk in ("时长", "本集任务", "开场钩子", "集尾钩子", "下集预告"):
            self.assertNotIn(junk, blob)
        # 无空镜头
        self.assertTrue(all(b.action or b.dialogue for b in beats))
        # 场景地点挂到内容节拍，而非孤立空镜头
        located = [b for b in beats if b.location == "傅氏宴会厅"]
        self.assertTrue(located)
        self.assertTrue(any(b.action for b in located))
        # 加粗角色对白被正确识别，情绪保留
        dia = [b for b in beats if "让我们恭喜" in b.dialogue]
        self.assertTrue(dia)
        self.assertIn("司仪", dia[0].dialogue)
        # 镜头调度语言归动作，不误判为对白
        cam = [b for b in beats if "压低机位" in b.action]
        self.assertTrue(cam)
        self.assertFalse(any("压低机位" in b.dialogue for b in beats))


class PromptSchemaTests(unittest.TestCase):
    def setUp(self):
        self.assets = [
            Asset(id="a1", project_id="p1", type="role", name="小明",
                  voice_id="v_xiaoming", approved=True, image_url="a1.png", version=2),
            Asset(id="a2", project_id="p1", type="role", name="小红",
                  voice_id="v_xiaohong", approved=True, image_url="a2.png", version=1),
        ]
        self.shot = Shot(
            id="s1", episode_id="e1", index=1,
            location="雨夜天台", action="小明缓缓走上天台",
            dialogue="小明：我们该走了\n小红：等等",
        )

    def test_three_segment_complete_fields(self):
        e = expand_prompt(self.shot, self.assets, mode="t2v")
        self.assertIn("subject", e.three_segment)
        self.assertIn("scene", e.three_segment)
        self.assertIn("camera", e.three_segment)
        self.assertEqual(e.three_segment["scene"], "雨夜天台")

    def test_r2v_six_segment_with_primary_reference(self):
        e = expand_prompt(self.shot, self.assets, mode="r2v")
        self.assertEqual(len(e.six_segment), 6)
        self.assertIn("主参考", e.six_segment["subject_definitions"])
        self.assertIn("asset_version=2", e.six_segment["subject_definitions"])

    def test_unapproved_asset_not_referenced(self):
        unapproved = Asset(id="a3", project_id="p1", type="role", name="路人",
                           approved=False, image_url="a3.png")
        e = expand_prompt(self.shot, self.assets + [unapproved], mode="r2v")
        self.assertNotIn("路人", e.six_segment["subject_definitions"])

    def test_speaker_binding_only_when_audio_capable(self):
        # t2v 不支持音频 → 无绑定
        e_plain = expand_prompt(self.shot, self.assets, mode="t2v")
        self.assertEqual(len(e_plain.speaker_bindings), 0)
        # r2v 支持音频 → 有绑定，且按对白出现顺序 S1/S2
        e_r2v = expand_prompt(self.shot, self.assets, mode="r2v")
        self.assertEqual(len(e_r2v.speaker_bindings), 2)
        self.assertEqual(e_r2v.speaker_bindings[0].token, "S1")
        self.assertEqual(e_r2v.speaker_bindings[0].name, "小明")
        self.assertEqual(e_r2v.speaker_bindings[0].voice_id, "v_xiaoming")

    def test_validate_bindings_flags_missing_voice(self):
        no_voice = [Asset(id="a4", project_id="p1", type="role", name="无名",
                          approved=True)]  # 无 voice_id
        shot = Shot(id="s2", episode_id="e1", index=2, dialogue="无名：你好")
        e = expand_prompt(shot, no_voice, mode="r2v")
        problems = validate_bindings(e, "r2v")
        self.assertTrue(any("voice_id" in p for p in problems))

    def test_no_dialogue_no_binding_even_if_audio(self):
        shot = Shot(id="s3", episode_id="e1", index=3, action="安静的场景")
        e = expand_prompt(shot, self.assets, mode="r2v")
        self.assertEqual(len(e.speaker_bindings), 0)

    def test_compliance_block_in_prompt(self):
        bad = Shot(id="s4", episode_id="e1", index=4, action="宫崎骏风格的天空")
        with self.assertRaises(ComplianceBlocked):
            expand_prompt(bad, self.assets, mode="t2v")

    def test_compliance_can_be_disabled(self):
        bad = Shot(id="s5", episode_id="e1", index=5, action="宫崎骏风格的天空")
        e = expand_prompt(bad, self.assets, mode="t2v", enforce_compliance=False)
        self.assertIn("宫崎骏", e.three_segment["subject"])

    def test_rule_set_version_tracked(self):
        rs = RuleSet(id="r1", name="custom", version=7, schema_json=dict())
        e = expand_prompt(self.shot, self.assets, mode="t2v", rule_set=rs)
        self.assertEqual(e.rule_set_version, 7)

    def test_submission_text(self):
        e = expand_prompt(self.shot, self.assets, mode="r2v")
        text = e.to_submission()
        self.assertIn("[subject_definitions]", text)
        self.assertIn("(S1) 小明 voice_id=v_xiaoming", text)


class RuleRegistryTests(unittest.TestCase):
    def test_default_rule_bootstrap(self):
        r = RuleRegistry()
        self.assertGreaterEqual(r.default().version, 1)

    def test_hot_reload_from_files(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {
                "name": "custom",
                "is_default": True,
                "schema_json": {"negative_default": "test-neg"},
            }
            Path(d, "rules.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            r = RuleRegistry(d)
            n = r.reload()
            self.assertEqual(n, 1)
            self.assertEqual(r.default().schema_json["negative_default"], "test-neg")
            # 再 reload 生成新版本
            r.reload()
            self.assertEqual(r.default().version, 2)


class LLMPluginTests(unittest.TestCase):
    def test_stub_provider(self):
        """本地生成引擎：产出结构化、可拆镜正文（不再是占位文字）。"""
        p = get_provider()
        outlines = p.generate_outline("旅人回到江边寻找故人", 3)
        self.assertEqual(len(outlines), 3)
        # 每集都含可拆镜结构：场景行 + 对白
        for text in outlines:
            self.assertIn("场景：", text)
            self.assertGreater(len(text), 40)
        # 改写保留原文并带指令痕迹；续写非空且有场景行
        rewritten = p.rewrite("正文", "更幽默")
        self.assertIn("正文", rewritten.text)
        self.assertTrue(rewritten.text)
        self.assertIn("场景：", p.continue_script("前文", 2))


if __name__ == "__main__":
    unittest.main()
