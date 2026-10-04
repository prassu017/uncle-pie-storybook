"""Uncle Pie's workshop: intake -> plan -> write -> (Vishnu reviews -> revise)* -> book.

`create_story` and `revise_story` are generators. They yield small event dicts as
they go, so the CLI can print progress and the web app can stream it live, and
the last event always carries the finished story.
"""
import copy
import json
import os
import re
from functools import lru_cache

from . import prompts as P
from .llm import REFUSE, call_json, moderate
from .readability import (MAX_SENTENCE_WORDS, PAGE_WORDS, format_metrics, page_metrics, problem,
                          story_metrics)

MAX_DRAFTS = 4            # first draft + up to three revisions
PASS_SCORE = 4            # every rubric score must reach this
MAX_FIXES = 6             # more notes than this and gpt-3.5-turbo starts dropping things
MIN_SENT, MAX_SENT = 6, 9
CRITERIA = ["age_fit", "comfort", "arc", "fidelity", "heart", "read_aloud", "moral"]
STOP = {"the", "a", "an", "and", "to", "of", "in", "on", "with", "his", "her", "their", "is", "was", "they",
        "he", "she", "it", "for", "at", "by", "from", "that", "this", "up", "out", "as", "be"}
META_WORDS = re.compile(r"\b(must[_ ]show|refrain|the plan|story arc|"
                        r"the family(?:'s)? (?:told|asked|said|words|wanted|wishe?d))\b", re.I)


# ------------------------------------------------------------------- validators

def _from_request(item: str, request_words: set[str]) -> bool:
    """A 'must include' idea has to come from the family's own words. gpt-3.5 invents plot points for a
    bare topic ("dinosaurs" -> "an earthquake traps them"), and fidelity would then demand them."""
    words = set(_words(item).split()) - STOP
    stems = {w[:5] for w in words}
    return bool(words) and len(stems & {w[:5] for w in request_words}) / len(stems) >= 0.34


def _check_intake(d, request: str = ""):
    if d.get("category") not in P.CATEGORIES:
        raise ValueError(f"category must be one of {list(P.CATEGORIES)}")
    d["pages"] = max(4, min(6, int(d.get("pages") or 5)))
    for k in ("characters", "must_include"):
        if not isinstance(d.get(k), list):
            raise ValueError(f"{k} must be a list")
    # The lesson is not an event; keep it out of the plot list even if the model put it there.
    moral = set(_words(str(d.get("moral", ""))).split())
    def is_moral(x):
        w = set(_words(x).split())
        return bool(w) and len(w & moral) / len(w) > 0.6
    req = set(_words(request).split()) - STOP
    d["must_include"] = [str(x) for x in d["must_include"]
                         if str(x).strip() and not is_moral(str(x)) and (not request or _from_request(str(x), req))][:7]
    if not isinstance(d.get("safety"), dict):
        raise ValueError("safety must be an object")


def tidy_moral(text: str) -> str:
    """One clean sentence: no 'Moral:' prefix or quotes, a capital first letter, a full stop at the end."""
    m = re.sub(r"^\s*(?:the\s+)?moral(?:\s+of\s+the\s+story)?(?:\s+is)?\s*[:\-]?\s*(?:that\s+)?", "",
               str(text or "").strip(), flags=re.I).strip().strip('"\'“”‘’').strip()
    if not m:
        return ""
    m = m[0].upper() + m[1:]
    return m if m[-1] in ".!?" else m + "."


def _check_moral(d):
    d["moral"] = tidy_moral(d.get("moral", ""))
    words = len(d["moral"].split())
    if not 6 <= words <= 25:
        raise ValueError(f"'moral' has {words} words; write one complete sentence of 8 to 18 words")


def _check_plan(n, distinct_events: bool = True):
    def v(d):
        _check_moral(d)
        pages = d.get("pages", [])
        if len(pages) != n:
            raise ValueError(f"plan must have exactly {n} pages, got {len(pages)}")
        if not all(isinstance(p, dict) and p.get("events") and p.get("picture") for p in pages):
            raise ValueError("every page needs 'events' and 'picture'")
        if not d.get("characters"):
            raise ValueError("characters with a 'look' are required")
        # Each page must move the story: no two pages may have the same new event (code checks overlap).
        news = [set(_words(str(p.get("new", ""))).split()) - STOP for p in pages]
        if any(not x for x in news):
            raise ValueError("every page needs 'new': the one thing that happens for the first time on it")
        for i in range(n if distinct_events else 0):
            for j in range(i + 1, n):
                overlap = len(news[i] & news[j]) / max(1, len(news[i] | news[j]))  # Jaccard
                if overlap >= 0.7:
                    raise ValueError(f"pages {i + 1} and {j + 1} have the same new event; give each page its own")
    return v


def assign_ideas(ideas: list[str], n: int) -> list[list[str]]:
    """Spread the family's ideas over the pages in order. Done in code, because the model
    is unreliable at this bookkeeping (it claims an idea is on a page when it is not)."""
    pages = [[] for _ in range(n)]
    k = max(1, len(ideas))
    for i, idea in enumerate(ideas):
        # Spread in order, leaning late: with one idea it lands on the turning point, not page 1.
        pages[min(n - 1, int((i + 0.7) * n / k))].append(idea)
    return pages


def _check_review(d):
    r = d.get("review", {})
    for c in CRITERIA:
        s = int(r[c]["score"])
        if not 1 <= s <= 5:
            raise ValueError(f"{c} score must be 1-5")
        r[c]["score"] = s
    if not isinstance(d.get("fixes", []), list):
        raise ValueError("fixes must be a list")


def _check_fidelity(n_items):
    def v(d):
        results = d.get("results")
        if not isinstance(results, list) or len(results) != n_items or not all(isinstance(r, dict) for r in results):
            raise ValueError(f"results must be a list of exactly {n_items} objects, one per event")
    return v


def _check_ledger(d):
    if not isinstance(d.get("events"), list) or not isinstance(d.get("who"), list):
        raise ValueError("'events' and 'who' must be lists of short strings")
    d["events"] = [str(e) for e in d["events"] if str(e).strip()][:3]
    d["who"] = [str(w) for w in d["who"] if str(w).strip()][:10]


def _check_logic(d):
    if not isinstance(d.get("problems"), list) or not all(isinstance(x, dict) for x in d["problems"]):
        raise ValueError("'problems' must be a list of objects with page, quote and problem")


