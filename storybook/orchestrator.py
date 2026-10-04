"""The orchestrator: plain code that runs the agents and decides what happens next.

The text comes first: the book opens as soon as Vishnu passes the story. After that, two lanes
run at the same time and never wait on each other:

    narration lane : one narrator per page (+ the moral)   ~5 s, needs only the text
    picture lane   : cast sheet, then one picture worker per page
                     shot spec -> paint -> art judge -> (one redraw) ~60-90 s

The text is not split across agents: one writer writes page by page so the refrain, callbacks
and arc survive. Pictures are the slow part, so that is where the work fans out.
"""
import base64
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from . import media
from .pipeline import CRITERIA, shot_spec

BYTES_PER_WORD = 6500  # measured from gpt-4o-mini-tts mp3 output; used for a rough sanity check


def audio_check(audio: bytes, text: str) -> dict:
    """Audio 'judge' is code: TTS reads exactly what it is given, so we only check it is plausible."""
    expected = max(1, len(text.split())) * BYTES_PER_WORD
    ratio = len(audio) / expected
    return {"passed": 0.33 <= ratio <= 3.0, "ratio": round(ratio, 2), "bytes": len(audio)}


def moral_line(moral: str) -> str:
    """What the narrator says on the last page."""
    return f"And here is what our story teaches us. {moral}"


def voice_for(page: dict) -> str:
    """Narration direction comes from the plan's feeling for the page, so narration needs nothing but the text."""
    feeling = page.get("feeling", "")
    return f"The feeling of this page is: {feeling}. Let your voice show it, gently." if feeling else ""


def make_narration(index: int, page: dict) -> dict:
    """Narration worker: needs only the page text, so it starts the moment the story is ready."""
    voice = voice_for(page)
    audio = media.narrate(page["text"], voice)
    check = audio_check(audio, page["text"])
    if not check["passed"]:
        audio = media.narrate(page["text"], voice)
        check = audio_check(audio, page["text"])
    return {"page": index + 1, "audio_b64": base64.b64encode(audio).decode(), "audio_check": check,
            "voice": voice}


def make_cast_sheet(characters: list[dict], references: list[str] | None = None) -> str:
    """Draw every character once, side by side. Each picture worker gets this sheet as a reference
    image, which keeps characters looking the same across pages painted in parallel."""
    from .prompts import CAST_SHEET_SCENE
    return media.illustrate(media.illustration_prompt(CAST_SHEET_SCENE, characters), references)


def make_picture(index: int, story: dict, references: list[str] | None = None, emit=lambda ev: None) -> dict:
    """Picture worker for one page: shot spec -> paint -> art judge -> (one redraw)."""
    n = index + 1
    page = story["pages"][index]
    characters = story["characters"]
    moment = page.get("moment", "")
    emit({"type": "page", "page": n, "step": "directing", "message": "Uncle Pie is planning the shot"})
    spec = shot_spec(story, index)
    scene = spec["scene"]
    trail = [{"from": "Uncle Pie", "to": "Caldecott (painter)", "prompt": scene}]
    emit({"type": "page", "page": n, "step": "painting", "message": "Caldecott is painting from Uncle Pie's shot spec",
          "prompt": scene})
    image = media.illustrate(media.illustration_prompt(scene, characters), references)

    emit({"type": "page", "page": n, "step": "judging", "message": "Ursula, the art editor, is checking it"})
    verdict = media.judge_image(image, scene, characters, moment)
    redrawn = False
    if not verdict["passed"]:
        # Ursula hands Caldecott a rewritten shot spec, not just a complaint.
        scene = verdict["revised_scene"] or f"{scene}\nIMPORTANT: {verdict['note']}"
        trail.append({"from": "Ursula (art editor)", "to": "Caldecott (painter)", "prompt": scene, "why": verdict["note"]})
        emit({"type": "page", "page": n, "step": "redrawing", "message": f"Redrawing: {verdict['note']}",
              "prompt": scene})
        image = media.illustrate(media.illustration_prompt(scene, characters), references)
        verdict = media.judge_image(image, scene, characters, moment)
        redrawn = True
    return {"page": n, "image_b64": image, "image_check": {**verdict, "redrawn": redrawn},
            "scene": scene, "prompt_trail": trail}


