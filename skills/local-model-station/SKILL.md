---
name: local-model-station
description: Submit Gateway Task v1 jobs and register a server-side completion callback to the current DSH Web session.
---

When asked to submit tasks and wake this session on completion:

1. Read the current session ID from `DSH_SESSION_ID`. If unavailable, ask for this session's ID before submitting. Never guess the session.
2. Use the requested 8777 URL or `LMG_URL`. Discover the API in `/api/help.md`. When 8777 is on the same host, use its loopback URL for callback registration. A remote 8777 requires the administrator-provided `X-DSH-Callback-Key` header. Use `MEDIA_API_KEY` if configured.
3. Submit the requested Gateway Task v1 jobs through `POST /api/v1/tasks` with stable `Idempotency-Key` values. TTS uses `kind: audio` and `inputs.prompt`.
4. Call `POST /api/v1/callbacks` with `target: dsh-web`, the returned `task_ids` array, and `session_id`. Report the task IDs after the server accepts registration, then finish the current turn. If registration fails, report the registered tasks and failure without submitting duplicates.
5. On the callback turn, verify the results as requested. For WAV, check the RIFF/WAVE header. On failure, report the relevant reason.
