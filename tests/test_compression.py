from __future__ import annotations

import unittest

from server.compression import compress, compress_preserve_code


class CompressionTestCase(unittest.TestCase):
    def test_empty_text_returns_empty(self) -> None:
        result = compress("")
        self.assertEqual(result.compressed, "")
        self.assertEqual(result.original_length, 0)
        self.assertFalse(result.was_compressed)

    def test_basic_lite_compression(self) -> None:
        text = "In order to complete the task, it is important to note that the function has been successfully completed."
        result = compress(text, level="lite")
        # Should have "To complete" and compression
        self.assertLess(len(result.compressed), len(text))
        self.assertGreater(result.saved_ratio, 0)

    def test_basic_full_compression(self) -> None:
        text = "The authentication middleware is handling the session token validation through the integrated JWT validation pipeline."
        result = compress(text, level="full")
        self.assertIn("Auth handles JWT validation", result.compressed)

    def test_ultra_compression_maximum(self) -> None:
        text = "The function has been successfully executed and the results are available. In order to complete the task."
        result = compress(text, level="ultra")
        # Ultra should compress more aggressively
        self.assertLess(len(result.compressed), len(text))

    def test_preserve_code_blocks(self) -> None:
        text = """Here is the code:

```python
def hello():
    return "Hello, World!"
```

The code above demonstrates the function."""
        result = compress_preserve_code(text, level="full")
        # Code block should be preserved
        self.assertIn('```python', result.compressed)
        self.assertIn('return "Hello, World!"', result.compressed)

    def test_preserve_urls(self) -> None:
        text = "Check out https://api.example.com/endpoint for more information."
        result = compress(text, level="full")
        self.assertIn("https://api.example.com/endpoint", result.compressed)

    def test_preserve_file_paths(self) -> None:
        text = "File created at D:\\Projects\\obsmcp\\server\\main.py"
        result = compress(text, level="full")
        self.assertIn("D:\\Projects\\obsmcp\\server\\main.py", result.compressed)

    def test_preserve_error_messages(self) -> None:
        text = "Error: Connection timeout after 30 seconds"
        result = compress(text, level="full")
        self.assertIn("Error: Connection timeout", result.compressed)

    def test_preserve_inline_code(self) -> None:
        text = "Use the `print()` function to output text."
        result = compress(text, level="full")
        self.assertIn("`print()`", result.compressed)

    def test_no_compression_when_no_patterns_match(self) -> None:
        text = "abc def ghi"
        result = compress(text, level="full")
        # Should still remove extra whitespace
        self.assertIn("abc def ghi", result.compressed)

    def test_removed_filler_words(self) -> None:
        text = "Obviously, the file has been created."
        result = compress(text, level="full")
        self.assertNotIn("Obviously,", result.compressed)
        # Should still have "the file has been created" but shorter
        self.assertLess(len(result.compressed), len(text))

    def test_saved_ratio_calculation(self) -> None:
        text = "In order to complete the task, it is important to note that the function has been successfully completed."
        result = compress(text, level="full")
        self.assertGreater(result.saved_ratio, 0)
        self.assertLessEqual(result.saved_ratio, 1.0)
        expected_saved = result.original_length - result.compressed_length
        self.assertAlmostEqual(result.saved_ratio, expected_saved / result.original_length, places=2)

    def test_was_compressed_flag(self) -> None:
        text = "In order to fix this, it is important to note that the authentication middleware is handling the session token validation through the integrated JWT validation pipeline."
        result = compress(text, level="full")
        self.assertTrue(result.was_compressed)

        # Short text with no patterns should not be marked as compressed
        result2 = compress("abc def ghi", level="full")
        self.assertFalse(result2.was_compressed)

    def test_multiple_pattern_applications(self) -> None:
        text = "In order to In order to In order to fix this."
        result = compress(text, level="full")
        # Multiple "In order to" should all be compressed
        self.assertNotIn("In order to In order to", result.compressed)

    def test_whitespace_normalization(self) -> None:
        text = "Hello    world\n\n\n\nTest"
        result = compress(text, level="full")
        self.assertNotIn("    ", result.compressed)  # No multiple spaces
        self.assertNotIn("\n\n\n", result.compressed)  # No more than 2 newlines

    def test_full_vs_lite_different_savings(self) -> None:
        text = "The authentication middleware is handling the session token validation through the integrated JWT validation pipeline."
        lite_result = compress(text, level="lite")
        full_result = compress(text, level="full")
        # Full should compress more than lite
        self.assertLessEqual(full_result.compressed_length, lite_result.compressed_length)


if __name__ == "__main__":
    unittest.main()