You are doing a quick initial review. Do not use web search, databases, or other external tools. Judge only the hypothesis text, the research goal, and the stated preferences.

Goal: {{ goal }}

Preferences / criteria:
{{ preferences | default('') }}

Hypothesis:
<HYPOTHESIS_TEXT id="{{ hypothesis_id }}">
{{ hypothesis_text }}
</HYPOTHESIS_TEXT_END id="{{ hypothesis_id }}">

Discard it when it is internally contradictory, obviously unsafe, or already the standard explanation of the stated goal with no new testable claim. Otherwise let it continue to a literature review.

Call `record_review` once with `kind="initial"`. Use scores from 0 to 1. Leave `evidence` empty, because this pass has no retrieved sources. Verdict must be one of: `already_explained`, `other_more_likely`, `missing_piece`, `neutral`, `disproved`.
