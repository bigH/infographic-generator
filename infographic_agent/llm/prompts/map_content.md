You are filling an infographic template's content slots from source material.

## Content

- Topic: {topic}
- Audience: {audience}
- Learning preference: {learning_preference}
- Headline: {headline}
- Summary: {summary}
- Facts: {facts}
- Key points: {key_points}
- Steps: {steps}
- Comparisons: {comparisons}
- Quotes: {quotes}
- Timeline events: {timeline_events}

## Chosen template

- id: {template_id}
- description: {template_description}

## Available images

Each candidate below can be referenced by `image_id` in any image slot the template
schema defines. Choose the image whose `alt_text`/`tags` best fit each slot's purpose,
or leave `image_id` null if no candidate is a good match — do not force a bad fit.

{image_candidates}

## Instructions

Fill every field of the template's slot schema:

1. Trim and summarize the source content to fit each slot's length constraints — do not
   just truncate; write concise, complete phrasing.
2. Assign `image_id` values from the candidates above to any image slots, or leave null
   if nothing fits well.
3. If the source content has no explicit title/headline suited to a required text field,
   generate a short, clear one grounded in the topic and summary — do not invent facts
   not present in the source content.

Return the fully filled slot object via the structured output schema for this template.
