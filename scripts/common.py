"""Shared helpers for the obsidian-flashcards skill.

Pure Python 3 stdlib only — no third-party deps, no harness-specific calls — so
the skill is portable to any agent runtime. All reliability-critical logic
(hashing, slugs, ID handling, INDEX parse/write, deck-file region parsing,
state reconstruction) lives here and is shared by scan.py and write_deck.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

INDEX_NAME = "INDEX.md"
ORPHANS_NAME = "_Orphans.md"

# Managed-region markers (HTML comments — invisible in Obsidian preview).
HEADER_START = "<!-- obsidian-flashcards:header:start -->"
HEADER_END = "<!-- obsidian-flashcards:header:end -->"
CARDS_START = "<!-- obsidian-flashcards:cards:start -->"
CARDS_END = "<!-- obsidian-flashcards:cards:end -->"
INDEX_DATA_START = "<!-- obsidian-flashcards:index-data:start -->"
INDEX_DATA_END = "<!-- obsidian-flashcards:index-data:end -->"

ALL_MARKERS = (
    HEADER_START, HEADER_END, CARDS_START, CARDS_END,
    INDEX_DATA_START, INDEX_DATA_END,
)

# Directories never scanned for source notes (matched by resolved path).
BUILTIN_SKIP_DIRS = {
    ".trash", ".obsidian", ".git", "templates", "node_modules",
}

# A deck file may never take one of these basenames.
RESERVED_DECK_NAMES = {INDEX_NAME.lower(), ORPHANS_NAME.lower()}

FC_ID_RE = re.compile(r"<!--\s*fc-id:\s*([^\s>]+?)\s*-->")
SR_RE = re.compile(r"<!--\s*SR:.*?-->", re.DOTALL)
TAG_RE = re.compile(r"(#flashcards(?:/[^\s#]+)?)")
SOURCE_LINK_RE = re.compile(r">\s*Source:\s*\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Logging — warnings to stderr, never stdout (stdout is reserved for JSON).
# --------------------------------------------------------------------------- #

def warn(msg: str) -> None:
    print(f"[obsidian-flashcards] WARNING: {msg}", file=sys.stderr)


def emit_json(obj) -> None:
    """Print a JSON result object to stdout (the machine-readable channel)."""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def fail(message: str, **extra) -> None:
    """Emit a JSON error object on stdout + human message on stderr, exit 1.

    No file changes have been made by the time this is called.
    """
    err = {"error": message}
    err.update(extra)
    print(json.dumps(err, ensure_ascii=False, indent=2))
    print(f"[obsidian-flashcards] ERROR: {message}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# IDs
# --------------------------------------------------------------------------- #

def new_id() -> str:
    """A fresh UUID4 string for an fc-id."""
    return str(uuid.uuid4())


def is_valid_id(value: str) -> bool:
    return bool(UUID4_RE.match(value or ""))


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #

def read_text(path: Path) -> str:
    """Read a note as utf-8-sig (BOM-tolerant); invalid bytes -> replacement.

    Returns the file text with line endings normalized to LF.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        warn(f"{path}: invalid UTF-8, decoding with replacement characters")
        text = raw.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def source_hash(path: Path) -> str:
    """SHA-256 of the note text (frontmatter included), LF-normalized.

    CRLF/LF checkout churn does not change the hash; whitespace-only edits DO
    (not normalized in v1 — documented in SKILL.md).
    """
    text = read_text(path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def path_hash(rel_path: str, length: int = 6) -> str:
    """Stable short hex hash derived from a vault-relative POSIX path."""
    digest = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()
    return digest[:length]


# --------------------------------------------------------------------------- #
# Slugs
# --------------------------------------------------------------------------- #

def slugify(title: str, rel_path: str | None = None) -> str:
    """Kebab-case slug from a note title.

    Handles empty / numeric-only / non-ASCII titles safely. Falls back to a
    path-hash slug when the title yields nothing usable.
    """
    normalized = unicodedata.normalize("NFKD", title or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not re.search(r"[a-z]", slug):
        # Empty, punctuation-only, or numeric-only -> stable fallback.
        seed = rel_path if rel_path else (title or "note")
        slug = f"note-{path_hash(seed)}" if not slug else f"n-{slug}-{path_hash(seed)}"
    return slug


def title_from_path(path: Path) -> str:
    return path.stem


# --------------------------------------------------------------------------- #
# .fcignore / ignore patterns (segment-aware glob, NOT raw fnmatch)
# --------------------------------------------------------------------------- #

def _seg_to_regex(seg: str) -> str:
    """Translate one path segment glob (* ? but not /) to a regex fragment."""
    out = []
    for ch in seg:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def compile_ignore(pattern: str) -> re.Pattern | None:
    """Compile a .fcignore-style pattern into a regex over POSIX rel paths.

    Supported subset (v1):
      - `*` and `?` match within a single segment (not `/`)
      - `**` may cross directories
      - trailing `/` matches a directory subtree
      - no `!` negation
    """
    pat = pattern.strip()
    if not pat or pat.startswith("#"):
        return None
    subtree = pat.endswith("/")
    if subtree:
        pat = pat.rstrip("/")
    pat = pat.lstrip("/")

    segments = pat.split("/")
    regex_parts = []
    for seg in segments:
        if seg == "**":
            regex_parts.append("(?:.*)")
        else:
            regex_parts.append(_seg_to_regex(seg))
    # Join, collapsing the slash that follows a ** so it can match zero dirs.
    body = ""
    for i, part in enumerate(regex_parts):
        if i > 0:
            prev = segments[i - 1]
            body += "/?" if prev == "**" else "/"
        body += part
    if subtree:
        body += "(?:/.*)?"
    return re.compile(f"^{body}$")


def path_is_ignored(rel_posix: str, patterns: list[re.Pattern]) -> bool:
    """True if the vault-relative POSIX path matches any ignore pattern.

    A pattern matches the path itself or any ancestor directory of it, so an
    entry like `Archive/` excludes everything beneath it.
    """
    candidates = [rel_posix]
    parts = rel_posix.split("/")
    for i in range(1, len(parts)):
        candidates.append("/".join(parts[:i]))
    for pat in patterns:
        for cand in candidates:
            if pat.match(cand):
                return True
    return False


def load_ignore_patterns(cli_ignores: list[str], ignore_files: list[Path]) -> list[re.Pattern]:
    patterns: list[re.Pattern] = []
    raw: list[str] = list(cli_ignores or [])
    for f in ignore_files:
        if f and f.is_file():
            raw.extend(read_text(f).splitlines())
    for line in raw:
        compiled = compile_ignore(line)
        if compiled is not None:
            patterns.append(compiled)
    return patterns


# --------------------------------------------------------------------------- #
# Card-block parsing & normalization
# --------------------------------------------------------------------------- #

@dataclass
class Card:
    """One managed card block."""
    text: str            # the card body (separators/cloze), no fc-id/SR comments
    fc_id: str
    sr: str | None = None   # the entire <!--SR:...--> comment, verbatim

    def render(self) -> str:
        lines = [self.text.rstrip("\n"), f"<!--fc-id:{self.fc_id}-->"]
        if self.sr:
            lines.append(self.sr.strip())
        return "\n".join(lines)

    def normalized_text(self) -> str:
        """Whitespace-normalized body for the conservative exact-text fallback."""
        return re.sub(r"\s+", " ", self.text).strip()


def split_blocks(text: str) -> list[str]:
    """Split a chunk of markdown into blank-line-separated blocks."""
    blocks = re.split(r"\n[ \t]*\n", text.strip("\n"))
    return [b.strip("\n") for b in blocks if b.strip()]


def parse_card_block(block: str):
    """Parse one block into (body_text, fc_id_or_None, sr_or_None, malformed).

    `malformed` is True when a block carries an fc-id comment whose value is not
    a valid UUID4, or when it has more than one fc-id.
    """
    sr_match = SR_RE.search(block)
    sr = sr_match.group(0) if sr_match else None
    body = SR_RE.sub("", block)

    ids = FC_ID_RE.findall(body)
    body = FC_ID_RE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")

    if not ids:
        return body, None, sr, False
    if len(ids) > 1:
        return body, ids[0], sr, True
    fc_id = ids[0]
    malformed = not is_valid_id(fc_id)
    return body, fc_id, sr, malformed


# --------------------------------------------------------------------------- #
# Deck-file regions
# --------------------------------------------------------------------------- #

@dataclass
class DeckFile:
    pre: str = ""                 # user content before the header region
    between: str = ""             # user content between header and cards regions
    post: str = ""                # user content after the cards region
    source_rel: str | None = None # from the managed header backlink
    tag: str | None = None        # from the managed header
    cards: list[Card] = field(default_factory=list)
    has_regions: bool = False


def _extract_region(text: str, start: str, end: str):
    """Return (before, inner, after) around the first start..end region, or None."""
    si = text.find(start)
    if si == -1:
        return None
    ei = text.find(end, si + len(start))
    if ei == -1:
        return None
    inner = text[si + len(start):ei]
    after = text[ei + len(end):]
    before = text[:si]
    return before, inner, after


def parse_deck_file(text: str) -> DeckFile:
    """Parse a managed deck file into header data, cards, and user content."""
    deck = DeckFile()
    header = _extract_region(text, HEADER_START, HEADER_END)
    if header is None:
        # No managed regions — treat whole thing as user content (pre).
        deck.pre = text
        return deck
    deck.has_regions = True
    deck.pre, header_inner, rest = header

    link = SOURCE_LINK_RE.search(header_inner)
    if link:
        deck.source_rel = link.group(1).strip()
    tag = TAG_RE.search(header_inner)
    if tag:
        deck.tag = tag.group(1).strip()

    cards = _extract_region(rest, CARDS_START, CARDS_END)
    if cards is None:
        deck.between = rest
        return deck
    deck.between, cards_inner, deck.post = cards
    for block in split_blocks(cards_inner):
        body, fc_id, sr, malformed = parse_card_block(block)
        deck.cards.append(Card(text=body, fc_id=fc_id or "", sr=sr))
        if malformed or not fc_id:
            # Caller decides how to react; record via a sentinel empty id.
            pass
    return deck


def deck_has_malformed_managed_ids(text: str) -> bool:
    """True if any managed card block has a missing/invalid fc-id."""
    header = _extract_region(text, HEADER_START, HEADER_END)
    if header is None:
        return False
    _, _, rest = header
    cards = _extract_region(rest, CARDS_START, CARDS_END)
    if cards is None:
        return False
    _, cards_inner, _ = cards
    for block in split_blocks(cards_inner):
        _, fc_id, _, malformed = parse_card_block(block)
        if not fc_id or malformed:
            return True
    return False


def render_deck_file(source_rel: str, title: str, tag: str, cards: list[Card],
                     pre: str = "", between: str = "", post: str = "") -> str:
    """Render a full deck file with managed header + cards regions.

    User content (pre/between/post) is preserved verbatim around the regions.
    """
    link_target = source_rel[:-3] if source_rel.endswith(".md") else source_rel
    header = "\n".join([
        HEADER_START,
        f"> Source: [[{link_target}|{title}]]",
        tag,
        HEADER_END,
    ])
    card_lines = "\n\n".join(c.render() for c in cards)
    cards_region = f"{CARDS_START}\n\n{card_lines}\n\n{CARDS_END}" if cards \
        else f"{CARDS_START}\n\n{CARDS_END}"

    parts = []
    if pre.strip():
        parts.append(pre.rstrip("\n"))
    parts.append(header)
    if between.strip():
        parts.append(between.strip("\n"))
    parts.append(cards_region)
    if post.strip():
        parts.append(post.strip("\n"))
    return "\n\n".join(parts).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# INDEX.md — managed data block + human dashboard
# --------------------------------------------------------------------------- #

def load_index(out_dir: Path) -> dict:
    """Load INDEX state. Returns {"version":1,"notes":{...}}.

    On a corrupt data block, the caller is responsible for backup + rebuild;
    here we just signal corruption by raising ValueError. A missing file
    returns an empty state.
    """
    index_path = out_dir / INDEX_NAME
    if not index_path.is_file():
        return {"version": 1, "notes": {}}
    text = read_text(index_path)
    region = _extract_region(text, INDEX_DATA_START, INDEX_DATA_END)
    if region is None:
        raise ValueError("INDEX.md has no managed data block")
    _, inner, _ = region
    inner = inner.strip()
    # Strip an optional ```json fence.
    inner = re.sub(r"^```json\s*", "", inner)
    inner = re.sub(r"\s*```$", "", inner)
    try:
        data = json.loads(inner)
    except json.JSONDecodeError as exc:
        raise ValueError(f"INDEX.md data block is not valid JSON: {exc}")
    if "notes" not in data:
        data["notes"] = {}
    data.setdefault("version", 1)
    return data


def render_index(state: dict) -> str:
    """Render INDEX.md: dashboard table + marker-delimited JSON data block."""
    notes = state.get("notes", {})
    rows = ["# Flashcard Index", "",
            "| Note | Deck | Blocks | Last run |",
            "|---|---|---|---|"]
    for src in sorted(notes):
        entry = notes[src]
        title = PurePosixPath(src).stem
        link = f"[[{src[:-3] if src.endswith('.md') else src}\\|{title}]]"
        rows.append(
            f"| {link} | {entry.get('deck_file','')} | "
            f"{entry.get('card_blocks',0)} | {entry.get('last_run','')} |"
        )
    rows += ["", INDEX_DATA_START, "```json",
             json.dumps(state, ensure_ascii=False, indent=2),
             "```", INDEX_DATA_END, ""]
    return "\n".join(rows)


def backup_corrupt_index(out_dir: Path, timestamp: str) -> Path | None:
    """Copy a corrupt INDEX.md to INDEX.md.bak.<timestamp>. Missing -> None."""
    index_path = out_dir / INDEX_NAME
    if not index_path.is_file():
        return None
    backup = out_dir / f"{INDEX_NAME}.bak.{timestamp}"
    backup.write_bytes(index_path.read_bytes())
    return backup


def reconstruct_index(out_dir: Path, vault_root: Path) -> dict:
    """Rebuild INDEX state from existing managed deck files (never start empty).

    Conservative: current_ids = active_ids, no orphan split invented,
    orphan_policy = kept-active, recovered = True. Unmappable decks are
    preserved and warned about, never overwritten (they simply aren't indexed).
    """
    state = {"version": 1, "notes": {}}
    for deck_path in sorted(out_dir.glob("*.md")):
        if deck_path.name.lower() in RESERVED_DECK_NAMES or deck_path.name.startswith("_"):
            continue
        deck = parse_deck_file(read_text(deck_path))
        if not deck.has_regions or not deck.source_rel:
            warn(f"{deck_path.name}: cannot map to a source note — preserved, not indexed")
            continue
        src_rel = deck.source_rel
        if not src_rel.endswith(".md"):
            src_rel += ".md"
        active_ids = [c.fc_id for c in deck.cards if c.fc_id]
        src_abs = vault_root / src_rel
        entry = {
            "deck_file": deck_path.name,
            "tag": deck.tag or "",
            "hash": source_hash(src_abs) if src_abs.is_file() else None,
            "orphan_policy": "kept-active",
            "current_ids": list(active_ids),
            "active_orphan_ids": [],
            "archived_orphan_ids": [],
            "active_ids": list(active_ids),
            "card_blocks": len(active_ids),
            "estimated_review_cards": estimate_review_cards(deck.cards),
            "last_run": None,
            "recovered": True,
        }
        state["notes"][src_rel] = entry
    return state


# --------------------------------------------------------------------------- #
# Review-card estimation
# --------------------------------------------------------------------------- #

def estimate_review_cards(cards: list[Card]) -> int:
    """Estimate review-card count: :: ->1, ::: ->2, ? ->1, ?? ->2, cloze ->1."""
    total = 0
    for card in cards:
        body = card.text
        if "??" in body or re.search(r"(?<!:):::(?!:)", body):
            total += 2
        else:
            total += 1
    return total


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #

def atomic_write(path: Path, content: str) -> None:
    """Write via temp file + os.replace for atomicity."""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
