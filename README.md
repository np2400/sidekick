# Sidekick

A personal AI assistant you text on Telegram. It reads your Todoist tasks, Google Calendar and Gmail, can read web pages, and **asks for your approval before it creates anything**.

Built from scratch in Python (FastAPI), with no agent framework: a hand-written tool-calling loop, a provider-neutral model router, an offline test suite and a live evaluation harness.

> Status: working personal prototype (single user, runs locally behind a tunnel). Deployment, persistence and multi-user support are next.

## What it does

- **Chat on Telegram:** "What's on my calendar tomorrow?", "Add a task to call the dentist", "Summarize Sam's latest email", "/brief".
- **Approval before writes:** the model can only *propose* a task or event; code holds it until you reply yes.
- **Integrations:** Todoist (API token), Google Calendar and Gmail read-only (OAuth 2.0 with PKCE), web pages through the Fetch MCP server.
- **MCP both ways:** Sidekick uses an external MCP server, and exposes its own read-only tools to MCP clients.

## How it works

```
Telegram → webhook (secret check, owner allowlist, dedupe)
        → agent loop: model router ⇄ tools (validated; writes staged for approval)
        → reply + one JSON trace line (model, steps, tokens, timing; no private content)
```

- `app/agent.py`: tool-calling loop, tool schemas, approval gate, crash containment.
- `app/llm/`: router with retries, fallback across Gemini and Groq, circuit breaker, privacy routing (email content only to allowed providers), cost estimates.
- `app/trace.py`, `app/logging_setup.py`: per-message trace and correlation ids on every log line.
- `evals/`: synthetic dataset, scorers and runner for comparing models.

## Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your keys (see comments in the file)
uvicorn app.main:app --reload
```

To talk to it from Telegram, expose the server with a tunnel (e.g. `cloudflared tunnel --url http://127.0.0.1:8000`) and register the URL:

```bash
python -m app.set_webhook https://<your-tunnel>.trycloudflare.com
python -m app.google_auth   # one-time Google sign-in for Calendar/Gmail
```

## Tests and evaluations

```bash
pytest -q                                          # offline: fake world, scripted model, no network
python -m evals.run --models groq:qwen/qwen3.8-27b # live: real model, synthetic data only
```

The offline suite checks the rules that must never break (no write without approval, no private content in logs, a crashing tool can't crash the agent, duplicate deliveries are ignored). The live evals score each model on task success, safety and efficiency, and write a report to `evals/reports/`.

## Security

Secrets live in `.env` and Google token files, which are git-ignored. Logs record metadata only. Gmail access is read-only by scope, and content from emails and web pages is treated as untrusted input.
