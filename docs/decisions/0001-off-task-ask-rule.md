# 1. Hold a call nobody asked for at a lower risk level

Date: 10 October 2026. Status: accepted.

## Context

The build plan's band table uses `on_task` for one thing: blocking a high-risk call that
is off task. Below that level, an edit the user asked for and the same edit made
unprompted were treated alike.

The first run of the evaluation showed the cost. On the golden test split the gate held
28.7% of benign calls, and 26 of the 27 it held were easy-to-undo changes the user had
asked for. Tuning thresholds could not change that, because no threshold separated the
two kinds of edit.

## Decision

The policy has a second ask level, `risk_ask_off_task`, which must not be above
`risk_ask`. A call whose risk score is between the two levels runs only if the model is
confident that it serves the user's request.

- If the model is confident the call is off task, the call is held (Ask).
- If the client sent no user request, the call is held (Ask). Nothing shows that it
  serves the user.
- If the model's `on_task` answer is uncertain, the call escalates, like any other
  uncertain answer that could change the band.

Setting `risk_ask_off_task` equal to `risk_ask` switches the rule off.

## Consequences

- With both ask levels tuned on the train split, the gate held 7.4% of benign test calls
  at 93.3% recall. Tuned the same way with the rule off, it held 30.9% at the same recall.
- At the starting thresholds the rule does not reduce holds. It raises recall from 94.1%
  to 97.0%. The reduction comes from raising `risk_ask`, which the rule makes safe.
- A client that sends no user request gets no reduction: the lower level applies to all
  of its calls.
- The policy file has one more number, and the engine differs from the plan's band table.
- The rule was chosen after reading test-split errors, so the test split is no longer
  untouched by design choices. `docs/results.md` says so.

The figures are from `docs/results.md` as generated on 10 October 2026.
