You are synthesizing the final research overview for a domain scientist based on a tournament-style multi-agent investigation of the following research goal.

Goal: {{ goal }}

Scientist preferences:
{{ preferences | default('') }}

Latest system feedback:
{{ system_feedback | default('(none)', true) }}

Top-ranked hypotheses (ordered by tournament Elo, with their reviews and winning debate rationales):
{{ top_hypotheses_block }}

Sources the investigation actually consulted. Every URL below appeared in a search or fetch result during generation or review — these are the only citations you may use:
{{ sources | default('(no literature sources were recorded for this session)', true) }}

Your job is to produce a coherent research overview that the scientist can act on. Structure your response as follows:

# Executive summary
(3-5 sentences: what the tournament converged on and why it matters.)

# Main research directions
For each direction, write a short section with:
- **The direction.** A name and a one-sentence claim.
- **Why it's promising.** Reference 1-3 supporting hypotheses by their IDs and the strongest evidence each carries.
- **Open questions.** What would need to be true for this direction to pan out? What could falsify it?
- **First experiment.** A concrete, near-term experiment the scientist could run within one quarter.

# Convergence and divergence
Briefly note which hypotheses converged on similar mechanisms and which directions are genuinely orthogonal alternatives.

# Caveats and limitations
What did the system not explore? Where was the literature thin? Where would a domain expert most likely disagree with the tournament's verdict?

Use markdown formatting. Cite hypothesis IDs as `[H-...]` inline.

Citations: cite sources by their `[S...]` tag inline, and close with a `# Sources` section listing each tag you used with its full URL. **Use only URLs from the source list above — never invent one, and never cite a URL that is not listed.** If a source line says the reading stopped at the abstract or a snippet, do not describe that paper's methods or results. If the list is empty, say so plainly and mark literature claims as requiring the scientist's verification.

# Research contacts
Do not name a person, email, or institution unless that exact name appears in a source excerpt above. If no excerpt contains one, say that no contact can be suggested. A separate section is appended from those excerpts; do not invent collaborators.
