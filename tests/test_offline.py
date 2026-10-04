"""Offline tests: no OpenAI calls. Model calls are replaced with fakes, so these test the code that
decides things (gates, routing, bookkeeping, scoring, failure handling), which is where bugs hide."""
import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-not-a-real-key")

from storybook import orchestrator, pipeline, readability  # noqa: E402
from storybook.readability import MAX_SENTENCE_WORDS, page_metrics, story_metrics  # noqa: E402

GOOD_PAGE = ("Pitter-patter went the rain. Bear sat by the river. Fox ran up with wet paws. "
             "\"Can I sit here?\" asked Fox. Bear moved over a little. They watched the frogs jump. "
             "\"Same jungle, same sky,\" said Bear. Fox smiled and shook his tail. The frogs sang a song.")


# ------------------------------------------------------------------ readability gates

def test_metrics_count_words_sentences_and_dialogue():
    m = page_metrics(GOOD_PAGE)
    assert m["sentences"] >= 9  # a question inside dialogue counts as its own sentence
    assert m["has_dialogue"]
    assert m["longest_sentence_words"] <= MAX_SENTENCE_WORDS
    assert m["present_tense"] == 0


def test_present_tense_narration_is_caught_but_dialogue_is_ignored():
    assert page_metrics("T-Rex and Steggy explore the deep cave together. They smile and share their "
                        "best stories.")["present_tense"] >= 2
    assert page_metrics("Grandma placed a hand down. 'She says hi and runs,' said Robbie.")["present_tense"] == 0
    # "red" is a colour, not a past-tense verb
    assert page_metrics("On a farm with a big red barn, the cow dreams of dancing. She gazes at the bright "
                        "stars every single night.")["present_tense"] == 2


def test_the_same_scene_told_twice_is_caught():
    a = "Percy flapped his small wings on the snowy hill and tried to fly to the moon."
    b = "Percy flapped his small wings again on the snowy hill, trying hard to fly to the moon."
    assert readability.repeated_scenes([a, GOOD_PAGE, b]) == [(1, 3)]
    assert readability.repeated_scenes([a, GOOD_PAGE]) == []


def test_long_sentence_and_blocklist_block_but_cliches_only_note():
    long_sentence = "Bear " + "and Bear " * 10 + "ran."
    out = story_metrics([GOOD_PAGE + " " + long_sentence, GOOD_PAGE.replace("Bear sat", "Bear felt pure joy and sat")
                         + " Bear said he would kill time."])
    blocking = [p for p in out["problems"] if p["blocking"]]
    notes = [p for p in out["problems"] if not p["blocking"]]
    assert any("split this" in p["text"] and p["page"] == 1 for p in blocking)
    assert any("unsuitable" in p["text"] and p["page"] == 2 for p in blocking)
    assert any("pure joy" in p["text"] for p in notes)


def test_refrain_is_expected_only_on_code_chosen_pages():
    pages = [GOOD_PAGE, GOOD_PAGE, GOOD_PAGE.replace("Same jungle, same sky", "Hello")]
    out = story_metrics(pages, "Same jungle, same sky", {0, 2})
    texts = [p["text"] for p in out["problems"]]
    assert any(t.startswith("Page 2: remove the line") for t in texts)
    assert any(t.startswith("Page 3: have a character say") for t in texts)


# ------------------------------------------------------------------ bookkeeping done in code

def test_assign_ideas_keeps_order_and_covers_every_idea():
    slots = pipeline.assign_ideas(["a", "b", "c", "d"], 5)
    flat = [x for s in slots for x in s]
    assert flat == ["a", "b", "c", "d"] and len(slots) == 5


def test_must_include_must_come_from_the_request():
    words = set(pipeline._words("a bear and a fox who don't get along").split()) - pipeline.STOP
    assert pipeline._from_request("Bear and Fox don't get along", words)
    assert not pipeline._from_request("A sudden earthquake traps them", words)


def test_route_notes_sends_each_note_to_its_page():
    plan = {"pages": [{"must_show": []}, {"must_show": ["Bear saves Fox"]}, {"must_show": []}]}
    routed = pipeline.route_notes(["Page 3: shorter please",
                                   "The family asked for this and it is missing: Bear saves Fox. Add it.",
                                   "General polish"], plan, 3)
    assert routed[2] == ["Page 3: shorter please", "General polish"]
    assert "missing: Bear saves Fox" in routed[1][0]


