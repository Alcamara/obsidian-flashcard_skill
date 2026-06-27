# Obsidian Spaced Repetition — card syntax reference

Syntax for the `st3v3nmw/obsidian-spaced-repetition` plugin, plus the rules this
skill relies on. Separators below are the plugin defaults.

## Card types

### Single-line Q&A — `::`
One review card, front → back.
```
What pigment absorbs light in photosynthesis?::Chlorophyll
```

### Single-line reversed — `:::`
Two review cards (front→back and back→front). Use for term ↔ definition pairs
that are worth recalling in both directions.
```
Stroma:::Fluid-filled space surrounding the thylakoids
```

### Multi-line Q&A — `?`
Front above the `?`, back below. Use when either side needs multiple lines.
```
What three things happen in the light-dependent reactions?
?
- Water is split
- ATP is produced
- NADPH is produced
```

### Multi-line reversed — `??`
Like multi-line Q&A but generates both directions.
```
Calvin cycle
??
Light-independent reactions that fix CO2 into glucose in the stroma
```

### Cloze deletion — `==highlight==`
Hides the highlighted span. The surrounding sentence is the prompt.
```
The Calvin cycle takes place in the ==stroma==.
```

## Deck assignment

Every card in a deck file belongs to the file's `#flashcards` tag. This skill
writes one hierarchical tag per deck file, derived from the note title, e.g.
`#flashcards/photosynthesis`. You do not add tags to individual cards — the tag
lives once in the managed header.

## Scheduling comments — DO NOT WRITE THESE

After a review the plugin appends its own comment to a card:
```
What pigment absorbs light?::Chlorophyll
<!--SR:!2026-07-01,4,270-->
```
- **Never generate `<!--SR:-->`.** New cards have none; the plugin adds it.
- A reversed/multi card may pack **multiple** schedule entries in one comment.
  The writer script preserves the whole comment verbatim — you never touch it.

## fc-id comments — script-owned, but you reuse them

This skill tags each card with a hidden id so schedules survive edits:
```
What pigment absorbs light?::Chlorophyll
<!--fc-id:550e8400-e29b-41d4-a716-446655440000-->
```
- For a **new** fact: omit the id; `write_deck.py` assigns a UUID4.
- For a **still-valid existing** fact (note `changed`/`renamed`): copy its
  existing `<!--fc-id:-->` onto the line after your (possibly reworded) card so
  the card keeps its review schedule. This id reuse is the only reliable way to
  preserve a schedule across a reword.
- Put the `fc-id` on the line directly **after** the card text. Never invent or
  alter an id value.

## Authoring rules & pitfalls

- **One card per blank-line block.** Blocks are separated by a blank line; the
  writer relies on this.
- **No blank line inside a multi-line answer** — a blank line ends the block.
- **One cloze per card block.** Multiple clozes in one block create coupled
  cards; keep them separate for clean review.
- **Balanced cloze syntax:** `==text==`, never `=text==` or `==text=`.
- **Avoid `::` / `:::` in ordinary prose** unless it's the intended separator —
  a stray `::` turns a sentence into a card by accident.
- **Atomic cards:** one idea each. Split compound facts.
- **No trivia dumps / verbatim paste.** Card the testable claim, not the whole
  paragraph.
- **Cloze wording:** prefer the note's own sentence so the hidden span reads
  naturally in context.
- A malformed cloze or a `{{...}}`/Dataview-style expression colliding with
  card syntax causes plugin parse errors — write clean, balanced syntax and do
  not copy such constructs from source notes into cards.