# ------------------------------------------------------------------------ steps

def _fence_safe(text: str) -> str:
    """Family text sits between <<< and >>>; stop it from closing the fence early."""
    return text.replace("<<<", "< < <").replace(">>>", "> > >")


def intake(request: str, references: list[dict] | None = None) -> dict:
    note = ""
    if references:
        looks = "; ".join(f"{r['label']}: {r['look']}" for r in references)
        note = f"The family also uploaded photos of: {looks}. These are probably characters in the story.\n"
    prompt = P.INTAKE_PROMPT.format(request=_fence_safe(request), reference_note=note, data_only=P.DATA_ONLY,
                                    categories=list(P.CATEGORIES))
    brief = call_json(prompt, P.INTAKE_SYSTEM, temperature=0.0, validate=lambda d: _check_intake(d, request))
    # Code safety net: if the request uses violent words but intake called it fine, ask again, explicitly.
    if re.search(r"\b(funny|silly|laugh\w*|giggl\w*|jokes?|hilarious|goofy)\b", request, re.I):
        brief["must_include"].append("something silly happens that would make a child laugh out loud")
    risky = page_metrics(request)["blocklisted_words"]
    if risky and brief["safety"].get("ok", True):
        prompt += (f"\n\nIMPORTANT: this request includes {', '.join(risky)}. That is not suitable on the page for "
                   "ages 5-10: set ok=false, give a gentle reframe, and write every must_include item as its gentle version.")
        brief = call_json(prompt, P.INTAKE_SYSTEM, temperature=0.0, validate=lambda d: _check_intake(d, request))
    return brief


def plan(brief: dict, references: list[dict] | None = None) -> dict:
    ref_block = ""
    if references:
        ref_block = ("REFERENCE PHOTOS FROM THE FAMILY - draw matching characters to look like these:\n"
                     + "\n".join(f"- {r['label']}: {r['look']}" for r in references) + "\n")
    brief_for_plan = {k: v for k, v in brief.items() if k not in ("safety", "must_include")}
    if not brief["safety"].get("ok", True) and brief["safety"].get("reframe"):
        brief_for_plan["gentle_version"] = brief["safety"]["reframe"]
    n = brief["pages"]
    slots = assign_ideas(brief["must_include"], n)
    assignments = "\n".join(
        f"Page {i}: " + ("; ".join(s) if s else "free - connect the story") for i, s in enumerate(slots, 1))
    prompt = P.PLAN_PROMPT.format(
        pages=n, brief=json.dumps(brief_for_plan, indent=1),
        category=brief["category"], strategy=P.CATEGORIES[brief["category"]],
        reference_block=ref_block, assignments=assignments)
    try:
        story_plan = call_json(prompt, P.PLAN_SYSTEM, temperature=0.7, validate=_check_plan(n))
    except RuntimeError:
        # The distinct-events rule steers the planner but must not sink the story; the continuity
        # read in review still catches any repetition that gets through.
        story_plan = call_json(prompt, P.PLAN_SYSTEM, temperature=0.7, validate=_check_plan(n, distinct_events=False))
    for page, s in zip(story_plan["pages"], slots):
        page["must_show"] = s  # the writer sees exactly what each page owes the family
    return story_plan


def _plan_for_writer(story_plan: dict) -> str:
    """The writer sees the plan in plain words. Internal field names (must_show, covers...) leaked
    straight into the story when it saw raw JSON, e.g. 'the must_show moment lingered'."""
    lines = [f"Title: {story_plan.get('title', '')}",
             f"Line the characters repeat: \"{story_plan.get('refrain', '')}\"",
             "Characters: " + "; ".join(f"{c.get('name')} ({c.get('pronoun') + ', ' if c.get('pronoun') else ''}"
                                        f"{c.get('look')})" for c in story_plan["characters"])]
    for i, p in enumerate(story_plan["pages"], 1):
        owed = "; ".join(p.get("must_show") or [])
        lines.append(f"Page {i} ({p.get('beat', '')}): {p.get('events', '')} Feeling: {p.get('feeling', '')}."
                     + (f" This MUST happen on this page: {owed}." if owed else ""))
    lines.append(f"Lesson: {story_plan.get('moral', '')}")
    return "\n".join(lines)


