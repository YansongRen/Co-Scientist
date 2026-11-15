You are simulating a hypothesis step by step to find where it would fail. This is model reasoning about a proposed mechanism or experiment. It is not an experiment, not a measurement, and not evidence that the hypothesis is true.

Goal: {{ goal }}

Hypothesis:
<HYPOTHESIS_TEXT id="{{ hypothesis_id }}">
{{ hypothesis_text }}
</HYPOTHESIS_TEXT_END id="{{ hypothesis_id }}">

Procedure:
1. Walk through the proposed mechanism or experiment in order.
2. At each step, name a concrete way it could fail and what observation would show that failure.
3. Say whether any failure is fatal to the core claim or only to a peripheral detail.
4. Do not invent numerical results, citations, or laboratory outcomes.

Call `record_review` once with `kind="simulation"`. Leave `evidence` empty. Put the failure scenarios in `notes`. Verdict must be one of: `already_explained`, `other_more_likely`, `missing_piece`, `neutral`, `disproved`. Use `disproved` only when the walk-through shows the core claim cannot hold together.
