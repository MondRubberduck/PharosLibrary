"""Words, stems, themes and query matching -- ONE implementation.

Every importer (index side) and every search endpoint (query side) cuts
and compares words through this module. Before it existed the same
helpers lived in five places with different rules: the importer knew
that 'benches' is 'bench', the query side did not, so `q=benches`
answered "0 results" for a library with 38 benches -- and an agent that
is told "not in the library" models the asset from scratch.

Stdlib only; no state besides small caches.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from functools import lru_cache

_RUN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# camelCase and letter<->digit boundaries inside one alphanumeric run
_CUT_RE = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")
# numbers and variant codes ('01', '01a', '300') carry no meaning as tags
_CODE_RE = re.compile(r"^\d+[a-z]{0,2}$")

_IRREGULAR = {
    "shelves": "shelf", "knives": "knife", "leaves": "leaf",
    "wives": "wife", "lives": "life",
    "wolves": "wolf", "halves": "half", "loaves": "loaf",
    "feet": "foot", "teeth": "tooth", "men": "man", "women": "woman",
    "mice": "mouse", "geese": "goose", "children": "child",
    "wooden": "wood", "golden": "gold", "woolen": "wool",
    "woollen": "wool", "rusty": "rust", "rusted": "rust",
}

# Aliases, applied on the QUERY side in every section (they used to work
# for animation search only: q=couch found 1 model, q=sofa 12). A synonym
# hit is a weaker match than the word itself. Kept small on purpose.
SYNONYMS = {
    "ac": ["acunit", "airconditioner", "hvac"],
    "tv": ["television", "monitor", "screen"],
    "car": ["vehicle", "automobile", "sedan", "suv"],
    "bike": ["motorcycle", "motorbike", "scooter"],
    "shack": ["favela", "slum", "shanty", "cabin", "shed"],
    "trash": ["garbage", "rubbish", "waste"],
    "sofa": ["couch", "settee"],
    "gun": ["firearm", "pistol", "rifle", "weapon"],
    "phone": ["smartphone", "mobile", "telephone"],
    "ambience": ["ambiance", "ambient", "atmosphere"],
    "streetlight": ["streetlamp", "lamppost", "lampost"],
    "plant": ["vegetation", "foliage", "flora"],
}


# connective words that are no tags
STOPWORDS = frozenset(("the", "and", "with", "for", "from", "into"))


def fold(s: str) -> str:
    """Lower-case and accent-fold ('Vâse' -> 'vase'): names in non-English
    libraries are full of accents, queries are typed on a plain keyboard."""
    s = (s or "").lower()
    if s.isascii():
        return s
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def _camel(run: str) -> str:
    out = []
    for i, c in enumerate(run):
        if i and c.isupper() and (run[i - 1].islower() or run[i - 1].isdigit()):
            out.append(" ")
        out.append(c)
    return "".join(out)


@lru_cache(maxsize=65536)
def tokens(text: str) -> tuple:
    """Every token of `text`, folded: split on non-alphanumerics, camelCase
    and letter/digit boundaries ('SM_StreetLamp01' -> sm street lamp 01)."""
    out = []
    for run in _RUN_RE.findall(text or ""):
        for part in _camel(run).split():
            out.extend(p for p in _CUT_RE.split(fold(part)) if p)
    return tuple(out)


def words(text: str, stop: frozenset = frozenset()) -> list:
    """Human-readable tag words: tokens longer than 2 characters, without
    numbers / variant codes, de-duplicated in order."""
    return list(dict.fromkeys(
        t for t in tokens(text)
        if len(t) > 2 and not _CODE_RE.match(t) and t not in stop))


@lru_cache(maxsize=65536)
def variants(word: str) -> frozenset:
    """The word plus its plain inflection variants (plural, -ing, -ed).

    Used on BOTH sides, so 'benches' and 'bench' meet whichever way the
    name and the query are spelled. A variant only ever matches a WHOLE
    token: the junk some rules produce ('trees' -> 'tre') can therefore
    not match 'trellis'."""
    w = fold(word)
    out = {w}
    n = len(w)
    if w in _IRREGULAR:
        out.add(_IRREGULAR[w])
    if n > 4 and w.endswith("ies"):
        out.add(w[:-3] + "y")
    if n > 4 and w.endswith("ves"):
        out.add(w[:-3] + "f")          # '-fe' only via _IRREGULAR: 'caves'
                                       # is not 'cafe'
    if n > 3 and w.endswith("es"):
        out.add(w[:-2])
    if n > 3 and w.endswith("s") and not w.endswith("ss"):
        out.add(w[:-1])
    for suf in ("ing", "ed"):
        if n > len(suf) + 2 and w.endswith(suf):
            base = w[:-len(suf)]
            out.add(base)
            out.add(base + "e")                       # damaged -> damage
            if len(base) > 2 and base[-1] == base[-2]:
                out.add(base[:-1])                    # running -> run
    return frozenset(v for v in out if v)


@lru_cache(maxsize=65536)
def _stems(text: str) -> tuple:
    out = []
    for t in tokens(text):
        if len(t) < 2 or t.isdigit():
            continue
        out.append(t)
        out.extend(sorted(variants(t) - {t}))
    return tuple(dict.fromkeys(out))


def stems(text: str) -> list:
    """Index-side searchable words: every token (2+ chars, no bare
    numbers) with its variants, de-duplicated in order. Cached: pack,
    folder and material names repeat across thousands of rows."""
    return list(_stems(text or ""))


_PREPARED: dict = {}     # id(table) -> (table, [(theme, folded keywords)], cache)


def match_themes(text: str, table: dict, strict: bool = False) -> list:
    """Themes whose keyword occurs in `text` AS A WORD.

    The old test was `keyword in text` on the raw string: 'bus' tagged
    every bush as a vehicle, 'tree' every 'street', 'house' every model
    of a pack with 'house' in its name (158 of 177 'vehicle' models were
    bushes, cardboard and carpets). A keyword now has to equal a token (or one of its
    variants); keywords of 5+ letters may also start or end a longer
    compound token ('street' in 'streetlamp', 'house' in 'lighthouse').
    `strict` keeps the texture/audio/purchase rule: one long keyword
    (5+) or two distinct keywords."""
    prep = _PREPARED.get(id(table))
    if prep is None or prep[0] is not table:
        prep = _PREPARED[id(table)] = (
            table, [(theme, [fold(k) for k in kws])
                    for theme, kws in table.items()], {})
    cache = prep[2]
    done = cache.get((text, strict))
    if done is not None:
        return list(done)
    seq = tokens(text)
    toks = set(seq)
    forms = set()
    for t in toks:
        forms |= variants(t)
        for part in COMPOUNDS.get(t, ()):
            forms |= variants(part)
    for a, b in zip(seq, seq[1:]):
        if a.isalpha() and b.isalpha() and (len(a) > 1 or len(b) > 1):
            forms.add(a + b)
    long_toks = [t for t in toks if len(t) > 5]
    hits = []
    for theme, kws in prep[1]:
        found = [k for k in kws if k in forms or (
            long_toks and len(k) >= 5 and any(
                t != k and (t.startswith(k) or t.endswith(k))
                for t in long_toks))]
        if strict:
            ok = len(found) >= 2 or (len(found) == 1 and len(found[0]) >= 5)
        else:
            ok = bool(found)
        if ok:
            hits.append(theme)
    if len(cache) < 50000:          # pack / category names repeat per row
        cache[(text, strict)] = tuple(hits)
    return hits


def synonyms(word: str) -> frozenset:
    """Variants of the aliases of `word` (never the word itself)."""
    w = fold(word)
    mine = variants(w)
    out = set()
    for base, syns in SYNONYMS.items():
        group = {base, *syns}
        if mine & group:
            for g in group:
                out |= variants(g)
    return frozenset(out - mine)


# ---------------------------------------------------------------------------
# query side
# ---------------------------------------------------------------------------

# Connective words of a natural query ('door with frame', 'sound of rain').
# They are OPTIONAL: not required for an AND match, a small bonus when they
# do hit (so 'knock on' still ranks first for 'knock on'). Required only
# when the query consists of nothing else.
CONNECTIVES = frozenset((
    "the", "and", "with", "for", "from", "into", "of", "on", "in", "at",
    "to", "by", "or", "an"))


def _code(piece: str) -> str:
    """A letters+digits piece that splits into a 1-character part ('LOD0',
    'v2', '9mm', 'M16', '4K', 'A4') is ONE word, folded: cut apart, its
    single character was dropped and 'lod0' searched as 'lod' (LOD1 rows
    came back as exact matches). '' for anything else."""
    parts = [p for p in _CUT_RE.split(fold(piece)) if p]
    if len(parts) > 1 and min(len(p) for p in parts) == 1:
        return "".join(parts)
    return ""


def _pieces(text: str) -> list:
    """camelCase pieces of `text` in original case, per alphanumeric run."""
    return [p for run in _RUN_RE.findall(text or "") for p in _camel(run).split()]


class Query:
    """A parsed search string.

    groups    one dict per REQUIRED query word: {'word', 'exact' (variants),
              'ctx' (variants + stored plural forms), 'syn' (alias variants),
              'code' (True for a letters+digits code such as 'lod0')}
    optional  connective words ('with', 'of' ...), same shape: not required
    exclude   one list per '-term': alternatives, each a list of variant sets
              that must ALL be present for a row to be dropped
    joined    the 2-3 query words glued together ('street lamp' ->
              'streetlamp'), or ''
    empty     a non-blank string that left no usable word: matches nothing
    """

    __slots__ = ("raw", "groups", "optional", "exclude", "joined", "empty")

    def __init__(self, q: str, use_synonyms: bool = True):
        # NOT lower-cased before tokens(): camelCase ('PropFuseBox') is only
        # visible in the original case
        text = " " + (q or "").strip()
        self.raw = text.strip().lower()
        self.groups, self.optional, self.exclude = [], [], []
        for m in re.finditer(r"(?<=\s)-([^\W_][\w-]*)", text, re.UNICODE):
            # '-sci-fi' excludes the glued word and the phrase, never turns
            # its second half into a required word
            parts = [t for t in tokens(m.group(1)) if len(t) >= 2]
            if not parts:
                continue
            alts = [[variants("".join(parts))]]
            if len(parts) > 1:
                alts.append([variants(p) for p in parts])
            self.exclude.append(alts)
        text = re.sub(r"(?<=\s)-[^\W_][\w-]*", " ", text, flags=re.UNICODE)
        seen = set()
        found = []
        for piece in _pieces(text):
            code = _code(piece)
            if code:
                found.append((code, True))
                continue
            for t in [p for p in _CUT_RE.split(fold(piece)) if p]:
                if len(t) >= 2:              # 1-char tokens match everything
                    found.append((t, False))
        for t, is_code in found:
            if t in seen:
                continue
            seen.add(t)
            ex = frozenset((t,)) if is_code else variants(t)
            self.groups.append({
                "word": t, "exact": ex, "code": is_code,
                # context lists written by older importers / the scanner
                # hold plain words without their variants: meet a stored
                # plural half-way ('character' vs the tag 'characters')
                "ctx": ex | ({t} if is_code else {t + "s", t + "es"}),
                "syn": (synonyms(t) if use_synonyms and not is_code
                        else frozenset())})
        if any(g["word"] not in CONNECTIVES for g in self.groups):
            self.optional = [g for g in self.groups if g["word"] in CONNECTIVES]
            self.groups = [g for g in self.groups
                           if g["word"] not in CONNECTIVES]
        plain = [g["word"] for g in self.groups]
        self.joined = "".join(plain) if 2 <= len(plain) <= 3 else ""
        self.empty = bool(self.raw) and not self.groups and not self.exclude

    def __bool__(self) -> bool:
        return bool(self.groups)

    @property
    def stems(self) -> set:
        out = set()
        for g in self.groups:
            out |= g["exact"]
        return out


# Single-word compounds whose parts a search should reach: 'door' finds
# 'Doorway'. A short word never matches inside a longer one in general
# ('car' is not 'cardboard'), so the real compounds are listed instead.
COMPOUNDS = {
    "doorway": ("door", "way"), "doorframe": ("door", "frame"),
    "doormat": ("door", "mat"), "frontdoor": ("front", "door"),
    "signage": ("sign",), "signpost": ("sign", "post"),
    "handrail": ("hand", "rail"), "railway": ("rail", "way"),
    "railroad": ("rail", "road"), "lamppost": ("lamp", "post"),
    "streetlamp": ("street", "lamp"), "streetlight": ("street", "light"),
    "bedside": ("bed", "side"), "bookshelf": ("book", "shelf"),
    "bookcase": ("book", "case"), "notebook": ("note", "book"),
    "sidewalk": ("side", "walk"), "rooftop": ("roof", "top"),
    "fireplace": ("fire", "place"), "firewood": ("fire", "wood"),
    "trashcan": ("trash", "can"), "dustbin": ("dust", "bin"),
    "wallpaper": ("wall", "paper"), "armchair": ("arm", "chair"),
    "staircase": ("stair", "case"), "stairway": ("stair", "way"),
    "doghouse": ("dog", "house"), "lighthouse": ("light", "house"),
    "tabletop": ("table", "top"), "barstool": ("bar", "stool"),
}


@lru_cache(maxsize=65536)
def name_forms(name: str) -> tuple:
    """(whole-word forms, long tokens, compound parts) of a name, cached.

    Whole-word forms = every token's variants, each letters+digits code
    as written ('LOD0' -> 'lod0'), and each pair of ADJACENT alphabetic
    tokens glued together (not two single letters), so the query
    'streetlamp' meets the name 'Street Lamp' and 'neonpunk' a 'NeonPunk'
    pack, while 'Tire A C' does not become 'ac'."""
    toks = tokens(name)
    forms = set()
    parts = set()
    for t in toks:
        forms |= variants(t)
        for p in COMPOUNDS.get(t, ()):
            parts |= variants(p)
    for piece in _pieces(name):
        c = _code(piece)
        if c:
            forms.add(c)
    for a, b in zip(toks, toks[1:]):
        if a.isalpha() and b.isalpha() and (len(a) > 1 or len(b) > 1):
            forms.add(a + b)
    return (frozenset(forms), tuple(t for t in toks if len(t) > 5),
            frozenset(parts - forms))


def searchable(*lists) -> set:
    """Fold a row's tag/stem/theme lists into one comparable set."""
    out = set()
    for lst in lists:
        for t in lst or ():
            if isinstance(t, str):
                out.add(t if t.isascii() and t.islower() else fold(t))
    return out


