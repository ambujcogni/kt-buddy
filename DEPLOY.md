# Deploying KT-Buddy

The app is packaged as a Docker container that runs Streamlit. Any host that runs
Docker images and accepts an HTTP port works — these notes target Render's free
tier because that's what we picked, but Railway, Fly.io, Cloud Run, and a plain
VM all work the same way.

## What you need before pushing

1. **An Anthropic API key — or not.** This deploy supports three modes:
   - **Owner-paid (env var set on Render):** set `ANTHROPIC_API_KEY` in the
     Render service env. Every visitor uses your key; you pay for everyone's
     tokens. Use the passcode gate (`KT_BUDDY_PASSCODE`) to limit access.
   - **BYOK / Bring Your Own Key (env var unset on Render):** the app shows a
     prompt for visitors to paste their own `sk-ant-...` key. The key lives in
     their browser session only — never written to disk or logs on the server.
     Each visitor pays for their own usage. **This is the default if you don't
     set `ANTHROPIC_API_KEY` on Render.**
   - **Local dev (CLI fallback):** when running on your laptop without an env
     var, `llm.py` shells out to `claude -p` (your Claude Code auth). Works
     locally only — the deployed container has no CLI installed.
2. **A passcode** (optional). If you set `KT_BUDDY_PASSCODE`, visitors will see
   a gate before they can use the app. **Without it the URL is fully open and
   your API key pays for every visitor's tokens.** Leave it set unless you mean
   it.
3. The repo pushed to GitHub (Render reads from a connected repo).

## Render quickstart

1. Push this repo to GitHub.
2. In Render: **New → Web Service → connect your repo → "Apply" the
   `render.yaml`.** The blueprint sets up a Docker service on the free plan.
3. On the service's **Environment** page, fill in the two secrets:
   - `ANTHROPIC_API_KEY` — your key
   - `KT_BUDDY_PASSCODE` — any string you'll share with users (delete this var
     to disable the gate)
4. Render builds the Docker image, exposes port `$PORT` (Streamlit binds there
   automatically via the `CMD` line), and gives you a `https://kt-buddy.onrender.com`
   URL.

Cold starts on the free tier are slow (~30s) because the container sleeps after
15 minutes of inactivity. The ephemeral disk also resets on restart — every
visitor effectively gets a fresh `output/` tree, which is the multi-tenant
behavior we want anyway.

## Local Docker run

To smoke-test the image before pushing:

```powershell
docker build -t kt-buddy .
docker run --rm -p 8501:8501 `
    -e ANTHROPIC_API_KEY=$env:ANTHROPIC_API_KEY `
    -e KT_BUDDY_PASSCODE=letmein `
    kt-buddy
```

Open http://localhost:8501.

## Operational gotchas

- **Cost runaway.** With the gate disabled, a single tab on a public URL can
  burn through real dollars in minutes (commenting a 200-class repo is ~200
  API calls). Keep `KT_BUDDY_PASSCODE` set; rotate it if you ever share it
  beyond the intended audience.
- **Free-tier RAM.** Render free is 512MB. Cloning a large monorepo and parsing
  it with tree-sitter can spike memory. If you hit OOM, upgrade the plan or
  cap repo size in `kt_buddy.resolve_source` before deploying for real use.
- **Persistent storage.** The container's filesystem is ephemeral — when the
  service restarts, every `output/sessions/*` tree is gone. For an MVP that's
  fine; users start fresh each visit. If you need persistence, mount a Render
  disk and point `OUTPUT_DIR` at it.
- **Local CLI workflows.** `python kt_buddy.py <repo>` and `python
  pdf_generator.py <repo>` still work on your laptop — they need
  `ANTHROPIC_API_KEY` in your environment (the old `claude -p` CLI path was
  removed during this migration).
