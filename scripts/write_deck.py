#!/usr/bin/env python3
"""write_deck.py — merge generated cards into one deck file, by fc-id.

Reads card blocks from stdin (blank-line separated). Strips any model-emitted
<!--SR:--> comments, assigns UUID4 fc-ids to id-less blocks, preserves the whole
SR comment for any reused id, handles renames/orphans, and updates INDEX.md
atomically. Stdout is JSON only; warnings go to stderr.

On ANY validation failure the whole write aborts with no file changes (exit 1,
JSON error object on stdout).

Usage:
  write_deck.py --source <note> --vault-root <path> --out-dir <Flashcards>
                --deck-file <from-scan> --tag <from-scan>
                [--matched-old-path <old>] [--force-adopt] [--migrate-name]
                [--prune | --orphan-archive]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path, PurePosixPath

import common as C


def parse_args(argv):
    p = argparse.ArgumentParser(description="Write/merge one flashcard deck file.")
    p.add_argument("--source", required=True)
    p.add_argument("--vault-root", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--deck-file", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--matched-old-path", default=None)
    p.add_argument("--force-adopt", action="store_true")
    p.add_argument("--migrate-name", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--prune", action="store_true")
    g.add_argument("--orphan-archive", action="store_true")
    return p.parse_args(argv)


def parse_incoming(stdin_text: str) -> list[C.Card]:
    """Parse + validate stdin into cards. Fails the whole write on any problem."""
    if any(marker in stdin_text for marker in C.ALL_MARKERS):
        C.fail("stdin contains a managed region marker — refusing to write "
               "(the model should supply card blocks only)")
    cards: list[C.Card] = []
    for block in C.split_blocks(stdin_text):
        body, fc_id, _sr, malformed = C.parse_card_block(block)  # SR stripped
        if not body.strip():
            continue
        if fc_id and malformed:
            C.fail(f"incoming card has a malformed fc-id: {fc_id!r}")
        cards.append(C.Card(text=body, fc_id=fc_id or "", sr=None))
    if not cards:
        C.fail("no card blocks found on stdin")
    return cards


def merge(incoming: list[C.Card], existing: list[C.Card]):
    """Resolve each incoming card to an fc-id, preserving SR by id or exact text.

    Returns (current_cards, orphan_cards).
    """
    by_id = {c.fc_id: c for c in existing if c.fc_id}
    claimed: set[str] = set()

    # Pass 1: cards that already carry a known id.
    for card in incoming:
        if card.fc_id and card.fc_id in by_id:
            claimed.add(card.fc_id)

    current: list[C.Card] = []
    for card in incoming:
        if card.fc_id and card.fc_id in by_id:
            old = by_id[card.fc_id]
            current.append(C.Card(text=card.text, fc_id=card.fc_id, sr=old.sr))
            continue
        if card.fc_id:
            # Provided id not present in the deck — keep it, no schedule.
            current.append(C.Card(text=card.text, fc_id=card.fc_id, sr=None))
            continue
        # Id-less: conservative exact-text fallback (unique match only).
        norm = card.normalized_text()
        matches = [c for c in existing
                   if c.fc_id and c.fc_id not in claimed
                   and c.normalized_text() == norm]
        if len(matches) == 1:
            old = matches[0]
            claimed.add(old.fc_id)
            current.append(C.Card(text=card.text, fc_id=old.fc_id, sr=old.sr))
        else:
            if len(matches) > 1:
                C.warn(f"ambiguous text match for a new card — assigning a new id")
            current.append(C.Card(text=card.text, fc_id=C.new_id(), sr=None))

    current_ids = {c.fc_id for c in current}
    orphans = [c for c in existing if c.fc_id and c.fc_id not in current_ids]
    return current, orphans


def archive_orphans(out_dir: Path, orphans: list[C.Card]) -> list[str]:
    """Append orphan cards to _Orphans.md (untagged, idempotent). Returns ids added."""
    path = out_dir / C.ORPHANS_NAME
    existing_ids: set[str] = set()
    pre = ""
    if path.is_file():
        text = C.read_text(path)
        pre = text.rstrip("\n")
        for block in C.split_blocks(text):
            _, fc_id, _, _ = C.parse_card_block(block)
            if fc_id:
                existing_ids.add(fc_id)
    new_blocks = [c.render() for c in orphans if c.fc_id not in existing_ids]
    added = [c.fc_id for c in orphans if c.fc_id not in existing_ids]
    if not new_blocks:
        return []
    header = "# Orphaned flashcards\n\n" \
             "Cards no longer generated from their source notes. Untagged, so " \
             "they are not reviewed under tag-based deck assignment.\n"
    body = "\n\n".join(new_blocks)
    content = (pre + "\n\n" + body) if pre else (header + "\n" + body)
    C.atomic_write(path, content.rstrip("\n") + "\n")
    return added


def load_index_with_recovery(out_dir: Path, vault_root: Path):
    """Load INDEX; on corruption, back it up and reconstruct from deck files."""
    try:
        return C.load_index(out_dir), False
    except ValueError as exc:
        C.warn(f"{exc} — backing up and reconstructing")
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        C.backup_corrupt_index(out_dir, ts)
        return C.reconstruct_index(out_dir, vault_root), True


def main(argv):
    args = parse_args(argv)
    source = Path(args.source)
    vault_root = Path(args.vault_root)
    out_dir = Path(args.out_dir)
    deck_file = args.deck_file
    tag = args.tag

    # --- Basic validation (no file changes yet). ---
    if "/" in deck_file or "\\" in deck_file:
        C.fail(f"--deck-file must be a bare basename, got {deck_file!r}")
    if deck_file.lower() in C.RESERVED_DECK_NAMES or deck_file.startswith("_"):
        C.fail(f"--deck-file uses a reserved name: {deck_file!r}")
    if not source.is_file():
        C.fail(f"source note does not exist: {source}")
    if not vault_root.is_dir():
        C.fail(f"vault root does not exist: {vault_root}")
    out_dir.mkdir(parents=True, exist_ok=True)

    source_rel = PurePosixPath(source.resolve().relative_to(vault_root.resolve())).as_posix()
    title = source.stem

    stdin_text = sys.stdin.read()
    incoming = parse_incoming(stdin_text)

    index, _recovered = load_index_with_recovery(out_dir, vault_root)
    notes = index.setdefault("notes", {})

    # --- Rename adoption. ---
    old_path = args.matched_old_path
    existing_deck_path = out_dir / deck_file
    remove_key = None
    if old_path:
        if old_path not in notes:
            C.fail(f"--matched-old-path not found in INDEX: {old_path}")
        old_entry = notes[old_path]
        if (vault_root / old_path).exists():
            C.fail(f"--matched-old-path still exists on disk; not a rename: {old_path}")
        if not args.migrate_name:
            if deck_file != old_entry.get("deck_file") or tag != old_entry.get("tag"):
                C.fail("supplied --deck-file/--tag differ from the old INDEX entry; "
                       "pass --migrate-name to rename intentionally")
        old_deck_path = out_dir / old_entry.get("deck_file", "")
        if not old_deck_path.is_file() and not args.force_adopt:
            C.fail(f"old deck file missing: {old_deck_path.name} "
                   "(pass --force-adopt to accept schedule loss)")
        existing_deck_path = old_deck_path if old_deck_path.is_file() else (out_dir / deck_file)
        remove_key = old_path

    # --- Read existing deck (preserve user content; reject malformed managed ids). ---
    if existing_deck_path.is_file():
        deck_text = C.read_text(existing_deck_path)
        if C.deck_has_malformed_managed_ids(deck_text):
            C.fail(f"existing deck has a malformed managed fc-id: {existing_deck_path.name} "
                   "(a future --repair mode will handle this)")
        deck = C.parse_deck_file(deck_text)
    else:
        deck = C.DeckFile()

    # --- Merge by id. ---
    current, orphans = merge(incoming, deck.cards)

    # --- Orphan policy. ---
    prev_entry = notes.get(old_path) if old_path else notes.get(source_rel)
    prev_archived = list((prev_entry or {}).get("archived_orphan_ids", []))
    pruned_count = 0
    archived_now: list[str] = []
    if args.prune:
        policy = "pruned"
        pruned_count = len(orphans)
        deck_cards = current
        active_orphan_ids: list[str] = []
    elif args.orphan_archive:
        policy = "archived-inactive"
        archived_now = archive_orphans(out_dir, orphans)
        deck_cards = current
        active_orphan_ids = []
    else:
        policy = "kept-active"
        deck_cards = current + orphans
        active_orphan_ids = [c.fc_id for c in orphans]

    active_ids = [c.fc_id for c in deck_cards]

    # --- Render + write deck (atomic). ---
    final_deck_path = out_dir / deck_file
    rendered = C.render_deck_file(
        source_rel=source_rel, title=title, tag=tag, cards=deck_cards,
        pre=deck.pre, between=deck.between, post=deck.post,
    )
    C.atomic_write(final_deck_path, rendered)
    # If renamed to a different file, remove the stale old deck file.
    if old_path and existing_deck_path != final_deck_path and existing_deck_path.is_file():
        existing_deck_path.unlink()

    # --- Update INDEX (atomic). ---
    if remove_key and remove_key in notes:
        del notes[remove_key]
    notes[source_rel] = {
        "deck_file": deck_file,
        "tag": tag,
        "hash": C.source_hash(source),
        "orphan_policy": policy,
        "current_ids": [c.fc_id for c in current],
        "active_orphan_ids": active_orphan_ids,
        "archived_orphan_ids": sorted(set(prev_archived) | set(archived_now)),
        "active_ids": active_ids,
        "card_blocks": len(active_ids),
        "estimated_review_cards": C.estimate_review_cards(deck_cards),
        "last_run": _dt.date.today().isoformat(),
    }
    C.atomic_write(out_dir / C.INDEX_NAME, C.render_index(index))

    C.emit_json({
        "source": source_rel,
        "deck_file": deck_file,
        "tag": tag,
        "orphan_policy": policy,
        "preserved": sum(1 for c in current if c.sr),
        "added": sum(1 for c in current if not c.sr),
        "orphaned": len(orphans),
        "pruned": pruned_count,
        "current_ids": [c.fc_id for c in current],
        "active_orphan_ids": active_orphan_ids,
        "archived_orphan_ids": archived_now,
        "active_ids": active_ids,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