def make_book(story: dict, references: list[str] | None = None, audio: bool = True, images: bool = True):
    """Run the narration lane and the picture lane at the same time; stream events as they happen."""
    n = len(story["pages"])
    events: queue.Queue = queue.Queue()
    pictures, narrations = [None] * n, [None] * (n + 1)
    moral_page = {"text": moral_line(story["moral"]), "feeling": "warm, slow and sleepy"}

    # A failure on one page is reported and skipped; it never stops the other pages or the other lane,
    # and every lane always announces it is done (otherwise the caller would wait forever).
    def narration_lane():
        def one(i):
            try:
                res = make_narration(i, story["pages"][i] if i < n else moral_page)
                narrations[i] = res
                events.put({"type": "narration_done", **{k: v for k, v in res.items() if k != "audio_b64"}})
            except Exception as e:  # noqa: BLE001 - report and carry on
                events.put({"type": "error", "lane": "narration", "page": i + 1, "message": str(e)[:200]})
        try:
            with ThreadPoolExecutor(max_workers=n + 1) as pool:
                list(pool.map(one, range(n + 1)))
        finally:
            events.put({"type": "lane_done", "lane": "narration"})

    def picture_lane():
        try:
            events.put({"type": "cast", "message": "Painting the cast sheet so every page draws the same characters"})
            refs = list(references or [])
            try:
                sheet = make_cast_sheet(story["characters"], references)
                events.put({"type": "cast_done", "image_b64": sheet})
                refs = [f"data:image/jpeg;base64,{sheet}"] + refs
            except Exception as e:  # noqa: BLE001 - pages can still be painted from the written descriptions
                events.put({"type": "error", "lane": "pictures", "page": 0, "message": f"cast sheet: {str(e)[:200]}"})

            def one(i):
                try:
                    res = make_picture(i, story, refs or None, events.put)
                    pictures[i] = res
                    events.put({"type": "picture_done", "page": i + 1, "image_check": res["image_check"]})
                except Exception as e:  # noqa: BLE001
                    events.put({"type": "error", "lane": "pictures", "page": i + 1, "message": str(e)[:200]})

            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(one, range(n)))
        finally:
            events.put({"type": "lane_done", "lane": "pictures"})

    lanes = [lane for lane, on in ((narration_lane, audio), (picture_lane, images)) if on]
    for lane in lanes:
        threading.Thread(target=lane, daemon=True).start()
    finished = 0
    while finished < len(lanes):
        ev = events.get()
        finished += ev["type"] == "lane_done"
        yield ev
    checks = [{"image_check": (p or {}).get("image_check", {}), "audio_check": (a or {}).get("audio_check", {})}
              for p, a in zip(pictures, narrations)]
    yield {"type": "report", "report": report_card(story, checks)}
    yield {"type": "book_done", "pictures": pictures, "narrations": narrations}


def report_card(story: dict, page_checks: list[dict]) -> dict:
    """The scorer. Deterministic: same inputs, same grade."""
    review = story["final_review"]
    # Rubric average, minus 10 for each problem code found that should have blocked printing.
    blocking = review.get("blocking", 0)
    text_score = max(0, round(sum(review["scores"].values()) / (5 * len(CRITERIA)) * 100) - 10 * blocking)
    found = [c for c in review.get("checklist", []) if c.get("present")]
    asked = review.get("checklist", [])
    pics = [p.get("image_check", {}) for p in page_checks if p]
    audio = [p.get("audio_check", {}) for p in page_checks if p]
    pics_ok = sum(1 for p in pics if p.get("passed"))
    audio_ok = sum(1 for a in audio if a.get("passed"))
    overall = round(0.6 * text_score
                    + 0.25 * (100 * pics_ok / max(1, len(pics)))
                    + 0.15 * (100 * audio_ok / max(1, len(audio))))
    if not review.get("passed"):
        overall = min(overall, text_score)  # pictures and voice can't paper over a story that failed review
    return {
        "overall": overall,
        "text": {"score": text_score, "scores": review["scores"], "passed": review["passed"], "blocking": blocking,
                 "drafts": len(story.get("reviews", [])), "chosen_draft": review.get("round")},
        "fidelity": {"found": len(found), "asked": len(asked)},
        "reading_grade": review.get("metrics", {}).get("avg_reading_grade"),
        "pictures": {"passed": pics_ok, "total": len(pics), "redrawn": sum(1 for p in pics if p.get("redrawn"))},
        "narration": {"passed": audio_ok, "total": len(audio)},
    }
