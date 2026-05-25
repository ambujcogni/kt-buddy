"""KT-Buddy Phase 1.5 — accept a git URL or local path, walk all .java files,
and write commented copies into a mirrored ./output/<name>/ tree.

Usage:
    python kt_buddy.py <git-url-or-local-path>

Examples:
    python kt_buddy.py https://github.com/foo/bar.git
    python kt_buddy.py ./tests
"""

import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from commenter import comment_file
from llm import LLMError

REPOS_DIR = Path("repos")
OUTPUT_DIR = Path("output")
SKIP_DIRS = {"target", "build", ".git", "node_modules", "out", ".gradle", ".idea"}

URL_SCHEMES = ("http://", "https://", "git://", "ssh://")
SCP_LIKE = re.compile(r"^[\w.-]+@[\w.-]+:.+")


def looks_like_url(s: str) -> bool:
    return s.startswith(URL_SCHEMES) or bool(SCP_LIKE.match(s))


def validate_url(url: str) -> str:
    if not looks_like_url(url):
        raise ValueError(f"Not a recognizable git URL: {url}")
    if url.startswith(URL_SCHEMES):
        parsed = urllib.parse.urlparse(url)
        if not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError(f"URL missing host or path: {url}")
    return url


def repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def clone_repo(url: str, dest: Path) -> Path:
    if dest.exists():
        print(f"Reusing existing clone at {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {url} -> {dest}")
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
    )
    return dest


def resolve_source(arg: str) -> tuple[Path, str]:
    """Returns (source_dir, name_for_output)."""
    if looks_like_url(arg):
        validate_url(arg)
        name = repo_name_from_url(arg)
        dest = REPOS_DIR / name
        clone_repo(arg, dest)
        return dest, name
    path = Path(arg).resolve()
    if not path.is_dir():
        raise ValueError(f"Local path is not a directory: {path}")
    return path, path.name


def find_java_files(root: Path):
    for p in root.rglob("*.java"):
        if SKIP_DIRS & set(p.parts):
            continue
        yield p


def comment_repo_iter(source_dir: Path, name: str, output_root: Path = OUTPUT_DIR):
    """Process every .java file under source_dir, writing mirrored output.
    Yields progress events so callers (CLI, UI) can render their own feedback.
    `output_root` lets the UI scope outputs per session for multi-tenant safety."""
    out_root = output_root / name
    out_root.mkdir(parents=True, exist_ok=True)
    java_files = list(find_java_files(source_dir))
    total = len(java_files)
    yield {"type": "start", "total": total, "out_root": out_root, "source_dir": source_dir}

    for i, java_path in enumerate(java_files, start=1):
        rel = java_path.relative_to(source_dir)
        out_path = out_root / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        yield {"type": "file_start", "index": i, "total": total, "rel": rel}
        try:
            commented = comment_file(java_path)
            out_path.write_text(commented, encoding="utf-8")
            yield {"type": "file_done", "index": i, "total": total, "rel": rel, "out_path": out_path}
        except LLMError as e:
            yield {"type": "file_error", "index": i, "total": total, "rel": rel, "error": str(e)}

    yield {"type": "done", "out_root": out_root, "total": total}


def process_repo(arg: str) -> None:
    source_dir, name = resolve_source(arg)
    successes = 0
    failures: list[tuple[Path, str]] = []
    total = 0
    out_root = None

    for event in comment_repo_iter(source_dir, name):
        t = event["type"]
        if t == "start":
            total = event["total"]
            out_root = event["out_root"]
            if total == 0:
                print(f"No .java files found under {source_dir}")
                return
            print(f"Found {total} .java file(s) under {source_dir}")
        elif t == "file_start":
            print(f"[{event['index']}/{total}] {event['rel']}")
        elif t == "file_done":
            successes += 1
        elif t == "file_error":
            failures.append((event["rel"], event["error"]))
            first = event["error"].splitlines()[0] if event["error"] else "unknown error"
            print(f"  FAILED: {first}")

    print(f"\nDone. {successes}/{total} commented; output in {out_root}")
    if failures:
        print(f"{len(failures)} failure(s):")
        for rel, err in failures:
            first = err.splitlines()[0] if err else "unknown"
            print(f"  - {rel}: {first}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python kt_buddy.py <git-url-or-local-path>")
        sys.exit(1)
    try:
        process_repo(sys.argv[1])
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
