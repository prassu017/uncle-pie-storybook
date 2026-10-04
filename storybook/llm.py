"""Thin wrapper around the OpenAI chat model.

The assignment pins the text model to gpt-3.5-turbo, so every storytelling and
judging call in this project goes through `call_model` below. Other OpenAI
models are only used for things gpt-3.5-turbo cannot do (images, speech,
reading photos); those live in media.py.
"""
import json
import os
import random
import re
import time
from functools import lru_cache

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

STORY_MODEL = "gpt-3.5-turbo"  # do not change: required by the assignment


def _load_env_file(path: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")):
    """Read KEY=value lines from a project .env file into the environment (existing variables win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#") and value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


@lru_cache(maxsize=1)
def client() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY")
    key_file = os.getenv("OPENAI_API_KEY_FILE")
    if not key and key_file and os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("Set OPENAI_API_KEY (or OPENAI_API_KEY_FILE) before running.")
    return OpenAI(api_key=key)


def call_model(prompt: str, max_tokens=3000, temperature=0.1, system: str | None = None,
               json_mode: bool = False) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    for attempt in range(6):
        try:
            resp = client().chat.completions.create(
                model=STORY_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            return resp.choices[0].message.content or ""
        except (RateLimitError, APIConnectionError, InternalServerError) as e:
            # Three drafts in parallel can hit the per-minute token limit: wait as long as OpenAI asks.
            if attempt == 5:
                raise
            m = re.search(r"try again in ([\d.]+)(ms|s)", str(e))
            wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 2.0 * (attempt + 1)
            time.sleep(wait + random.uniform(0.2, 1.0))
    return ""


def call_json(prompt: str, system: str, temperature=0.2, max_tokens=3000,
              validate=None, retries: int = 2) -> dict:
    """Ask for JSON, parse it, optionally validate it, and retry with the error.

    gpt-3.5-turbo sometimes drops a field or returns the wrong shape; feeding the
    exact problem back once is usually enough to fix it.
    """
    last_err = ""
    for attempt in range(retries + 1):
        p = prompt if not last_err else (
            f"{prompt}\n\nYour previous answer was rejected: {last_err}\n"
            "Return the complete JSON again, fixing that problem."
        )
        try:
            raw = call_model(p, max_tokens=max_tokens, temperature=temperature,
                             system=system, json_mode=True)
            data = json.loads(raw)
            if validate:
                validate(data)
            return data
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            last_err = str(e)[:300]
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Model did not return valid JSON: {last_err}")


# Categories serious enough that no gentle reframe makes them a bedtime story.
REFUSE = ("sexual", "sexual/minors", "self-harm", "self-harm/intent", "self-harm/instructions",
          "hate/threatening", "harassment/threatening", "violence/graphic", "illicit/violent")


def moderate(text: str) -> list[str]:
    """OpenAI's moderation classifier (a safety filter, not a writer). Returns flagged categories.
    Fails open with [] if the service is unavailable, because intake and the judge still guard the story."""
    try:
        r = client().moderations.create(model="omni-moderation-latest", input=text[:4000]).results[0]
    except Exception:  # noqa: BLE001
        return []
    cats = r.categories.model_dump(by_alias=True) if hasattr(r.categories, "model_dump") else dict(r.categories)
    return sorted(k for k, v in cats.items() if v)
