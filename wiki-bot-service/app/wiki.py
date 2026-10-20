"""Wiki retrieval and ingest, following the llm-wiki (OKF v0.1) workflows.

Query:  read index.md -> LLM picks pages -> answer from only those pages.
Ingest: read index.md -> LLM picks related pages -> LLM merges note into them
        (or creates new ones) -> bot adds provenance, updates index.md and
        log.md, and commits everything in one commit.
"""
import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple, Union

import yaml

from .github_client import WikiStore
from .llm_client import LLMClient
from .prompts import INGEST_PROMPT, RETRIEVE_PROMPT

logger = logging.getLogger(__name__)

INDEX = "wiki/index.md"
LOG = "wiki/log.md"
RESERVED = {INDEX, LOG, "wiki/CLAUDE.md"}

SMALL_WIKI_PAGES = 8  # at or below this, skip retrieval and load every page
MAX_CONTEXT_CHARS = 60_000
MAX_INGEST_PAGES = 6
NEW_PAGE_RE = re.compile(r"^wiki/(entities|concepts|syntheses)/[a-z0-9][a-z0-9-]{0,80}\.md$")
FILE_RE = re.compile(r"^=== FILE: (.+?) ===[ \t]*$", re.MULTILINE)
SECTIONS = {
    "entities": "Entities",
    "concepts": "Concepts",
    "syntheses": "Syntheses",
    "references": "References",
}

ROOT_INDEX = '---\nokf_version: "0.1"\n---\n\n# Wiki\n'


class IngestError(Exception):
    pass


# ── Pure helpers ────────────────────────────────────────────────────
def split_frontmatter(md: str) -> Tuple[dict, str]:
    text = md.strip() + "\n"
    m = re.match(r"^---\n(.*?)\n---[ \t]*\n", text, re.DOTALL)
    if not m:
        raise ValueError("no YAML frontmatter")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise ValueError(f"invalid frontmatter: {e}")
    if not isinstance(meta, dict) or not meta.get("type"):
        raise ValueError("frontmatter needs a non-empty 'type'")
    return meta, text[m.end():].lstrip("\n")


def render_page(meta: dict, body: str) -> str:
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{head}---\n\n{body.strip()}\n"


def parse_json_list(raw: str) -> List[str]:
    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [x for x in data if isinstance(x, str)]


def strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\s*\n", "", text)
    return re.sub(r"\n```\s*$", "", text)


def parse_files(raw: str) -> Dict[str, str]:
    raw = strip_fences(raw)
    parts = FILE_RE.split(raw)  # [pre, path, body, path, body ...]
    files: Dict[str, str] = {}
    for path, body in zip(parts[1::2], parts[2::2]):
        files[path.strip()] = strip_fences(body)
    return files


def update_index(index_md: str, entries: List[Tuple[str, str, str, str]]) -> str:
    """entries: (directory, relpath, title, description). Replaces or adds lines."""
    if not index_md.strip():
        index_md = ROOT_INDEX
    lines = index_md.rstrip("\n").split("\n")
    for directory, rel, title, desc in entries:
        lines = [l for l in lines if f"]({rel})" not in l]
        new_line = f"- [{title}]({rel}) — {desc}".rstrip(" —")
        heading = f"## {SECTIONS.get(directory, directory.title())}"
        if heading not in lines:
            lines += ["", heading, ""]
        i = lines.index(heading) + 1
        while i < len(lines) and not lines[i].startswith("## "):
            i += 1
        # insert after the last non-blank line of the section
        j = i
        while j > 0 and lines[j - 1].strip() == "":
            j -= 1
        lines.insert(j, new_line)
    out = "\n".join(lines)
    out = re.sub(r"(^## .+)\n(?!\n)", r"\1\n\n", out, flags=re.MULTILINE)  # blank line after headings
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.rstrip("\n") + "\n"


def update_log(log_md: str, entry: str, today: str) -> str:
    if not log_md.strip():
        log_md = "# Log\n"
    lines = log_md.rstrip("\n").split("\n")
    heading = f"## {today}"
    if heading in lines:
        i = lines.index(heading) + 1
        if i < len(lines) and lines[i] == "":
            i += 1
        lines.insert(i, f"- {entry}")
    else:
        i = next((k for k, l in enumerate(lines) if l.startswith("## ")), len(lines))
        block = [heading, "", f"- {entry}", ""]
        if i == len(lines):
            block = [""] + block[:-1]
        lines[i:i] = block
    return "\n".join(lines).rstrip("\n") + "\n"


