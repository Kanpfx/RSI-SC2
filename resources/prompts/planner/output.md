- Return only the four level-two headings below, in order, with their content. Do not wrap the output in a code block or include DSL actions.
- `assessment`: Summarize the current strategic situation from observation; aim for about 100 tokens, without listing observed details. State uncertainty when relevant.
- `stage`: Current strategic stage name.
- `objective`: The purpose of this decision, in one sentence.
- `guidence`: Aim for 1-3 concise, atomic items ordered by urgency and tactical value, with one concrete action goal per item. Specify the unit or structure type, intended target, and necessary quantity. Do not specify entity IDs, exact coordinates, function calls, or argument assignments.

```text
## assessment
Brief strategic assessment.

## stage
Current stage name.

## objective
The purpose of this decision.

## guidence
1. One concrete action goal.
```
