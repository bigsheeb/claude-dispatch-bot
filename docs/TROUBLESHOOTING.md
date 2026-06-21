# Troubleshooting

Common failure modes and what to do about them.

## Bot doesn't reply at all

Start with:
```bash
tail -50 ~/.dispatch-bot/launchd-stderr.log     # macOS
journalctl --user -u dispatch-bot -n 50         # Linux
```

The log tells you whether messages are being received, whether Claude is being invoked, and whether replies are being sent.

Then try `/diag` from Telegram. The bot handles `/diag` *before* the Claude call, so even if Claude is fully broken, you'll still get a diagnostic reply that pinpoints the failure.

### "allowlist drop: user=... chat=..." in the log

The user_id sending the message isn't on `allowlisted_user_ids`, and the chat_id isn't on `allowlisted_chat_ids`. Add the right ID and `/restart` (or reload the service).

### No "processing: chat=..." line ever appears

The bot isn't seeing your messages. Either:
- Wrong `bot_token` in `config.json` — Telegram is rejecting `getUpdates`.
- `.last_offset` is way ahead of your latest update_id (rare; can happen if you swapped tokens between bots). Delete `.last_offset` and restart.

## macOS: `Operation not permitted` reading the script

```
/Library/Developer/CommandLineTools/usr/bin/python3: can't open file '...': [Errno 1] Operation not permitted
```

`/usr/bin/python3` (the Command Line Tools Python) doesn't have **Full Disk Access**, and the script (or one of its targets if it's a symlink) lives in a TCC-protected folder like `~/Documents`, `~/Desktop`, or `~/Downloads`.

**Fix:** keep the install dir in a non-TCC location. The default `~/.dispatch-bot` is safe. Same applies to `transcript_dir`, `media_dir`, `claude_working_dir` if you customized them.

If you've already symlinked the script from an exposed location into a TCC-protected folder, replace the symlink with a real file:
```bash
rm ~/.dispatch-bot/dispatch_bot.py
cp /path/to/original/dispatch_bot.py ~/.dispatch-bot/dispatch_bot.py
```

The fragile alternative is granting `/usr/bin/python3` Full Disk Access in System Settings → Privacy & Security. This works but breaks on the next Command Line Tools update.

## `claude -p` returns 401 / Unauthorized

Subscription auth has expired. Refresh it:
```bash
claude                 # opens interactive REPL
/login                 # inside the REPL
# follow the browser flow
/exit
```

Then run `claude -p "hi"` to confirm. Restart the bot.

## `claude -p` works in your shell but fails when launchd / systemd runs it

The supervisor environment doesn't have `claude` on PATH. Two fixes:

**Option A — set absolute path in `config.json`.** Find where the binary lives:
```bash
which claude
# /Users/yourname/.local/bin/claude
```
Then in `config.json`:
```json
"claude_code_command": "/Users/yourname/.local/bin/claude"
```

**Option B — ensure the supervisor's PATH includes it.** The macOS plist and Linux service template already set `PATH` to a reasonable default that covers `~/.local/bin`, `/usr/local/bin`, and `/opt/homebrew/bin`. If your `claude` binary is somewhere else, edit the supervisor file accordingly.

## Bot is stuck in a restart loop

```bash
launchctl list | grep dispatch    # macOS — second column is last exit code
systemctl --user status dispatch-bot   # Linux
```

If the bot crashes within seconds of launching and the supervisor keeps respawning:
- Check the stderr log for the actual error (config parse error, missing file, etc.).
- `ThrottleInterval=30` in the plist limits respawn rate to once per 30 seconds — but the loop will still happen until you fix the underlying issue.
- Fastest path: stop the supervisor, run the bot in the foreground (`python3 ~/.dispatch-bot/dispatch_bot.py`), see the error directly, fix it, restart.

```bash
# macOS
launchctl bootout gui/$(id -u)/com.example.dispatch-bot
python3 ~/.dispatch-bot/dispatch_bot.py    # foreground; Ctrl-C to stop
# After fixing config, re-run install.sh

# Linux
systemctl --user stop dispatch-bot
python3 ~/.dispatch-bot/dispatch_bot.py    # foreground; Ctrl-C to stop
systemctl --user start dispatch-bot
```

## Telegram replies are slow / no typing indicator

The `TypingPinger` thread refreshes the "typing..." indicator every 4 seconds. If you see no indicator at all:
- Network failures sending `sendChatAction` are logged but don't block the main flow. Look for `telegram exception on sendChatAction` in the log — usually transient SSL timeouts.
- If the call to Claude itself is taking minutes, that's expected behavior with thinking-heavy models (Opus + max effort). Either accept the latency or pick a faster model in `claude_args`.

## Group bot doesn't respond when mentioned

- Confirm the bot is a member of the group.
- Confirm group privacy is **off** in BotFather (otherwise it only sees `/commands`, not normal text). `/mybots` → pick your bot → Bot Settings → Group Privacy → Turn off.
- Confirm the group's `chat_id` is on `allowlisted_chat_ids`.
- Confirm you're @-mentioning the correct username (case-insensitive but the rest must match).

## Transcript file corruption

If the bot logs `transcript save failed`, the reply still goes out (that's by design) but history won't update. Common causes:
- Disk full.
- TCC permission on the transcript dir (see macOS section above).
- Manual JSON edit that left invalid syntax — delete the file and start a fresh history.

To wipe just one chat's history:
```bash
rm ~/.dispatch-bot/transcripts/chat_<chat_id>.json
```