# ── Service ─────────────────────────────────────────────────────────
class WikiService:
    def __init__(
        self, store: WikiStore, llm: LLMClient, now: Callable[[], datetime], save_raw_notes: bool = True,
        domain: str = "a personal knowledge base", ingest_guidance: str = "",
    ):
        self.store = store
        self.llm = llm
        self.now = now
        self.save_raw_notes = save_raw_notes
        self.domain = domain
        self.ingest_guidance = ingest_guidance
        self._write_lock = asyncio.Lock()

    async def _index_text(self, snap: Dict[str, dict]) -> str:
        """index.md plus a list of any pages it doesn't mention."""
        index = await self.store.read(INDEX, snap) if INDEX in snap else ""
        missing = [
            p[len("wiki/"):] for p in sorted(snap)
            if p not in RESERVED and f"({p[len('wiki/'):]})" not in index
        ]
        if missing:
            index += "\n\n## Not yet in index\n" + "\n".join(f"- {p}" for p in missing)
        return index

    async def select_pages(
        self, task: str, snap: Dict[str, dict], *, max_pages: int, chat_id: Optional[int], label: str
    ) -> List[str]:
        pages = [p for p in sorted(snap) if p not in RESERVED]
        if len(pages) <= SMALL_WIKI_PAGES:
            return pages
        prompt = RETRIEVE_PROMPT.format(
            index=await self._index_text(snap), task=task, max_pages=max_pages
        )
        raw = await self.llm.chat(
            system="You are a precise retrieval assistant. Output only a JSON array.",
            user=prompt,
            temperature=0.0,
            chat_id=chat_id,
            metadata={"retrieval": label},
        )
        chosen = []
        for p in parse_json_list(raw):
            full = p if p.startswith("wiki/") else f"wiki/{p.lstrip('/')}"
            if full in snap and full not in RESERVED and full not in chosen:
                chosen.append(full)
        return chosen[:max_pages]

    async def context_for(
        self, task: str, chat_id: Optional[int], *, max_pages: int = 6, label: str = "query"
    ) -> Tuple[str, List[str]]:
        """Return (prompt-ready page text, page paths) for the task."""
        snap = await self.store.snapshot()
        chosen = await self.select_pages(task, snap, max_pages=max_pages, chat_id=chat_id, label=label)
        contents = await self.store.read_many(chosen, snap)
        parts, used, total = [], [], 0
        for p in chosen:
            block = f"--- FILE: {p} ---\n{contents[p]}\n"
            if total + len(block) > MAX_CONTEXT_CHARS:
                break
            parts.append(block)
            used.append(p)
            total += len(block)
        return ("\n".join(parts) or "(no relevant pages found)"), used

    async def ingest(
        self,
        chat_id: int,
        note: str,
        *,
        label: str = "Telegram note",
        attachments: Optional[Dict[str, bytes]] = None,
        extra_note_context: str = "",
    ) -> Dict[str, List[str]]:
        """Merge a note into the wiki. Returns {"created": [...], "updated": [...], "source": path}."""
        async with self._write_lock:  # snapshot -> commit must not interleave
            now = self.now()
            today = now.strftime("%Y-%m-%d")
            stamp = now.strftime("%Y%m%d-%H%M%S")
            snap = await self.store.snapshot()

            source_rel = f"references/note-{stamp}.md"
            n = 2
            while f"wiki/{source_rel}" in snap:
                source_rel = f"references/note-{stamp}-{n}.md"
                n += 1
            source_link = "/" + source_rel
            if self.save_raw_notes:
                conflict_ref = f" (see [Note]({source_link}))"
                source_rule = f"Link the raw note as `[Note]({source_link})` where useful."
            else:
                conflict_ref = ""
                source_rule = (
                    "The raw note is NOT stored. Keep every fact, date and image link "
                    "(`![...](/...)`) from the note in the page text."
                )

            # 1. pick related existing pages
            selected = await self.select_pages(
                note, snap, max_pages=5, chat_id=chat_id, label="ingest"
            )
            existing = await self.store.read_many(selected, snap)
            pages_text = "\n".join(
                f"--- FILE: {p} ---\n{c}\n" for p, c in existing.items()
            ) or "(none — the wiki has no related pages)"

            # 2. merge note into pages
            raw = await self.llm.chat(
                system="You maintain an OKF wiki. Follow the output format exactly.",
                user=INGEST_PROMPT.format(
                    today=now.isoformat(),
                    index=await self._index_text(snap),
                    pages=pages_text,
                    note=note + extra_note_context,
                    conflict_ref=conflict_ref,
                    source_rule=source_rule,
                    max_pages=MAX_INGEST_PAGES,
                    domain=self.domain,
                    extra_rules=self.ingest_guidance,
                ),
                chat_id=chat_id,
                metadata={"ingest": True},
            )
            proposed = parse_files(raw)
            if not proposed:
                raise IngestError("The model returned no pages.")
            if len(proposed) > MAX_INGEST_PAGES:
                raise IngestError(f"The model proposed {len(proposed)} pages; limit is {MAX_INGEST_PAGES}.")

            # 3. the raw note, kept verbatim as a Source page (optional)
            files: Dict[str, Union[str, bytes]] = {}
            source_entry = None
            if self.save_raw_notes:
                source_page = render_page(
                    {
                        "type": "Source",
                        "title": f"{label} {today}",
                        "description": f"Raw {label.lower()} received on {today}.",
                        "tags": ["telegram", "captured"],
                        "timestamp": now.isoformat(),
                    },
                    f"Captured {today}. Content below is verbatim; do not edit.\n\n---\n\n{note}",
                )
                files[f"wiki/{source_rel}"] = source_page
                source_entry = {
                    "path": source_link,
                    "sha256": hashlib.sha256(source_page.encode()).hexdigest(),
                }

            # 4. validate + assemble pages with provenance
            index_entries = []
            created, updated = [], []
            for path, md in proposed.items():
                is_update = path in existing
                if not is_update:
                    if not NEW_PAGE_RE.match(path):
                        raise IngestError(f"The model proposed an invalid path: {path}")
                    if path in snap:
                        raise IngestError(f"The model tried to overwrite a page it was not shown: {path}")
                try:
                    llm_meta, body = split_frontmatter(md)
                except ValueError as e:
                    raise IngestError(f"{path}: {e}")
                old_meta = split_frontmatter(existing[path])[0] if is_update else {}
                sources = [s for s in old_meta.get("sources", []) if isinstance(s, dict)]
                meta = {**old_meta, **{k: v for k, v in llm_meta.items() if k not in ("sources", "timestamp")}}
                meta["timestamp"] = now.isoformat()
                sources += [source_entry] if source_entry else []
                if sources:
                    meta["sources"] = sources
                meta.setdefault("title", path.rsplit("/", 1)[-1][:-3].replace("-", " ").title())
                files[path] = render_page(meta, body)
                directory = path.split("/")[1]
                rel = path[len("wiki/"):]
                index_entries.append((directory, rel, str(meta["title"]), str(meta.get("description", ""))))
                (updated if is_update else created).append(path)

            if self.save_raw_notes:
                index_entries.append(
                    ("references", source_rel, f"{label} {today}", f"Raw {label.lower()} received on {today}.")
                )

            # 5. bookkeeping: index.md + log.md
            index_md = await self.store.read(INDEX, snap) if INDEX in snap else ""
            log_md = await self.store.read(LOG, snap) if LOG in snap else ""
            files[INDEX] = update_index(index_md, index_entries)
            summary = ", ".join(
                ([f"created {', '.join('/' + p[5:] for p in created)}"] if created else [])
                + ([f"updated {', '.join('/' + p[5:] for p in updated)}"] if updated else [])
            )
            files[LOG] = update_log(log_md, f"**Ingest** {label}{f' → /{source_rel}' if self.save_raw_notes else ''} — {summary}", today)

            # 6. one atomic commit (page images etc. ride along)
            if attachments:
                files.update(attachments)
            await self.store.commit_files(files, f"Ingest via Telegram: {summary}"[:200])
            return {"created": created, "updated": updated, "source": f"wiki/{source_rel}" if self.save_raw_notes else None}
