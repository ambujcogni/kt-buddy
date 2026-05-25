"""Single point of contact with Claude. All KT-Buddy modules call `call_claude()`.

Two backends, picked once per process:

  1. Anthropic Python SDK — used when `ANTHROPIC_API_KEY` is set.
     This is the production path (Render deploy) and what this code is built around.

  2. `claude -p` subprocess fallback — used when no API key is set but the Claude
     Code CLI is on PATH. Lets us run locally against the CTS-provided Claude
     auth without needing a personal API key. **Not used in production** — the
     deployed Docker image has no CLI installed and the env var will be set.
"""

import contextvars
import os
import shutil
import subprocess

import anthropic


# Lets the Streamlit UI inject a per-session API key for BYOK ("bring your own key")
# deploys. The Render-deployed instance has no env var; visitors paste their own key,
# which lives in their browser session and never touches the server's storage.
_user_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_api_key", default=None,
)


def set_user_api_key(key: str | None) -> None:
    """Set or clear the runtime API key for subsequent `call_claude` invocations."""
    _user_api_key.set(key or None)


def _effective_api_key() -> str | None:
    return _user_api_key.get() or os.environ.get("ANTHROPIC_API_KEY")


DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 16000
CLI_TIMEOUT_SECONDS = 180


class LLMError(Exception):
    """Raised when a Claude call fails (auth, network, rate limit, CLI exit, etc.)."""


def _strip_fences(text: str) -> str:
    """Smaller models sometimes wrap output in ```java/```json fences despite the prompt.
    Strip a single leading/trailing fence pair if present."""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines) + "\n"


def _backend() -> str:
    """Dynamic per-call: 'sdk' if any API key is available, else 'cli' if claude
    is on PATH. Re-evaluates each call so the BYOK UI input takes effect immediately."""
    if _effective_api_key():
        return "sdk"
    if shutil.which("claude"):
        return "cli"
    raise LLMError(
        "No API key provided and no Claude Code CLI available. "
        "Paste your Anthropic API key into the app's settings to continue."
    )


def _call_via_sdk(system: str, user: str, max_tokens: int, model: str) -> str:
    client = anthropic.Anthropic(api_key=_effective_api_key())
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError as e:
        raise LLMError(f"Auth failed: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise LLMError(f"Rate limited: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"API error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Connection error: {e}") from e

    text_blocks = [b.text for b in response.content if b.type == "text"]
    if not text_blocks:
        raise LLMError("Claude returned no text content.")
    return "".join(text_blocks)


def _call_via_cli(system: str, user: str, model: str) -> str:
    """Dev-only fallback. Concatenates system + user into stdin for `claude -p`."""
    claude_exe = shutil.which("claude") or "claude"
    combined = f"{system}\n\n{user}"
    try:
        result = subprocess.run(
            [claude_exe, "-p", "--model", model],
            input=combined,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as e:
        first = (e.stderr or str(e)).strip().splitlines()[:1]
        raise LLMError(f"`claude -p` failed: {first[0] if first else 'unknown error'}") from e
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"`claude -p` timed out after {CLI_TIMEOUT_SECONDS}s.") from e
    return result.stdout


def call_claude(
    system: str,
    user: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str = DEFAULT_MODEL,
) -> str:
    """Send one Claude request, return the assistant's text (fences stripped)."""
    if _backend() == "sdk":
        raw = _call_via_sdk(system, user, max_tokens, model)
    else:
        raw = _call_via_cli(system, user, model)
    return _strip_fences(raw)
