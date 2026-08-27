# Contributing to Ghost Hunter 👻

Thanks for considering contributing! This is a solo-built project, still early (v1.0.0), so contributions — code, bug reports, or just battle-testing — are genuinely appreciated.

## Ways to contribute

You don't need to write code to help:
- **Test the untested paths.** The GitHub App authentication flow (Option B in the README) is documented but not yet verified end-to-end. If you try it and it works (or doesn't), please open an issue with your results.
- **Try different LLM models.** `nemotron-free` is confirmed unreliable for this tool's structured JSON parsing. If you test `openai/gpt-4o-mini`, the free Llama tier, or anything else, report back what you find.
- **Report bugs** you hit while setting up or running the tool.
- **Improve the docs** if something in the README was confusing or incomplete when you followed it.

## Reporting a bug

Please include:
- Your Host OS (Windows/macOS/Linux)
- The exact LLM model you were using (e.g. `openai/gpt-4o-mini`)
- The terminal output showing the error, including any `[DEBUG]` lines if you ran with `-d`/`--debug`
- Whether you're using PAT auth or GitHub App auth

This is the same info requested in the README's Contributing section — it's genuinely the fastest way to get a bug fixed, since this tool touches several systems (Docker, an LLM, and the GitHub API) and the failure could be in any of them.

## Submitting code changes

1. Fork the repo and create a branch from `main`.
2. Make your changes. Keep them focused — smaller PRs are easier to review and merge.
3. If you're changing behavior in `app/services/`, please run the existing test suite before opening a PR:
   ```bash
   pytest -v
   ```
   If a test fails because of an intentional behavior change, update the corresponding test and explain why in your PR description.
4. Open a PR with a clear description of what changed and why.

## What's especially welcome right now

- Verifying and hardening the **GitHub App authentication path** (Option B) — this is the single biggest documented gap.
- Testing additional LLM models for structured-output reliability and reporting results.
- Anything that improves the reliability of the Smee.io webhook connection (see the Limitations section in the README).

## Code of Conduct

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you're expected to uphold it.

## Questions

If something's unclear, open an issue and ask — it likely means the docs need improving anyway, so asking helps the project as much as it helps you.
