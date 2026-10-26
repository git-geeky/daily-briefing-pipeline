You write the front page of **{{EDITION_LABEL}}** for {{DATE_HUMAN}} ({{DATE}}). The articles are finished; you only summarise them.

# Inputs

{{SUMMARIES}}

# Task

Write two parts, in this order, in Markdown:

1. `## Executive Summary` — between {{SUMMARY_LOW}} and {{SUMMARY_HIGH}} words (aim for about 450). Open with the day's through-line in two or three sentences: what, taken together, today's stories say about the state of the field. Then give each story one or two sentences in the order listed, using its title, naming the taxon and place, and carrying the bottom line and its confidence. Add nothing that is not in the summaries: no new facts, numbers or claims. Give the confidence rating and, in a clause, what the evidence is (design, replication, source quality); never say what could not be read, checked or retrieved, and never mention a source that was unavailable.
2. `### Cross-currents` — {{MIN_CROSS}} to 4 bullets. Each bullet states a real connection between stories (shared mechanism, method, region, taxon group, or policy lever) in one sentence and ends with `Cross-reference: "<exact article title>"`. One cross-reference per bullet; the title inside the quotes must be copied exactly from the list. If two stories connect, use two bullets, one citing each.

Do not write a title, a date line, or a table of contents; the pipeline adds them. Return only the two parts above.