def test_plan_with_two_pages_sharing_a_new_event_is_rejected():
    page = {"events": "x", "picture": "y"}
    plan = {"characters": [{"name": "Bear"}], "moral": "Sharing a kite makes the fun twice as big.",
            "pages": [{**page, "new": "Bear finds a red kite"}, {**page, "new": "Bear finds the red kite"}]}
    with pytest.raises(ValueError, match="same new event"):
        pipeline._check_plan(2)(plan)


def test_moral_is_tidied_into_one_clean_sentence():
    assert pipeline.tidy_moral("Moral: be kind to everyone") == "Be kind to everyone."
    assert pipeline.tidy_moral("the moral of the story is that storms pass") == "Storms pass."
    assert pipeline.tidy_moral("Helping others brings happiness.") == "Helping others brings happiness."


def test_generic_morals_are_rejected_and_specific_ones_kept():
    check = pipeline._check_story_moral(["Bear", "Fox"])
    with pytest.raises(ValueError, match="generic"):
        check({"moral": "Facing our fears with the help of friends can lead to new discoveries and bravery."})
    with pytest.raises(ValueError, match="name the story's characters"):
        check({"moral": "It is good to be kind to people who are different from you."})
    good = {"moral": "bear and Fox learned that you don't have to be alike to help each other"}
    check(good)
    assert good["moral"] == "Bear and Fox learned that you don't have to be alike to help each other."


def test_add_requests_become_plot_ideas_but_style_changes_do_not():
    assert pipeline._added_ideas("Make Bob a bit funnier and add a baby squirrel who needs help.") == [
        "a baby squirrel who needs help"]
    assert pipeline._added_ideas("Make it shorter please") == []


def _rv(passed, blocking, total):
    return {"passed": passed, "blocking": blocking, "total": total}


def test_best_candidate_by_code_then_side_by_side_both_ways(monkeypatch):
    drafts = [{"title": t, "moral": "", "pages": [{"text": t}]} for t in "ABC"]
    # Code separates them: C has fewer blocking problems, no comparison needed.
    assert pipeline.choose_best(drafts, [_rv(False, 2, 30), _rv(False, 3, 35), _rv(False, 0, 28)]) == 2
    # Tie on problems: B wins only if it wins BOTH orderings.
    answers = iter(["B", "A"])
    monkeypatch.setattr(pipeline, "call_json", lambda *a, **k: {"winner": next(answers)})
    assert pipeline.choose_best(drafts, [_rv(False, 1, 33), _rv(False, 1, 31), _rv(False, 4, 35)]) == 1
    answers = iter(["B", "B"])  # split decision (position bias): keep code's ranking
    assert pipeline.choose_best(drafts, [_rv(False, 1, 33), _rv(False, 1, 31), _rv(False, 4, 35)]) == 0


def test_ledger_catches_repeated_events_and_characters_from_nowhere(monkeypatch):
    ledgers = {
        "p1": {"events": ["Mia invites Lila to play"], "who": ["Lila", "Mia"]},
        "p2": {"events": ["Lila hides behind a tree"], "who": ["Lila"]},
        "p3": {"events": ["Mia invites Lila to play"], "who": ["Lila", "Mia"]},
        "p4": {"events": ["Lila finds the lost kitten"], "who": ["Lila", "lost kitten"]},
        "p5": {"events": ["Lila and Mia sing 'Brave and kind'"], "who": ["Lila", "Mia"]},
    }
    pages = {"p1": "Mia smiled. Lila waved.", "p2": "Lila hid.", "p3": "Mia asked again.",
             "p4": "Lila found the lost kitten under the stairs.", "p5": "They sang."}
    pipeline.page_ledger.cache_clear()
    monkeypatch.setattr(pipeline, "page_ledger", lambda text: ledgers[next(k for k, v in pages.items() if v == text)])
    probs = pipeline.ledger_problems(list(pages.values()), ["Lila", "Mia"], refrain="Brave and kind")
    found = sorted((p["page"], "repeats" in p["text"], "already know" in p["text"]) for p in probs)
    assert found == [(3, True, False), (4, False, True)]


def test_split_whole_needs_every_marker():
    assert pipeline._split_whole("[1] One. [2] Two.", 2) == ["One.", "Two."]
    with pytest.raises(ValueError):
        pipeline._split_whole("[1] One. [3] Three.", 2)


# ------------------------------------------------------------------ quote-then-verify

