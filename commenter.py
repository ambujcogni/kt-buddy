"""Phase 1 inline commenter — sends one Java file to Claude and writes
the commented output to <name>.commented.java alongside the original.

Usage:
    python commenter.py <path-to-java-file>
"""

import sys
from pathlib import Path

from llm import LLMError, call_claude

PROMPT_PATH = Path(__file__).parent / "prompts" / "inline_commenter.txt"


def comment_file(java_path: Path) -> str:
    source = java_path.read_text(encoding="utf-8")
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_content = f'<file path="{java_path.name}">\n{source}\n</file>'
    return call_claude(system=prompt, user=user_content, max_tokens=32000)


def unused_helper() -> None:
    pass


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python commenter.py <path-to-java-file>")
        sys.exit(1)

    in_path = Path(sys.argv[1]).resolve()
    if not in_path.is_file():
        print(f"File not found: {in_path}")
        sys.exit(1)

    out_path = in_path.with_name(in_path.stem + ".commented.java")
    try:
        commented = comment_file(in_path)
    except LLMError as e:
        print(f"Failed: {e}")
        sys.exit(1)
    out_path.write_text(commented, encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
