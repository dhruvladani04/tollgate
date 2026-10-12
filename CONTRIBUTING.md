# Contributing to Tollgate

Thanks for looking. Bug reports, questions and pull requests are all welcome. Tollgate is a
young project with one maintainer, so a short issue before a large change saves us both time.

## What helps most

- **Running it on your own agent and saying what happened.** Calls it held that it should
  not have, and calls it let through that it should not have, are the most useful reports.
  Include the audit row if you can: `uv run python -m tollgate.audit`.
- **Running it somewhere new.** It has been developed on one Windows machine. A report from
  macOS or Linux, working or not, is valuable.
- **Harder test cases.** The evaluation's weak points are listed under "Limits" in the
  [README](README.md). Cases that try to talk the model into a wrong answer are missing
  entirely.

## Set up

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```text
git clone https://github.com/dhruvladani04/tollgate.git
cd tollgate
uv sync
uv run pytest
```

The tests need no API key and no network, and take about a minute and a half. A few of them
start real server processes.

To run the proxy or the demos you need a Jev API key, and [Node.js](https://nodejs.org/) for
the example filesystem server. The README's "Try it in five minutes" has the steps.

## Before you open a pull request

```text
uv run ruff format .
uv run ruff check .
uv run pytest
```

- **Add a test for what you change.** The policy engine and the pre-filter are pure
  functions, so their tests come first.
- **Keep the pull request to one change,** and say what you ran to check it.
- **Describe behaviour, not just code.** If a call is now held that was allowed before, or
  the other way round, say so.

## Rules the code keeps

These come from the design in [docs/spec.md](docs/spec.md). A change that breaks one needs
a reason in the pull request.

- **The model is pinned.** Jev is fixed at version 1.13.0, because thresholds are tuned
  against one version. Never use a moving alias such as `jev-latest`.
- **Code decides what code can decide.** Allow lists, path checks, secret patterns and size
  limits never go to the model.
- **Every threshold lives in the policy file.** No number that changes a decision is written
  into the code.
- **Fail closed.** If the model is slow or fails, a person is asked. Nothing is allowed by
  default.
- **Keys come from the environment only.** Never put a key in a file that git tracks, in a
  test, or in a log. Test data that needs something key-shaped builds it at run time.

## If you change the evaluation

The numbers in the README come from [docs/results.md](docs/results.md), which is generated
from stored model answers:

```text
uv run python -m evals.report
```

- **Do not edit `docs/results.md` by hand.** A test fails if it differs from what the stored
  answers produce.
- **Changing a test case changes its cache key,** so the model has to be asked again, which
  needs a key: `uv run --env-file .env python -m evals.run`.
- **Do not tune on the test split.** Thresholds are fitted on the train split only. If a
  design choice was made after looking at test results, the report must say so.

## If you change the dashboard

The pages are a Next.js app in `dashboard/`. The built files are committed, so that running
the dashboard needs no Node.

```text
cd dashboard
npm install
npm run dev      # live pages on port 3000; start tollgate-dashboard too, for the data
npm run build    # rebuild the files under src/tollgate/dashboard/static
```

Commit the rebuilt `src/tollgate/dashboard/static` with your change. Text from tool calls is
untrusted: render it as text, and never add cross-origin headers to the API, because the
dashboard can approve held calls.

## Reporting a security problem

Please do not open a public issue for something that lets a call past the gate in a way an
attacker could use. Email the maintainer at the address in `pyproject.toml` instead.

## Licence

Tollgate is released under the [MIT licence](LICENSE). By contributing, you agree that your
contribution is released under it too.
