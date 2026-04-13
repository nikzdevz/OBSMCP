"""
Output compression engine for obsmcp.

Provides rule-based text compression to reduce output tokens
while preserving technical accuracy and important content.

Levels:
- lite: Minimal compression
- full: Default compression (recommended)
- ultra: Maximum compression (may lose nuance)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class CompressionResult:
    compressed: str
    original_length: int
    compressed_length: int
    saved_ratio: float
    was_compressed: bool


# Compression patterns by level
# Order matters - longer/more specific patterns should come first
LITE_PATTERNS: list[tuple[str, str]] = [
    # Long filler phrases -> short
    ("Due to the fact that ", "Because "),
    ("It is important to note that ", "Note: "),
    ("It should be noted that ", "Note: "),
    ("Please note that ", "Note: "),
    ("As you can see, ", ""),
    ("As we can see, ", ""),
    ("As you can see.", ""),
    ("As we can see.", ""),
    ("Obviously, ", ""),
    ("Obviously.", ""),
    ("Clearly, ", ""),
    ("Of course, ", ""),
    ("Essentially, ", ""),
    ("Basically, ", ""),
    ("In other words, ", ""),
    ("That being said, ", ""),
    ("With that being said, ", ""),
    ("On the other hand, ", "However, "),
    ("In the meantime, ", "Meanwhile, "),
    ("At this point in time, ", "Now, "),
    ("In the event that ", "If "),
    ("In case that ", "If "),
    ("In spite of the fact that ", "Although "),
    ("For the purpose of ", "To "),
    ("In addition to this, ", "Also, "),
    ("Furthermore, ", "Also, "),
    ("Moreover, ", "Also, "),
    ("Additionally, ", "Also, "),
    ("In addition, ", "Also, "),
    ("With respect to ", "Regarding "),
    ("In regard to ", "Regarding "),
    ("In terms of ", "For "),
    ("In relation to ", "Regarding "),
    ("In order to ", "To "),
    # Remove redundant
    ("both A and B", "A and B"),
    ("each and every", "every"),
    ("each and all", "all"),
    ("one and only", "only"),
    ("any and all", "all"),
    # The reason
    ("The reason for this is that ", "Because "),
    ("This is because ", "Because "),
    ("This means that ", "Meaning "),
    ("In this case, ", ""),
    ("When it comes to ", "About "),
    ("As far as ", "About "),
    ("The fact that ", ""),
    ("Needless to say, ", ""),
    ("It goes without saying that ", ""),
    ("As stated previously, ", ""),
    ("As mentioned earlier, ", ""),
    ("As mentioned above, ", ""),
    ("As we discussed earlier, ", ""),
    ("As we have seen, ", ""),
]

FULL_PATTERNS: list[tuple[str, str]] = LITE_PATTERNS + [
    # Common verbose technical phrases
    ("The authentication middleware is handling the session token validation through the integrated JWT validation pipeline", "Auth handles JWT validation"),
    ("The authentication system validates ", "Auth validates "),
    ("The authorization system checks ", "Auth checks "),
    ("User authentication is handled by ", "Auth via "),
    ("Session token validation is performed by ", "Token validated by "),
    ("The database connection is established", "DB connected"),
    ("Successfully connected to the database", "DB connected"),
    ("Query execution completed successfully", "Query done"),
    ("No results were returned from the database", "No DB results"),
    ("File has been successfully created at", "File created:"),
    ("The file has been written to", "Written to"),
    ("Reading from the file system", "Reading files"),
    ("Writing to the file system", "Writing files"),
    ("API request has been processed", "API done"),
    ("The API response indicates", "API response:"),
    ("Successfully received response from API", "API responded"),
    # This function/class/method -> shorter
    ("This function ", "Fn "),
    ("This method ", "Method "),
    ("This class ", "Class "),
    # Remove "has been", "was", etc. in certain contexts
    ("The file has been created", "File created"),
    ("The data has been saved", "Data saved"),
    ("The operation has been completed", "Operation done"),
]

ULTRA_PATTERNS: list[tuple[str, str]] = FULL_PATTERNS + [
    # More aggressive compression
    ("Please ", ""),
    ("Thank you for ", ""),
    ("I would like to ", ""),
    ("I am ", "I'm "),
    ("I have ", "I've "),
    ("it is ", "it's "),
    ("that is ", "that's "),
    ("there is ", "there's "),
    ("there are ", "there're "),
    ("you are ", "you're "),
    ("we are ", "we're "),
    ("they are ", "they're "),
    # Remove common suffixes
    (" therefore", ""),
    (" however", ""),
    (" moreover", ""),
    (" furthermore", ""),
    (" that", ""),
    (" which", ""),
]


def _extract_code_blocks(text: str) -> tuple[str, list[str]]:
    """Extract code blocks and replace with placeholders."""
    code_blocks: list[str] = []
    pattern = re.compile(r'(```[\s\S]*?```|`[^`]+`)')

    def replacer(match):
        code_blocks.append(match.group())
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    protected = pattern.sub(replacer, text)
    return protected, code_blocks


def _restore_code_blocks(text: str, code_blocks: list[str]) -> str:
    """Restore code blocks from placeholders."""
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", block)
    return text


def _extract_urls_and_paths(text: str) -> tuple[str, list[str]]:
    """Extract URLs and file paths and replace with placeholders."""
    preserved: list[str] = []

    # URLs
    pattern = re.compile(r'https?://\S+')

    def url_replacer(match):
        preserved.append(match.group())
        return f"\x00URL{len(preserved) - 1}\x00"

    protected = pattern.sub(url_replacer, text)

    # File paths
    path_pattern = re.compile(r'(?:[A-Z]:\\)?(?:/[\w\-\.]+)+')

    def path_replacer(match):
        preserved.append(match.group())
        return f"\x00PATH{len(preserved) - 1}\x00"

    protected = path_pattern.sub(path_replacer, protected)
    return protected, preserved


def _restore_urls_and_paths(text: str, preserved: list[str]) -> str:
    """Restore URLs and paths from placeholders."""
    for i, item in enumerate(preserved):
        text = text.replace(f"\x00URL{i}\x00", item)
        text = text.replace(f"\x00PATH{i}\x00", item)
    return text


def _apply_patterns(text: str, patterns: list[tuple[str, str]], max_iterations: int = 3) -> str:
    """Apply compression patterns until no more changes."""
    for _ in range(max_iterations):
        original = text
        for old, new in patterns:
            text = text.replace(old, new)
        if text == original:
            break
    return text


def _remove_extra_whitespace(text: str) -> str:
    """Remove extra spaces and newlines."""
    # Remove multiple spaces (but keep single spaces)
    text = re.sub(r' +', ' ', text)
    # Remove multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Remove spaces at start of lines
    text = re.sub(r'\n +', '\n', text)
    return text.strip()


def compress(text: str, level: str = "full") -> CompressionResult:
    """
    Compress text using rule-based patterns.

    Args:
        text: The text to compress
        level: Compression level - "lite", "full", or "ultra"

    Returns:
        CompressionResult with compressed text and stats
    """
    if not text or not text.strip():
        return CompressionResult(
            compressed=text,
            original_length=0,
            compressed_length=0,
            saved_ratio=0.0,
            was_compressed=False,
        )

    original_length = len(text)

    # Choose pattern set based on level
    if level == "lite":
        patterns = LITE_PATTERNS
    elif level == "ultra":
        patterns = ULTRA_PATTERNS
    else:  # full
        patterns = FULL_PATTERNS

    # Step 1: Protect code blocks
    protected, code_blocks = _extract_code_blocks(text)

    # Step 2: Protect URLs and paths
    protected, preserved = _extract_urls_and_paths(protected)

    # Step 3: Apply compression patterns
    compressed = _apply_patterns(protected, patterns)

    # Step 4: Restore protected content
    compressed = _restore_urls_and_paths(compressed, preserved)
    compressed = _restore_code_blocks(compressed, code_blocks)

    # Step 5: Remove extra whitespace
    compressed = _remove_extra_whitespace(compressed)

    # Calculate stats
    compressed_length = len(compressed)
    saved = original_length - compressed_length
    saved_ratio = saved / original_length if original_length > 0 else 0

    return CompressionResult(
        compressed=compressed,
        original_length=original_length,
        compressed_length=compressed_length,
        saved_ratio=saved_ratio,
        was_compressed=saved_ratio > 0.01,  # Consider compressed if > 1% saved
    )


def compress_preserve_code(text: str, level: str = "full") -> CompressionResult:
    """
    Compress text while preserving code blocks entirely.

    This is the recommended function for compressing technical output
    where code blocks must remain exact.
    """
    # Code blocks are already preserved in compress()
    return compress(text, level)