# obsidian-flashcards

Turn [Obsidian](https://obsidian.md) notes into flashcards for the
[**Spaced Repetition**](https://github.com/st3v3nmw/obsidian-spaced-repetition)
plugin (`st3v3nmw/obsidian-spaced-repetition`).

An agent **skill**: the model authors the card *content*, while two bundled
Python scripts own all the reliability-critical plumbing — scanning, stable
naming, ID-based merging, review-schedule preservation, and state tracking — so
behavior is deterministic and identical across agent harnesses.

> **Audience:** this README is for humans installing or porting the skill. The
> model-facing instructions live in [`SKILL.md`](./SKILL.md); the card-syntax
> spec is in [`references/sr-syntax.md`](./references/sr-syntax.md).

---

## What it does

- Reads notes from a directory you choose and generates flashcards from their
  durable, testable facts.
- Writes **one deck file per source note** into an output folder (default
  `Flashcards/`), e.g. `Flashcards/photosynthesis.md`, tagged
  `#flashcards/<slug-of-title>`.
- Tracks state in `Flashcards/INDEX.md` — a human dashboard table plus a
  script-managed JSON data block (each note's content hash, deck, and card IDs).
- **Never modifies your source notes.**
- Preserves the plugin's review schedules across note edits (see *How schedule
  preservation works*).

## Requirements

- **Python 3** (standard library only — no `pip install` needed).
- The Obsidian **Spaced Repetition** plugin, to review the generated decks.
- An agent harness that loads `SKILL.md`-style skills (Claude Code, etc.).

## Install

Copy the `obsidian-flashcards/` folder into your harness's skills directory:

```
~/.claude/skills/obsidian-flashcards/      # Claude Code (user-level)
```

That's it — the folder is self-contained:

```
obsidian-flashcards/
├── README.md                 # this file (human docs)
├── SKILL.md                  # model-facing instructions + workflow
├── references/
│   └── sr-syntax.md          # Spaced Repetition card-syntax spec
└── scripts/
    ├── common.py             # shared helpers (hashing, slugs, IDs, INDEX I/O)
    ├── scan.py               # diff a directory against INDEX → per-note status
    └── write_deck.py         # merge cards into a deck file by fc-id; update INDEX
```

## Usage

Invoke from natural language or the slash command:

```
make flashcards from my Resource/Biology folder, skip the archive
/obsidian-flashcards Resource/Biology --ignore Archive/
```

The skill determines your **vault root** and **source dir**, scans, shows you the
breakdown by status, authors cards per note, and writes the decks.

### Slash command

In harnesses that expose skills as slash commands (e.g. Claude Code), the skill
is available as **`/obsidian-flashcards`**. The command name matches the skill
folder. Everything after the command is passed through as arguments and mapped
onto the script flags:

```
/obsidian-flashcards                                   # asks which folder to card
/obsidian-flashcards Resource/Biology                  # card that folder
/obsidian-flashcards Resource/Biology --ignore Archive/
/obsidian-flashcards Resource --ignore "**/drafts/**" --ignore Archive/
```

| Argument | Meaning |
|---|---|
| *(none)* | Scan is run after you confirm the source folder. |
| `<path>` | Source folder to card (relative to the vault root). |
| `--ignore <pattern>` | Exclude matching notes; repeatable. Same syntax as `.fcignore`. |
| plain words | "…ignore Archive and Templates" is understood and converted to `--ignore` flags. |

Notes:

- A new skill may only register on the next session start — if `/obsidian-flashcards`
  doesn't autocomplete, restart the harness once.
- If the source folder or vault root is ambiguous, the skill asks before scanning.
- Updating after edits/renames is just re-running the command — see
  [*Updating after note changes*](#updating-after-note-changes).

### Ignoring folders

Three ways, which compose:

1. **Per-run flag** — `--ignore Archive/ --ignore "**/drafts/**"`
2. **Plain language** — "card my vault but ignore Archive and Templates"
3. **Persistent `.fcignore`** — a gitignore-style file at the vault root or source
   dir, read automatically every run:

   ```gitignore
   # .fcignore
   Archive/
   Projects/Private/
   **/templates/**
   *.excalidraw.md
   ```

**Pattern syntax:** vault-relative POSIX paths; `*` and `?` match within a single
path segment; `**` crosses directories; a trailing `/` matches a whole directory
subtree; `#` comments and blank lines are ignored; **no `!` negation** (v1).
Built-in skips (`.obsidian/`, `.trash/`, `.git/`, `templates/`, `node_modules/`,
and the output folder) always apply on top.

## How it works

### Scan statuses

`scan.py` classifies every candidate note so only the necessary work runs:

| Status | Meaning |
|---|---|
| `new` | Not yet carded. |
| `up-to-date` | Hash matches and the deck file exists → skipped. |
| `changed` | Source note edited since last run → re-card (schedules preserved). |
| `renamed` | Same content at a new path → adopt the old deck, IDs, schedules. |
| `rename-ambiguous` | Multiple candidates share the content hash → you choose. |
| `deck-missing` | Indexed but the deck file is gone → regen (schedules can't be saved). |
| `ignored` | Excluded by `.fcignore`/`--ignore`; existing deck left untouched. |
| `index-stale` | An INDEX entry whose source note no longer exists. |

### Stable, sticky deck names

Deck names derive from the note title (`Photosynthesis.md` →
`#flashcards/photosynthesis`). Once a note is in `INDEX.md` its name is **sticky**
— it never silently renames, even if a same-title note appears later (the newcomer
gets a short path-hash suffix instead). Names are guaranteed unique across the
whole vault.

### How schedule preservation works

The plugin records review schedules as `<!--SR:...-->` comments on each card. To
keep those across edits, this skill stamps every card with a hidden, stable
`<!--fc-id:...-->` (a UUID4). On re-run, `write_deck.py` matches cards **by ID**
and carries the existing `<!--SR:-->` schedule over to the (possibly reworded)
card.

- **Deterministic:** the script's by-ID merge and schedule carry-over.
- **Model-assisted:** when a note changes, the model must *reuse* the existing
  `fc-id` for a fact that still applies. That ID reuse is the only reliable way a
  reworded card keeps its schedule. New facts omit the ID; the script assigns one.

You never write `<!--SR:-->` comments yourself — the plugin owns them.

### Updating after note changes

To refresh a deck after editing a note, **re-run the skill on its folder**. What
happens depends on the kind of change (all schedule-preserving except where
noted):

| You did this | What to do | Result |
|---|---|---|
| **Edited a note** (same name) | Re-run `/obsidian-flashcards <folder>` | Reported `changed`. Reused `fc-id`s keep schedules; new facts added; dropped facts become orphans. |
| **Renamed only** (content identical) | Re-run | Auto-detected as `renamed`; the old deck, IDs, and schedules are adopted automatically. |
| **Renamed *and* edited** | Re-run; the skill adopts the old deck with `--matched-old-path` | The content hash changed, so the rename can't be auto-detected — the new path shows as `new` and the old entry as `index-stale`. The skill links them, preserving schedules by `fc-id`, and (optionally) migrates the deck/tag name to match the new title. |
| **Deleted a note** | Re-run | Old entry shows as `index-stale`; the skill reports it and can prune the orphaned deck on request. |

The underlying mechanic: schedules are matched **by `fc-id`**, not by card text —
so a fact keeps its review progress across rewordings and renames as long as its
ID is reused. For a renamed-and-edited note, name migration is **never automatic**
(deck names are sticky); the skill confirms whether to keep the old deck/tag name
or rename it to follow the note.

### Orphaned cards

When a fact is no longer generated for a note, its card becomes an *orphan*:

- **default** — kept reviewable in the deck (schedule intact);
- `--prune` — deleted;
- `--orphan-archive` — moved to an untagged `Flashcards/_Orphans.md` (inactive
  under tag-based decks; idempotent).

### Generated deck file

Each deck uses managed regions so your own edits outside them are preserved:

```markdown
<!-- obsidian-flashcards:header:start -->
> Source: [[Biology/Photosynthesis|Photosynthesis]]
#flashcards/photosynthesis
<!-- obsidian-flashcards:header:end -->

<!-- obsidian-flashcards:cards:start -->

What pigment absorbs light in photosynthesis?::Chlorophyll
<!--fc-id:550e8400-e29b-41d4-a716-446655440000-->

The Calvin cycle takes place in the ==stroma==.
<!--fc-id:6ba7b810-9dad-11d1-80b4-00c04fd430c8-->

<!-- obsidian-flashcards:cards:end -->
```

## Safety & guarantees

- Source notes are never modified.
- Deck and INDEX writes are atomic (temp file + replace).
- On any malformed input the whole write aborts with **no file changes** and a
  JSON error on stdout / message on stderr (non-zero exit).
- A corrupt or missing `INDEX.md` is backed up (`INDEX.md.bak.<timestamp>`) and
  reconstructed from existing deck files — sticky names and schedules survive.
- **Run writers sequentially.** There is no index lock, so concurrent
  `write_deck.py` runs against one output folder can corrupt `INDEX.md`.

## Running the scripts directly

The skill normally drives these, but they're plain CLI tools:

```bash
SK=~/.claude/skills/obsidian-flashcards/scripts

# 1. Scan (JSON on stdout, warnings on stderr)
python3 "$SK/scan.py" <source_dir> \
    --out-dir <vault>/Flashcards --vault-root <vault> \
    [--ignore <pattern> ...] [--ignore-file <path>]

# 2. Write one deck (card blocks on stdin, blank-line separated)
printf '%s' "$CARDS" | python3 "$SK/write_deck.py" \
    --source <note> --vault-root <vault> --out-dir <vault>/Flashcards \
    --deck-file <from-scan> --tag <from-scan> \
    [--matched-old-path <old>] [--prune | --orphan-archive]
```

Both exit `0` on success with a JSON result on stdout, or non-zero with a JSON
`{"error": ...}` and no file changes on failure.

## License / portability

Pure Python 3 standard library, no third-party dependencies, no harness-specific
calls. Copy the folder anywhere; only Python 3 is required.
