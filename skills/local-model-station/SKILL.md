---
name: local-model-station
description: Submit Gateway Task v1 jobs to Local Model Station and wake this DSH Web session to verify the results after the batch finishes.
---

When asked to submit a task and wake the same session on completion:

1. Read the current session ID from the shell's `DSH_SESSION_ID`. If unavailable, ask for this session's ID before submitting. Never use the most recent session as a substitute.
2. Use the user's 8777 URL or `LMG_URL`. Use the trusted Web address in `DSH_WEB_URL`. If either is missing, ask. Download the standalone waiter from the gateway's `/api/v1/clients/batch_wait.py` before submitting. Use `MEDIA_API_KEY` if configured.
3. Submit the requested Gateway Task v1 payload(s) with stable `Idempotency-Key` values. Save the returned task IDs. For TTS, use `kind: audio`, the requested profile, and `inputs.prompt` for the text to read.
4. Run `python3 batch_wait.py --url "$LMG_URL" TASK_IDS --detach --log LOG --resume-agent dsh-web --dsh-web-url "$DSH_WEB_URL" --session-id "$DSH_SESSION_ID" --receipt RECEIPT`. Use separate log and receipt paths for each batch in a private local state directory. Report the task IDs after the process returns a PID, then finish the current turn.
5. When the results arrive in the next turn, verify requested artifacts. For WAV, check that the artifact is readable and has a RIFF/WAVE header. Report failed tasks and reasons. Do not resubmit on notification.

DSH Web must already allow the caller to use its normal API. Use explicit operator-provided auth if required. Do not extract browser credentials or invoke headless DSH for a Web-owned session.
