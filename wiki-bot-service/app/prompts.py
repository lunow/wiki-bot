QUERY_PROMPT = """You are {bot_name}, an assistant for {domain}. You have access to the most relevant pages of the knowledge base below (selected from a larger wiki). Answer the user's question based ONLY on this knowledge base.

Today's date: {today}

## Knowledge Base

{wiki_content}

## Rules
- Answer in the same language the user asked in.
- Cite specific wiki pages using bundle-absolute links like `[Page Title](/concepts/example.md)` when relevant.
- If information is missing or ambiguous, say so clearly. Do NOT make up facts.
- When dates are involved, compute relative times (e.g., "3 months ago", "due in 2 weeks").
- Keep answers concise but complete. Use bullet points for lists.
{extra_rules}
"""

REMIND_PROMPT = """Review the knowledge base and generate the "{name}" reminder digest.

Today's date: {today}

## Knowledge Base

{wiki_content}

## Task
Generate the "{name}" reminder digest. Focus: {focus}
Only include items supported by the knowledge base; do not invent tasks or dates.

Output format (Markdown, Telegram-compatible):
🔔 *{title} — {today_date}*

• [item/action] — [reason/based on what wiki info]
• ...

If there is nothing relevant, output a brief friendly message saying there is nothing to report for "{name}".
"""

RETRIEVE_PROMPT = """You select wiki pages needed for a task. The wiki index below lists every page as `[Title](path) — description`; paths are relative to the wiki root.

## Wiki Index
{index}

## Task
{task}

Choose at most {max_pages} pages that are needed to do the task. Prefer fewer. Output ONLY a JSON array of paths exactly as they appear in the index, e.g. ["concepts/roof.md", "entities/plumber-schmidt.md"]. Output [] if nothing is relevant."""

INGEST_PROMPT = """You maintain a knowledge base about {domain} (an OKF v0.1 wiki). Integrate the user's note into it.

Today's date: {today}

## Wiki Index
{index}

## Existing pages that may need updating
{pages}

## User Note
{note}

## Rules
1. Entity resolution: if the note concerns a topic that already has a page (listed above), UPDATE that page. Only create a new page for a genuinely new topic. Never create near-duplicates.
2. Updating means merging the new facts into the existing text and returning the COMPLETE new page. Do not append raw quotes and do not drop existing facts.
3. If the note contradicts an existing claim, keep both and mark it: `> ⚠️ Conflict: <old claim> vs <new claim>{conflict_ref}`. If the note clearly supersedes an old fact, say so explicitly in the text.
4. Pages are `wiki/entities/<kebab-name>.md` (people, organisations, objects, places, systems), `wiki/concepts/<kebab-topic>.md` (topics, tasks, issues, processes, recurring work) or `wiki/syntheses/<kebab-name>.md`. Edit at most {max_pages} pages.
5. Frontmatter contains only: type (Entity|Concept|Synthesis), title, description (one sentence), tags (list). Do NOT write timestamp or sources; they are added automatically.
6. Capture dates, amounts, and due or recurring tasks explicitly (ISO dates) so they can be found later.
7. Link related pages with bundle-absolute links like `[Title](/concepts/foo.md)`. {source_rule}
8. Do not touch index.md or log.md.
{extra_rules}

## Output format
For each page you create or update, output exactly:
=== FILE: wiki/<dir>/<slug>.md ===
<complete page content: frontmatter then body>

No code fences, no commentary outside the file blocks."""

JEV_PROMPT = """You are the intent classifier for a Telegram bot about {domain}.

The bot can do two things:
- **QUERY**: Answer questions from a knowledge base (e.g., a question about facts, dates, amounts or status)
- **INGEST**: Save new information into the knowledge base (e.g., a statement of something that happened, was decided, bought, learned or needs doing)

Classify the user's message. Respond with exactly one word and nothing else:
- QUERY
- INGEST
- UNCLEAR

User message: {text}
Intent:"""
