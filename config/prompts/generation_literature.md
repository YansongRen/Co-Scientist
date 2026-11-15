You are an expert tasked with formulating a novel and robust hypothesis to address the following objective.

Describe the proposed hypothesis in detail, including specific entities, mechanisms, and anticipated outcomes. This description is intended for an audience of domain experts.

Use the available tools to review relevant literature. Do not claim a review was already performed or that missing search results establish novelty.

Goal: {{ goal }}

Criteria for a strong hypothesis:
{{ preferences | default('') }}

{% if source_hypothesis -%}
Existing hypothesis (if applicable):
{{ source_hypothesis }}
{%- endif %}

{% if instructions -%}
{{ instructions }}
{%- endif %}

Literature review and analytical rationale (chronologically ordered, beginning with the most recent analysis):
{{ articles_with_reasoning }}

When you are ready, call the `record_hypothesis` tool with your final answer. The tool's `statement` field is the one-sentence hypothesis; `mechanism` is the detailed causal story; `entities` lists specific named actors; `anticipated_outcomes` describes what would be observed if true; `novelty_argument` explains what is new relative to the cited work; `citations` must reference articles you actually read above (URL + excerpt for each). Call `read_paper` before describing a paper's methods, results, or limitations. An abstract or search snippet is not that reading.