def _part_of(word_forms, long_toks, glued: bool) -> bool:
    """A word that starts or ends a longer name word, with at least three
    letters left over ('chair' in 'armchair', 'walk' in 'catwalk').
    Anywhere inside, or with less left over, it is a different word:
    'round' in 'ground', 'range' in 'orange', 'actor' in 'factory',
    'cabin' in 'cabinet', 'male' in 'female'. 3-4 letter words only with
    `glued` (clip titles glue words: 'sitchair')."""
    for w in word_forms:
        n = len(w)
        if n >= 5 or (glued and n >= 3):
            for t in long_toks:
                if len(t) - n >= 3 and (t.startswith(w) or t.endswith(w)):
                    return True
    return False


def hits(query: Query, name: str, context: set, extra=frozenset(),
         glued: bool = False) -> list:
    """Per REQUIRED query word, how well a row matches:

      3  the word (or an inflection of it) is a whole word of the name
      2  the word starts or ends a longer name word ('chair' in
         'armchair', see _part_of) or is a part of a listed compound
         ('door' in 'Doorway')
      1  the word is only in the row's context (tags, themes, category
         words) or in `extra` (pack / folder / material words), or only an
         alias of it matches (aliases never reach into `extra`: 'trash'
         would otherwise pull in every row of a pack named after it)
      0  no match

    Short words never match as a prefix: 'car' is not 'cardboard'.
    Adjacent query words are also tried glued ('sci fi door': 'scifi')."""
    forms, long_toks, parts = name_forms(name)
    groups = query.groups
    out = []
    for g in groups:
        if g["exact"] & forms:
            out.append(3)
        elif not g["code"] and (_part_of(g["exact"], long_toks, glued)
                                or g["exact"] & parts):
            out.append(2)
        elif (g["ctx"] & context or g["ctx"] & extra
              or (g["syn"] and (g["syn"] & forms or g["syn"] & context))):
            out.append(1)
        else:
            out.append(0)
    if len(out) > 1 and min(out) < 3:
        for i in range(len(out) - 1):
            if out[i] == 3 and out[i + 1] == 3:
                continue
            j = variants(groups[i]["word"] + groups[i + 1]["word"])
            if j & forms:
                out[i] = out[i + 1] = 3
            elif j & context or j & extra:
                out[i], out[i + 1] = max(out[i], 1), max(out[i + 1], 1)
    if query.joined and not all(out):
        # the glued form of the whole query: 'trash can' -> 'trashcan'
        j = variants(query.joined)
        if j & forms:
            return [3] * len(out)
        if j & context or j & extra:
            return [max(h, 1) for h in out]
    return out


