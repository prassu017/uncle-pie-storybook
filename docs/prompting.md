# Prompt and agent design

All prompts live in [`storybook/prompts.py`](../storybook/prompts.py). This page explains the
strategies behind them.

## Principles

**One job per call.** Intake, planning, writing a page, auditing fidelity, reading for continuity,
reviewing, and designing a shot are separate calls with narrow instructions. A small model does narrow
jobs well and broad ones poorly.

**Structured output, validated in code.** Every call returns JSON. Code validates the shape and the
content (page counts, word and sentence limits, tense, distinct events) and, on failure, sends the exact
problem back: *"this sentence has 21 words: '...'. Keep every sentence to 16 words or fewer."*

**Bookkeeping belongs to code.** The model is asked to write and judge, not to keep track of things. Code
assigns the family's ideas to pages, decides where the refrain appears, spreads camera angles across
pages, routes each note to its page, and chooses which draft to print.

**Plain language between agents.** The writer receives the plan as sentences, not JSON, so internal field
names never leak into the story. Code rejects any draft that mentions how the story was made.

**Show, don't tell, with an example from elsewhere.** The writer sees one sample page from a different
story (a tortoise in the rain) that shows the length, rhythm and dialogue wanted, without anything it
could copy.

## The storyteller

- **Role and audience:** a warm storyteller writing for a 5-year-old listener and a 10-year-old who
  should still enjoy it.
- **Craft rules:** sentences of 5 to 12 words (never over 16), everyday words, past tense throughout,
  feelings shown through faces and actions rather than named, a refrain the child can join in on, gentle
  peril that always resolves, and a calm ending.
- **Category strategies:** fable, friendship, adventure, funny, mystery, discovery, feelings and calm each
  get their own shape (rule of three for funny, fair clues for mystery, name-the-feeling for feelings).
- **Five-beat arc:** setup, trouble, rising, turn, home.
- **Continuity:** each page's writer is told what is new on this page and what already happened, so events
  are not repeated and nobody "arrives" twice.

## The judge

A single prompt asking a small model "is this good?" is lenient. Vishnu is a small system instead:

| Layer | How it works |
|---|---|
| Story ledger | One literal extraction call per page lists what happens and who is there; code flags an event that matches an earlier page's (ignoring names) and a new character first mentioned as "the X" with no introduction. The writer of each page is shown the ledger so far |
| Repeated scenes | Code compares every pair of pages and blocks a page that shares most of its words with another; a scene retold in new words can still slip through |
| Measured checks | Word and sentence counts, reading grade, tense, stock phrases, blocklist and refrain placement are computed in code and handed to the judge as facts |
| Fidelity audit (Ida) | For each requested idea, quote the sentence that shows it; code credits it only if the quote is really on a page |
| Continuity read | Two independent reads report repeated events, double arrivals and things used before they were set up, each with a quote; code keeps only verified quotes, and a problem blocks when both reads agree |
| Rubric | Seven criteria on an explicit 1-5 scale, weaknesses listed before scores, evidence for each score |
| Gate | A draft passes only if every score is 4+, no blocking problem remains and every idea was found |
| Notes | Ranked (blocking first), routed to the page they concern, at most three per page |
| First-draft choice | Three drafts are written; code ranks them, and ties are broken by a side-by-side comparison asked both ways round, which a small model does far more reliably than absolute scoring |
| Selection | Drafts ranked by (passed, fewest blocking problems, total score); the best is printed, not the last |
| Read-aloud polish | Pages above the reading-level target are simplified; code keeps a rewrite only if it is easier, faultless and loses nothing |

The judge never sees the writer's instructions, only the request and the story.

## The moral

The moral is written last, from the finished story, so it can be about what actually happened. Uncle Pie
closes the book the way a grandparent would: one plain sentence that names the characters and what they
learned ("Robbie found out that storms are loud and scary, but they always pass."). Code rejects morals
that don't name the characters, read above a young child's level, or fall back on stock phrasing ("can
lead to", "overcome any challenge", "the greatest treasure"), and asks again.

## Shot specs for illustrations

Image models misread vague verbs ("Fox helps Bear" can be drawn as Fox running away). Inside each picture
worker, Uncle Pie fills a shot spec:

- **Main action:** one physical sentence of who does what to whom
- **Who is where:** position in frame, pose, gaze and face for each character
- **Setting, light, camera, mood**
- **Do not show:** what would make the picture wrong for this moment

Code assembles the spec in a fixed order, main action first, then appends the locked style and
character sheet.

## Agent handoffs

| From | To | What is handed over |
|---|---|---|
| Ranganathan | Uncle Pie (planner) | Category, ideas, lesson, safety reframe |
| Uncle Pie (planner) | Uncle Pie (writer) | The plan in plain language, with each page's required and new events |
| Vishnu | Uncle Pie (writer) | Ranked notes for specific pages |
| Uncle Pie | Caldecott | A shot spec per page |
| Ursula | Caldecott | A rewritten shot spec when a picture fails |
| Homai | Ranganathan, Uncle Pie | Appearance descriptions from family photos |

## Guarding against prompt injection

Text typed by a family is fenced between `<<<` and `>>>` (with any fence characters in it neutralised),
and every prompt that includes it states that it is story material only, never instructions.
