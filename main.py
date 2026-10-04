"""Uncle Pie's Storybook - command-line version.

    python main.py                         # interactive: tell a story, then ask for changes
    python main.py "a bear and a fox..."   # one-shot
    python main.py "..." --out book --audio   # also record narration
    python main.py "..." --out book --media   # narration and illustrations, in parallel

The web app (api/index.py + public/) runs the same pipeline.

Before submitting the assignment, describe here in a few sentences what you would have built next if you spent 2 more hours on this project:

1. An evaluation set: 30 varied requests (plot-heavy, one-line, unsafe, multi-character, with photos)
   run nightly, tracking Vishnu's scores, fidelity, reading grade and art-judge pass rate, so every
   prompt change is measured instead of eyeballed. I'd also calibrate Vishnu against a few stories I
   score by hand, the way I calibrated the art judge on a known-good and a known-bad picture.
2. Re-plan the arc for change requests that alter the whole plot ("make it about pirates instead");
   today additions are placed on one page and verified, but plot-wide changes are applied page by page.
3. Read-along highlighting: use word timestamps from transcribing the narration to light up each word
   as Uncle Pie reads it, which helps early readers.
"""
import argparse
import base64
import json
import os
import sys
import time

from storybook.llm import call_model  # noqa: F401  (kept from the original skeleton; all text goes through it)
from storybook.pipeline import create_story, revise_story

example_requests = "A story about a girl named Alice and her best friend Bob, who happens to be a cat."


def run(gen):
    """Print progress events and return the finished story."""
    story = None
    for ev in gen:
        t = ev["type"]
        if t == "stage":
            print(f"  [{ev['agent']}] {ev['message']}")
        elif t == "brief":
            print(f"  [Uncle Pie] This sounds like a {ev['category']} story, {ev['pages']} pages.")
            if not ev["safety"].get("ok", True):
                print(f"  [Uncle Pie] I'll tell a gentler version: {ev['safety'].get('reframe')}")
        elif t == "review":
            s = " ".join(f"{k}={v}" for k, v in ev["scores"].items())
            print(f"  [Vishnu] Draft {ev['round']}: {s}  -> {'PASS' if ev['passed'] else 'needs work'}")
            print(f"           {ev['summary']}")
        elif t == "done":
            story = ev["story"]
    return story


def show(story):
    print("\n" + "=" * 70)
    print(f"  {story['title']}")
    print("=" * 70)
    for i, p in enumerate(story["pages"], 1):
        print(f"\n[Page {i}]\n{p['text']}")
    print(f"\nThe moral: {story['moral']}\n")


def save(story, out_dir, audio=False, images=False, t0=None):
    """Write story.json, plus narration and/or pictures. The two lanes run in parallel."""
    os.makedirs(out_dir, exist_ok=True)
    elapsed = lambda: f"{time.time() - t0:.0f}s" if t0 else ""  # noqa: E731
    if audio or images:
        from storybook.orchestrator import make_book
        n = len(story["pages"])
        for ev in make_book(story, audio=audio, images=images):
            t = ev["type"]
            if t == "cast":
                print(f"  [Pictures] {ev['message']}")
            elif t == "cast_done":
                with open(os.path.join(out_dir, "cast.jpg"), "wb") as f:
                    f.write(base64.b64decode(ev["image_b64"]))
                story["cast_image"] = "cast.jpg"
            elif t == "error":
                print(f"  [{ev['lane'].capitalize()}] page {ev['page']} failed, skipped: {ev['message']}")
            elif t == "page" and ev["step"] == "redrawing":
                print(f"  [Page {ev['page']}] {ev['message']}")
            elif t == "picture_done":
                ic = ev["image_check"]
                print(f"  [Page {ev['page']}] picture {'OK' if ic['passed'] else 'flagged'}"
                      f"{' (redrawn)' if ic['redrawn'] else ''}  {elapsed()}")
            elif t == "lane_done":
                print(f"  [{ev['lane'].capitalize()}] all done at {elapsed()}")
                story.setdefault("timings", {})[ev["lane"]] = round(time.time() - t0) if t0 else None
            elif t == "report":
                story["report"] = r = ev["report"]
                print(f"  [Report card] overall {r['overall']}/100 | text {r['text']['score']} | "
                      f"ideas {r['fidelity']['found']}/{r['fidelity']['asked']} | pictures "
                      f"{r['pictures']['passed']}/{r['pictures']['total']} | narration "
                      f"{r['narration']['passed']}/{r['narration']['total']}")
            elif t == "book_done":
                for res in ev["narrations"]:
                    if not res:
                        continue
                    i = res["page"]
                    name = f"page{i}.mp3" if i <= n else "moral.mp3"
                    with open(os.path.join(out_dir, name), "wb") as f:
                        f.write(base64.b64decode(res["audio_b64"]))
                    if i <= n:
                        story["pages"][i - 1].update(audio=name, audio_check=res["audio_check"])
                    else:
                        story["moral_audio"] = name
                for res in ev["pictures"]:
                    if not res:
                        continue
                    i = res["page"]
                    with open(os.path.join(out_dir, f"page{i}.jpg"), "wb") as f:
                        f.write(base64.b64decode(res["image_b64"]))
                    story["pages"][i - 1].update(image=f"page{i}.jpg", image_check=res["image_check"],
                                                 scene=res["scene"], prompt_trail=res["prompt_trail"])
    with open(os.path.join(out_dir, "story.json"), "w", encoding="utf-8") as f:
        json.dump(story, f, indent=2, ensure_ascii=False)
    print(f"  Saved to {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="Uncle Pie tells bedtime stories for ages 5-10.")
    ap.add_argument("request", nargs="?", help="what the story should be about")
    ap.add_argument("--out", help="folder to save story.json (and media)")
    ap.add_argument("--audio", action="store_true", help="also record narration")
    ap.add_argument("--images", action="store_true", help="also paint illustrations")
    ap.add_argument("--media", action="store_true", help="both narration and illustrations")
    args = ap.parse_args()

    user_input = args.request or input("What kind of story do you want to hear? ")
    t0 = time.time()
    story = run(create_story(user_input or example_requests))
    story["timings"] = {"text": round(time.time() - t0)}
    print(f"  [Text] ready to read at {story['timings']['text']}s")
    show(story)

    if sys.stdin.isatty() and not args.request:
        while True:
            fb = input("Want to change anything? (Enter to finish) ").strip()
            if not fb:
                break
            story = run(revise_story(story, fb))
            show(story)

    if args.out:
        save(story, args.out, audio=args.audio or args.media, images=args.images or args.media, t0=t0)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as e:  # e.g. no API key, or a request the safety gate refused
        sys.exit(f"Uncle Pie: {e}")
    except KeyboardInterrupt:
        sys.exit(1)
