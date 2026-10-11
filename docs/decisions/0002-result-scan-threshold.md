# 2. Keep the result-scan threshold at 0.5

Date: 11 October 2026. Status: accepted.

## Context

After a tool call runs, Tollgate asks Jev whether the text it returned contains
instructions addressed to an AI assistant. A probability above
`result_scan.injected` withholds the result from the agent.

The question was measured on deepset's prompt-injections set, using its own split
of 546 train and 116 test texts. On the test texts, 60 of which are injections:

| Model | Threshold | Withheld texts that were injections | Injections withheld |
| --- | --- | --- | --- |
| Jev | 0.50 | 100.0% | 73.3% |
| Jev, threshold fitted on the train split | 0.10 | 98.1% | 88.3% |
| Gemini 3.8 Flash | 0.50 | 98.0% | 80.0% |
| Gemma 4 31B | 0.50 | 100.0% | 86.7% |

Jev's AUROC is 0.987, Gemini's 0.994 and Gemma's 0.933.

## Decision

The example policy keeps `result_scan.injected` at 0.5.

## Why

- The texts are prompts typed to a chatbot. A benign text there is an ordinary
  question. A real tool result is a web page, a file or an email, which often
  contains imperative text that is harmless, such as install steps in a README.
- A withheld result costs the agent a legitimate page until a person reads the
  audit log. The set cannot show how often 0.10 would do that on real results.
- At 0.5 the scan raised no false alarm in the test texts.

## Consequences

- About a quarter of the injections in the test texts get through at 0.5.
- Anyone who prefers the other trade can lower the value in their own policy.
- Measuring the scan on real tool output, benign and injected, is what would
  justify changing the shipped value.

The figures are from `docs/results.md` as generated on 11 October 2026.
