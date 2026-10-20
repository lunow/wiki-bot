import os
import re
import hmac
import logging
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import httpx

from .github_client import WikiStore
from .wiki import WikiService, IngestError
from .llm_client import LLMClient
from .prompts import QUERY_PROMPT, REMIND_PROMPT, JEV_PROMPT
from .posthog_tracker import track_event, error_props

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs contain the Telegram bot token
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────
from . import config as cfg
from .config import (
    ADMIN_TOKEN, ALLOWED_CHAT_IDS, BOT_NAME, GITHUB_BRANCH, GITHUB_REPO, GITHUB_TOKEN,
    INGEST_GUIDANCE, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MAX_FILE_BYTES, QUERY_GUIDANCE,
    REMINDERS, SAVE_RAW_NOTES, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_URL, TZ, WEBHOOK_SECRET,
    WHISPER_API_KEY, WIKI_DOMAIN,
)

for _problem in cfg.validate():
    logging.getLogger(__name__).warning(f"Config: {_problem}")
_seen_updates: deque = deque(maxlen=1000)
REMINDER_BY_NAME = {r["name"]: r for r in REMINDERS}


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TZ))

# ── Clients ─────────────────────────────────────────────────────────
wiki_store = WikiStore(token=GITHUB_TOKEN, repo=GITHUB_REPO, branch=GITHUB_BRANCH)
llm = LLMClient(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
    model=LLM_MODEL,
    site_name=BOT_NAME,
    whisper_api_key=WHISPER_API_KEY,
)
wiki = WikiService(
    wiki_store, llm, now_local, save_raw_notes=SAVE_RAW_NOTES,
    domain=WIKI_DOMAIN, ingest_guidance=INGEST_GUIDANCE,
)
tg_bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

# ── FastAPI ─────────────────────────────────────────────────────────
app = FastAPI(title=BOT_NAME)
scheduler = AsyncIOScheduler(timezone=TZ)


# ── Helpers ─────────────────────────────────────────────────────────
def check_auth(chat_id: int):
    if chat_id not in ALLOWED_CHAT_IDS:
        raise HTTPException(status_code=403, detail="Unauthorized chat ID")


async def send_message(chat_id: int, text: str):
    if not tg_bot:
        logger.warning("Telegram bot not configured")
        return
    text = text[:4095]
    try:
        try:
            await tg_bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            # LLM output often has unbalanced Markdown; retry as plain text
            logger.warning(f"Markdown send failed ({e}); retrying as plain text")
            await tg_bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        track_event(
            distinct_id=chat_id,
            event="telegram_send_error",
            properties={**error_props(e), "text_length": len(text)},
        )


