# Changelog

## 0.2.0

- Every divergence now includes a ready-to-paste pytest regression test. The benchmark checks that each
  one passes on base and fails on head (21/21).
- Inputs drop arguments that only repeat their default (`ship(40.0)` instead of `ship(40.0, False)`).
- Report wording is accessible: status in words (Passed / For review / Failed) as well as an icon,
  proper plurals, clearer column names.
- Setup pages (`twinrun create-app`) have a language, a title, light and dark contrast, visible focus,
  a no-JavaScript fallback, and screen-reader status messages.
- Lint (ruff) in CI. Security policy, contributing guide, and issue and PR templates.

## 0.1.0

- First release: Behavior Diff check run and a single updated-in-place PR comment, behavior lock labels,
  sandboxed base-vs-head execution, Hypothesis input generation and shrinking, `create-app` manifest flow.