def refrain_pages(n: int) -> set[int]:
    """Code decides where the refrain goes: first, middle and last page, once each."""
    return {0, n // 2, n - 1}


def _refrain_rule(story_plan: dict, i: int, n: int) -> str:
    refrain = story_plan.get("refrain", "")
    if refrain and i in refrain_pages(n):
        return f'A character says or thinks the line "{refrain}" exactly once on this page.'
    return "Do not use the repeated line on this page."


def _page_events(story_plan: dict, i: int) -> str:
    """What the writer must do on page i, including what already happened and must not be repeated."""
    p = story_plan["pages"][i]
    owed = "; ".join(p.get("must_show") or [])
    before = [q.get("new", "") for q in story_plan["pages"][:i] if q.get("new")]
    return (p.get("events", "")
            + (f" This MUST happen on this page: {owed}." if owed else "")
            + (f" NEW on this page: {p['new']}." if p.get("new") else "")
            + (" Already happened on earlier pages, so do NOT repeat or redo it: " + "; ".join(before) + "."
               if before else ""))


def _check_text_only(d):
    if not isinstance(d.get("text"), str) or len(d["text"].split()) < 20:
        raise ValueError("'text' must be the page as a string of at least 20 words")


def _check_page(d):
    _check_text_only(d)
    text = d["text"]
    m = page_metrics(text)
    if m["sentences"] < MIN_SENT - 1:
        raise ValueError(f"the page has only {m['sentences']} sentences; write {MIN_SENT}-{MAX_SENT}")
    # Same limits the judge enforces (readability.py), so a page that passes here is never blocked later.
    if m["longest_sentence_words"] > MAX_SENTENCE_WORDS:
        raise ValueError(f"this sentence has {m['longest_sentence_words']} words: \"{m['longest_sentence']}\". "
                         f"Keep every sentence to {MAX_SENTENCE_WORDS} words or fewer")
    if m["present_tense"] >= 2:
        raise ValueError("the narration slips into present tense; tell the whole page in past tense")
    lo, hi = PAGE_WORDS
    if not lo <= m["words"] <= hi:
        raise ValueError(f"the page has {m['words']} words; it must be {lo} to {hi} words")


def _page_rules(story_plan, i, n):
    return dict(min_sent=MIN_SENT, max_sent=MAX_SENT, refrain_rule=_refrain_rule(story_plan, i, n))


def write(story_plan: dict, n: int) -> dict:
    """Uncle Pie writes the pages in order, one focused call each, always seeing the story so far."""
    plan_txt, pages = _plan_for_writer(story_plan), []
    for i in range(n):
        so_far = "\n\n".join(f"Page {k + 1}: {p['text']}" for k, p in enumerate(pages)) or "(this is the first page)"
        prompt = P.WRITE_PAGE_PROMPT.format(
            i=i + 1, n=n, plan=plan_txt, so_far=so_far, events=_page_events(story_plan, i),
            feeling=story_plan["pages"][i].get("feeling", ""), example=" ".join(P.EXAMPLE_PAGE),
            **_page_rules(story_plan, i, n))
        if pages:
            prompt += ("\n\nWHAT HAS ALREADY HAPPENED (from the story so far; do not repeat any of it):\n"
                       + _ledger_so_far(pages))
        page = _write_page(prompt)
        names = [c.get("name", "") for c in story_plan["characters"]]
        repeats = [p for p in ledger_problems([q["text"] for q in pages] + [page], names,
                                              story_plan.get("refrain", "")) if p["page"] == len(pages) + 1]
        if repeats:  # one immediate retry with the exact problem
            page = _write_page(prompt + "\n\nYour last try had this problem: " + repeats[0]["text"])
        pages.append({"text": page})
    return {"title": story_plan.get("title", "A Bedtime Story"), "moral": story_plan.get("moral", ""), "pages": pages}


WRITER_MODE = os.getenv("WRITER_MODE", "pages")  # "pages": one call per page; "whole": one call, split by code


def _split_whole(text: str, n: int) -> list[str]:
    parts = re.split(r"\[(\d+)\]", text)
    sections = {int(parts[k]): parts[k + 1].strip() for k in range(1, len(parts) - 1, 2)}
    if sorted(sections) != list(range(1, n + 1)):
        raise ValueError(f"write exactly {n} sections marked [1] to [{n}], each marker once, in order")
    return [sections[k] for k in range(1, n + 1)]


def _check_whole(n):
    def v(d):
        pages = _split_whole(str(d.get("story", "")), n)
        for k, text in enumerate(pages, 1):
            try:
                _check_page({"text": text})
            except ValueError as e:
                raise ValueError(f"section [{k}]: {e}")
        d["pages"] = pages
    return v


def write_whole(story_plan: dict, n: int) -> dict:
    """Uncle Pie writes the WHOLE story in one call so it reads as one piece; code splits it into pages
    at the markers and checks every page against the same limits."""
    refrain = story_plan.get("refrain", "")
    rule = (f'Say the line "{refrain}" exactly once in each of sections '
            + ", ".join(f"[{k + 1}]" for k in sorted(refrain_pages(n))) + " and nowhere else.") if refrain else ""
    prompt = P.WRITE_WHOLE_PROMPT.format(
        plan=_plan_for_writer(story_plan), n=n, example=" ".join(P.EXAMPLE_PAGE),
        beats="\n".join(f"[{i + 1}] {_page_events(story_plan, i)}" for i in range(n)),
        min_sent=MIN_SENT, max_sent=MAX_SENT, refrain_rule=rule)
    try:
        out = call_json(prompt, P.WRITE_SYSTEM, temperature=0.8, max_tokens=2500, validate=_check_whole(n))
    except RuntimeError:
        return write(story_plan, n)  # fall back to page-by-page rather than fail the story
    return {"title": story_plan.get("title", "A Bedtime Story"), "moral": story_plan.get("moral", ""),
            "pages": [{"text": t} for t in out["pages"]]}


def route_notes(fixes: list[str], story_plan: dict, n: int) -> dict[int, list[str]]:
    """Send each editor's note to the page it is about. Notes naming no page go to every page."""
    routed: dict[int, list[str]] = {}
    per_page_cap = 3  # one rewrite reliably lands about three notes; fixes arrive most important first
    for f in fixes:
        pages = {int(m) - 1 for m in re.findall(r"[Pp]age (\d+)", f) if 0 < int(m) <= n}
        if not pages and "missing:" in f:
            item = f.split("missing:", 1)[1].rsplit(". Add it", 1)[0].strip()
            pages = {i for i, p in enumerate(story_plan["pages"]) if item in (p.get("must_show") or [])}
        for i in pages or range(n):
            if len(routed.setdefault(i, [])) < per_page_cap:
                routed[i].append(f)
    return routed


def revise(story_plan: dict, draft: dict, fixes: list[str], n: int, feedback: str = "") -> dict:
    """Rewrite only the pages that have notes, in page order, each seeing the pages already rewritten.
    (Rewriting pages in parallel against the old story let them contradict each other.)
    A failed rewrite keeps the old page."""
    routed = {i: fixes for i in range(n)} if feedback else route_notes(fixes, story_plan, n)
    plan_txt = _plan_for_writer(story_plan)
    current = {**draft, "pages": [dict(p) for p in draft["pages"]]}
    for i in sorted(routed):
        prompt = P.REVISE_PAGE_PROMPT.format(
            i=i + 1, plan=plan_txt, story=_story_text(current), events=_page_events(story_plan, i),
            notes="\n".join(f"- {f}" for f in routed[i]), **_page_rules(story_plan, i, n))
        if feedback:
            prompt = P.FEEDBACK_PROMPT.format(feedback=_fence_safe(feedback)) + "\n\n" + prompt
        try:
            current["pages"][i] = {"text": call_json(prompt, P.WRITE_SYSTEM, temperature=0.7, max_tokens=700,
                                                     validate=_check_page)["text"].strip()}
        except RuntimeError:
            # The rewrite still breaks a limit. Keep it anyway if it breaks fewer than the old page did.
            try:
                new = call_json(prompt, P.WRITE_SYSTEM, temperature=0.7, max_tokens=700,
                                validate=_check_text_only)["text"].strip()
            except RuntimeError:
                continue
            if _page_faults(new) < _page_faults(current["pages"][i]["text"]):
                current["pages"][i] = {"text": new}
    return current


def _past_tense(text: str) -> str:
    """One narrow job: put a page that slipped into present tense back into past tense. Kept only if code
    confirms the tense is fixed and nothing else got worse."""
    if page_metrics(text)["present_tense"] < 2:
        return text
    try:
        new = call_json(P.TENSE_PROMPT.format(page=text), P.WRITE_SYSTEM, temperature=0.0, max_tokens=700,
                        validate=_check_text_only)["text"].strip()
    except RuntimeError:
        return text
    fixed = page_metrics(new)["present_tense"] < 2 and _page_faults(new) < _page_faults(text)
    return new if fixed else text


def _write_page(prompt: str) -> str:
    try:
        page = call_json(prompt, P.WRITE_SYSTEM, temperature=0.8, max_tokens=700, validate=_check_page)
    except RuntimeError:
        # Length rules steer the writer but must not stop the story: accept any real page here,
        # and let Vishnu's review send it back with a specific note.
        page = call_json(prompt, P.WRITE_SYSTEM, temperature=0.8, max_tokens=700, validate=_check_text_only)
    return page["text"].strip()


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - one failed candidate shouldn't stop the others
        return None


def _page_faults(text: str) -> int:
    """How many hard limits one page breaks (used to keep the better of two imperfect versions)."""
    return sum(p["blocking"] for p in story_metrics([text])["problems"])


def _words(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", s.lower()))


def check_fidelity(items: list[str], draft: dict) -> list[dict]:
    """For each requested idea, get a quoted sentence and verify in code that it is really there."""
    if not items:
        return []
    prompt = P.FIDELITY_PROMPT.format(story=_story_text(draft),
                                      items="\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)))
    out = call_json(prompt, P.FIDELITY_SYSTEM, temperature=0.0, validate=_check_fidelity(len(items)))
    pages = [set(_words(p["text"]).split()) for p in draft["pages"]]
    checks = []
    for item, r in zip(items, out["results"]):
        # The model sometimes trims or re-punctuates its quote, so match on words, not exact text:
        # at least 85% of the quoted words must be on one page.
        qw = _words(str(r.get("quote", ""))).split()
        overlap = [len(set(qw) & t) / max(1, len(set(qw))) for t in pages]
        page = 1 + max(range(len(pages)), key=lambda i: overlap[i]) if qw else 0
        present = bool(r.get("shows_it")) and len(qw) >= 4 and page > 0 and overlap[page - 1] >= 0.85
        checks.append({"item": item, "page": page if present else 0, "present": present,
                       "quote": r.get("quote", "") if present else ""})
    return checks


def check_logic(draft: dict, refrain: str = "") -> list[dict]:
    """Continuity read, done twice independently. Each problem must quote its sentence (code keeps only
    quotes that really are on that page), and a problem counts as confirmed when both reads flag the
    same page. Confirmed problems block a pass; single sightings go back as notes."""
    first, second = (_logic_read(draft, refrain, t) for t in (0.0, 0.6))
    second_pages = {x["page"] for x in second}
    for x in first:
        x["confirmed"] = x["page"] in second_pages
    return first


def _logic_read(draft: dict, refrain: str, temperature: float) -> list[dict]:
    try:
        out = call_json(P.LOGIC_PROMPT.format(story=_story_text(draft), refrain=refrain or "(none)"),
                        P.LOGIC_SYSTEM, temperature=temperature, max_tokens=900, validate=_check_logic)
    except RuntimeError:
        return []
    found = []
    for x in out["problems"][:6]:
        try:
            page = int(x.get("page"))
        except (TypeError, ValueError):
            continue
        if not 1 <= page <= len(draft["pages"]):
            continue
        qw = set(_words(str(x.get("quote", ""))).split())
        text = set(_words(draft["pages"][page - 1]["text"]).split())
        if len(qw) >= 3 and len(qw & text) / len(qw) >= 0.85 and str(x.get("problem", "")).strip():
            found.append({"page": page, "quote": str(x["quote"])[:200], "problem": str(x["problem"])[:160]})
    return found


def judge(request: str, brief: dict, draft: dict, refrain: str = "",
          slots: list[list[str]] | None = None, cast: list[str] | None = None) -> dict:
    n = len(draft["pages"])
    metrics = story_metrics([p["text"] for p in draft["pages"]], refrain, refrain_pages(n) if refrain else None)
    probs = metrics["problems"]
    checklist = check_fidelity(brief.get("must_include") or [], draft)
    missing = [c["item"] for c in checklist if not c["present"]]
    logic = check_logic(draft, refrain)
    # Only confirmed problems (both reads agree) block: a single read raises false alarms often enough
    # that blocking on every finding made revisions oscillate.
    for x in logic:
        probs.append(problem(x["page"], f"Page {x['page']}: {x['problem']} (\"{x['quote']}\"). Fix it so the "
                                        "story makes sense.", x["confirmed"]))

    # The story ledger: repeated events and characters from nowhere, checked by code.
    probs.extend(ledger_problems([p["text"] for p in draft["pages"]],
                                 cast or [c.get("name", "") for c in brief.get("characters", [])], refrain))

    # Pasting the brief into the story ("They grow up and see...") reads badly aloud.
    for i, (page, ideas) in enumerate(zip(draft["pages"], slots or []), 1):
        for idea in ideas:
            if len(idea.split()) >= 5 and _words(idea) in _words(page["text"]):
                probs.append(problem(i, f"Page {i} copies the idea \"{idea}\" word for word; show it happening "
                                        "in your own words.", True))

    # A last safety net on the words a child will hear.
    flagged = [c for c in moderate(_story_text(draft)) if c in REFUSE]  # mild categories are intake's job
    if flagged:
        probs.append(problem(None, f"The story was flagged by the safety check ({', '.join(flagged)}); make every "
                                   "page gentle and safe for a young child.", True))

    # Words about how the story was made must never reach a child's ears.
    for i, page in enumerate(draft["pages"], 1):
        leaked = sorted({m.group(0).lower() for m in META_WORDS.finditer(page["text"])})
        if leaked:
            probs.append(problem(i, f"Page {i} talks about how the story was made ({', '.join(leaked)}); remove "
                                    "that and just tell the story.", True))

    fidelity_txt = "\n".join(
        f"- {'FOUND on page ' + str(c['page']) if c['present'] else 'MISSING'}: {c['item']}" for c in checklist
    ) or "(the family gave no specific events)"
    logic_txt = "\n".join(f"- Page {x['page']}: {x['problem']} (\"{x['quote']}\")" for x in logic
                          if x["confirmed"]) or "none"
    prompt = P.JUDGE_PROMPT.format(
        request=_fence_safe(request), moral=brief.get("moral", ""), story=_story_text(draft),
        metrics=format_metrics(metrics), fidelity=fidelity_txt, logic=logic_txt)
    review = call_json(prompt, P.JUDGE_SYSTEM, temperature=0.0, validate=_check_review)
    scores = {c: review["review"][c]["score"] for c in CRITERIA}

    # Code-verified facts override the judge's impression.
    if missing:
        scores["fidelity"] = min(scores["fidelity"], 2)
    if any(x["confirmed"] for x in logic):
        scores["arc"] = min(scores["arc"], 3)

    # Notes, most important first: blocking problems, missing requests, the judge's own notes, then polish.
    hard = [p["text"] for p in probs if p["blocking"]]
    soft = [p["text"] for p in probs if not p["blocking"]]
    judge_notes = [f for f in review.get("fixes", []) if isinstance(f, str) and f.strip()]
    weaknesses = [w for w in review.get("weaknesses", []) if isinstance(w, str) and w.strip()][:3]
    fixes = hard + [f"The family asked for this and it is missing: {m}. Add it." for m in missing]
    fixes += judge_notes or weaknesses
    fixes += soft
    fixes = list(dict.fromkeys(fixes))[:MAX_FIXES * 2]  # route_notes keeps at most 3 per page

    return {
        "scores": scores,
        "total": sum(scores.values()),
        "passed": min(scores.values()) >= PASS_SCORE and not hard and not missing,
        "blocking": len(hard) + len(missing),
        "fixes": fixes,
        "checklist": checklist,
        "logic": logic,
        "weaknesses": weaknesses,
        "summary": review.get("summary", ""),
        "best_line": review.get("best_line", ""),
        "evidence": {c: review["review"][c].get("evidence", "") for c in CRITERIA},
        "metrics": {"avg_reading_grade": metrics["avg_reading_grade"],
                    "words_per_page": [p["words"] for p in metrics["pages"]],
                    "refrain_pages": metrics["refrain_pages"]},
    }


def _check_shot(p):
    if len(str(p.get("action", "")).split()) < 8:
        raise ValueError("'action' must be one concrete sentence (8+ words) of who does what to whom")
    if not isinstance(p.get("characters"), list) or not p["characters"]:
        raise ValueError("'characters' must list each character with frame, pose, looking_at and face")
    for k in ("setting", "light", "camera"):
        if not str(p.get(k, "")).strip():
            raise ValueError(f"'{k}' is missing")


# Code spreads camera angles across the book so parallel calls don't all pick the same shot.
CAMERAS = ["wide establishing shot, eye level", "medium shot, slightly low angle",
           "close-up on the faces and paws", "medium-wide shot, from slightly above", "wide shot at a gentle angle"]


def key_moments(story_plan: dict) -> list[str]:
    """What each picture must show: the family's idea for that page if there is one, else the plan's picture."""
    return ["; ".join(p.get("must_show") or []) or p.get("picture", "") for p in story_plan["pages"]]


def compose_scene(spec: dict) -> str:
    """Turn Uncle Pie's shot spec into an image prompt. Code fixes the order and wording so every page
    follows the same proven structure: the main action first (image models weight early text most),
    then who is where, then setting, light, camera and mood, then what must not appear."""
    cast = "\n".join(
        f"- {c.get('name', '')}: {c.get('frame', '')}; {c.get('pose', '')}; looking at {c.get('looking_at', '')}; "
        f"face: {c.get('face', '')}" for c in spec.get("characters", []) if isinstance(c, dict))
    avoid = ", ".join(str(a) for a in spec.get("avoid", []) if str(a).strip())
    return (f"MAIN ACTION (the most important thing in the picture): {spec['action']}\n"
            f"WHO IS WHERE:\n{cast}\n"
            f"SETTING: {spec.get('setting', '')}\n"
            f"LIGHT: {spec.get('light', '')}\n"
            f"CAMERA: {spec.get('camera', '')}\n"
            f"MOOD: {spec.get('mood', '')}"
            + (f"\nDO NOT SHOW: {avoid}" if avoid else ""))


def shot_spec(story: dict, i: int) -> dict:
    """Uncle Pie engineers the shot spec for ONE page. It runs inside that page's picture worker, so
    pictures never hold up the text or the narration. If the call fails, a plain spec built from the
    page's required moment is used instead."""
    page = story["pages"][i]
    cast = "\n".join(f"- {c.get('name')}: {c.get('look')}" for c in story["characters"])
    try:
        spec = call_json(P.DIRECT_PROMPT.format(
            story=_story_text(story), cast=cast, i=i + 1, page_text=page["text"],
            moment=page.get("moment") or page.get("picture", ""), camera_hint=CAMERAS[i % len(CAMERAS)]),
            P.DIRECT_SYSTEM, temperature=0.5, max_tokens=900, validate=_check_shot)
    except RuntimeError:
        spec = {"action": f"{page.get('moment') or page.get('picture', '')}, shown clearly in progress.",
                "characters": [], "setting": page.get("picture", ""), "light": "soft, warm daylight",
                "camera": CAMERAS[i % len(CAMERAS)], "mood": page.get("feeling", ""), "avoid": []}
    spec["scene"] = compose_scene(spec)
    return spec


def _story_text(d: dict) -> str:
    body = "\n\n".join(f"Page {i}: {p['text']}" for i, p in enumerate(d["pages"], 1))
    return f"Title: {d['title']}\n\n{body}\n\nMoral: {d['moral']}"


# ------------------------------------------------------------------ the loop

def _edit_loop(request, brief, story_plan, draft, n, handoffs, first_review=None):
    """Vishnu reviews, Uncle Pie revises, until it passes or drafts run out. Keeps the best draft."""
    reviews, best = [], None
    refrain = story_plan.get("refrain", "")
    for round_no in range(1, MAX_DRAFTS + 1):
        yield {"type": "stage", "agent": "Vishnu", "message": f"Reading draft {round_no} closely..."}
        try:
            if round_no == 1 and first_review is not None:
                review = first_review  # already judged while choosing between candidate drafts
            else:
                review = judge(request, brief, draft, refrain, [p.get("must_show", []) for p in story_plan["pages"]],
                               [c.get("name", "") for c in story_plan["characters"]])
        except Exception:  # noqa: BLE001
            if best is None:
                raise
            break  # a judge failure on a later round should not lose the drafts we already have
        review["round"] = round_no
        reviews.append(review)
        yield {"type": "review", **review}
        rank = lambda r: (r["passed"], -r["blocking"], r["total"])  # noqa: E731
        if best is None or rank(review) > rank(best[1]):
            best = (draft, review)
        if review["passed"] or round_no == MAX_DRAFTS or not review["fixes"]:
            break  # passed, out of drafts, or nothing concrete left to fix
        yield _handoff(handoffs, "Vishnu", "Uncle Pie (writer)", "\n".join(f"- {f}" for f in review["fixes"]))
        targets = sorted(route_notes(review["fixes"], story_plan, n))
        yield {"type": "stage", "agent": "Uncle Pie",
               "message": f"Rewriting page{'s' if len(targets) > 1 else ''} "
                          f"{', '.join(str(t + 1) for t in targets)} from Vishnu's notes..."}
        draft = revise(story_plan, draft, review["fixes"], n)
        yield {"type": "draft", "round": round_no + 1, "title": draft["title"]}
    if best[1]["round"] != reviews[-1]["round"]:
        yield {"type": "stage", "agent": "Vishnu",
               "message": f"Draft {best[1]['round']} was the strongest, so that is the one we print."}
    return best, reviews


N_CANDIDATES = 3  # first drafts written in parallel; the best one is revised

# ---------------------------------------------------------------- the story ledger
# Generic groups that can appear with "the" without an introduction ("the kids", "the villagers").
GENERIC_WHO = {"kid", "children", "child", "animal", "friend", "villager", "neighbor", "people", "family",
               "everyone", "crowd", "bird", "creature", "other", "class", "mama", "papa", "mom", "dad",
               "mother", "father", "grandma", "grandpa", "teacher", "sun", "moon", "star"}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _stems(s: str, drop: set[str] = frozenset()) -> set[str]:
    return {_stem(w) for w in _words(s).split() if w not in STOP} - drop


@lru_cache(maxsize=512)
def page_ledger(text: str) -> dict:
    """What happens on a page and who is in it, extracted once per page text."""
    try:
        return call_json(P.LEDGER_PROMPT.format(page=text), P.LEDGER_SYSTEM, temperature=0.0,
                         max_tokens=300, validate=_check_ledger)
    except RuntimeError:
        return {"events": [], "who": []}


def ledger_problems(pages: list[str], names: list[str], refrain: str = "") -> list[dict]:
    """Code reads the ledger: an event that matches an earlier page's event (ignoring character names, which
    every event shares) is a repeat; a new character whose first mention is 'the X' with no 'a X' is from nowhere."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(pages))) as pool:
        ledgers = list(pool.map(page_ledger, pages))
    name_stems = {_stem(w) for n in names for w in _words(n).split()}
    refrain_stems = _stems(refrain) if refrain else set()
    seen, earlier, probs = set(name_stems), [], []
    for i, (text, led) in enumerate(zip(pages, ledgers), 1):
        low = text.lower()
        for who in led["who"]:
            ws = [w for w in _words(who).split() if w not in STOP]
            if not ws or any(_stem(w) in seen for w in ws) or _stem(ws[-1]) in GENERIC_WHO:
                seen.update(_stem(w) for w in ws)
                continue
            pos = low.find(ws[-1])
            before = re.findall(r"[a-z']+", low[max(0, pos - 40):pos])[-3:] if pos >= 0 else []
            if pos >= 0 and "the" in before and not {"a", "an", "some", "another"} & set(before):
                probs.append(problem(i, f"Page {i}: \"the {ws[-1]}\" appears as if we already know it; introduce "
                                        f"it first (\"a {ws[-1]}\") or set it up on an earlier page.", True))
            seen.update(_stem(w) for w in ws)
        for ev in led["events"]:
            se = _stems(ev, name_stems)
            if not se or (refrain_stems and len(se & refrain_stems) >= 2):
                continue  # the refrain repeats on purpose
            for j, (pev, pse) in earlier:
                if len(se & pse) / len(se | pse) >= 0.5:
                    probs.append(problem(i, f"Page {i} repeats something that already happened on page {j} "
                                            f"(\"{pev}\"); make something new happen instead.", True))
                    break
        earlier += [(i, (ev, _stems(ev, name_stems))) for ev in led["events"] if _stems(ev, name_stems)]
    return probs


def _ledger_so_far(pages: list[dict]) -> str:
    lines = []
    for k, p in enumerate(pages, 1):
        led = page_ledger(p["text"])
        if led["events"]:
            lines.append(f"Page {k}: " + "; ".join(led["events"]))
    return "\n".join(lines)


def _pair(a: dict, b: dict) -> str | None:
    try:
        out = call_json(P.PAIR_PROMPT.format(a=_story_text(a), b=_story_text(b)), P.PAIR_SYSTEM,
                        temperature=0.0, max_tokens=200,
                        validate=lambda d: None if d.get("winner") in ("A", "B") else (_ for _ in ()).throw(
                            ValueError("winner must be A or B")))
        return out["winner"]
    except RuntimeError:
        return None


def choose_best(drafts: list[dict], reviews: list[dict]) -> int:
    """Code ranks the candidates by their verified problems; Vishnu breaks ties between the top two by
    comparing them side by side, asked both ways round so the order they are shown in cannot decide."""
    rank = lambda i: (reviews[i]["passed"], -reviews[i]["blocking"], reviews[i]["total"])  # noqa: E731
    order = sorted(range(len(drafts)), key=rank, reverse=True)
    if len(order) < 2:
        return order[0]
    a, b = order[0], order[1]
    if (reviews[a]["passed"], -reviews[a]["blocking"]) != (reviews[b]["passed"], -reviews[b]["blocking"]):
        return a  # code already separates them
    first, second = _pair(drafts[a], drafts[b]), _pair(drafts[b], drafts[a])
    if first == "B" and second == "A":
        return b
    return a  # agreement on A, a split decision, or no answer: keep code's ranking


def simplify(story_plan: dict, draft: dict, brief: dict) -> tuple[dict, list[int]]:
    """Read-aloud polish: rewrite pages that are too hard for a 5-year-old. A rewrite is kept only if it reads
    easier, breaks no hard limit, keeps the page's content, and every requested idea is still in the story."""
    # Pages that are too hard to follow, or that still break a hard rule (tense, sentence length): a story
    # that never passed review must not print with a mechanical fault.
    hard = [i for i, p in enumerate(draft["pages"])
            if (lambda m: m["reading_grade"] > 4.0 or m["longest_sentence_words"] > 12)(page_metrics(p["text"]))
            or _page_faults(p["text"]) > 0]
    if not hard:
        return draft, []
    story_txt = _story_text(draft)

    def one(i):
        old = draft["pages"][i]["text"]
        try:
            new = call_json(P.SIMPLIFY_PROMPT.format(story=story_txt, page=old, i=i + 1), P.WRITE_SYSTEM,
                            temperature=0.3, max_tokens=700, validate=_check_text_only)["text"].strip()
        except RuntimeError:
            return i, old
        before, after = page_metrics(old), page_metrics(new)
        old_w, new_w = set(_words(old).split()) - STOP, set(_words(new).split()) - STOP
        keeps_content = len(old_w & new_w) / max(1, len(old_w)) >= 0.45
        # Shorter sentences naturally drop words; the page minimum is still enforced by _page_faults.
        fewer_faults = _page_faults(new) < _page_faults(old)
        easier = after["reading_grade"] < before["reading_grade"] and _page_faults(new) <= _page_faults(old)
        ok = (fewer_faults or easier) and keeps_content and after["words"] >= 0.6 * before["words"]
        return i, _past_tense(new if ok else old)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(hard)) as pool:
        results = dict(pool.map(one, hard))
    simpler = {**draft, "pages": [{"text": results.get(i, p["text"])} for i, p in enumerate(draft["pages"])]}
    changed = [i for i in hard if results[i] != draft["pages"][i]["text"]]
    if changed and brief.get("must_include"):
        before = sum(c["present"] for c in check_fidelity(brief["must_include"], draft))
        after = sum(c["present"] for c in check_fidelity(brief["must_include"], simpler))
        if after < before:
            return draft, []  # simplifying lost a requested idea: keep the reviewed text
    return simpler, changed


GENERIC_MORAL = re.compile(
    r"\b(can lead to|leads? to|overcome any|any challenge|greatest treasure|true friendship is|the power of|"
    r"journey|discoveries|bravery and|and bravery|teamwork and|and teamwork|in life|makes life|forge\w*|"
    r"strengthen\w*|wonders|brightest|creates?|importance|value of|truly|essence|bonds?|unexpected|"
    r"no matter our|differences|conquer\w*|obstacles?|resourceful\w*|true joy|treasure|shining|"
    r"brings? (?:true )?(?:joy|happiness)|peace|inner|find(?:ing)? (?:joy|calm)|no matter what)\b", re.I)


def _check_story_moral(names: list[str], strict: bool = True):
    def v(d):
        d["moral"] = tidy_moral(d.get("moral", ""))
        words = len(d["moral"].split())
        if not 7 <= words <= 22:
            raise ValueError(f"the moral has {words} words; write one sentence of 8 to 18 words")
        found = GENERIC_MORAL.findall(d["moral"]) if strict else []
        if found:
            raise ValueError(f"the moral sounds generic ({', '.join(sorted(set(found)))}); say plainly what the "
                             "characters in this story learned")
        grade = page_metrics(d["moral"])["reading_grade"]
        if grade > 6:
            raise ValueError(f"the moral reads at grade {grade}; use short, everyday words a 5-year-old would say")
        if names and not any(n.lower() in d["moral"].lower() for n in names):
            raise ValueError(f"the moral must name the story's characters ({', '.join(names)}) and what they learned")
    return v


def write_moral(draft: dict, story_plan: dict, hoped: str) -> str:
    """Written last, from the finished story, so the lesson is about what actually happened in it."""
    # "Percy the Penguin" is called Percy; "Sir Cedric" stays Sir Cedric.
    names = [re.split(r"\s+the\s+", str(c.get("name", "")), flags=re.I)[0].strip()
             for c in story_plan.get("characters", []) if c.get("name")]
    prompt = P.MORAL_PROMPT.format(story=_story_text({**draft, "moral": ""}), hoped=hoped or "(any)",
                                   names=", ".join(names) or "(see the story)")
    # Strict first, then with more variety, then without the phrase list; never fall back to a generic line
    # unless every attempt fails.
    for temperature, strict in ((0.5, True), (0.9, True), (0.7, False)):
        try:
            return call_json(prompt, P.MORAL_SYSTEM, temperature=temperature, max_tokens=150,
                             validate=_check_story_moral(names, strict))["moral"]
        except RuntimeError:
            continue
    return tidy_moral(draft.get("moral", ""))


def _assemble(draft, story_plan, brief, request, reviews, best_review, used_references, handoffs):
    moments = key_moments(story_plan)
    pages = []
    for i, p in enumerate(draft["pages"]):
        scene = story_plan["pages"][i]
        pages.append({
            "text": p["text"].strip(),
            "beat": scene.get("beat", ""),
            "feeling": scene.get("feeling", ""),
            "picture": scene.get("picture", ""),
            "moment": moments[i],
        })
    return {
        "request": request,
        "title": draft["title"],
        "moral": draft["moral"],
        "refrain": story_plan.get("refrain", ""),
        "category": brief["category"],
        "brief": brief,
        "plan": story_plan,
        "characters": story_plan["characters"],
        "pages": pages,
        "reviews": reviews,
        "final_review": best_review,
        "used_references": bool(used_references),
        "handoffs": handoffs,
    }


def _handoff(handoffs, frm, to, prompt):
    """Record a prompt one agent wrote for another, and announce it to the UI."""
    h = {"from": frm, "to": to, "prompt": prompt}
    handoffs.append(h)
    return {"type": "handoff", **h}


def _safety_gate(text: str):
    """Hard stop for requests no gentle reframe can rescue. Milder things (a scary monster, a sword
    fight) go on to intake, which tells a kind version instead of refusing."""
    severe = [c for c in moderate(text) if c in REFUSE]
    if severe:
        raise ValueError("Uncle Pie only tells gentle bedtime stories for children. Could you try a different idea?")


def create_story(request: str, references: list[dict] | None = None):
    request = (request or "").strip()
    if not request:
        raise ValueError("Tell Uncle Pie what the story should be about.")
    _safety_gate(request)
    handoffs = []
    yield {"type": "stage", "agent": "Uncle Pie", "message": "Listening to your idea..."}
    brief = intake(request, references)
    yield {"type": "brief", "category": brief["category"], "pages": brief["pages"],
           "characters": brief["characters"], "must_include": brief["must_include"],
           "moral": brief.get("moral", ""), "safety": brief["safety"]}
    yield _handoff(handoffs, "Ranganathan (intake)", "Uncle Pie (planner)",
                   f"A {brief['category']} story, {brief['pages']} pages. Must include: "
                   + "; ".join(brief["must_include"] or ["(free plot)"]) + f". Lesson: {brief.get('moral', '')}")

    yield {"type": "stage", "agent": "Uncle Pie", "message": "Sketching the story arc..."}
    story_plan = plan(brief, references)
    yield {"type": "plan", "title": story_plan.get("title", ""), "refrain": story_plan.get("refrain", ""),
           "characters": story_plan["characters"],  # lets the caller start the cast sheet right away
           "beats": [p.get("beat", "") + ": " + p.get("events", "") for p in story_plan["pages"]]}
    yield _handoff(handoffs, "Uncle Pie (planner)", "Uncle Pie (writer)",
                   " | ".join(f"Page {i}: {p.get('events', '')}" for i, p in enumerate(story_plan["pages"], 1)))

    n = brief["pages"]
    slots = [p.get("must_show", []) for p in story_plan["pages"]]
    yield {"type": "stage", "agent": "Uncle Pie", "message": f"Writing {N_CANDIDATES} versions of the story at once..."}
    writer = write_whole if WRITER_MODE == "whole" else write
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=N_CANDIDATES) as pool:
        drafts = [d for d in pool.map(lambda _: _try(writer, story_plan, n), range(N_CANDIDATES)) if d]
    if not drafts:
        drafts = [writer(story_plan, n)]
    yield {"type": "stage", "agent": "Vishnu", "message": f"Checking all {len(drafts)} versions..."}
    with ThreadPoolExecutor(max_workers=len(drafts)) as pool:
        cand_reviews = list(pool.map(
            lambda d: judge(request, brief, d, story_plan.get("refrain", ""), slots,
                            [c.get("name", "") for c in story_plan["characters"]]), drafts))
    best_i = choose_best(drafts, cand_reviews)
    summary = "; ".join(f"version {chr(65 + i)}: {r['blocking']} blocking problem(s)" for i, r in enumerate(cand_reviews))
    yield {"type": "stage", "agent": "Vishnu",
           "message": f"Version {chr(65 + best_i)} is the strongest ({summary}). Starting from that one."}
    draft = drafts[best_i]
    cand_reviews[best_i]["round"] = 1
    yield {"type": "draft", "round": 1, "title": draft["title"]}

    (final, final_review), reviews = yield from _edit_loop(request, brief, story_plan, draft, n, handoffs,
                                                           first_review=cand_reviews[best_i])
    yield {"type": "stage", "agent": "Uncle Pie", "message": "Making every page easy to read aloud..."}
    final, simplified = simplify(story_plan, final, brief)
    if simplified:
        yield {"type": "stage", "agent": "Uncle Pie",
               "message": "Simplified page " + ", ".join(str(i + 1) for i in simplified) + " for young listeners."}
    yield {"type": "stage", "agent": "Uncle Pie", "message": "Choosing the words for the moral..."}
    final = {**final, "moral": write_moral(final, story_plan, brief.get("moral", ""))}
    yield {"type": "done", "story": _assemble(final, story_plan, brief, request, reviews, final_review,
                                              references, handoffs)}


def revise_story(story: dict, feedback: str):
    """Apply a family's change request to a finished story, then run the edit loop again."""
    feedback = (feedback or "").strip()
    if not feedback:
        raise ValueError("Tell Uncle Pie what to change.")
    _safety_gate(feedback)
    n = len(story["pages"])
    current = {"title": story["title"], "moral": story["moral"],
               "pages": [{"text": p["text"]} for p in story["pages"]]}
    story_plan, brief = copy.deepcopy(story["plan"]), copy.deepcopy(story["brief"])
    adds = _added_ideas(feedback)
    yield {"type": "stage", "agent": "Uncle Pie", "message": "Rewriting with your changes..."}
    if adds:
        # "Add a baby squirrel who needs help" belongs on ONE page. Sending it to every page made every
        # page add its own rescue. It becomes that page's required event, and Ida checks it like any
        # other requested idea.
        target = n // 2
        story_plan["pages"][target]["must_show"] = list(story_plan["pages"][target].get("must_show") or []) + adds
        brief["must_include"] = list(brief.get("must_include") or []) + adds
        notes = [f"Page {target + 1}: the family asked for this change: {_fence_safe(feedback)}"]
        draft = revise(story_plan, current, notes, n)
    else:
        # A whole-story change ("make it funnier", "shorter") applies to every page.
        draft = revise(story_plan, current, [f"The family asked for a change: {_fence_safe(feedback)}"], n,
                       feedback=feedback)
    yield {"type": "draft", "round": 1, "title": draft["title"]}
    request = f"{story['request']}\n\nChange requested afterwards: {feedback}"
    handoffs = list(story.get("handoffs", []))
    yield _handoff(handoffs, "Family", "Uncle Pie (writer)", feedback)
    (final, final_review), reviews = yield from _edit_loop(request, brief, story_plan, draft, n, handoffs)
    final = {**final, "moral": write_moral(final, story_plan, brief.get("moral", ""))}
    yield {"type": "done", "story": _assemble(final, story_plan, brief, request, reviews,
                                              final_review, story.get("used_references"), handoffs)}


def _added_ideas(feedback: str) -> list[str]:
    """The 'add ...' parts of a change request ("add a baby squirrel who needs help"), as plot ideas."""
    parts = re.split(r"\band\s+(?=add|include|put)|[.;]", feedback, flags=re.I)
    return [re.sub(r"^\s*(?:please\s+)?(?:add|include|put in)\s+", "", p, flags=re.I).strip()
            for p in parts if re.match(r"^\s*(?:please\s+)?(?:add|include|put in)\b", p, re.I)]
