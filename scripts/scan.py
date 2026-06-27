#!/usr/bin/env python3
"""scan.py — diff a source directory against the flashcard INDEX.

Emits, on stdout, a JSON object:
  {"notes": [ ... ], "stale_entries": [ ... ]}

Each note carries a deterministic status and its FINAL (sticky, collision-safe)
deck_file + tag, so write_deck.py never has to re-decide naming. Warnings go to
stderr; stdout is JSON only. Exit 0 on success, 1 on a usable failure.

Usage:
  scan.py <source_dir> --out-dir <Flashcards> --vault-root <path>
          [--ignore <pat> ...] [--ignore-file <.fcignore>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path, PurePosixPath

import common as C


def parse_args(argv):
    p = argparse.ArgumentParser(description="Scan notes for flashcard conversion.")
    p.add_argument("source_dir")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--vault-root", required=True)
    p.add_argument("--ignore", action="append", default=[])
    p.add_argument("--ignore-file", default=None)
    return p.parse_args(argv)


def rel_posix(path: Path, root: Path) -> str:
    return PurePosixPath(path.resolve().relative_to(root.resolve())).as_posix()


def is_builtin_skipped(rel: str, out_rel: str) -> bool:
    parts = rel.split("/")
    if rel == out_rel or rel.startswith(out_rel + "/"):
        return True
    for part in parts[:-1]:
        if part.lower() in C.BUILTIN_SKIP_DIRS:
            return True
    return False


def discover_ignore_files(args, source_dir: Path, vault_root: Path) -> list[Path]:
    if args.ignore_file:
        return [Path(args.ignore_file)]
    candidates = [source_dir / ".fcignore", vault_root / ".fcignore"]
    seen, out = set(), []
    for c in candidates:
        rc = c.resolve()
        if rc not in seen and c.is_file():
            seen.add(rc)
            out.append(c)
    return out


def assign_unique(slug: str, rel: str, taken_files: set[str], taken_tags: set[str],
                  force_suffix: bool) -> tuple[str, str]:
    """Return a unique (deck_file, tag), suffixing with a path hash as needed."""
    base_file = f"{slug}.md"
    base_tag = f"#flashcards/{slug}"
    reserved = base_file.lower() in C.RESERVED_DECK_NAMES or slug.startswith("_")
    clash = base_file.lower() in taken_files or base_tag in taken_tags
    if not (force_suffix or reserved or clash):
        return base_file, base_tag
    length = 6
    while True:
        h = C.path_hash(rel, length)
        deck_file = f"{slug}--{h}.md"
        tag = f"#flashcards/{slug}-{h}"
        if (deck_file.lower() not in taken_files and tag not in taken_tags
                and deck_file.lower() not in C.RESERVED_DECK_NAMES):
            return deck_file, tag
        length += 1
        if length > 64:  # pathological guard
            C.fail(f"could not assign a unique deck name for {rel}")


def main(argv):
    args = parse_args(argv)
    source_dir = Path(args.source_dir)
    out_dir = Path(args.out_dir)
    vault_root = Path(args.vault_root)

    if not source_dir.is_dir():
        C.fail(f"source dir does not exist: {source_dir}")
    if not vault_root.is_dir():
        C.fail(f"vault root does not exist: {vault_root}")

    out_rel = rel_posix(out_dir, vault_root) if out_dir.resolve().is_relative_to(
        vault_root.resolve()) else "\0not-in-vault"

    ignore_files = discover_ignore_files(args, source_dir, vault_root)
    ignore_patterns = C.load_ignore_patterns(args.ignore, ignore_files)

    # --- Load INDEX (recover on corruption). ---
    try:
        index = C.load_index(out_dir)
        recovered = False
    except ValueError as exc:
        C.warn(f"{exc} — reconstructing from deck files")
        # Backup is the writer's job on write; scan just reconstructs read-only.
        index = C.reconstruct_index(out_dir, vault_root)
        recovered = True
    indexed = index.get("notes", {})

    # --- Discover source notes. ---
    on_disk: set[str] = set()      # every .md rel path that physically exists
    scanned: list[dict] = []       # candidate notes after skip+ignore
    ignored_rel: set[str] = set()

    for path in sorted(source_dir.rglob("*.md")):
        if not path.is_file():
            continue
        rel = rel_posix(path, vault_root)
        on_disk.add(rel)
        if is_builtin_skipped(rel, out_rel):
            continue
        if C.path_is_ignored(rel, ignore_patterns):
            ignored_rel.add(rel)
            continue
        scanned.append({"path": path, "rel": rel})

    # --- Rename matching: index entries whose source is gone from disk. ---
    missing_indexed = {rel: e for rel, e in indexed.items() if rel not in on_disk}
    hash_to_missing: dict[str, list[str]] = {}
    for rel, e in missing_indexed.items():
        h = e.get("hash")
        if h:
            hash_to_missing.setdefault(h, []).append(rel)

    # --- Build the taken-name sets from sticky indexed entries. ---
    taken_files = {e.get("deck_file", "").lower() for e in indexed.values() if e.get("deck_file")}
    taken_tags = {e.get("tag") for e in indexed.values() if e.get("tag")}
    taken_files |= set(C.RESERVED_DECK_NAMES)

    # First pass: compute slugs + group sizes for NEW notes.
    note_meta = []
    slug_groups: dict[str, int] = {}
    for item in scanned:
        path, rel = item["path"], item["rel"]
        title = C.title_from_path(path)
        h = C.source_hash(path)
        meta = {"path": path, "rel": rel, "title": title, "hash": h,
                "word_count": len(C.read_text(path).split())}
        note_meta.append(meta)
        if rel not in indexed:
            slug = C.slugify(title, rel)
            meta["slug"] = slug
            # Defer rename check to the second pass.

    consumed_old: set[str] = set()
    notes_out = []

    for meta in note_meta:
        path, rel, h, title = meta["path"], meta["rel"], meta["hash"], meta["title"]

        # --- Already indexed: sticky name, status by hash + deck presence. ---
        if rel in indexed:
            entry = indexed[rel]
            deck_file = entry.get("deck_file")
            tag = entry.get("tag")
            deck_path = out_dir / deck_file if deck_file else None
            if entry.get("hash") == h:
                status = "up-to-date" if (deck_path and deck_path.is_file()) else "deck-missing"
            else:
                status = "changed"
            # Sticky-name migration hint (never auto-rename).
            current_slug = C.slugify(title, rel)
            migrate = bool(deck_file) and deck_file != f"{current_slug}.md" \
                and not deck_file.startswith(f"{current_slug}--")
            notes_out.append({
                "path": str(path), "rel_path": rel, "title": title,
                "word_count": meta["word_count"], "status": status,
                "deck_file": deck_file, "tag": tag,
                "naming_migration_available": migrate,
                "matched_old_path": None,
            })
            continue

        # --- New on this path: maybe a rename of a missing indexed note. ---
        candidates = [old for old in hash_to_missing.get(h, []) if old not in consumed_old]
        if len(candidates) == 1:
            old = candidates[0]
            consumed_old.add(old)
            old_entry = indexed[old]
            new_slug = C.slugify(title, rel)
            proposed_file = f"{new_slug}.md"
            notes_out.append({
                "path": str(path), "rel_path": rel, "title": title,
                "word_count": meta["word_count"], "status": "renamed",
                "deck_file": old_entry.get("deck_file"), "tag": old_entry.get("tag"),
                "matched_old_path": old,
                "proposed_deck_file": proposed_file if proposed_file != old_entry.get("deck_file") else None,
                "proposed_tag": f"#flashcards/{new_slug}" if f"#flashcards/{new_slug}" != old_entry.get("tag") else None,
            })
            continue
        if len(candidates) > 1:
            notes_out.append({
                "path": str(path), "rel_path": rel, "title": title,
                "word_count": meta["word_count"], "status": "rename-ambiguous",
                "deck_file": None, "tag": None,
                "rename_candidates": candidates,
            })
            continue

        # --- Genuinely new: assign a unique, collision-safe name. ---
        slug = meta["slug"]
        force = sum(1 for m in note_meta
                    if m.get("slug") == slug) > 1
        deck_file, tag = assign_unique(slug, rel, taken_files, taken_tags, force)
        taken_files.add(deck_file.lower())
        taken_tags.add(tag)
        notes_out.append({
            "path": str(path), "rel_path": rel, "title": title,
            "word_count": meta["word_count"], "status": "new",
            "deck_file": deck_file, "tag": tag,
            "matched_old_path": None,
        })

    # --- Ignored-but-previously-carded. ---
    for rel in sorted(ignored_rel):
        if rel in indexed:
            entry = indexed[rel]
            notes_out.append({
                "path": str(vault_root / rel), "rel_path": rel,
                "title": PurePosixPath(rel).stem, "status": "ignored",
                "deck_file": entry.get("deck_file"), "tag": entry.get("tag"),
            })

    # --- Stale: missing indexed paths not consumed by rename and not ignored. ---
    stale = []
    for rel, entry in sorted(missing_indexed.items()):
        if rel in consumed_old or rel in ignored_rel:
            continue
        stale.append({"path": rel, "deck_file": entry.get("deck_file"),
                      "status": "index-stale", "exists": False})

    notes_out.sort(key=lambda n: n["rel_path"])
    result = {"notes": notes_out, "stale_entries": stale}
    if recovered:
        result["index_recovered"] = True
    C.emit_json(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
