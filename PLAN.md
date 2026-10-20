# Wiki Bot — Architecture & Decisions

A Telegram bot in front of an **LLM-wiki** (OKF v0.1 markdown bundle in the `wiki/` folder of a GitHub repo, created and maintained with the `wiki` skill). The service is domain-agnostic: **one deployment = one bot + one wiki repo**, configured only through environment variables. Property management is just one configuration (`wiki-bot-service/examples/`).

```
Telegram ──► wiki-bot-service (one container per wiki)
               │
               ├── POST /telegram  (secret header → ack → background task)
               │     ├── text   → prefix ask:/save:  or  JEV → QUERY | INGEST | UNCLEAR
               │     ├── photo  → download → ingest (image + pages in one commit)
               │     └── voice  → download → Whisper → same routing as text
               │
               ├── WikiService
               │     ├── query:  index.md → LLM picks pages → answer from those pages
               │     └── ingest: index.md → LLM picks related pages → LLM merges note
               │                → bot adds provenance, index.md, log.md → one commit
               │
               ├── APScheduler → optional reminders from REMINDERS JSON
               └── PostHog (privacy-filtered events + LLM generations)
        ▲                          │
        └──── GitHub Git Data API ◄┘  (blob reads, atomic commits)    OpenRouter / OpenAI
```

One container. No database. All state lives in the GitHub repo.

## Key decisions

| Decision | Rationale |
|---|---|
| **Standalone FastAPI** | No workflow engine; logic is code that can be tested and reviewed |
| **Env-only configuration** | Deploying a second wiki means a new env file and a new bot token, not a code change |
| **Index-first retrieval** | Follows the wiki skill: `index.md` lists every page with a description, so the model picks pages instead of reading all of them. Cost stays flat as the wiki grows. Wikis of ≤ 8 pages are loaded whole. |
| **Merge, don't append** | Ingest does entity resolution first: it updates existing pages and creates new ones only for new topics. Conflicts are marked `> ⚠️ Conflict:`. |
| **Bot owns bookkeeping** | Provenance (`sources` + `sha256`), `timestamp`, `index.md` and `log.md` are written by code, not the model, and the model's output is validated before anything is committed |
| **One atomic commit per ingest** | Git Data API (tree + commit + fast-forward ref update): pages, index, log and any image land together or not at all |
| **Raw notes optional** | `SAVE_RAW_NOTES` keeps each note as a Source page (provenance, verbatim text); turn it off for a leaner repo |
| **JEV routing** | An LLM call classifies each message. `ask:` / `save:` prefixes skip it. UNCLEAR asks the user to use a prefix. |
| **Reminders are configuration** | A JSON list of `{name, cron, focus, title}`; none by default |
| **OpenRouter for LLM, OpenAI for Whisper** | One key for many models; OpenRouter has no audio transcription |
| **GitHub as source of truth** | Everything versioned and reviewable |
| **Privacy-filtered PostHog** | See Security |

## Message routing

```
Telegram update
  ├── forged / replayed?    → 403 (wrong secret) or ignored (duplicate update_id)
  ├── chat not allowed?     → logged, no reply
  ├── voice                 → Whisper → text → (below)
  ├── photo                 → ingest with image attachment
  └── text
        ├── "ask: …"        → QUERY
        ├── "save: …"       → INGEST
        └── otherwise       → JEV → QUERY | INGEST | UNCLEAR
```

## Ingest pipeline (details)

1. Snapshot the wiki tree. Pick related pages (all pages if the wiki is small).
2. LLM returns full content for each page it creates or updates (`=== FILE: … ===` blocks).
3. Validate: at most 6 pages; new paths must match `wiki/{entities,concepts,syntheses}/<kebab>.md`; the model may only overwrite pages it was shown; frontmatter must parse and have a `type`.
4. Assemble: keep existing frontmatter and `sources`, set `timestamp`, append the new `sources` entry (when raw notes are saved).
5. Update `index.md` (replace or add the entry in the right section) and prepend a `log.md` entry under today's date.
6. Commit everything in one commit. Any validation failure aborts with nothing changed.

## Security & privacy

- **Webhook secret** (`TELEGRAM_WEBHOOK_SECRET`) checked on every call; chat-ID whitelist; duplicate `update_id`s ignored.
- `/health` exposes nothing; `/trigger-remind/{name}` needs `ADMIN_TOKEN`.
- Log levels for `httpx` / `httpcore` are raised so the bot token never appears in URLs in logs.
- Files from Telegram are capped at 20 MB; the container runs as a non-root user; the port binds to `127.0.0.1`.
- **PostHog:** chat IDs are HMAC-pseudonymised; person profiles and GeoIP are off; prompts and responses are redacted to sizes unless `POSTHOG_CAPTURE_CONTENT=true`; error messages reduce to class and HTTP status; an allowlist drops any other property.
- Content still reaches the LLM provider, Whisper (voice) and GitHub. Choose providers and repo visibility accordingly; photos in the repo stay in its history.

## Cost (gpt-4o-mini, indicative)

| Activity | Calls | Rough cost |
|---|---|---|
| Routing | 1 small call per text message | ~$0.00002 |
| Query | 1 page-selection call (wikis > 8 pages) + 1 answer call | ~$0.001 |
| Ingest | 1 page-selection call (wikis > 8 pages) + 1 merge call | ~$0.002 |
| Reminder | 1 selection call + 1 digest call per reminder | ~$0.001 |
| Voice | Whisper | ~$0.006 / minute |

Cost no longer grows with the size of the wiki, only with the pages selected (capped at 60k characters).

## Repository layout

```
Bot/
├── PLAN.md                    # this file
├── readme.md                  # deployment guide
└── wiki-bot-service/
    ├── app/
    │   ├── config.py          # every environment setting; reminders loader
    │   ├── main.py            # webhook, routing, handlers, scheduler
    │   ├── wiki.py            # retrieval + ingest + index/log bookkeeping
    │   ├── github_client.py   # WikiStore: Git Data API reads/commits
    │   ├── llm_client.py      # chat + Whisper
    │   ├── prompts.py         # templates filled from BOT_NAME / WIKI_DOMAIN
    │   └── posthog_tracker.py # pseudonymisation, allowlist, error scrubbing
    ├── examples/              # sample deployment (property wiki)
    ├── Dockerfile
    ├── docker-compose.yml     # one compose project per wiki
    ├── .env.example
    └── README.md
```

## Status

Implemented: webhook with secret, routing with prefixes, index-first query, merge ingest with provenance and atomic commits, photo and voice handling, configurable reminders, privacy-filtered PostHog, multi-deployment configuration.

Not yet run end-to-end against live GitHub / Telegram / LLM services; logic is covered by local smoke tests only.

## Backlog

- [ ] Embedding or BM25 retrieval once a wiki passes a few hundred pages
- [ ] Conversation memory (last few messages)
- [ ] Vision model for photos (currently documented from the caption only)
- [ ] Optional per-wiki lint (`wiki_lint.py`) run on a schedule
- [ ] Configurable wiki folder name (fixed to `wiki/` today)
- [ ] Lifespan handlers instead of `on_event`; Whisper key fallback cleanup
- [ ] One process serving several wikis (today: one container each)
