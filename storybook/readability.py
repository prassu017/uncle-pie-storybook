"""Cheap, deterministic checks that run before the LLM judge.

Language models are bad at counting, so the numbers come from code and are passed
to the judge as facts. Every problem names its page, so the note can be sent to
exactly the page that needs it, and says whether it blocks printing.

The limits here are the single source of truth: the writer's own check uses the
same numbers, so a page the writer is allowed to produce is never blocked later.
"""
import re

MAX_SENTENCE_WORDS = 16           # a 5-year-old loses the thread in longer sentences
PAGE_WORDS = (45, 120)            # hard bounds for one page
TARGET_GRADE = 3.5                # Flesch-Kincaid; 2-3 suits ages 5-7

# Words that should not appear in a bedtime story for ages 5-10. Blocking.
BLOCKLIST = {
    "kill", "kills", "killed", "killing", "blood", "bloody", "dead", "death", "die", "died", "dying",
    "murder", "gun", "guns", "knife", "stab", "hate", "stupid", "idiot", "dumb", "shut up",
    "corpse", "suicide", "drunk", "beer", "wine", "sexy", "kiss", "kissed", "demon", "hell",
    "slay", "slays", "slayed", "slain", "slew", "vanquish", "vanquished", "stabbed", "shoot", "shot",
}

# Grown-up abstractions and stock phrases gpt-3.5-turbo reaches for. Not blocking: noted on the page.
CLICHES = re.compile(
    r"\b(tapestry|intertwin\w*|newfound|testament|hearts? (?:aglow|glowing|full)|realization dawn\w*|"
    r"(?:comforting |warm )?embrace|unbreakable bond|bond (?:grew|strengthen\w*|formed|forming)|"
    r"tension|flicker of|a sense of|harmony|deftly|camaraderie|gratitude|unity|united in|"
    r"eyes (?:wide|sparkling|shining) with|side by side|pure (?:joy|happiness)|filled with (?:wonder|joy)|"
    r"closer than ever|true friendship is)\b", re.I)

# Present-tense narration ("Grandma places", "Robbie watches"). Dialogue is removed before checking.
# Past-tense markers. A narration sentence with none of them is probably in present tense
# ("T-Rex and Steggy explore the cave"); a page with two or more such sentences is flagged.
PAST = re.compile(
    r"\b(?:\w{3,}ed|was|were|had|did|said|ran|saw|went|came|found|felt|took|made|got|gave|knew|thought|told|"
    r"sat|stood|held|heard|began|became|left|brought|kept|flew|swam|ate|drank|fell|grew|threw|caught|built|"
    r"led|met|won|wore|woke|shook|hid|slid|sang|rang|spun|swung|dug|stuck|bit|lit|could|would|forgot|spoke|"
    r"broke|chose|drove|rode|rose|wrote|blew|drew|knelt|crept|slept|swept|wept|leapt|meant|sent|spent|bent|"
    r"hung|clung|flung|stung|struck|sprang|sank|shrank|beat|tore|swore|let|put|set|cut|hit|shut|hurt|cast|"
    r"spread|quit|split|might|should|lay|laid|paid|wound|fought|bought|sought|taught|ground|bound|slid|"
    r"fled|bled|fed|sped|stole|froze|wove|dove|shone|strode|sought|overcame|understood|became)\b", re.I)
_QUOTED = re.compile(r"[\"“][^\"”]*[\"”]|(?:^|\s)'[^']*'")

# "Telling" a feeling instead of showing it.
TELLING = re.compile(r"\b(?:felt|feeling|feels)\b", re.I)

_WORD = re.compile(r"[A-Za-z']+")
_SENT = re.compile(r"[^.!?]+[.!?]+[\"'”’]?|[^.!?]+$")


def _syllables(word: str) -> int:
    w = word.lower().strip("'")
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.findall(text) if _WORD.search(s)]


def page_metrics(text: str) -> dict:
    words = _WORD.findall(text)
    sents = sentences(text)
    n_w, n_s = len(words), max(1, len(sents))
    syl = sum(_syllables(w) for w in words)
    grade = 0.39 * (n_w / n_s) + 11.8 * (syl / max(1, n_w)) - 15.59 if n_w else 0.0
    longest = max(sents, key=lambda s: len(_WORD.findall(s)), default="")
    lowered = f" {text.lower()} "
    return {
        "words": n_w,
        "sentences": len(sents),
        "avg_sentence_words": round(n_w / n_s, 1),
        "longest_sentence_words": len(_WORD.findall(longest)),
        "longest_sentence": longest,
        "reading_grade": round(grade, 1),
        "has_dialogue": bool(re.search(r"[\"“”]|(?:^|\s)'\w", text)),
        "blocklisted_words": sorted(b for b in BLOCKLIST if re.search(rf"\b{re.escape(b)}\b", lowered)),
        "cliches": sorted({c.group(0).lower() for c in CLICHES.finditer(text)}),
        "telling": len(TELLING.findall(text)),
        "present_tense": _present_sentences(text),
    }


def _present_sentences(text: str) -> int:
    """Narration sentences (dialogue removed, 4+ words) with no past-tense verb at all."""
    narration = _QUOTED.sub(" ", text)
    return sum(1 for s in _SENT.findall(narration) if len(_WORD.findall(s)) >= 4 and not PAST.search(s))


