# Tagging & Metatagging Proposal — Collection Assets

Status: proposal, 2026-09-13. The collection (256 purchases from
the crawler CSV) is the ground truth; this is how tagging should evolve.

## Current state (already live)

Every imported asset carries auto-derived tags in `collection.tags` (JSON):
name tokens (camelCase split, plural-stemmed), type taxonomy tokens
(group/category/sub — e.g. `environments`, `medieval`), store, seller.
The search already tiers: exact name → tags → adjacent concepts
(17 concept clusters like {medieval, castle, fortress, fantasy}).

## Proposal: three tag layers

1. **Facet tags (controlled, auto-assigned, hierarchical)**
   `store:fab` · `group:ue-3d-asset` · `cat:environments` · `sub:medieval`
   · `seller:leartes-studios`
   These come from the CSV structure — never hand-edit them. They power
   the outliner and exact filters.

2. **Theme tags (controlled vocabulary, auto + human)**
   A fixed theme list matching how the owner thinks: `medieval` `scifi`
   `cyberpunk` `horror` `nature` `city` `interior` `military` `fantasy`
   `victorian` `steampunk` `desert` `water` `japan` `ruins` `vehicles`
   `characters` `props` `textures` `kitbash`
   Auto-assigned by the concept clusters: when 2+ words of a product name
   hit a cluster, the theme tag is added. Human can add/remove in the
   detail modal (endpoint /api/collection/tag already exists).

3. **Free tags (human only)**
   Anything else worth remembering: `low-poly`, `game-ready`,
   `needs-repurchase`, `favorite`. Free-form, searchable at tier 2.

## Meta-tags (computed, never stored)

Derived at query time from fields — always current, zero maintenance:
- `image-only` — no meshes downloaded yet (true for most of the collection)
- `has-local-files` — Local Folder contains non-image files
- `price:free` / `price:paid` · `removed-from-store` (Type contains
  "listing removed" — 5 assets)
- `old-purchase` (Purchased before 2024)

## Auto-tagger pipeline (next iteration)

1. Import (live): name/type/seller tokens — done.
2. Theme pass: cluster matching on name + description — cheap, local.
3. Vision pass (optional, OFF by default, rule-3 safe): local Ollama
   vision model looks at the folder's images and proposes 2-4 theme tags
   with confidence. Human accepts/rejects in the modal — never auto-saved.
4. Review queue: "needs tags" filter (items with fewer than 3 theme tags),
   so the owner can curate 20 assets per coffee break instead of facing 256.

## Search contract (already implemented)

tier 1 = query words in the NAME · tier 2 = query in tags/seller/type or
name-words from the same concept cluster · tier 3 = adjacent cluster words
in tags. Every new tag is instantly searchable; `related`/`tag` badges on
cards show why a result matched.
