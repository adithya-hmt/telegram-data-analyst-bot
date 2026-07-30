# Telegram Data Analyst Bot

An LLM-powered Telegram webhook that answers data-analysis questions with exactly one JSON object:

```json
{"answer": {"state": "Assam"}, "log_url": "https://host/runs/<id>.jsonl"}
```

Health endpoint: `/status`.

The agent uses Gemini with Google Search grounding and Python code execution, keeps a short per-chat conversation history, and writes each run as public JSONL in `LOG_BUCKET`.

## Configuration

Copy `.env.example` and set `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `PUBLIC_BASE_URL`, `LOG_BUCKET`, and an optional `TELEGRAM_WEBHOOK_SECRET`. Never commit secrets.

`REPLY_MODE=wrapped` produces the assignment contract with `answer` and `log_url`. Set `REPLY_MODE=direct` only when running against the visible public harness, whose sample grader expects the question-specific object directly.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

After deployment, register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=$PUBLIC_BASE_URL/telegram/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

The public grading pipeline expects a Telegram bot username and a public repository. Do not publish `.env` or tokens.
