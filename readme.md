# Setup Guide — Wiki Bot

A Telegram bot for an LLM-wiki in a GitHub repo. One deployment serves one wiki; repeat the steps for each additional wiki. Architecture and decisions: [PLAN.md](PLAN.md). All settings: [wiki-bot-service/README.md](wiki-bot-service/README.md).

## What you need (per wiki)

| Item | How to get it |
|---|---|
| **A wiki repo** | A GitHub repo with a `wiki/` folder created by the `wiki` skill (`index.md`, `log.md`, `entities/`, `concepts/`, `syntheses/`, `references/`) |
| **Telegram bot token** | [@BotFather](https://t.me/botfather) → new bot. Use a separate bot per wiki. |
| **GitHub fine-grained PAT** | https://github.com/settings/personal-access-tokens/new → only the wiki repo → `Contents`: Read and write |
| **LLM API key** | https://openrouter.ai/keys (or any OpenAI-compatible endpoint) |
| **OpenAI key** *(voice only)* | https://platform.openai.com/api-keys. OpenRouter keys do not work for Whisper. |
| **PostHog key** *(optional)* | Project settings → Project API Key |
| **A host** | Coolify or any Docker host with a public HTTPS domain |

## 1. Configure

```bash
cd wiki-bot-service
mkdir -p envs
cp .env.example envs/<name>.env      # e.g. envs/recipes.env
```

Fill in `envs/<name>.env`. Minimum:

```
BOT_NAME=Recipe Bot
WIKI_DOMAIN=a vegan recipe collection
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_URL=https://recipes-bot.example.com/telegram
TELEGRAM_WEBHOOK_SECRET=<random string: letters, digits, _ and ->
ALLOWED_CHAT_IDS=            # fill in at step 3
GITHUB_TOKEN=...
GITHUB_REPO=yourname/recipes-wiki
LLM_API_KEY=...
```

- `WIKI_DOMAIN` is one phrase describing the subject. It appears in every prompt, so make it specific.
- `INGEST_GUIDANCE` and `QUERY_GUIDANCE` add rules for this wiki (for example "always record the source URL").
- Generate secrets with `openssl rand -hex 24` (`TELEGRAM_WEBHOOK_SECRET`, `ADMIN_TOKEN`).
- Set `INSTANCE_NAME` if several deployments share one PostHog project.

### Reminders (optional)

None by default. To enable, point `REMINDERS_FILE` at a JSON file (or put the JSON in `REMINDERS`):

```json
[{"name": "weekly", "cron": "0 9 * * mon", "title": "Weekly Digest",
  "focus": "tasks due within 7 days and imminent deadlines"}]
```

`examples/property.env` and `examples/property-reminders.json` show a complete configuration. Files under `examples/` are copied into the image at `/app/examples/`; for your own file, add it there or use `REMINDERS` inline.

## 2. Deploy

**Docker host:**

```bash
ENV_FILE=envs/recipes.env HOST_PORT=8002 docker compose -p wiki-recipes up -d --build
```

Put a reverse proxy with HTTPS in front of `127.0.0.1:8002`. Use a different `-p` name and `HOST_PORT` for every wiki.

**Coolify:** create one service per wiki from the same Git source (root `Bot/wiki-bot-service`), paste the env file contents into the environment panel, assign the domain.

On startup the bot registers its Telegram webhook with the secret. Check the logs for `Config:` warnings, which list missing required settings.

## 3. Find your chat ID

1. Send any message to the bot.
2. The logs show `Unauthorized chat_id: 12345678`.
3. Put the IDs (comma-separated, no spaces) in `ALLOWED_CHAT_IDS` and redeploy.

## 4. Verify

```bash
curl https://recipes-bot.example.com/health          # {"status":"ok"}

# only if reminders are configured:
curl -H "Authorization: Bearer $ADMIN_TOKEN" https://recipes-bot.example.com/trigger-remind/weekly
```

Then in Telegram: `ask: what do we have so far?`, then save a note and check the new commit in the repo.

## Using the bot

| You send | The bot |
|---|---|
| A question | Reads `index.md`, picks the relevant pages, answers with page citations |
| A statement or note | Merges it into existing pages (or creates new ones), updates `index.md` and `log.md`, commits once, replies with what it created or updated |
| `ask: …` / `save: …` | Skips classification and does exactly that |
| A photo (+ caption) | Stores the image under `wiki/references/images/` and documents it from the caption (the bot cannot see the image) |
| A voice message | Transcribes (Whisper), shows what it heard, then handles it like text |
| Something ambiguous | Asks you to use `ask:` or `save:` |

If an ingest is rejected (invalid output from the model, a path outside the allowed folders, an attempt to overwrite an unseen page), the bot says so and **nothing is committed**.

## Observability (PostHog)

LLM calls arrive as `$ai_generation` events; actions arrive as business events. Content is redacted to sizes, chat IDs are pseudonymous, and error messages are reduced to class and status.

| Event | Properties (all non-identifying) |
|---|---|
| `$ai_generation` | model, provider, latency, tokens, estimated cost (known models), content sizes |
| `telegram_message_received` | text_length, chat_type, has_photo/voice/caption |
| `intent_classified`, `intent_classified_voice`, `intent_unclear` | intent, lengths |
| `query_received`, `wiki_retrieved`, `query_answered` | lengths, page_count |
| `ingest_received`, `ingest_committed`, `ingest_rejected`, `ingest_commit_error` | content_length, created_count, updated_count, error_type |
| `photo_received`, `photo_error` | has_caption, caption_length, error_type |
| `voice_received`, `voice_downloaded`, `voice_transcribed`, `voice_error` | duration, sizes, error_type |
| `reminder_triggered`, `reminder_sent`, `reminder_error` | cadence (the reminder name), message_length |
| `unauthorized_access_attempt`, `update_error`, `telegram_send_error`, `llm_error` | lengths, error_type, http_status |
| `bot_startup`, `bot_shutdown` | model, allowed_chat_count, scheduler_job_count |

Every event carries `instance` when `INSTANCE_NAME` is set, so one project can host several deployments. Useful insights: cost and latency per `instance`, intent distribution, error counts by `error_type`, reminders triggered vs sent.

## Troubleshooting

| Problem | Likely cause |
|---|---|
| Bot is silent | `ALLOWED_CHAT_IDS` missing; check logs for `Unauthorized chat_id` and `Config:` warnings |
| Webhook returns 403 | `TELEGRAM_WEBHOOK_SECRET` changed or differs from what Telegram has; restart to re-register |
| Answers miss information | `index.md` descriptions are vague or stale: fix the page `description`s; pages missing from the index are still offered to the model |
| "I couldn't save that safely" | The model's output failed validation; nothing was changed. Retry, or add `INGEST_GUIDANCE`. |
| New note not visible | Check the repo for the commit; the next query reads the current branch head |
| Reminders not firing | Invalid `REMINDERS` JSON (startup fails loudly), wrong `TZ`, or none configured |
| GitHub errors | PAT lacks Contents read+write, wrong `GITHUB_REPO` / `GITHUB_BRANCH`, or branch protection blocks direct pushes |
| LLM errors | Check the key and `LLM_BASE_URL` / `LLM_MODEL` |
| Voice fails | `WHISPER_API_KEY` must be an OpenAI key |
| Wrong routing | Use the `ask:` / `save:` prefix, or tune `JEV_PROMPT` in `app/prompts.py` |
| PostHog empty | Check `POSTHOG_API_KEY` and `POSTHOG_HOST` |

## Backlog

See [PLAN.md](PLAN.md#backlog).
