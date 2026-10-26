You write the front page of **{{EDITION_LABEL}}** for {{DATE_HUMAN}} ({{DATE}}), JST. The articles are finished; you only summarise them.

# Inputs

{{SUMMARIES}}

# Task

Write two parts, in this order, in Markdown:

1. `## Executive Summary` — between {{SUMMARY_LOW}} and {{SUMMARY_HIGH}} words. Open with the day's through-line in a short paragraph, then summarise the stories section by section in the order given, naming people, places and numbers exactly as the summaries carry them. Add nothing that is not in the summaries: no new facts, numbers or claims.
2. `### {{CROSS_LABEL}}` — at least {{MIN_CROSS}} bullets, each naming a real structural force that links stories (a shared mechanism, constraint, actor or policy lever) in one or two sentences and ending with `{{CROSS_FORMAT}}`. One cross-reference per bullet; the title inside the quotes must be copied exactly from the list.

Do not write a title, a date line, or a table of contents; the pipeline adds them. Return only the two parts above.
