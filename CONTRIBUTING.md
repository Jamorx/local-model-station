# Contributing

The initial supported platform is macOS (Apple Silicon inference). Project-owned code is distributed under Apache-2.0. Keep upstream notices when contributing third-party code.

Use Python 3.12 and an editable source checkout. Install `requirements-test.txt`, run `python -m unittest discover -s tests -v`, and `node --check model_gateway/studio.js`. CI runs on macOS. Never run test submissions against production jobs.

New engine contributions must document supported inputs, model identity, memory estimate, dependency paths, output contract and cancellation behavior. Add isolated tests for success, failure, cancellation and immutable task configuration. Real-model acceptance results must identify engine and model versions; mock tests are not generation quality evidence.

Keep API and inference environments separate. Do not commit weights, generated media, tokens, private prompts, user task stores or machine-specific configs. Put personal configuration in `config/*.local.json`.

PR descriptions should state the user-visible behavior, validation, compatibility changes and unresolved limits. Update API.md, ADAPTERS.md and CHANGELOG.md for protocol changes. No claim of full provider compatibility without contract tests and real client verification.