async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """Download a file from Telegram servers. Returns (bytes, file_path)."""
    # Step 1: Get file path
    getfile_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    async with httpx.AsyncClient() as client:
        resp = await client.get(getfile_url, params={"file_id": file_id}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram getFile failed")
        if data["result"].get("file_size", 0) > MAX_FILE_BYTES:
            raise RuntimeError("File too large")
        file_path = data["result"]["file_path"]

    # Step 2: Download file
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(download_url, timeout=60.0)
        resp.raise_for_status()
        return resp.content, file_path


# ── JEV & Intent ────────────────────────────────────────────────────
async def classify_intent(text: str, chat_id: int) -> str:
    """JEV: Judge Every incoming message. Returns QUERY, INGEST, or UNCLEAR."""
    system = "You are a strict intent classifier. Output exactly one word: QUERY, INGEST, or UNCLEAR. No punctuation, no explanation."
    user = JEV_PROMPT.format(text=text, domain=WIKI_DOMAIN)
    try:
        result = await llm.chat(
            system=system,
            user=user,
            temperature=0.0,
            chat_id=chat_id,
            metadata={"jev": True, "intent_classification": True},
        )
    except Exception as e:
        logger.error(f"JEV call failed: {e}")
        return "UNCLEAR"
    raw = result.strip().upper().rstrip(".")
    if raw in ("QUERY", "INGEST", "UNCLEAR"):
        return raw
    logger.warning(f"JEV returned unexpected value: {result!r}, falling back to heuristics")
    if "?" in text or re.search(r"\b(when|how much|how many|what|where|why|who|which)\b", text.lower()):
        return "QUERY"
    return "INGEST"


# ── Handlers ────────────────────────────────────────────────────────
async def handle_query(chat_id: int, text: str):
    """Answer a question from the wiki (index-first retrieval)."""
    check_auth(chat_id)
    track_event(
        distinct_id=chat_id,
        event="query_received",
        properties={"question_length": len(text)},
    )

    wiki_context, pages = await wiki.context_for(text, chat_id, label="query")
    track_event(
        distinct_id=chat_id,
        event="wiki_retrieved",
        properties={"page_count": len(pages), "context_length": len(wiki_context)},
    )

    system = QUERY_PROMPT.format(
        bot_name=BOT_NAME,
        domain=WIKI_DOMAIN,
        today=now_local().isoformat(),
        wiki_content=wiki_context,
        extra_rules=QUERY_GUIDANCE,
    )
    answer = await llm.chat(
        system=system,
        user=text,
        chat_id=chat_id,
        metadata={"query": True, "model": LLM_MODEL},
    )

    await send_message(chat_id, answer)
    track_event(
        distinct_id=chat_id,
        event="query_answered",
        properties={"answer_length": len(answer)},
    )


async def handle_ingest(chat_id: int, text: str, *, attachments=None, label="Telegram note", extra=""):
    """Merge a note into the wiki and commit it."""
    check_auth(chat_id)
    track_event(
        distinct_id=chat_id,
        event="ingest_received",
        properties={"content_length": len(text)},
    )
    try:
        result = await wiki.ingest(
            chat_id, text, label=label, attachments=attachments, extra_note_context=extra
        )
    except IngestError as e:
        track_event(distinct_id=chat_id, event="ingest_rejected", properties=error_props(e))
        await send_message(chat_id, f"❌ I couldn't save that safely: {e}\nNothing was changed.")
        return
    except Exception as e:
        logger.error(f"Ingest failed: {e}")
        track_event(distinct_id=chat_id, event="ingest_commit_error", properties=error_props(e))
        raise

    track_event(
        distinct_id=chat_id,
        event="ingest_committed",
        properties={"created_count": len(result["created"]), "updated_count": len(result["updated"])},
    )
    lines = ["✅ Saved."]
    lines += [f"• created `{p}`" for p in result["created"]]
    lines += [f"• updated `{p}`" for p in result["updated"]]
    await send_message(chat_id, "\n".join(lines))


async def handle_photo(chat_id: int, photo_sizes: list, caption: str | None):
    """Download photo; commit image + wiki pages in one commit."""
    check_auth(chat_id)
    track_event(
        distinct_id=chat_id,
        event="photo_received",
        properties={"has_caption": bool(caption), "caption_length": len(caption or "")},
    )

    photo = photo_sizes[-1]  # largest
    file_id = photo["file_id"]

    try:
        file_bytes, file_path = await download_telegram_file(file_id)
        ext = os.path.splitext(file_path)[1] or ".jpg"
        stamp = now_local().strftime("%Y%m%d_%H%M%S")
        image_name = f"photo_{stamp}_{chat_id}{ext}"
        image_path = f"wiki/references/images/{image_name}"
        image_link = f"/references/images/{image_name}"

        note = f"Photo uploaded: ![photo]({image_link})\n\nCaption: {caption or '(none)'}"
        extra = (
            "\n\n(You cannot see the image. Describe only what the caption says; "
            "do not guess the photo's contents.)"
        )
        await handle_ingest(
            chat_id, note, attachments={image_path: file_bytes}, label="Telegram photo", extra=extra
        )
    except Exception as e:
        logger.error(f"Photo handling failed: {e}")
        track_event(
            distinct_id=chat_id,
            event="photo_error",
            properties=error_props(e),
        )
        await send_message(chat_id, f"❌ Sorry, I couldn't process the photo: {e}")


async def handle_voice(chat_id: int, voice: dict):
    """Download voice, transcribe with Whisper, then route as text message."""
    check_auth(chat_id)
    file_id = voice["file_id"]
    duration = voice.get("duration", 0)

    track_event(
        distinct_id=chat_id,
        event="voice_received",
        properties={"duration_seconds": duration, "file_size": voice.get("file_size")},
    )

    try:
        # Download voice file
        file_bytes, file_path = await download_telegram_file(file_id)
        track_event(
            distinct_id=chat_id,
            event="voice_downloaded",
            properties={"size_bytes": len(file_bytes)},
        )

        # Transcribe with Whisper
        await send_message(chat_id, "🎙️ Transcribing voice message...")
        transcribed = await llm.transcribe(
            audio_bytes=file_bytes,
            filename=f"voice_{chat_id}.ogg",
            chat_id=chat_id,
            metadata={"duration_seconds": duration},
        )

        track_event(
            distinct_id=chat_id,
            event="voice_transcribed",
            properties={"transcript_length": len(transcribed)},
        )

        # Send the transcription back so the user knows what was understood
        await send_message(chat_id, f'📝 I heard: *"{transcribed}"*')

        # Route the transcribed text through normal JEV flow
        intent = await classify_intent(transcribed, chat_id)
        logger.info(f"JEV on voice transcript chat_id={chat_id} intent={intent} text={transcribed[:50]!r}")
        track_event(
            distinct_id=chat_id,
            event="intent_classified_voice",
            properties={"intent": intent, "transcript_length": len(transcribed)},
        )

        if intent == "QUERY":
            await handle_query(chat_id, transcribed)
        elif intent == "INGEST":
            await handle_ingest(chat_id, transcribed)
        else:
            await handle_unclear(chat_id, transcribed)

    except Exception as e:
        logger.error(f"Voice handling failed: {e}")
        track_event(
            distinct_id=chat_id,
            event="voice_error",
            properties=error_props(e),
        )
        await send_message(chat_id, f"❌ Sorry, I couldn't process the voice message: {e}")


async def handle_unclear(chat_id: int, text: str):
    """Ask the user to clarify whether they want to ask or save."""
    msg = (
        "🤔 I'm not sure if you're *asking* something or *telling* me something new.\n\n"
        "Reply with:\n"
        "• ask: your question\n"
        "• save: your note"
    )
    await send_message(chat_id, msg)
    track_event(
        distinct_id=chat_id,
        event="intent_unclear",
        properties={"text_length": len(text)},
    )


async def run_reminder(name: str):
    """Generate and send one configured reminder digest."""
    reminder = REMINDER_BY_NAME[name]
    track_event(
        distinct_id=0,
        event="reminder_triggered",
        properties={"cadence": name},
    )
    try:
        wiki_context, _ = await wiki.context_for(
            f"Find everything relevant to a '{name}' reminder digest. Focus: {reminder['focus']}",
            None,
            max_pages=12,
            label="reminder",
        )
        system = f"You are {BOT_NAME}, an assistant for {WIKI_DOMAIN}."
        user = REMIND_PROMPT.format(
            today=now_local().isoformat(),
            today_date=now_local().strftime("%Y-%m-%d"),
            wiki_content=wiki_context,
            name=name,
            focus=reminder["focus"],
            title=reminder["title"],
        )
        message = await llm.chat(
            system=system,
            user=user,
            metadata={"reminder": True, "cadence": name, "model": LLM_MODEL},
        )
        for cid in ALLOWED_CHAT_IDS:
            await send_message(cid, message)
            track_event(
                distinct_id=cid,
                event="reminder_sent",
                properties={"cadence": name, "message_length": len(message)},
            )
    except Exception as e:
        logger.error(f"Reminder ({name}) failed: {e}")
        track_event(
            distinct_id=0,
            event="reminder_error",
            properties={"cadence": name, **error_props(e)},
        )


# ── Scheduler Jobs ──────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # Register Telegram webhook
    if tg_bot and TELEGRAM_WEBHOOK_URL:
        await tg_bot.set_webhook(url=TELEGRAM_WEBHOOK_URL, secret_token=WEBHOOK_SECRET or None)
        logger.info(f"Telegram webhook set to {TELEGRAM_WEBHOOK_URL}")

    # Schedule reminders (configured via REMINDERS / REMINDERS_FILE)
    for r in REMINDERS:
        scheduler.add_job(
            run_reminder,
            CronTrigger.from_crontab(r["cron"], timezone=TZ),
            args=[r["name"]],
            id=f"reminder_{r['name']}",
            replace_existing=True,
        )
    scheduler.start()
    logger.info("Scheduler started")
    track_event(
        distinct_id=0,
        event="bot_startup",
        properties={
            "model": LLM_MODEL,
            "allowed_chat_count": len(ALLOWED_CHAT_IDS),
            "scheduler_job_count": len(scheduler.get_jobs()),
        },
    )


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    await wiki_store.close()
    if tg_bot:
        await tg_bot.delete_webhook()
    track_event(distinct_id=0, event="bot_shutdown", properties={})


# ── Routes ──────────────────────────────────────────────────────────
async def process_update(data: dict):
    """Handle one Telegram update. Never raises (Telegram would redeliver)."""
    chat_id = None
    try:
        msg = data.get("message", {})
        if not msg:
            return

        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        chat_type = msg.get("chat", {}).get("type", "private")
        photo = msg.get("photo", [])
        voice = msg.get("voice")
        caption = msg.get("caption")

        if not chat_id:
            return

        track_event(
            distinct_id=chat_id,
            event="telegram_message_received",
            properties={
                "text_length": len(text),
                "chat_type": chat_type,
                "has_photo": bool(photo),
                "has_voice": bool(voice),
                "has_caption": bool(caption),
            },
        )

        if chat_id not in ALLOWED_CHAT_IDS:
            logger.warning(f"Unauthorized chat_id: {chat_id}")
            track_event(
                distinct_id=chat_id,
                event="unauthorized_access_attempt",
                properties={"text_length": len(text), "has_photo": bool(photo), "has_voice": bool(voice)},
            )
            return

        if voice:
            await handle_voice(chat_id, voice)
            return
        if photo:
            await handle_photo(chat_id, photo, caption)
            return
        if not text:
            return

        # Explicit prefixes skip the classifier
        prefix = re.match(r"^\s*(ask|save)\s*:\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
        if prefix:
            intent = "QUERY" if prefix.group(1).lower() == "ask" else "INGEST"
            text = prefix.group(2).strip()
        else:
            intent = await classify_intent(text, chat_id)
        logger.info(f"JEV chat_id={chat_id} intent={intent} text_length={len(text)}")
        track_event(
            distinct_id=chat_id,
            event="intent_classified",
            properties={"intent": intent, "text_length": len(text)},
        )

        if intent == "QUERY":
            await handle_query(chat_id, text)
        elif intent == "INGEST":
            await handle_ingest(chat_id, text)
        else:
            await handle_unclear(chat_id, text)
    except Exception as e:
        logger.exception("Update processing failed")
        track_event(distinct_id=chat_id, event="update_error", properties=error_props(e))
        if chat_id in ALLOWED_CHAT_IDS:
            await send_message(chat_id, f"❌ Sorry, something went wrong: {e}")


@app.post("/telegram")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    """Receive Telegram updates. Acks immediately; work runs in the background."""
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        logger.warning("TELEGRAM_WEBHOOK_SECRET not set; webhook is unauthenticated")

    data = await request.json()

    update_id = data.get("update_id")
    if update_id is not None:
        if update_id in _seen_updates:
            return {"ok": True}
        _seen_updates.append(update_id)

    background.add_task(process_update, data)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/trigger-remind/{cadence}")
async def manual_remind(cadence: str, request: Request):
    """Manually trigger a configured reminder. Requires ADMIN_TOKEN as a Bearer token."""
    auth = request.headers.get("Authorization", "")
    if not ADMIN_TOKEN or not hmac.compare_digest(auth, f"Bearer {ADMIN_TOKEN}"):
        raise HTTPException(status_code=403, detail="Forbidden")
    if cadence not in REMINDER_BY_NAME:
        raise HTTPException(status_code=400, detail="Unknown reminder")
    await run_reminder(cadence)
    return {"triggered": cadence}