def test_fidelity_only_credits_quotes_that_are_really_on_a_page(monkeypatch):
    draft = {"title": "T", "moral": "M", "pages": [{"text": GOOD_PAGE}, {"text": "Bear lifted the log off Fox."}]}
    fake = {"results": [{"event": "Bear and Fox sit by the river", "quote": "Bear sat by the river.", "shows_it": True},
                        {"event": "Fox rescues Bear", "quote": "Fox heaved the log off Bear with all his might.",
                         "shows_it": True}]}
    monkeypatch.setattr(pipeline, "call_json", lambda *a, **k: fake)
    found = pipeline.check_fidelity(["Bear and Fox sit by the river", "Fox rescues Bear"], draft)
    assert found[0]["present"] and found[0]["page"] == 1
    assert not found[1]["present"]  # invented quote: not in the story, so no credit


def test_logic_problem_blocks_only_when_both_reads_agree(monkeypatch):
    draft = {"title": "T", "moral": "M", "pages": [{"text": GOOD_PAGE}, {"text": "Bear lifted the log off Fox."}]}
    reads = iter([
        {"problems": [{"page": 1, "quote": "Fox ran up with wet paws.", "problem": "Fox arrives twice"},
                      {"page": 2, "quote": "Bear lifted the log off Fox.", "problem": "log never set up"}]},
        {"problems": [{"page": 1, "quote": "Bear sat by the river.", "problem": "Bear was already sitting"}]},
    ])
    monkeypatch.setattr(pipeline, "call_json", lambda *a, **k: next(reads))
    found = {f["page"]: f["confirmed"] for f in pipeline.check_logic(draft)}
    assert found == {1: True, 2: False}


def test_logic_check_drops_problems_whose_quote_is_not_in_the_story(monkeypatch):
    draft = {"title": "T", "moral": "M", "pages": [{"text": GOOD_PAGE}]}
    fake = {"pages": [], "problems": [
        {"page": 1, "quote": "Fox ran up with wet paws.", "problem": "Fox arrives twice"},
        {"page": 1, "quote": "A dragon breathed fire on the castle.", "problem": "made up"}]}
    monkeypatch.setattr(pipeline, "call_json", lambda *a, **k: fake)
    found = pipeline.check_logic(draft)
    assert [f["problem"] for f in found] == ["Fox arrives twice"]


# ------------------------------------------------------------------ scoring and failure handling

def _review(passed=True, blocking=0):
    return {"scores": {c: 5 for c in pipeline.CRITERIA}, "passed": passed, "blocking": blocking,
            "checklist": [], "metrics": {}}


def test_report_card_cannot_hide_a_failed_story_behind_pictures():
    ok = {"image_check": {"passed": True}, "audio_check": {"passed": True}}
    good = orchestrator.report_card({"final_review": _review()}, [ok] * 5)
    bad = orchestrator.report_card({"final_review": _review(passed=False, blocking=2)}, [ok] * 5)
    assert good["overall"] == 100
    assert bad["overall"] == bad["text"]["score"] == 80


def test_book_lanes_finish_even_when_every_media_call_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated outage")
    monkeypatch.setattr(orchestrator.media, "narrate", boom)
    monkeypatch.setattr(orchestrator.media, "illustrate", boom)
    monkeypatch.setattr(pipeline, "call_json", boom)
    story = {"title": "T", "moral": "Be kind.", "characters": [{"name": "Bear", "look": "brown"}],
             "pages": [{"text": "Bear ran.", "moment": "Bear runs", "picture": "Bear", "feeling": "happy"}] * 2,
             "final_review": _review()}
    events = list(orchestrator.make_book(story))
    types = [e["type"] for e in events]
    assert types.count("lane_done") == 2 and types[-1] == "book_done"
    assert types.count("error") >= 4


# ------------------------------------------------------------------ API input checks

def test_api_rejects_malformed_stories_and_requires_passcode(monkeypatch):
    from fastapi.testclient import TestClient

    from api import index
    monkeypatch.setattr(index, "PASSCODE", "pie")
    c = TestClient(index.app)
    assert c.post("/api/report", json={"story": {}, "pages": []}).status_code == 401
    r = c.post("/api/report", headers={"X-Passcode": "pie"}, json={"story": {"pages": []}, "pages": []})
    assert r.status_code == 400
    r = c.post("/api/revise", headers={"X-Passcode": "pie"},
               json={"story": {"pages": [{"text": "x"}] * 20}, "feedback": "more"})
    assert r.status_code == 400
