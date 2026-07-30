import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from google import genai
from google.genai import types
from google.cloud import storage

app = FastAPI(title="Telegram Data Analyst")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LOG_BUCKET = os.environ.get("LOG_BUCKET", "")
REPLY_MODE = os.environ.get("REPLY_MODE", "wrapped")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_HISTORY = 8
conversations: dict[str, list[dict[str, str]]] = {}
runs: dict[str, str] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_from_model(text: str) -> Any:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


def analyst_prompt(history: list[dict[str, str]]) -> str:
    transcript = "\n".join(f"{item['role']}: {item['text']}" for item in history)
    return f"""You are a rigorous data analyst answering a Telegram grading question.

Conversation:
{transcript}

Rules:
- Answer the LAST user message, using earlier turns as context.
- Use Google Search when the question points to a public dataset or current source.
- Use Python code execution for arithmetic, tabulation, filtering, ranking, and statistics.
- Treat inline data as authoritative unless the question asks for an external source.
- Follow the requested answer shape exactly. If the user asks for a JSON object with a
  particular key, return that object. If they ask for a scalar, array, or string, return
  that JSON value.
- Return valid JSON only: no Markdown fences, prose, citations outside the JSON, or
  additional keys. This is the answer value that will be wrapped by the bot.
"""


async def ask_gemini(history: list[dict[str, str]]) -> tuple[Any, str]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=MODEL,
        contents=analyst_prompt(history),
        config=types.GenerateContentConfig(
            temperature=0.1,
            tools=[
                types.Tool(google_search=types.GoogleSearch()),
                types.Tool(code_execution=types.ToolCodeExecution),
            ],
        ),
    )
    raw = response.text or ""
    return json_from_model(raw), raw


async def send_message(chat_id: int | str, text: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


async def publish_log(run_id: str, record: dict[str, Any]) -> str:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    runs[run_id] = line
    if LOG_BUCKET:
        try:
            client = storage.Client()
            blob = client.bucket(LOG_BUCKET).blob(f"runs/{run_id}.jsonl")
            blob.upload_from_string(line, content_type="application/x-ndjson")
            return f"https://storage.googleapis.com/{LOG_BUCKET}/runs/{run_id}.jsonl"
        except Exception:
            record["log_upload"] = "fallback_in_memory"
    return f"{PUBLIC_BASE_URL}/runs/{run_id}.jsonl"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/runs/{run_id}.jsonl", response_class=PlainTextResponse)
async def run_log(run_id: str) -> str:
    line = runs.get(run_id)
    if line is None:
        raise HTTPException(status_code=404, detail="run not found")
    return line


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, bool]:
    if WEBHOOK_SECRET and not secrets.compare_digest(x_telegram_bot_api_secret_token or "", WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    update = await request.json()
    message = update.get("message") or update.get("edited_message") or {}
    text = message.get("text")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return {"ok": True}

    key = str(chat_id)
    history = conversations.setdefault(key, [])
    history.append({"role": "user", "text": text})
    history[:] = history[-MAX_HISTORY:]
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    record: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": now(),
        "chat_id": chat_id,
        "question": text,
        "model": MODEL,
    }
    try:
        answer, raw = await ask_gemini(history)
        record.update({"answer": answer, "model_output": raw, "status": "ok"})
        history.append({"role": "assistant", "text": json.dumps(answer, ensure_ascii=False)})
        log_url = await publish_log(run_id, record)
        reply_value = {"answer": answer, "log_url": log_url} if REPLY_MODE == "wrapped" else answer
        reply = json.dumps(reply_value, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        record.update({"status": "error", "error": type(exc).__name__})
        log_url = await publish_log(run_id, record)
        reply = json.dumps({"answer": {"error": "analysis_failed"}, "log_url": log_url}, separators=(",", ":"))
    await send_message(chat_id, reply)
    return {"ok": True}
