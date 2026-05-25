"""KT-Buddy Phase 3 — BM25-based Q&A over a commented Java repo.

HuggingFace is blocked on the user's corp network, so semantic embeddings
aren't available. We use lexical BM25 instead, with code-aware tokenization
(CamelCase split + crude stemming) which works well for Java code search
since class/method names usually echo the concepts users ask about.

Index location: output/<name>/qa_index.json (alongside commented files).
"""

import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

from java_parser import chunk_by_methods
from kt_buddy import find_java_files
from llm import LLMError, call_claude


QA_PROMPT_PATH = Path(__file__).parent / "prompts" / "qa.txt"

# Split on CamelCase boundaries, underscores, digits
_TOKEN_RE = re.compile(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z]|\b)|[a-z]+|\d+")

_STOPWORDS = {
    # Java keywords / common types
    "public", "private", "protected", "static", "final", "void", "class",
    "interface", "enum", "abstract", "import", "package", "return", "if",
    "else", "for", "while", "switch", "case", "break", "continue", "new",
    "this", "super", "null", "true", "false", "throw", "throws", "try",
    "catch", "finally", "int", "long", "short", "byte", "char", "double",
    "float", "boolean", "string", "object",
    # English glue tokens common in NL questions
    "the", "a", "an", "of", "to", "in", "on", "is", "are", "does", "do",
    "how", "what", "where", "when", "why", "which", "that",
}


def _stem(token: str) -> str:
    """Crude stem: strip common English suffixes so 'authenticates' matches 'authenticate'."""
    for suffix in ("ies", "es", "ed", "ing", "ly", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t.lower()) for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS]


def _window_split(text: str, max_chars: int, overlap: int = 200) -> list[str]:
    out = []
    i = 0
    step = max(1, max_chars - overlap)
    while i < len(text):
        out.append(text[i : i + max_chars])
        i += step
    return out


def chunk_java_text(text: str, max_chars: int = 3500) -> list[str]:
    """Tree-sitter-based chunking: file header + each method/constructor, size-capped."""
    chunks = chunk_by_methods(text)
    if not chunks:
        return _window_split(text, max_chars) if len(text) > max_chars else ([text] if text.strip() else [])

    final: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            final.extend(_window_split(c, max_chars))
    return final


def build_index(commented_dir: Path, index_path: Path) -> int:
    """Walk Java files under commented_dir, chunk, persist as JSON. Returns chunk count."""
    chunks: list[dict] = []
    for jf in find_java_files(commented_dir):
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(jf.relative_to(commented_dir))
        for piece in chunk_java_text(text):
            chunks.append({"id": len(chunks), "path": rel, "content": piece})

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"chunks": chunks}, indent=2),
        encoding="utf-8",
    )
    return len(chunks)


def load_index(index_path: Path) -> dict:
    return json.loads(index_path.read_text(encoding="utf-8"))


def retrieve(index: dict, question: str, k: int = 5) -> list[dict]:
    chunks = index["chunks"]
    if not chunks:
        return []
    tokenized_corpus = [tokenize(c["content"]) for c in chunks]
    # Guard against pathological case where every chunk tokenizes to empty
    if not any(tokenized_corpus):
        return chunks[:k]
    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(tokenize(question))
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    top = [chunks[i] for i, s in ranked[:k] if s > 0]
    # Fall back to top-K by raw rank if BM25 returned all zeros (e.g. no token overlap)
    if not top:
        top = [chunks[i] for i, _ in ranked[:k]]
    return top


def answer_question(index: dict, question: str, k: int = 5) -> tuple[str, list[str]]:
    """Retrieve top-K chunks, ground claude's answer in them, return (answer, sources)."""
    retrieved = retrieve(index, question, k=k)
    if not retrieved:
        return ("I couldn't find anything in this repository to answer that.", [])

    system_prompt = QA_PROMPT_PATH.read_text(encoding="utf-8")
    blocks = [
        f'<excerpt path="{c["path"]}">\n{c["content"]}\n</excerpt>'
        for c in retrieved
    ]
    user_content = "CONTEXT:\n" + "\n\n".join(blocks) + f"\n\nQUESTION:\n{question}"

    try:
        answer = call_claude(system=system_prompt, user=user_content)
    except LLMError as e:
        return (f"(Question failed: {e})", [])

    sources = sorted({c["path"] for c in retrieved})
    return (answer.strip(), sources)
