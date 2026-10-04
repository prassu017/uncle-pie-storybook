"""HTTP API for the web app. Deployed as a Vercel Python function; run locally via dev_server.py.

Story endpoints stream newline-delimited JSON events so the page can show Uncle
Pie and Vishnu working in real time.
"""
import base64
import hmac
import json
import os
import queue
import sys
import threading

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storybook import media  # noqa: E402
from storybook.orchestrator import make_cast_sheet, make_narration, make_picture, report_card  # noqa: E402
from storybook.pipeline import create_story, revise_story  # noqa: E402

app = FastAPI(title="Uncle Pie's Storybook API")

PASSCODE = os.getenv("APP_PASSCODE", "")
MAX_REF_BYTES = 1_500_000


def _guard(passcode: str | None):
    # Optional: set APP_PASSCODE to lock a deployment so only people with the passcode can spend the key.
    if PASSCODE and not hmac.compare_digest((passcode or "").encode(), PASSCODE.encode()):
        raise HTTPException(401, "Passcode needed")


def _check_story(story: dict, need_review: bool = False) -> dict:
    """Stories come back from the browser, so check their shape before spending calls on them."""
    pages = story.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 8:
        raise HTTPException(400, "A story needs 1 to 8 pages.")
    if not all(isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip() for p in pages):
        raise HTTPException(400, "Every page needs text.")
    if need_review and not isinstance(story.get("final_review"), dict):
        raise HTTPException(400, "The story is missing its review.")
    plan = story.get("plan")
    if plan is not None and (not isinstance(plan, dict) or len(plan.get("pages") or []) != len(pages)):
        raise HTTPException(400, "The story's plan does not match its pages.")
    return story


def _check_refs(refs: list[str], limit: int = 3):
    if len(refs) > limit:
        raise HTTPException(400, f"Up to {limit} reference pictures, please.")
    for r in refs:
        if not r.startswith("data:image/") or len(r) > MAX_REF_BYTES * 4 // 3:
            raise HTTPException(400, "Reference pictures must be images under 1.5 MB.")


def _stream(gen):
    def body():
        try:
            for ev in gen:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:  # surface the problem to the page instead of a broken stream
            yield json.dumps({"type": "error", "message": _friendly(e)}) + "\n"
    return StreamingResponse(body(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _friendly(e: Exception) -> str:
    msg = str(e)
    if "content_policy" in msg or "safety" in msg.lower():
        return "Uncle Pie can't draw that one. Try a gentler idea?"
    return msg[:300] or e.__class__.__name__


class StoryIn(BaseModel):
    request: str = Field(max_length=2000)
    references: list[str] = []


class ReviseIn(BaseModel):
    story: dict
    feedback: str = Field(max_length=600)


class PictureIn(BaseModel):
    index: int = Field(ge=0, le=7)
    story: dict          # title, moral, characters, pages[{text, moment, picture, feeling}]
    references: list[str] = []


class ReportIn(BaseModel):
    story: dict
    pages: list[dict] = []


class NarrateIn(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    feeling: str = Field(default="", max_length=200)


class TranscribeIn(BaseModel):
    audio: str  # data URL


@app.get("/api/health")
def health():
    return {"ok": True, "passcode_required": bool(PASSCODE)}


@app.post("/api/story")
def story(body: StoryIn, x_passcode: str | None = Header(default=None)):
    _guard(x_passcode)
    _check_refs(body.references)

    def gen():
        refs = None
        if body.references:
            yield {"type": "stage", "agent": "Uncle Pie", "message": "Looking closely at your pictures..."}
            refs = media.describe_references(body.references, body.request)
            yield {"type": "references", "subjects": refs}
        yield from create_story(body.request, refs)

    return _stream(gen())


@app.post("/api/revise")
def revise(body: ReviseIn, x_passcode: str | None = Header(default=None)):
    _guard(x_passcode)
    _check_story(body.story, need_review=True)
    if not isinstance(body.story.get("plan"), dict) or not isinstance(body.story.get("brief"), dict):
        raise HTTPException(400, "The story is missing its plan.")
    return _stream(revise_story(body.story, body.feedback))


@app.post("/api/picture")
def picture(body: PictureIn, x_passcode: str | None = Header(default=None)):
    """Picture worker for one page: shot spec -> paint -> art judge -> (one redraw), streamed.
    Independent of the text (already finished) and of narration (its own requests)."""
    _guard(x_passcode)
    _check_refs(body.references, limit=4)  # cast sheet + up to 3 family photos
    _check_story(body.story)
    pages = body.story["pages"]
    if not 0 <= body.index < len(pages):
        raise HTTPException(400, "That page does not exist.")
    story = {
        "title": str(body.story.get("title", ""))[:200], "moral": str(body.story.get("moral", ""))[:400],
        "characters": [{"name": str(c.get("name", ""))[:60], "look": str(c.get("look", ""))[:400]}
                       for c in (body.story.get("characters") or [])[:6] if isinstance(c, dict)],
        "pages": [{k: str(p.get(k, ""))[:1500] for k in ("text", "moment", "picture", "feeling")} for p in pages],
    }

    def gen():
        q: queue.Queue = queue.Queue()

        def work():
            try:
                q.put({"type": "picture_result", **make_picture(body.index, story, body.references or None, q.put)})
            except Exception as e:
                q.put({"type": "error", "page": body.index + 1, "message": _friendly(e)})
            q.put(None)

        threading.Thread(target=work, daemon=True).start()
        while (ev := q.get()) is not None:
            yield ev

    return _stream(gen())


class CastIn(BaseModel):
    characters: list[dict]
    references: list[str] = []


@app.post("/api/cast")
def cast(body: CastIn, x_passcode: str | None = Header(default=None)):
    """Character sheet, painted once before the page workers fan out."""
    _guard(x_passcode)
    _check_refs(body.references)
    return {"image": f"data:image/jpeg;base64,{make_cast_sheet(body.characters[:6], body.references or None)}"}


@app.post("/api/report")
def report(body: ReportIn, x_passcode: str | None = Header(default=None)):
    _guard(x_passcode)
    _check_story(body.story, need_review=True)
    try:
        return report_card(body.story, body.pages[:8])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Could not score that story.")


@app.post("/api/narrate")
def narrate(body: NarrateIn, x_passcode: str | None = Header(default=None)):
    """Narration worker for one page. Needs only the text, so it never waits for pictures."""
    _guard(x_passcode)
    res = make_narration(0, {"text": body.text, "feeling": body.feeling})
    return Response(base64.b64decode(res["audio_b64"]), media_type="audio/mpeg",
                    headers={"X-Audio-Check": json.dumps(res["audio_check"]),
                             "Access-Control-Expose-Headers": "X-Audio-Check"})


@app.post("/api/transcribe")
def transcribe(body: TranscribeIn, x_passcode: str | None = Header(default=None)):
    _guard(x_passcode)
    header, _, b64 = body.audio.partition(",")
    if not header.startswith("data:audio/") or len(b64) > 8_000_000:
        raise HTTPException(400, "Please send a short audio recording.")
    ext = "mp4" if "mp4" in header else "ogg" if "ogg" in header else "webm"
    try:
        audio = base64.b64decode(b64, validate=True)
    except ValueError:
        raise HTTPException(400, "That recording could not be read.")
    return {"text": media.transcribe(audio, f"speech.{ext}")}
