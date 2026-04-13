from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from server.opusmax_provider import OpusMaxTextProvider, OpusMaxToolProvider


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class OpusMaxProviderTestCase(unittest.TestCase):
    def test_text_provider_appends_response_contract_to_system_prompt(self) -> None:
        provider = OpusMaxTextProvider(api_key="test-key", base_url="https://api.opusmax.pro")

        with patch.object(provider._session, "post", return_value=_FakeResponse({"choices": [{"message": {"content": "{\"purpose\":\"p\",\"why_it_exists\":\"w\",\"how_it_is_used\":\"h\",\"inputs_outputs\":\"i\",\"side_effects\":\"s\",\"risks\":\"r\",\"language\":\"Python\"}"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})) as post:
            provider.generate_description(
                {"entity_type": "module", "name": "app.py", "file_path": "app.py", "signature": "", "feature_tags": [], "metadata": {}},
                "def run(): pass",
                response_contract="## Enforced Response Contract\n- Put the answer first.",
            )

        system_message = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("## Enforced Response Contract", system_message)
        self.assertIn("Put the answer first.", system_message)

    def test_web_search_uses_tools_endpoint(self) -> None:
        provider = OpusMaxToolProvider(api_key="test-key", base_url="https://api.opusmax.pro")

        with patch.object(provider._session, "post", return_value=_FakeResponse({"organic": [{"title": "obsmcp"}], "summary": "ok"}, status_code=201)) as post:
            result = provider.web_search("obsmcp")

        self.assertTrue(result["request_id"].startswith("ws_"))
        self.assertEqual(result["provider"], "opusmax")
        self.assertEqual(result["results"][0]["title"], "obsmcp")
        self.assertIn("/tools/web_search", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["query"], "obsmcp")

    def test_web_search_rejects_invalid_bounds(self) -> None:
        provider = OpusMaxToolProvider(api_key="test-key", base_url="https://api.opusmax.pro")
        with self.assertRaises(ValueError):
            provider.web_search("x" * 1001)
        with self.assertRaises(ValueError):
            provider.web_search("obsmcp", max_results=0)

    def test_understand_image_converts_local_file_to_data_url(self) -> None:
        provider = OpusMaxToolProvider(api_key="test-key", base_url="https://api.opusmax.pro")
        temp_dir = Path(__file__).resolve().parent.parent / ".tmp-tests"
        temp_dir.mkdir(parents=True, exist_ok=True)
        image_path = temp_dir / "provider-image.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        try:
            with patch.object(provider._session, "post", return_value=_FakeResponse({"analysis": "image ok"})) as post:
                result = provider.understand_image(prompt="Describe this image", image_path=str(image_path))
        finally:
            image_path.unlink(missing_ok=True)

        self.assertTrue(result["request_id"].startswith("img_"))
        self.assertEqual(result["provider"], "opusmax")
        self.assertEqual(result["analysis"], "image ok")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["prompt"], "Describe this image")
        self.assertTrue(payload["image_url"].startswith("data:image/png;base64,"))
        self.assertIn("/tools/understand_image", post.call_args.args[0])

    def test_understand_image_requires_an_image_source(self) -> None:
        provider = OpusMaxToolProvider(api_key="test-key", base_url="https://api.opusmax.pro")
        with self.assertRaises(ValueError):
            provider.understand_image(prompt="Describe this image")

    def test_understand_image_rejects_unsupported_mime_type(self) -> None:
        provider = OpusMaxToolProvider(api_key="test-key", base_url="https://api.opusmax.pro")
        with self.assertRaises(ValueError):
            provider.understand_image(prompt="Describe this image", image_base64="ZmFrZQ==", mime_type="image/gif")

    def test_provider_falls_back_to_claude_settings_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("server.opusmax_provider._read_claude_settings_env", return_value={
                "ANTHROPIC_AUTH_TOKEN": "settings-token",
                "ANTHROPIC_BASE_URL": "https://api.opusmax.pro",
            }):
                provider = OpusMaxToolProvider()

        self.assertEqual(provider.api_key, "settings-token")
        self.assertEqual(provider.base_url, "https://api.opusmax.pro")


if __name__ == "__main__":
    unittest.main()
