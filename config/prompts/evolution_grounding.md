You are improving one hypothesis by grounding it in literature. Produce a new hypothesis. Do not edit or replace the parent.

Goal: {{ goal }}

Preferences: {{ preferences | default('') }}

Parent:
<HYPOTHESIS_TEXT id="{{ hypothesis_id }}">
{{ hypothesis }}
</HYPOTHESIS_TEXT_END id="{{ hypothesis_id }}">

Review of the parent:
{{ review | default('(no review)', true) }}

Use search tools to find the paper, then call read_paper before relying on it. Search results are abstracts. If reading_depth is not full_text, say the full text was unavailable and do not invent methods or results. If search returns nothing, say so and do not invent a paper.

Register exactly one record_hypothesis. Set parent_ids to ["{{ hypothesis_id }}"]. Cite a URL only when it appeared in a tool result. This child must enter the tournament on its own; a retrieved paragraph is not an experimental result.
