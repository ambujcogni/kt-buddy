"""KT-Buddy Streamlit UI — paste a git URL (or local path), watch each
Java file get commented, then download the mirrored output as a zip.

Run with:
    streamlit run app.py
"""

import html
import io
import os
import shutil
import uuid
import zipfile
from pathlib import Path

import streamlit as st

import llm
from kt_buddy import comment_repo_iter, resolve_source, OUTPUT_DIR
from pdf_generator import generate_report
from qa import answer_question, build_index, load_index


def _expected_passcode() -> str | None:
    """Return the configured passcode, or None if the app is open (local dev)."""
    try:
        secret = st.secrets.get("passcode")
        if secret:
            return str(secret)
    except (FileNotFoundError, st.errors.StreamlitAPIException):
        pass
    return os.environ.get("KT_BUDDY_PASSCODE")


def _session_output_root() -> Path:
    """Per-session output dir so concurrent visitors don't clobber each other."""
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = uuid.uuid4().hex[:12]
    root = OUTPUT_DIR / "sessions" / st.session_state["session_id"]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _needs_byok() -> bool:
    """True iff this instance has no API key configured and no Claude CLI installed.
    On Render the env var is unset and the CLI is absent → visitors must paste their own key."""
    return not os.environ.get("ANTHROPIC_API_KEY") and shutil.which("claude") is None


def _propagate_user_key() -> None:
    """Streamlit re-executes the script on every interaction; contextvars don't survive
    that. Push the session-stored key into llm's contextvar on every run."""
    key = st.session_state.get("user_api_key")
    if key:
        llm.set_user_api_key(key)


def render_byok_card() -> None:
    st.markdown('<div class="ktb-card"><h3>Anthropic API key</h3>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ktb-hint">This instance ships without a key. Paste yours '
        '(<code>sk-ant-...</code>) to use the app &mdash; it stays in your browser '
        'session only, never written to disk or logs. Get one at '
        '<a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a>.</div>',
        unsafe_allow_html=True,
    )
    current = st.session_state.get("user_api_key", "")
    entered = st.text_input(
        "API key", value=current, type="password",
        label_visibility="collapsed", key="api_key_input",
        placeholder="sk-ant-...",
    )
    if entered != current:
        st.session_state["user_api_key"] = entered
        llm.set_user_api_key(entered or None)
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


st.set_page_config(
    page_title="KT-Buddy",
    page_icon="📘",
    layout="centered",
    initial_sidebar_state="collapsed",
)