_STOP = {"the", "a", "an", "and", "to", "of", "in", "on", "with", "his", "her", "their", "is", "was", "they",
         "he", "she", "it", "for", "at", "by", "from", "that", "this", "up", "out", "as", "be", "were", "had",
         "said", "i", "you", "we", "my", "your", "our", "them", "him", "into", "all", "so", "but", "not"}


def repeated_scenes(pages: list[str]) -> list[tuple[int, int]]:
    """Pairs of pages (1-based) that share most of their content words: the same scene told twice."""
    sets = [{w for w in _norm(p).split() if w not in _STOP and len(w) > 2} for p in pages]
    return [(i + 1, j + 1) for i in range(len(sets)) for j in range(i + 1, len(sets))
            if sets[i] and sets[j] and len(sets[i] & sets[j]) / len(sets[i] | sets[j]) >= 0.25]
# 0.25 was calibrated on real stories: a scene told twice shared 28% of its content words, while
# different pages of the same story shared at most 21%. A repeat retold in new words is not caught here.


def _norm(s: str) -> str:
    return " ".join(_WORD.findall(s.lower()))


def problem(page: int | None, text: str, blocking: bool) -> dict:
    return {"page": page, "text": text, "blocking": blocking}


def story_metrics(pages: list[str], refrain: str = "", refrain_pages: set[int] | None = None) -> dict:
    """refrain_pages: 0-based pages where the refrain is meant to appear (chosen by code)."""
    per_page = [page_metrics(p) for p in pages]
    probs = []
    lo, hi = PAGE_WORDS
    for i, (m, text) in enumerate(zip(per_page, pages), 1):
        if m["words"] < lo:
            probs.append(problem(i, f"Page {i} is too short ({m['words']} words); write at least {lo}.", True))
        if m["words"] > hi:
            probs.append(problem(i, f"Page {i} is too long ({m['words']} words); keep it under {hi}.", True))
        if m["longest_sentence_words"] > MAX_SENTENCE_WORDS:
            probs.append(problem(i, f"Page {i}: split this {m['longest_sentence_words']}-word sentence into two "
                                    f"short ones: \"{m['longest_sentence']}\"", True))
        if m["blocklisted_words"]:
            probs.append(problem(i, f"Page {i} uses words unsuitable for bedtime: {', '.join(m['blocklisted_words'])}.", True))
        if m["cliches"]:
            probs.append(problem(i, f"Page {i}: replace these stock or grown-up phrases with something concrete "
                                    f"a child can picture: {', '.join(m['cliches'])}.", False))
        if m["present_tense"] >= 2:
            probs.append(problem(i, f"Page {i} slips into present tense; tell the whole page in past tense "
                                    "(\"she walked\", not \"she walks\").", True))
        if m["telling"] >= 2:
            probs.append(problem(i, f"Page {i} names feelings ({m['telling']}x 'felt/feeling'); show them through "
                                    "faces, bodies and words instead.", False))
        if m["reading_grade"] > TARGET_GRADE + 2:  # only flag pages clearly too hard; fewer noise notes
            probs.append(problem(i, f"Page {i} reads at grade {m['reading_grade']}; use shorter sentences and "
                                    "simpler words (target grade 2-3).", False))
        if refrain:
            count = _norm(text).count(_norm(refrain))
            meant = refrain_pages is not None and (i - 1) in refrain_pages
            if count > 1:
                probs.append(problem(i, f"Page {i} says \"{refrain}\" {count} times; keep it to once.", True))
            elif meant and count == 0:
                probs.append(problem(i, f"Page {i}: have a character say \"{refrain}\" once.", False))
            elif refrain_pages is not None and not meant and count:
                probs.append(problem(i, f"Page {i}: remove the line \"{refrain}\"; it belongs on other pages.", False))
    for a, b in repeated_scenes(pages):
        probs.append(problem(b, f"Page {b} retells the same scene as page {a}; make page {b} move the story on "
                                "with something new.", True))
    if not any(m["has_dialogue"] for m in per_page):
        mid = len(pages) // 2 + 1
        probs.append(problem(mid, f"Page {mid}: add a few spoken lines; the story has no dialogue.", False))
    grades = [m["reading_grade"] for m in per_page]
    return {"pages": per_page, "avg_reading_grade": round(sum(grades) / max(1, len(grades)), 1),
            "problems": probs,
            "refrain_pages": sum(1 for p in pages if refrain and _norm(refrain) in _norm(p))}


def format_metrics(m: dict) -> str:
    lines = [f"Average reading grade (Flesch-Kincaid): {m['avg_reading_grade']} (target 2-3)"]
    for i, p in enumerate(m["pages"], 1):
        lines.append(
            f"Page {i}: {p['words']} words, {p['sentences']} sentences, avg {p['avg_sentence_words']} "
            f"words/sentence, longest {p['longest_sentence_words']}, dialogue={'yes' if p['has_dialogue'] else 'no'}"
        )
    found = [p["text"] for p in m["problems"]]
    lines.append("Problems found by code: " + ("; ".join(found) if found else "none"))
    return "\n".join(lines)
