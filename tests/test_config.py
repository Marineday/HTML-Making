import os
import tempfile
import unittest
from pathlib import Path

from hotdeal import config as config_module

MINIMAL = {
    "sources": [{"name": "s", "type": "rss", "url": "https://a.example/rss"}],
    "senders": [{"type": "console"}],
}


class ConfigTest(unittest.TestCase):
    def test_minimal_config_loads(self):
        cfg = config_module.from_dict(MINIMAL)
        self.assertEqual(cfg.poll_interval_seconds, 300)
        self.assertTrue(cfg.respect_robots)

    def test_requires_at_least_one_source(self):
        with self.assertRaisesRegex(config_module.ConfigError, "sources"):
            config_module.from_dict({"sources": [], "senders": [{"type": "console"}]})

    def test_requires_at_least_one_sender(self):
        with self.assertRaisesRegex(config_module.ConfigError, "senders"):
            config_module.from_dict({"sources": MINIMAL["sources"], "senders": []})

    def test_rejects_unknown_source_type(self):
        with self.assertRaisesRegex(config_module.ConfigError, "type"):
            config_module.from_dict({"sources": [{"name": "s", "type": "json", "url": "u"}], "senders": MINIMAL["senders"]})

    def test_html_list_requires_link_pattern(self):
        with self.assertRaisesRegex(config_module.ConfigError, "link_pattern"):
            config_module.from_dict({"sources": [{"name": "s", "type": "html_list", "url": "u"}], "senders": MINIMAL["senders"]})

    def test_rejects_aggressive_poll_interval(self):
        with self.assertRaisesRegex(config_module.ConfigError, "poll_interval_seconds"):
            config_module.from_dict({**MINIMAL, "poll_interval_seconds": 5})

    def test_rejects_unknown_filter_key(self):
        with self.assertRaisesRegex(config_module.ConfigError, "알 수 없는 키"):
            config_module.from_dict({**MINIMAL, "filters": {"include_keyword": ["토스"]}})

    def test_env_substitution(self):
        os.environ["HOTDEAL_TEST_TOKEN"] = "secret-value"
        cfg = config_module.from_dict(
            config_module._expand_env(
                {**MINIMAL, "senders": [{"type": "telegram", "bot_token": "${HOTDEAL_TEST_TOKEN}", "chat_id": "1"}]}
            )
        )
        self.assertEqual(cfg.senders[0].options["bot_token"], "secret-value")

    def test_missing_env_var_fails_loudly(self):
        os.environ.pop("HOTDEAL_ABSENT", None)
        with self.assertRaisesRegex(config_module.ConfigError, "HOTDEAL_ABSENT"):
            config_module._expand_env({"token": "${HOTDEAL_ABSENT}"})

    def test_line_comments_are_stripped(self):
        path = Path(tempfile.mkdtemp()) / "c.json"
        path.write_text(
            '// 주석\n{"sources":[{"name":"s","type":"rss","url":"https://a.example/rss"}],\n'
            '// 중간 주석\n "senders":[{"type":"console"}]}',
            encoding="utf-8",
        )
        self.assertEqual(config_module.load(path).sources[0].name, "s")

    def test_missing_file_message_points_to_example(self):
        with self.assertRaisesRegex(config_module.ConfigError, "config.example.json"):
            config_module.load("/nonexistent/config.json")

    def test_shipped_example_config_is_valid(self):
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "BRIDGE_TOKEN"):
            os.environ.setdefault(name, "x" * 20)
        cfg = config_module.load("config.example.json")
        self.assertTrue(cfg.enabled_sources)
        self.assertTrue(cfg.enabled_senders)


if __name__ == "__main__":
    unittest.main()
