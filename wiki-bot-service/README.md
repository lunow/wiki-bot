# Wiki Bot Service

A Telegram bot in front of an **LLM-wiki** (an [OKF v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog) markdown bundle in the `wiki/` folder of a GitHub repo). It is domain-agnostic: **one deployment = one bot + one wiki repo**, configured entirely through environment variables. Run it as many times as you have wikis.

## What it does

- **Ask** — a question is answered from the wiki. The bot reads `wiki/index.md`, lets the LLM pick the relevant pages, and answers from only those (small wikis, ≤ 8 pages, are loaded whole).
- **Save** — a note is merged into existing pages (or creates new ones). The bot adds provenance, updates `index.md` and `log.md`, and commits everything in a single commit.
- **Photos** — saved under `wiki/references/images/`, documented from the caption.
- **Voice** — transcribed with Whisper, then routed like text.
- **Routing** — an LLM classifies each text message as a question or a note. Prefix `ask:` / `save:` to force it.
- **Reminders** — optional scheduled digests, defined by you (see below).
- **PostHog** — privacy-filtered observability (pseudonymous IDs, no content by default).

## Deploy another wiki

Each deployment needs its own Telegram bot (a webhook belongs to one bot) and its own env file.

```bash
cp .env.example envs/recipes.env     # fill in; see the table below
ENV_FILE=envs/recipes.env HOST_PORT=8002 docker compose -p wiki-recipes up -d --build
```

On Coolify, create one service per wiki from the same Git source and give each its own environment variables and domain.

`examples/property.env` shows a fully configured domain (identity, guidance, reminders).

## Configuration

**Identity: what this deployment is about**

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `Wiki Bot` | Name used in prompts |
| `WIKI_DOMAIN` | `a personal knowledge base` | One phrase describing the subject; used in all prompts (e.g. `a vegan recipe collection`) |
| `QUERY_GUIDANCE` | — | Extra rules appended to the answering prompt |
| `INGEST_GUIDANCE` | — | Extra rules for how notes are filed |
| `INSTANCE_NAME` | — | Label on analytics events, so deployments can share a PostHog project |

**Required**

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/botfather) |
| `TELEGRAM_WEBHOOK_URL` | Public URL + `/telegram` |
| `TELEGRAM_WEBHOOK_SECRET` | Random string; checked on every webhook call |
| `ALLOWED_CHAT_IDS` | Comma-separated Telegram chat IDs |
| `GITHUB_TOKEN` | Fine-grained PAT, Contents read+write on the wiki repo |
| `GITHUB_REPO` | `owner/repo` |
| `LLM_API_KEY` | OpenRouter (or other OpenAI-compatible) key |

**Optional**

| Variable | Default | Description |
|---|---|---|
| `GITHUB_BRANCH` | `main` | Branch to read and commit to |
| `SAVE_RAW_NOTES` | `true` | Keep each raw note as a Source page in `references/` |
| `LLM_BASE_URL` / `LLM_MODEL` | OpenRouter / `openai/gpt-4o-mini` | LLM endpoint and model |
| `WHISPER_API_KEY` | — | OpenAI key, only for voice |
| `ADMIN_TOKEN` | — | Bearer token for `/trigger-remind/{name}` |
| `REMINDERS` / `REMINDERS_FILE` | none | Reminder digests (below) |
| `TZ` | `Europe/Berlin` | Timezone for schedules and dates |
| `POSTHOG_API_KEY`, `POSTHOG_HOST` | — / EU | Observability |
| `POSTHOG_ID_SALT` | bot token | Key for pseudonymising chat IDs |
| `POSTHOG_CAPTURE_CONTENT` | `false` | Send prompts/responses to PostHog (dev only) |

### Reminders

None by default. Define any number as JSON, inline in `REMINDERS` or in a file at `REMINDERS_FILE`:

```json
[{"name": "weekly", "cron": "0 9 * * mon", "title": "Weekly Digest",
  "focus": "tasks due within 7 days and imminent deadlines"}]
```

`cron` is a standard 5-field crontab in `TZ`. `focus` tells the bot what to look for in the wiki. Trigger one manually with `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://host/trigger-remind/weekly`.

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/telegram` | Webhook (checks the secret header, acks immediately, works in the background) |
| GET | `/health` | `{"status": "ok"}` |
| GET | `/trigger-remind/{name}` | Run a configured reminder (admin token) |

## Observability and privacy

LLM calls are sent as `$ai_generation` events and actions as business events. Chat IDs are HMAC-pseudonymised, person profiles and GeoIP are off, content is redacted to sizes, error messages are reduced to class and status, and an allowlist drops any unexpected property. Add new string properties to `SAFE_STR_KEYS` in `app/posthog_tracker.py`.

## Layout

```
app/config.py            all environment settings
app/main.py              webhook, routing, handlers, scheduler
app/wiki.py              index-first retrieval and ingest (OKF bookkeeping)
app/github_client.py     Git Data API store (blob reads, atomic commits)
app/llm_client.py        chat + Whisper
app/prompts.py           prompt templates ({domain}, {bot_name} filled from config)
app/posthog_tracker.py   privacy-filtered analytics
examples/                a sample deployment
```

## Local test

```bash
cp .env.example .env   # fill in
pip install -r requirements.txt
uvicorn app.main:app --reload
curl -X POST localhost:8000/telegram -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: $TELEGRAM_WEBHOOK_SECRET" \
  -d '{"update_id":1,"message":{"chat":{"id":12345678},"text":"ask: what do we know?"}}'
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Bot silent | Check logs for `Config:` warnings; make sure `ALLOWED_CHAT_IDS` contains your chat ID |
| Webhook 403 | `TELEGRAM_WEBHOOK_SECRET` changed: restart so the webhook is re-registered |
| Reminders not firing | Check `REMINDERS` JSON and `TZ`; startup fails loudly on invalid JSON |
| GitHub errors | PAT lacks Contents read+write, or `GITHUB_REPO` / `GITHUB_BRANCH` is wrong |
| Voice fails | `WHISPER_API_KEY` must be an OpenAI key, not an OpenRouter key |
| Wrong routing | Use the `ask:` / `save:` prefix, or tune `JEV_PROMPT` |
