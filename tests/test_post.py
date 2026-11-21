"""后期成片测试（标准库 unittest）。"""

import unittest

from video_studio.post import (
    UpscaleMode,
    build_compose_plan,
    build_cues,
    cues_from_shots,
    render_srt,
    srt_time,
    trim_head_noise,
    upscale_strategy,
)


class SubtitleTests(unittest.TestCase):
    def test_srt_time_format(self):
        self.assertEqual(srt_time(0), "00:00:00,000")
        self.assertEqual(srt_time(1.5), "00:00:01,500")
        self.assertEqual(srt_time(65.25), "00:01:05,250")
        self.assertEqual(srt_time(-1), "00:00:00,000")

    def test_build_cues_indices(self):
        cues = build_cues([(0, 2, "你好"), (2, 4, "世界")])
        self.assertEqual([c.index for c in cues], [1, 2])

    def test_cues_from_shots_timeline(self):
        shots = [
            {"duration": 5.0, "dialogue": "小明：你好"},
            {"duration": 3.0, "dialogue": ""},  # 无对白不出字幕
            {"duration": 2.0, "dialogue": "小红：再见"},
        ]
        cues = cues_from_shots(shots)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 0.0)
        self.assertEqual(cues[0].end, 5.0)
        self.assertEqual(cues[1].start, 8.0)  # 跳过第二个镜头的3秒
        self.assertEqual(cues[1].end, 10.0)

    def test_render_srt(self):
        cues = build_cues([(0, 1.2, "测试")])
        srt = render_srt(cues)
        self.assertIn("1\n00:00:00,000 --> 00:00:01,200\n测试", srt)


class ComposeTests(unittest.TestCase):
    def test_trim_head_default_and_bounds(self):
        self.assertEqual(trim_head_noise(), 0.14)
        self.assertEqual(trim_head_noise(0.12), 0.12)
        with self.assertRaises(ValueError):
            trim_head_noise(0.5)  # 超出经验区间

    def test_build_compose_plan(self):
        plan = build_compose_plan(
            ["a.mp4", "b.mp4"], srt_path="sub.srt", cta_text="关注我们", output="out.mp4"
        )
        self.assertEqual(len(plan.trims), 2)
        self.assertEqual(plan.trims[0].start, 0.14)
        self.assertIsNotNone(plan.cta)
        self.assertEqual(plan.cta.text, "关注我们")
        self.assertEqual(plan.output, "out.mp4")

    def test_ffmpeg_script_generated(self):
        plan = build_compose_plan(["a.mp4", "b.mp4"], srt_path="sub.srt")
        cmds = plan.to_ffmpeg_script()
        # 2 个裁头 + 1 个 concat 清单 + 1 个拼接 = 4
        self.assertEqual(len(cmds), 4)
        self.assertIn("subtitles=sub.srt", cmds[-1])
        self.assertIn("concat", cmds[-1])
        # 纯函数：重复调用结果一致
        self.assertEqual(cmds, plan.to_ffmpeg_script())

    def test_upscale_strategy(self):
        self.assertEqual(upscale_strategy("cloud_api"), UpscaleMode.API_REBIRTH)
        self.assertEqual(upscale_strategy("local_comfyui"), UpscaleMode.LOCAL_SEEDVR2)
        with self.assertRaises(ValueError):
            upscale_strategy("unknown")


if __name__ == "__main__":
    unittest.main()
