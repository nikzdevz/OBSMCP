from __future__ import annotations

import unittest

from server.config import _parse_output_compression


class ConfigParsingTestCase(unittest.TestCase):
    def test_output_compression_defaults_to_off_when_disabled(self) -> None:
        config = _parse_output_compression({"enabled": False, "level": "full"})

        self.assertFalse(config.enabled)
        self.assertEqual(config.mode, "off")
        self.assertTrue(config.preserve_patterns["code_blocks"])
        self.assertTrue(config.preserve_patterns["stack_traces"])

    def test_enabled_output_compression_defaults_to_prompt_only(self) -> None:
        config = _parse_output_compression({"enabled": True, "style": "concise_professional"})

        self.assertTrue(config.enabled)
        self.assertEqual(config.mode, "prompt_only")
        self.assertEqual(config.style, "concise_professional")


if __name__ == "__main__":
    unittest.main()
