from __future__ import annotations

import unittest
from unittest.mock import Mock

from server.mcp_protocol import handle_rpc


class McpProtocolTestCase(unittest.TestCase):
    def test_tools_call_wraps_list_structured_content(self) -> None:
        service = Mock()
        service.call_tool.return_value = [{"id": 1}, {"id": 2}]

        response = handle_rpc(
            service,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_active_tasks", "arguments": {}},
            },
        )

        assert response is not None
        self.assertEqual(response["result"]["structuredContent"], {"items": [{"id": 1}, {"id": 2}]})
        self.assertIn('"id": 1', response["result"]["content"][0]["text"])

    def test_tools_call_wraps_scalar_structured_content(self) -> None:
        service = Mock()
        service.call_tool.return_value = "ok"

        response = handle_rpc(
            service,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "generate_compact_context", "arguments": {}},
            },
        )

        assert response is not None
        self.assertEqual(response["result"]["structuredContent"], {"value": "ok"})
        self.assertEqual(response["result"]["content"][0]["text"], "ok")

    def test_tools_call_keeps_dict_structured_content(self) -> None:
        service = Mock()
        service.call_tool.return_value = {"status": "ok"}

        response = handle_rpc(
            service,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "health_check", "arguments": {}},
            },
        )

        assert response is not None
        self.assertEqual(response["result"]["structuredContent"], {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
