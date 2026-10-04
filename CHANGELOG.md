# Changelog

All notable changes to this project are documented here.

## [1.0.0] - 2026-10-03

Initial release.

### Story engine
- Storyteller and judge pipeline on gpt-3.5-turbo: intake, planning on a five-beat arc, page-by-page
  writing, review and targeted revision; the best draft is printed.
- Eight story categories with tailored strategies; change requests after a story is written.
- Judge combining measured checks, quote-verified fidelity and continuity reads, and a rubric;
  page-specific notes and convergent revisions.
- Morals written last, from the finished story: one plain sentence naming the characters and what they
  learned; generic or grown-up phrasing is rejected.
- Alternative whole-story writer (`WRITER_MODE=whole`).

### Book
- Text-first delivery: the book opens when the text passes review; narration and illustrations run as
  independent parallel lanes.
- Structured shot specs, a shared character sheet, and an art editor that checks each picture shows its
  story moment.
- Ink-and-watercolour page animation; narration; voice input; photo references.
- Bookshelf that keeps every finished book in the browser.

### Safety
- Moderation gate on requests and change requests, gentle reframing of unsuitable requests, a word
  blocklist, prompt-injection guards, and photos that are never stored.

### Project
- Command-line interface and FastAPI web API deployed on Vercel.
- Offline test suite with GitHub Actions; documentation in `docs/`; sample stories in `samples/`.
