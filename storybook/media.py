"""Pictures, voice and photo-reading.

gpt-3.5-turbo cannot see images, draw, speak or listen, so these jobs use other
OpenAI models. None of them write or judge the story text.
"""
import base64
import io
import json
import os
import random
import re
import time

from openai import RateLimitError

from . import prompts as P
from .llm import client

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2")
# gpt-4.1-mini was the smallest model that judged action correctly in calibration: gpt-4o-mini rejected a
# correct "Fox lifts the log off Bear" picture as "touching it lightly".
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4.1-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "ballad")
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")


def illustration_prompt(scene: str, characters: list[dict]) -> str:
    """Wrap a scene brief (written by Uncle Pie or the art judge) in the locked style and cast.
    Code owns the parts that must never drift between pages; agents only write the scene."""
    cast = "\n".join(f"- {c.get('name', '')}: {c.get('look', '')}" for c in characters)
    return (f"{scene}\n\nSTYLE: {P.ILLUSTRATION_STYLE}\n\n"
            f"CHARACTER SHEET (draw them exactly like this on every page):\n{cast}")


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    header, _, b64 = data_url.partition(",")
    mime = header.split(";")[0].removeprefix("data:") or "image/jpeg"
    return base64.b64decode(b64 if b64 else header), mime


def _with_rate_limit_retry(fn, tries: int = 5):
    """Image models have low per-minute limits on small accounts. Wait as long as OpenAI asks, then retry."""
    for attempt in range(tries):
        try:
            return fn()
        except RateLimitError as e:
            if attempt == tries - 1:
                raise
            m = re.search(r"try again in ([\d.]+)s", str(e))
            time.sleep((float(m.group(1)) if m else 10.0 * (attempt + 1)) + random.uniform(0.5, 3))


def illustrate(prompt: str, references: list[str] | None = None, quality: str = "medium") -> str:
    """Return a JPEG as base64. References (data URLs: the cast sheet and family photos) steer the look."""
    return _with_rate_limit_retry(lambda: _illustrate_once(prompt, references, quality))


def _illustrate_once(prompt: str, references: list[str] | None, quality: str) -> str:
    common = dict(model=IMAGE_MODEL, prompt=prompt, size="1024x1024", quality=quality,
                  output_format="jpeg", output_compression=82)
    if references:
        files = []
        for i, ref in enumerate(references[:4]):
            data, mime = _decode_data_url(ref)
            ext = "png" if "png" in mime else "jpg"
            files.append((f"ref{i}.{ext}", io.BytesIO(data), mime))
        ref_prompt = (prompt + "\n\nThe attached images are references for how the characters look: "
                      "the first is the book's character sheet; any others are family photos. Keep every "
                      "character exactly as on the sheet. Draw a new scene in the style above; do not copy "
                      "the sheet's layout or the photos' backgrounds.")
        resp = client().images.edit(image=files, **{**common, "prompt": ref_prompt})
    else:
        resp = client().images.generate(**common)
    return resp.data[0].b64_json


def judge_image(b64_jpeg: str, scene: str, characters: list[dict], moment: str = "") -> dict:
    """Art judge: a vision model checks one illustration against the story moment, shot spec and cast."""
    cast = "\n".join(f"- {c.get('name', '')}: {c.get('look', '')}" for c in characters)
    prompt = P.IMAGE_JUDGE_PROMPT.format(moment=moment or "(see the shot spec)", scene=scene, cast=cast)
    resp = _with_rate_limit_retry(lambda: client().chat.completions.create(
        model=VISION_MODEL, response_format={"type": "json_object"}, temperature=0, max_tokens=900,
        messages=[{"role": "system", "content": P.IMAGE_JUDGE_SYSTEM},
                  {"role": "user", "content": [
                      {"type": "text", "text": prompt},
                      {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_jpeg}", "detail": "low"}}]}]))
    try:
        v = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        v = {}
    checks = {k: bool(v.get(k)) for k in ("matches_scene", "characters_match", "no_text", "kid_safe")}
    rs = v.get("revised_scene", "")
    if isinstance(rs, dict):  # the model sometimes returns the spec as an object; flatten it to text
        v["revised_scene"] = "\n".join(f"{k}: {val}" for k, val in rs.items())
    return {**checks, "passed": all(checks.values()), "note": str(v.get("fix", ""))[:300],
            "what_i_see": str(v.get("what_i_see", ""))[:300],
            "revised_scene": str(v.get("revised_scene", ""))[:900]}


def narrate(text: str, direction: str = "", voice: str | None = None) -> bytes:
    """Return MP3 bytes of Uncle Pie reading the text, following the page's voice direction."""
    instructions = P.NARRATOR_INSTRUCTIONS + (f" For this page: {direction}" if direction else "")
    resp = _with_rate_limit_retry(lambda: client().audio.speech.create(
        model=TTS_MODEL, voice=voice or TTS_VOICE, input=text, instructions=instructions, response_format="mp3"))
    return resp.content


def transcribe(audio: bytes, filename: str = "speech.webm") -> str:
    resp = _with_rate_limit_retry(lambda: client().audio.transcriptions.create(
        model=STT_MODEL, file=(filename, io.BytesIO(audio))))
    return resp.text.strip()


def describe_references(images: list[str], request: str) -> list[dict]:
    """Turn uploaded photos into short drawing descriptions the text model can use."""
    content = [{"type": "text", "text": P.REFERENCE_PROMPT.format(request=request or "(not given yet)")}]
    for url in images[:4]:
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})
    resp = _with_rate_limit_retry(lambda: client().chat.completions.create(
        model=VISION_MODEL, response_format={"type": "json_object"}, temperature=0.2, max_tokens=600,
        messages=[{"role": "system", "content": P.REFERENCE_SYSTEM}, {"role": "user", "content": content}]))
    try:
        subjects = json.loads(resp.choices[0].message.content or "{}").get("subjects", [])
    except json.JSONDecodeError:
        subjects = []
    return [{"label": str(s.get("label", "a character"))[:60], "look": str(s.get("look", ""))[:300]}
            for s in subjects if isinstance(s, dict)]