def bonus(query: Query, name: str) -> int:
    """Optional (connective) words that are whole words of the name."""
    if not query.optional:
        return 0
    forms = name_forms(name)[0]
    return sum(1 for g in query.optional if g["exact"] & forms)


def excluded(query: Query, name: str, context: set,
             extra=frozenset()) -> bool:
    if not query.exclude:
        return False
    have = name_forms(name)[0] | context | extra
    return any(all(vs & have for vs in alt)
               for alts in query.exclude for alt in alts)


def tier(row_hits: list) -> int:
    """1 = every word is a whole word of the name, 2 = every word matches
    somehow, 3 = only some words match (served as OR fallback only)."""
    if all(h == 3 for h in row_hits):
        return 1
    return 2 if all(row_hits) else 3


def rarity_rank(entries: list, query: Query) -> None:
    """Sort OR-fallback entries in place by token RARITY: a row hitting
    the selective word ('cobblestone', 24 rows) outranks one hitting only
    the broad word ('medieval', 1,400 rows); name hits count double.
    Each entry needs '_hits' (the list from hits())."""
    freq = [0] * len(query.groups)
    for e in entries:
        for i, h in enumerate(e["_hits"]):
            if h:
                freq[i] += 1
    entries.sort(key=lambda e: -sum(
        (2.0 if h >= 2 else 1.0) / freq[i]
        for i, h in enumerate(e["_hits"]) if h))


def did_you_mean(query: Query, vocabulary, missing_only=None) -> dict:
    """Closest library words for query words that matched nothing
    ('barel' -> 'barrel'): a zero answer must not read as 'not owned'.
    Words under 3 letters and codes are not guessed at ('fi' -> 'fix')."""
    vocab = [v for v in vocabulary if len(v) > 2]
    out = {}
    for g in query.groups:
        w = g["word"]
        if len(w) < 3 or g["code"]:
            continue
        if missing_only is not None and w not in missing_only:
            continue
        near = [m for m in difflib.get_close_matches(w, vocab, n=3, cutoff=0.8)
                if m not in g["exact"]]
        if near:
            out[w] = near
    return out
