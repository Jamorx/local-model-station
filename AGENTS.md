# Repository instructions

Initial platform support is macOS only. Keep the gateway independent of video-production-system; do not import its modules or use its project database.

Use isolated temporary task directories for tests. Never send smoke generation to production unless explicitly within the user's authorized validation scope and resource availability has been checked. Do not cancel or restart around active user inference. The migration script must retain its busy-queue and destination-overwrite guards.

Run `python -m unittest discover -s tests -v` and `node --check model_gateway/studio.js` for relevant changes. Heavy model downloads and real inference are not unit tests. Record mocked, real subprocess, and actual model validation separately.

Keep engine options explicit. Unsupported features must fail clearly rather than being silently dropped. Script engines are trusted operator code; do not interpolate prompts into shell commands. Preserve idempotency and frozen execution configuration during refactors.

Do not commit task data, generated media, secrets or model binaries. Do not assign a license to inherited code without provenance review. Update docs when protocol or operational behavior changes.