CUSTOM_CSS = """
<style>
    /* Hide Streamlit chrome we don't need */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}

    /* Base layout breathing room */
    .block-container {
        padding-top: 2.5rem !important;
        padding-bottom: 3rem !important;
        max-width: 820px !important;
    }

    /* Hero */
    .ktb-hero {
        background: linear-gradient(135deg, #6366F1 0%, #8B5CF6 100%);
        color: white;
        padding: 2.25rem 2rem;
        border-radius: 18px;
        margin-bottom: 1.75rem;
        box-shadow: 0 12px 30px -10px rgba(99, 102, 241, 0.45);
    }
    .ktb-hero h1 {
        margin: 0 0 0.4rem 0;
        font-size: 2.1rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        color: white;
    }
    .ktb-hero p {
        margin: 0;
        opacity: 0.92;
        font-size: 1.02rem;
    }

    /* Card surface */
    .ktb-card {
        background: white;
        border: 1px solid #E5E7EB;
        border-radius: 14px;
        padding: 1.5rem 1.6rem;
        margin-bottom: 1.25rem;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .ktb-card h3 {
        margin: 0 0 0.85rem 0;
        font-size: 1.05rem;
        font-weight: 600;
        color: #0F172A;
        letter-spacing: -0.01em;
    }
    .ktb-card .ktb-hint {
        color: #64748B;
        font-size: 0.88rem;
        margin-top: 0.2rem;
    }

    /* Inputs */
    .stTextInput input {
        border-radius: 10px !important;
        border: 1px solid #E5E7EB !important;
        padding: 0.65rem 0.85rem !important;
        font-size: 0.98rem !important;
    }
    .stTextInput input:focus {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15) !important;
    }

    /* Primary button */
    .stButton > button[kind="primary"] {
        background: #6366F1 !important;
        border: none !important;
        border-radius: 10px !important;
        padding: 0.6rem 1.25rem !important;
        font-weight: 600 !important;
        box-shadow: 0 4px 12px -2px rgba(99, 102, 241, 0.4) !important;
        transition: transform 0.05s ease, box-shadow 0.15s ease !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: #5458E6 !important;
        box-shadow: 0 6px 16px -2px rgba(99, 102, 241, 0.5) !important;
    }
    .stButton > button[kind="primary"]:active {
        transform: translateY(1px);
    }

    /* Status badges */
    .ktb-row {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        padding: 0.35rem 0;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.85rem;
        color: #334155;
    }
    .ktb-dot {
        width: 8px; height: 8px; border-radius: 50%;
        flex-shrink: 0;
    }
    .ktb-dot.ok    { background: #10B981; }
    .ktb-dot.err   { background: #EF4444; }
    .ktb-dot.work  { background: #F59E0B; animation: pulse 1.2s infinite; }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50%      { opacity: 0.35; }
    }

    /* Q&A answer block + source chips */
    .ktb-answer {
        background: #F5F6FA;
        border-left: 3px solid #6366F1;
        padding: 1rem 1.15rem;
        border-radius: 8px;
        color: #0F172A;
        font-size: 0.95rem;
        line-height: 1.55;
        white-space: pre-wrap;
        margin-top: 0.4rem;
    }
    code.ktb-source {
        background: #EEF2FF;
        color: #4338CA;
        padding: 0.18rem 0.5rem;
        border-radius: 4px;
        font-size: 0.78rem;
        border: 1px solid #E0E7FF;
        display: inline-block;
        margin: 0.18rem 0.18rem 0.18rem 0;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    /* Streamlit tabs polish */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.4rem;
        border-bottom: 1px solid #E5E7EB;
        margin-bottom: 1.25rem;
    }
    .stTabs [data-baseweb="tab"] {
        height: 42px;
        padding: 0 1.1rem;
        font-weight: 600;
        color: #64748B;
    }
    .stTabs [aria-selected="true"] {
        color: #6366F1 !important;
    }

    /* Summary banner */
    .ktb-summary {
        background: #ECFDF5;
        border: 1px solid #A7F3D0;
        color: #065F46;
        padding: 0.9rem 1.1rem;
        border-radius: 12px;
        font-weight: 600;
        margin-bottom: 1rem;
    }
    .ktb-summary.warn {
        background: #FFFBEB;
        border-color: #FCD34D;
        color: #92400E;
    }
</style>
"""


