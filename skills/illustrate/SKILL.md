---
name: illustrate
description: |
  Adds generated illustrations to an HTML report the agent has already
  written. Use ONLY as a companion to another skill that produced an HTML
  deliverable, and ONLY when the spoken request asked for pictures — "use
  images", "make it visual", "with illustrations", "add some artwork".
  Declares `image.generate` intents; the pipeline generates the files after
  the agent exits, because the agent holds no provider key. Each image costs
  real money and is held for the operator's approval, so ask for few and make
  each one earn its place. Do NOT use it for diagrams, charts, flows or any
  data visualisation — those are inline SVG via html-artifact-output and
  dataviz, which are free, instant and better at it. Do NOT use it when no
  HTML report was written, and do NOT use it just because a report is long.
verbs: [image.generate]
requires: [ATTICUS_IMAGES]
risk: tracked
outputs: [html, png]
cost: medium
---

# illustrate

You have written an HTML report. The request also asked for images. Describe the
ones worth making; something outside this sandbox will draw them.

**You cannot generate images and must not try.** No provider key is reachable
from here, by design — this agent runs on text derived from ambient audio, so it
holds no keys. If you find yourself reaching for an API, a `curl`, or the
`image-asset-generation` skill's scripts, stop: you are doing the wrong half of
the task. Your half is the `<img>` tags and the intent files.

## Diagram or illustration? Answer this before writing anything

Most things that look like "add images" are diagrams, and a diagram must be
**inline SVG**, not a generated picture:

| Want | Use | Why |
|---|---|---|
| Architecture, flow, sequence, hierarchy | inline SVG | exact, legible, free, instant |
| Chart, plot, comparison, any data | inline SVG (`dataviz`) | a generated "chart" invents its numbers |
| Screenshot, UI mock, code | don't | generation fabricates plausible-looking lies |
| Cover art, section header, metaphor, mood | `image.generate` | genuinely needs a picture |

A generated image cannot render text reliably and will not draw your boxes where
you meant them. **If the thing you want has correct content, it is a diagram.**
Reach for this skill only for the last row.

## The report must be complete without the images

The report publishes immediately. The images do not: each one waits for the
operator to approve the spend, which may be hours later or never. So:

- Every `<img>` needs real `alt` text and a `<figcaption>` that carries the
  point on its own. A reader who never sees the picture must lose nothing but
  decoration.
- Never write "as shown in the image below" or make an argument that depends on
  a picture the reader may not have.
- Style the figure so a missing file collapses quietly rather than leaving a
  broken-image icon in the middle of a paragraph.

## What to write

Two things, both required, or nothing happens.

**1. The tag, in your HTML.** The path is always `images/<name>.png`, relative
to your report:

```html
<figure>
  <img src="images/context-window-metaphor.png" alt="A library reading room where
       only the nearest shelf is lit, the rest fading into dark" loading="lazy">
  <figcaption>Context is a lit shelf, not the whole library.</figcaption>
</figure>
```

**2. The intent, one file per image**, at
`output/outbox/NNN-image.generate.json`. `NNN` is a zero-padded sequence you
control:

```json
{
  "verb": "image.generate",
  "file": "images/context-window-metaphor.png",
  "description": "A wide reading room in an old library. One shelf in the foreground is warmly lit and clearly in focus; the shelves behind it fade progressively into blue-grey darkness. No people, no text, no signage.",
  "style": "clean editorial illustration, flat colours, restrained palette, generous whitespace",
  "said": "make it detailed and use images"
}
```

### The fields

| Field | Required | Notes |
|---|---|---|
| `verb` | yes | exactly `image.generate` |
| `file` | yes | `images/<name>.png` — lowercase, digits, dash, underscore, dot; no subdirectories, no `..`, always `.png` |
| `description` | yes | the scene, in plain prose. This is the prompt |
| `style` | no | overrides the house style for this one image |
| `said` | no | the words in the transcript that asked for pictures |

`file` must match the `src` in your `<img>` exactly, or the report will point at
a file that never arrives.

## Writing a description that produces something usable

- **Describe a scene, not a subject.** "A library reading room where one shelf is
  lit" beats "context windows".
- **Say what is absent.** "No people, no text, no signage" — generators add text
  unprompted and it always comes out misspelled.
- **Do not ask for words in the image.** Labels, captions and diagram text belong
  in your HTML, where they are correct and selectable.
- **One idea per image.** A description with three clauses produces a muddle.
- **Name the concrete nouns.** "A worn brass key on dark slate" survives
  generation; "the idea of access" does not.
- **Fix the frame.** Say the viewpoint and crop — close-up, wide, overhead, eye
  level. Left unsaid, you get an arbitrary one and re-rolling costs another
  charge.
- **Fix the light.** "Low warm side light" or "flat overcast" does more for how
  a picture reads than any adjective about mood.

## Then have the prompt reviewed — before anything is paid for

**Draft the description, get it critiqued in a SUBAGENT, revise, and only then
write the intent file.** Not optional, and not something to do in your own head:
a prompt reviewed in a separate context comes back better than one the same
context talks itself into, because the reviewer has not spent the last twenty
minutes deciding what the report is about and cannot silently supply the missing
detail from memory. An image is the one thing here where a weak prompt costs real
money to find out about — and it is the operator's money, and it waits on their
approval, so a re-roll is not a keystroke.

Spawn the reviewer with the `Agent` tool. Give it the description and nothing
else — no report, no context — because what you are testing is whether the words
alone produce the picture:

> Here is a prompt for an AI image generator. You cannot see the article it
> illustrates, which is the point: judge the prompt only on what it says.
>
> `<the description>`
>
> Answer in four short parts:
> 1. Describe the picture you would expect this to produce.
> 2. What is UNDERSPECIFIED — subject, viewpoint, framing, lighting, palette,
>    what should be absent? Name what a generator would be free to invent.
> 3. What is likely to go WRONG — text or lettering it will try to render,
>    anatomy, an abstraction it cannot draw, a cliché it will default to?
> 4. Rewrite it as one improved paragraph.
>
> Do not be polite about it. A prompt that produces something merely acceptable
> is a failure — it costs the same as a good one.

Then read part 1 first. **If the picture the reviewer describes is not the
picture you wanted, the prompt is wrong, however good it looked** — that gap is
the whole reason to ask. Take the rewrite, or merge it with your own, and use
that as the `description`.

One review per image is enough. Do not loop: a second round tunes wording that
the generator will not notice, and each round is agent time the operator pays for
in a different currency.

## How many

**At most four per report, and four is a lot.** Each is a separate charge and a
separate thing the operator has to approve on their phone. One good cover image
plus a diagram in SVG beats four mediocre pictures. If the request did not ask
for images, make none — a report is not improved by decoration nobody wanted.

The pipeline enforces its own cap and will refuse the rest, which shows up in the
report as a refusal rather than as an image. Asking for fewer is better than
being trimmed.

## The order, once more

1. Decide it is genuinely an illustration and not a diagram.
2. Write the `<figure>` with alt text and a caption that stand on their own.
3. Draft the description.
4. **Have it reviewed in a subagent. Revise.**
5. Write the intent file with the revised description.

Steps 3-5 are per image, and step 4 is the one that is easy to skip and expensive
to have skipped.

## What happens after you exit

1. The pipeline reads your intent files and validates each one.
2. Each is held for approval and announced to the operator by push, with the
   filename, your description and the estimated cost.
3. On approval it generates the PNG into `images/` beside your report.
4. The site rebuild picks it up and the `<img>` you wrote resolves.

A receipt for every request is injected into your report, so the operator can
see what was asked for and what came of it without opening a log.
