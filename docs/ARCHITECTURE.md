# Architecture

How a Telegram message becomes a Claude response and back.

## Overall shape

```
+------------+         long-poll       +------------------+        subprocess       +-------------+
|  Telegram  |  <-------------------- | dispatch_bot.py  | ---------------------> |  claude -p  |
|   Bot API  |  -------------------->  |  (python3 loop)  |  <-------------------- |  (Claude    |
+------------+      getUpdates         +------------------+      stdout response   |   Code CLI) |
                    sendMessage          |        ^                                +-------------+
                                         |        |                                       |
                                         v        |                                       v
                                    +------------------+                          +---------------+
                                    |  transcripts/    |                          | local MCPs,   |
                                    |  media/          |                          | memory, skills|
                                    |  .last_offset    |                          | (your stack)  |
                                    +------------------+                          +---------------+
```

One long-running Python process. No daemon framework, no web server, no inbound ports.

## Per-message flow

For each Telegram update received from `getUpdates`:

1. **Extract** the `message` (or `edited_message`) and pull out: `chat_id`, `user_id`, `chat_type`, `text` (or `caption`), `entities` (or `caption_entities`), `photo[]`, `document`.

2. **Allowlist check.** The message proceeds only if `user_id` is in `allowlisted_user_ids` OR `chat_id` is in `allowlisted_chat_ids`. Otherwise silent drop (no Telegram reply, just a log line).

3. **Magic commands.** Before any Claude call, check for `/restart` and `/diag`. These run inline in the bot — `/restart` calls `os._exit(0)` and the supervisor respawns the process. `/diag` runs three short `claude` subprocess calls and returns the raw output.

4. **Group mention check.** In `group` or `supergroup` chat types, the bot only responds if it was @-mentioned. The mention text is stripped before processing. DMs respond to every message.

5. **Media download.** Photos use the largest available size; documents are pulled by `file_id`. Both go to `media_dir/<chat_id>/<file_id_prefix>__<filename>`. Anything over 20 MB is skipped (Bot API limit). Media notes get appended to the prompt as bracketed `[Image attached: /path/to/file]` lines so Claude can Read them.

6. **Transcript load.** Per-chat transcript at `transcripts/chat_<chat_id>.json`. Capped at the last N turns (default 20). If the file doesn't exist or is unparseable, start fresh.

7. **Prompt build.**
   ```
   <system_prompt.md body below the --- marker>

   --- recent conversation ---
   [who @ ts]: text
   [who @ ts]: text
   ...
   [current sender @ now]: new message text
   --- end ---

   Respond to the last user message. Keep it short for mobile.
   ```

8. **Typing indicator.** A background thread spawned via `TypingPinger` re-issues `sendChatAction` every 4 seconds while Claude runs, so the "typing..." indicator stays continuous (Telegram's expires after ~5s).

9. **Claude call.** `subprocess.run([claude_cmd, "-p", prompt, *claude_args], cwd=claude_working_dir, env=merged_env, timeout=claude_timeout_s)`.
   - `--dangerously-skip-permissions` bypasses interactive MCP approvals (no interactive prompt is possible in `-p` mode).
   - Working dir gives Claude immediate Read access to that folder.
   - `claude_env` lets you set model aliases or thinking-budget vars.

10. **Transcript save.** Append the new turn + Claude's reply. Wrapped in `try/except` so a write failure (TCC on macOS, disk full, permissions, etc.) doesn't block the reply.

11. **Send.** Split response by `max_response_chars` (default 3800) on paragraph / newline / sentence boundaries. Each chunk goes via `sendMessage`. In groups, the first chunk uses `reply_to_message_id` so the reply is threaded to the original message.

12. **Offset update.** Bump `update_id + 1` and persist to `.last_offset` so a restart resumes after the most recently handled update.

## Files in the install dir

| File | Owner | Purpose |
| --- | --- | --- |
| `dispatch_bot.py` | you (copied at install) | The bot |
| `config.json` | you (edited) | Bot token, allowlist, paths, Claude args/env |
| `system_prompt.md` | you (edited) | Personality + comms rules; reloaded per message |
| `.last_offset` | bot | Telegram update_id high-water mark |
| `transcripts/chat_<id>.json` | bot | Per-chat turn history |
| `media/incoming/<chat_id>/...` | bot | Downloaded photos / docs |
| `dispatch_bot.log` | bot | Bot's own log (also written to launchd/systemd) |
| `launchd-stderr.log` / `launchd-stdout.log` | supervisor | Process-level stderr / stdout |

## Why these choices

- **Stdlib only.** No pip dependency means no venv to corrupt, no upgrade-day breakage, no PATH gymnastics for the supervisor.
- **System Python (`/usr/bin/python3`).** Always present on macOS, always present on most Linux distros. No "which Python is launchd using" ambiguity.
- **Subscription auth via `claude -p`.** No `ANTHROPIC_API_KEY`, no per-token billing. The bot rides whatever Claude plan you already pay for.
- **Magic commands run before the Claude call.** `/restart` and `/diag` work even when Claude is broken — the most useful time to have a remote recovery channel.
- **Transcript write wrapped in try/except.** Reply is the product; history is convenience. A storage failure should never block a message going out.
- **Allowlist enforced before everything.** A leaked bot token still can't get a stranger to your Claude — they're not on your allowlist.