def render_header() -> None:
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        """
        <div class="ktb-hero">
            <h1>KT-Buddy</h1>
            <p>Onboard onto unfamiliar Java codebases. Paste a repo and get a fully commented mirror.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_input_form() -> tuple[str, bool]:
    st.markdown('<div class="ktb-card"><h3>Source</h3>', unsafe_allow_html=True)
    with st.form("source_form", clear_on_submit=False):
        value = st.text_input(
            "Git URL or local path",
            placeholder="https://github.com/owner/repo.git",
            label_visibility="collapsed",
        )
        st.markdown(
            '<div class="ktb-hint">Accepts https / ssh / git@ URLs, or a path to a local directory.</div>',
            unsafe_allow_html=True,
        )
        submitted = st.form_submit_button("Start commenting", type="primary")
    st.markdown("</div>", unsafe_allow_html=True)
    return value.strip(), submitted


def zip_output(out_root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in out_root.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(out_root.parent))
    return buf.getvalue()


def run_pipeline(arg: str) -> None:
    try:
        source_dir, name = resolve_source(arg)
    except (ValueError, Exception) as e:
        st.error(f"Could not resolve source: {e}")
        return

    st.markdown('<div class="ktb-card"><h3>Progress</h3>', unsafe_allow_html=True)
    progress_bar = st.progress(0.0, text="Starting...")
    log_slot = st.empty()
    st.markdown("</div>", unsafe_allow_html=True)

    rows: list[str] = []
    successes = 0
    failures: list[tuple[str, str]] = []
    total = 0
    out_root: Path | None = None

    def render_log(current: str | None = None) -> None:
        body = "".join(rows)
        if current:
            body += (
                f'<div class="ktb-row"><span class="ktb-dot work"></span>'
                f'<span>{current}</span></div>'
            )
        log_slot.markdown(body or "&nbsp;", unsafe_allow_html=True)

    for event in comment_repo_iter(source_dir, name, output_root=_session_output_root()):
        t = event["type"]
        if t == "start":
            total = event["total"]
            out_root = event["out_root"]
            if total == 0:
                progress_bar.empty()
                st.warning(f"No .java files found under {source_dir}.")
                return
            progress_bar.progress(0.0, text=f"0 / {total} files")
        elif t == "file_start":
            render_log(current=str(event["rel"]))
        elif t == "file_done":
            successes += 1
            rows.append(
                f'<div class="ktb-row"><span class="ktb-dot ok"></span>'
                f'<span>{event["rel"]}</span></div>'
            )
            progress_bar.progress(event["index"] / total, text=f"{event['index']} / {total} files")
            render_log()
        elif t == "file_error":
            failures.append((str(event["rel"]), event["error"]))
            first = event["error"].splitlines()[0] if event["error"] else "unknown error"
            rows.append(
                f'<div class="ktb-row"><span class="ktb-dot err"></span>'
                f'<span>{event["rel"]} &mdash; {first}</span></div>'
            )
            progress_bar.progress(event["index"] / total, text=f"{event['index']} / {total} files")
            render_log()

    progress_bar.empty()
    st.session_state["last_run"] = {
        "name": name,
        "source_dir": str(source_dir),
        "out_root": str(out_root) if out_root else None,
        "total": total,
        "successes": successes,
        "failures": failures,
    }
    st.session_state.pop("pdf_path", None)


def render_results() -> None:
    run = st.session_state.get("last_run")
    if not run or not run.get("out_root"):
        return

    out_root = Path(run["out_root"])
    total = run["total"]
    successes = run["successes"]
    failures = run["failures"]

    warn = " warn" if failures else ""
    msg = f"Commented {successes}/{total} files"
    if failures:
        msg += f" ({len(failures)} failed)"
    st.markdown(
        f'<div class="ktb-summary{warn}">{msg} &middot; output in <code>{out_root}</code></div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="ktb-card"><h3>Output</h3>', unsafe_allow_html=True)
    if out_root.exists():
        zip_bytes = zip_output(out_root)
        st.download_button(
            label="Download commented output (zip)",
            data=zip_bytes,
            file_name=f"{run['name']}-commented.zip",
            mime="application/zip",
            type="primary",
        )
        files = sorted(p for p in out_root.rglob("*.java"))
        with st.expander(f"Browse {len(files)} commented file(s)"):
            for p in files:
                rel = p.relative_to(out_root)
                with st.expander(str(rel)):
                    st.code(p.read_text(encoding="utf-8"), language="java")
    st.markdown("</div>", unsafe_allow_html=True)

    render_pdf_section(run)


def render_pdf_section(run: dict) -> None:
    st.markdown('<div class="ktb-card"><h3>Onboarding report (PDF)</h3>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ktb-hint">A high-level PDF with project overview, UML class diagram, '
        'and a package/class index. Generated via <code>claude -p</code> &mdash; takes ~30s.</div>',
        unsafe_allow_html=True,
    )

    pdf_path_str = st.session_state.get("pdf_path")
    if pdf_path_str and Path(pdf_path_str).exists():
        pdf_path = Path(pdf_path_str)
        st.download_button(
            label="Download PDF report",
            data=pdf_path.read_bytes(),
            file_name=pdf_path.name,
            mime="application/pdf",
            type="primary",
        )
        if st.button("Regenerate"):
            st.session_state.pop("pdf_path", None)
            st.rerun()
    else:
        if st.button("Generate PDF report", type="primary"):
            with st.spinner("Generating PDF — extracting metadata, calling claude, rendering diagram..."):
                try:
                    pdf_path = generate_report(
                        Path(run["source_dir"]),
                        run["name"],
                        output_root=_session_output_root(),
                    )
                    st.session_state["pdf_path"] = str(pdf_path)
                    st.rerun()
                except Exception as e:
                    st.error(f"PDF generation failed: {e}")

    st.markdown("</div>", unsafe_allow_html=True)


def render_ask_tab() -> None:
    st.markdown('<div class="ktb-card"><h3>Ask the codebase</h3>', unsafe_allow_html=True)

    session_root = _session_output_root()
    repos = sorted([p.name for p in session_root.iterdir() if p.is_dir()])
    if not repos:
        st.markdown(
            '<div class="ktb-hint">No repos available yet in this session. Run a repo through '
            '<b>Comment &amp; Report</b> first, then come back here to ask questions.</div>',
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)
        return

    selected = st.selectbox("Repository", repos, key="qa_repo")
    if not selected:
        st.markdown("</div>", unsafe_allow_html=True)
        return

    repo_dir = session_root / selected
    index_path = repo_dir / "qa_index.json"

    if not index_path.exists():
        st.markdown(
            '<div class="ktb-hint">No index built yet for this repo. Building takes a few seconds &mdash; '
            'BM25 lexical retrieval, no model downloads required.</div>',
            unsafe_allow_html=True,
        )
        if st.button("Build Q&A index", type="primary", key="qa_build"):
            with st.spinner("Indexing source..."):
                count = build_index(repo_dir, index_path)
            st.success(f"Indexed {count} chunks.")
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        return

    index = load_index(index_path)
    chunk_count = len(index.get("chunks", []))
    st.markdown(
        f'<div class="ktb-hint">{chunk_count} chunks indexed &middot; '
        f'answers are grounded in the source and cite the files they came from.</div>',
        unsafe_allow_html=True,
    )

    with st.form("qa_form", clear_on_submit=False):
        question = st.text_input(
            "Your question",
            placeholder="How does the scheduler handle errors?",
            label_visibility="collapsed",
            key="qa_question",
        )
        ask = st.form_submit_button("Ask", type="primary")

    if ask and question.strip():
        with st.spinner("Searching codebase and calling claude..."):
            answer, sources = answer_question(index, question)
        st.session_state["qa_last"] = {
            "question": question,
            "answer": answer,
            "sources": sources,
            "repo": selected,
        }

    last = st.session_state.get("qa_last")
    if last and last.get("repo") == selected:
        st.markdown(
            f'<div class="ktb-answer">{html.escape(last["answer"])}</div>',
            unsafe_allow_html=True,
        )
        if last["sources"]:
            chips = "".join(
                f'<code class="ktb-source">{html.escape(s)}</code>'
                for s in last["sources"]
            )
            st.markdown(
                f'<div class="ktb-hint" style="margin-top:0.6rem;">Sources: {chips}</div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="margin-top:0.8rem;">', unsafe_allow_html=True)
    if st.button("Rebuild index", key="qa_rebuild"):
        index_path.unlink(missing_ok=True)
        st.session_state.pop("qa_last", None)
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


def render_comment_tab() -> None:
    source, submitted = render_input_form()
    if submitted:
        if not source:
            st.warning("Please enter a git URL or local path.")
        else:
            run_pipeline(source)
    render_results()


def render_gate(expected: str) -> bool:
    """Show a passcode prompt. Returns True once the visitor is authenticated."""
    if st.session_state.get("authed"):
        return True
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        """
        <div class="ktb-hero">
            <h1>KT-Buddy</h1>
            <p>Enter the access passcode to continue.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="ktb-card"><h3>Access</h3>', unsafe_allow_html=True)
    with st.form("gate_form", clear_on_submit=False):
        entered = st.text_input("Passcode", type="password", label_visibility="collapsed")
        ok = st.form_submit_button("Enter", type="primary")
    st.markdown("</div>", unsafe_allow_html=True)
    if ok:
        if entered == expected:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect passcode.")
    return False


def main() -> None:
    expected = _expected_passcode()
    if expected and not render_gate(expected):
        return

    _propagate_user_key()
    render_header()

    if _needs_byok() and not st.session_state.get("user_api_key"):
        render_byok_card()
        return

    tab_comment, tab_ask = st.tabs(["Comment & Report", "Ask"])
    with tab_comment:
        render_comment_tab()
    with tab_ask:
        render_ask_tab()


if __name__ == "__main__":
    main()
