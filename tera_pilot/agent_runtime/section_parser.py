"""
Parse and strip leading section-switch tokens from user messages.

Supported formats:
  {section}       — e.g. {office}, {heavy_code}, {general}
  /mode section   — e.g. /mode office, /mode heavy_code, /mode general

The token is parsed and stripped BEFORE the message reaches the LLM,
then AgentRuntime.set_section() is called. The new section persists as
session default until changed again.

v2.1.0: Loop 1 — Inline Section Switching (FEAT).
"""

import re
from enum import Enum
from typing import Optional, Tuple


class Section(str, Enum):
    """Valid section identifiers — matches the three sections already
    supported by AgentRuntime and PromptBuilder."""
    GENERAL = "general"
    HEAVY_CODE = "heavy_code"
    OFFICE = "office"


# Pattern: ^{section} or ^/mode section (optional whitespace after)
# Only matches at the START of the message to avoid false positives
# from JSON/code the user might paste (e.g. {"office": "value"}).
SECTION_PATTERN = re.compile(
    r'^\s*\{(general|heavy_code|office)\}\s*'
    r'|^\s*/mode\s+(general|heavy_code|office)\s*',
    re.IGNORECASE,
)


def parse_section_switch(message: str) -> Tuple[Optional[Section], str]:
    """Parse and strip a leading section-switch token from a user message.

    Returns:
        (new_section_or_None, cleaned_message).
        If no section token is found, returns (None, original_message).

    Examples:
        >>> parse_section_switch("{office} hello")
        (Section.OFFICE, "hello")
        >>> parse_section_switch("/mode heavy_code")
        (Section.HEAVY_CODE, "")
        >>> parse_section_switch("no tag")
        (None, "no tag")
        >>> parse_section_switch('{"office": "value"}')
        (None, '{"office": "value"}')
        >>> parse_section_switch("code {general} here")
        (None, "code {general} here")
    """
    match = SECTION_PATTERN.match(message)
    if not match:
        return None, message

    # match.group(1) for {section}, group(2) for /mode section
    section_str = match.group(1) or match.group(2)
    new_section = Section(section_str.lower())

    # Strip the matched portion and any leading whitespace after it
    cleaned = message[match.end():].lstrip()
    return new_section, cleaned
