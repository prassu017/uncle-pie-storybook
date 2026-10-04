# Safety and privacy

The audience is children aged 5 to 10, so safety is layered: no single check is relied on alone.

## Requests

- **Moderation gate.** Every request and change request is screened with OpenAI's moderation classifier.
  Sexual content, self-harm, graphic violence, threats and similar categories are refused with a kind
  message.
- **Gentle reframe.** Milder requests are not refused. Intake rewrites them into a gentle version that
  keeps what the child wanted, including the plot points the story must keep. "A knight kills a dragon"
  becomes a story about a knight and a dragon who learn to trust each other. If the request uses a
  violent word and intake still marks it safe, intake is asked again with an explicit instruction.
- **Prompt injection.** Family text is fenced and treated as story material only, never as instructions.

## The story

- A word blocklist blocks unsuitable words on the page.
- The judge's comfort score covers peril, fear and sadness left unresolved.
- The finished story is moderated again before it can pass.

## Pictures

- The art editor checks every illustration is kid-safe and contains no text.
- The image model's own content policy applies.

## Photos

- Photos are resized in the browser and sent to OpenAI only to describe the characters and draw the
  book. This app never stores them.
- The photo reader describes appearance only (colours, clothing, shapes), never identity, age or other
  sensitive attributes.
- Characters are inspired by photos, not likenesses.

## Data and cost

- Finished books are kept only in the reader's own browser (IndexedDB). No server-side storage.
- The OpenAI key is read from an environment variable and never committed.
- API inputs are size-limited and validated; `APP_PASSCODE` can lock a deployment.
