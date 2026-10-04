"""Generate the stories in samples/ (text only), so real output can be read without an API key.

Each sample is the best of RUNS runs, chosen by code: a story that passed review, then the fewest
problems found by the checks, then the easiest to read aloud.

    python tools/make_samples.py            # writes samples/<name>.md and samples/<name>.json
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storybook.pipeline import create_story, revise_story  # noqa: E402
from storybook.readability import story_metrics  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")

REQUESTS = {
    "1_assignment_example": "A story about a girl named Alice and her best friend Bob, who happens to be a cat.",
    "2_one_word_topic": "dinosaurs",
    "3_parent_request": ("My son is scared of thunderstorms. Make a story where a little robot learns that storms "
                         "are loud but they pass, and his grandma teaches him a breathing trick."),
    "4_unsafe_request_reframed": "A story where a brave knight fights and kills a scary dragon with his sword.",
}
CHANGE = ("1_assignment_example", "5_change_request", "Make Bob a bit funnier and add a baby squirrel who needs help.")


def run(gen):
    events, story = [], None
    for ev in gen:
        if ev["type"] == "done":
            story = ev["story"]
        else:
            events.append(ev)
    return story, events


def verdict(story):
    r = story["final_review"]
    if r["passed"]:
        return f"passed on draft {r['round']}"
    low = min(r["scores"], key=r["scores"].get)
    why = (f"{r['blocking']} blocking issue(s) left" if r["blocking"]
           else f"every code check passes, but Vishnu scored {low} {r['scores'][low]}/5 (a pass needs 4)")
    return f"no draft fully passed in {len(story['reviews'])}; printed draft {r['round']}, the closest: {why}"


def render(name, request, story, seconds, feedback=""):
    r = story["final_review"]
    lines = [f"# {story['title']}", "",
             f"**Request:** {request}" + (f"  \n**Change requested afterwards:** {feedback}" if feedback else ""), "",
             f"*Category: {story['category']} · {len(story['pages'])} pages · text ready in {seconds}s · "
             f"reading grade {story_metrics([p['text'] for p in story['pages']])['avg_reading_grade']} · "
             f"Vishnu: {verdict(story)}*", ""]
    brief = story["brief"]
    if not brief["safety"].get("ok", True):
        lines += [f"> **Safety reframe by Ranganathan:** {brief['safety'].get('reframe', '')}", ""]
    if brief.get("must_include"):
        lines += ["**Ideas kept from the request (checked by quote):** " + "; ".join(
            f"{'✅' if c['present'] else '❌'} {c['item']}" for c in r.get("checklist", [])), ""]
    for i, p in enumerate(story["pages"], 1):
        lines += [f"**Page {i}.** {p['text']}", ""]
    lines += [f"*The moral:* {story['moral']}", "", "## How Vishnu reviewed it", ""]
    for rv in story["reviews"]:
        scores = ", ".join(f"{k} {v}" for k, v in rv["scores"].items())
        lines += [f"**Draft {rv['round']}** ({'passed' if rv['passed'] else 'sent back'}): {scores}  ",
                  f"_{rv.get('summary', '')}_"]
        confirmed = [x for x in rv.get("logic", []) if x.get("confirmed", True)]
        unconfirmed = [x for x in rv.get("logic", []) if not x.get("confirmed", True)]
        if confirmed:
            lines += ["Story-logic problems (confirmed by both reads): "
                      + "; ".join(f"page {x['page']}: {x['problem']}" for x in confirmed)]
        if unconfirmed and not rv["passed"]:
            lines += ["Possible logic issues (flagged by one read only): "
                      + "; ".join(f"page {x['page']}: {x['problem']}" for x in unconfirmed)]
        if not rv["passed"]:
            blocking = rv.get("fixes", [])[: rv.get("blocking", 0)]
            low = [f"{k} {v}/5" for k, v in rv["scores"].items() if v < 4]
            why = blocking + ([f"rubric score below 4: {', '.join(low)}"] if low else [])
            lines += ["Sent back because:"] + [f"- {w}" for w in why]
            rest = [f for f in rv.get("fixes", []) if f not in blocking]
            if rest:
                lines += ["Other notes for Uncle Pie:"] + [f"- {f}" for f in rest]
        lines += [""]
    if r.get("round") and r["round"] != story["reviews"][-1]["round"]:
        lines += [f"Printed draft {r['round']} (the strongest), not the last one.", ""]
    with open(os.path.join(OUT, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(OUT, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(story, f, indent=2, ensure_ascii=False)


RUNS = 2


def quality(story):
    """Lower is better: passed first, then problems the checks still find, then reading grade."""
    from storybook.pipeline import _page_faults, ledger_problems
    pages = [p["text"] for p in story["pages"]]
    faults = sum(_page_faults(p) for p in pages)
    faults += len(ledger_problems(pages, [c["name"] for c in story["plan"]["characters"]], story.get("refrain", "")))
    return (not story["final_review"]["passed"], faults, story_metrics(pages)["avg_reading_grade"])


def make(item):
    name, request = item
    best = None
    for _ in range(RUNS):
        t0 = time.time()
        try:
            story, _ = run(create_story(request))
        except ValueError as e:  # refused by the safety gate
            with open(os.path.join(OUT, f"{name}.md"), "w", encoding="utf-8") as f:
                f.write(f"# Refused\n\n**Request:** {request}\n\n> {e}\n")
            return name, None
        seconds = round(time.time() - t0)
        if best is None or quality(story) < quality(best[0]):
            best = (story, seconds)
    render(name, request, best[0], best[1])
    return name, best[0]


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:  # each story already runs drafts in parallel
        stories = dict(pool.map(make, REQUESTS.items()))
    base, name, feedback = CHANGE
    if stories.get(base):
        t0 = time.time()
        best = None
        for _ in range(RUNS):
            t0 = time.time()
            story, _ = run(revise_story(stories[base], feedback))
            seconds = round(time.time() - t0)
            if best is None or quality(story) < quality(best[0]):
                best = (story, seconds)
        render(name, REQUESTS[base], best[0], best[1], feedback)
    print("wrote", sorted(os.listdir(OUT)))
