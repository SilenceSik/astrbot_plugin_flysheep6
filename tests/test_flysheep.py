from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from flysheep import (  # noqa: E402
    clean_intro,
    format_report,
    next_daily_run,
    parse_category_ids,
    parse_wordpress_post,
    split_report,
)


class FlysheepCoreTests(unittest.TestCase):
    def test_parse_post_cleans_html_and_configuration_text(self) -> None:
        post = parse_wordpress_post(
            {
                "id": 62249,
                "date_gmt": "2026-07-25T18:00:06",
                "title": {"rendered": "第一狂战士 &amp; 卡赞"},
                "link": "https://www.flysheep6.com/archives/62249",
                "categories": [4, 20],
                "excerpt": {
                    "rendered": "<p>一款<strong>硬核动作</strong>角色扮演游戏。 最低配置: Windows 10 [&hellip;]</p>"
                },
                "content": {"rendered": ""},
            },
            "Asia/Shanghai",
            100,
        )

        self.assertEqual(post.post_id, 62249)
        self.assertEqual(post.title, "第一狂战士 & 卡赞")
        self.assertEqual(post.directories, ("PC单机大作",))
        self.assertEqual(post.intro, "一款硬核动作角色扮演游戏。")
        self.assertEqual(post.published_at.hour, 2)

    def test_intro_falls_back_to_content_and_truncates(self) -> None:
        intro = clean_intro("", "<p>1234567890</p>", 6)
        self.assertEqual(intro, "123456…")

    def test_intro_keeps_wordpress_truncation_signal(self) -> None:
        intro = clean_intro("<p>这是一段未完摘要 [&hellip;]</p>", "", 100)
        self.assertEqual(intro, "这是一段未完摘要…")

    def test_parse_category_ids_ignores_invalid_and_duplicates(self) -> None:
        self.assertEqual(parse_category_ids("4, 5,xx,4,32"), [4, 5, 32])

    def test_next_daily_run_uses_configured_timezone(self) -> None:
        now = datetime(2026, 7, 26, 0, 30, tzinfo=timezone.utc)
        next_run = next_daily_run(now, "09:00", "Asia/Shanghai")
        self.assertEqual(next_run.isoformat(), "2026-07-26T09:00:00+08:00")

        after_time = datetime(2026, 7, 26, 2, 0, tzinfo=timezone.utc)
        tomorrow = next_daily_run(after_time, "09:00", "Asia/Shanghai")
        self.assertEqual(tomorrow.isoformat(), "2026-07-27T09:00:00+08:00")

    def test_report_contains_directory_intro_and_link(self) -> None:
        raw = {
            "id": 1,
            "date_gmt": "2026-07-25T18:00:06",
            "title": {"rendered": "测试游戏"},
            "link": "https://example.test/game",
            "categories": [32],
            "excerpt": {"rendered": "<p>测试简介</p>"},
            "content": {"rendered": ""},
        }
        report = format_report(
            [parse_wordpress_post(raw, "Asia/Shanghai", 100)], 3
        )
        self.assertIn("[小游戏/独立游戏] 测试游戏", report)
        self.assertIn("简介：测试简介", report)
        self.assertIn("链接：https://example.test/game", report)

    def test_local_date_fallback_is_not_treated_as_utc(self) -> None:
        raw = {
            "id": 2,
            "date": "2026-07-26T02:00:06",
            "title": {"rendered": "本地时间测试"},
            "link": "https://example.test/local-time",
            "categories": [4],
            "excerpt": {"rendered": "简介"},
            "content": {"rendered": ""},
        }
        post = parse_wordpress_post(raw, "Asia/Shanghai", 100)
        self.assertEqual(post.published_at.hour, 2)
        self.assertEqual(post.published_at.utcoffset().total_seconds(), 8 * 3600)

    def test_split_report_respects_limit(self) -> None:
        chunks = split_report("标题\n\n" + "a" * 70 + "\n\n结尾", 30)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 30 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
