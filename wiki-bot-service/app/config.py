"""All deployment-specific settings. One deployment = one env file = one wiki repo."""
import json
import os
from typing import Dict, List


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v == "" else v.lower() not in ("false", "0", "no")


def _load_reminders() -> List[Dict[str, str]]:
    """Reminder digests, as a JSON list from REMINDERS or the file REMINDERS_FILE.

    Each item: {"name": "weekly", "cron": "0 9 * * mon", "focus": "what to look for",
                "title": "Weekly Digest"}  (title optional)
    """
    raw = os.getenv("REMINDERS", "").strip()
    path = os.getenv("REMINDERS_FILE", "").strip()
    if not raw and path:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    if not raw:
        return []
    items = json.loads(raw)
    names = set()
    for it in items:
        for key in ("name", "cron", "focus"):
            if not it.get(key):
                raise ValueError(f"REMINDERS item missing '{key}': {it}")
        if it["name"] in names:
            raise ValueError(f"Duplicate reminder name: {it['name']}")
        names.add(it["name"])
        it.setdefault("title", it["name"].capitalize() + " Digest")
    return items


# ── Identity / domain (what this deployment is about) ───────────────
BOT_NAME = os.getenv("BOT_NAME", "Wiki Bot")
WIKI_DOMAIN = os.getenv("WIKI_DOMAIN", "a personal knowledge base")
# Optional extra rules appended to the answer / ingest prompts
QUERY_GUIDANCE = os.getenv("QUERY_GUIDANCE", "")
INGEST_GUIDANCE = os.getenv("INGEST_GUIDANCE", "")
# Optional label added to analytics events so several deployments can share a PostHog project
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "")

# ── Telegram ────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
ALLOWED_CHAT_IDS = [
    int(x.strip()) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
]
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# ── GitHub (the wiki repo) ──────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
# Keep each raw note as a references/ Source page with provenance hashes
SAVE_RAW_NOTES = _bool("SAVE_RAW_NOTES", True)

# ── LLM ─────────────────────────────────────────────────────────────
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY") or LLM_API_KEY

# ── Misc ────────────────────────────────────────────────────────────
TZ = os.getenv("TZ", "Europe/Berlin")
MAX_FILE_BYTES = 20 * 1024 * 1024
REMINDERS = _load_reminders()


def validate() -> List[str]:
    """Return human-readable problems with the configuration (empty = fine)."""
    problems = []
    for name in ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", "GITHUB_REPO", "LLM_API_KEY"):
        if not globals()[name]:
            problems.append(f"{name} is not set")
    if not ALLOWED_CHAT_IDS:
        problems.append("ALLOWED_CHAT_IDS is empty: every message will be ignored")
    if not WEBHOOK_SECRET:
        problems.append("TELEGRAM_WEBHOOK_SECRET is not set: webhook is unauthenticated")
    return problems
