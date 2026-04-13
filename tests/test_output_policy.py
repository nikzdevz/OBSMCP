from __future__ import annotations

import unittest

from server.config import OutputCompressionConfig, TaskOutputOverrideConfig
from server.output_policy import resolve_output_policy


class OutputPolicyTestCase(unittest.TestCase):
    def test_review_task_uses_findings_first_contract(self) -> None:
        config = OutputCompressionConfig(
            enabled=True,
            mode="prompt_only",
            task_overrides={"review": TaskOutputOverrideConfig(style="terse_technical", level="full")},
        )

        policy = resolve_output_policy(
            config,
            task={"title": "Code review pagination regression", "description": "Review bug fix", "tags": ["review"]},
        )

        self.assertEqual(policy.mode, "prompt_only")
        self.assertEqual(policy.task_type, "review")
        self.assertEqual(policy.style, "terse_technical")
        self.assertIn("findings and risks", policy.prompt_contract)

    def test_detail_request_bypasses_compression(self) -> None:
        config = OutputCompressionConfig(enabled=True, mode="prompt_only", expand_on_request=True)

        policy = resolve_output_policy(config, detail_requested=True)

        self.assertEqual(policy.mode, "off")
        self.assertTrue(policy.bypassed)
        self.assertEqual(policy.bypass_reason, "detail_requested")

    def test_destructive_command_bypasses_compression(self) -> None:
        config = OutputCompressionConfig(enabled=True, mode="prompt_only")

        policy = resolve_output_policy(config, command="rm -rf build", operation_kind="dangerous_actions")

        self.assertEqual(policy.mode, "off")
        self.assertEqual(policy.task_type, "dangerous_actions")
        self.assertEqual(policy.bypass_reason, "destructive_actions")


if __name__ == "__main__":
    unittest.main()
