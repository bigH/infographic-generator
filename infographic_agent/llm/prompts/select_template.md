You are choosing the best infographic layout template for a piece of content.

## Content

- Topic: {topic}
- Audience: {audience}
- Learning preference: {learning_preference} (one of: text_heavy, image_heavy, balanced)
- Summary: {summary}

Content shape (counts of each field populated on the source content):
- facts: {facts_count}
- key_points: {key_points_count}
- steps: {steps_count}
- comparisons: {comparisons_count}
- quotes: {quotes_count}
- timeline_events: {timeline_events_count}

## Available templates

{template_options}

## Instructions

Pick the single best-fit template id from the list above. Base your choice on:

1. Which content field(s) are richest — the template descriptions above tell you which
   field(s) each template expects to be well-populated.
2. The user's stated `learning_preference` — an image_heavy preference favors templates
   with a strong visual/hero-image focus, a text_heavy preference favors templates that
   can carry more prose per slot, and balanced works with either as long as the content
   shape fits.

Return your choice via the structured output schema: the chosen `template_id`, a
`confidence` score between 0 and 1, and a short `rationale` explaining why this template
fits the content shape and learning preference better than the alternatives.
