# claude-dispatch-bot

A personal AI assistant on Telegram, backed by **Claude Code** running on your own machine. Text it the way you'd text a capable colleague: it answers questions, runs research, edits your files, and drives your MCPs (Notion, Linear, Slack, Figma, or whatever you've connected), with your memory, skills, and local context loaded the whole time. The same Claude you use at your desk, now reachable from your phone.

**It runs on your subscription, not the API.** The bot invokes `claude -p` headless, which uses your Claude Pro / Team subscription auth. No `ANTHROPIC_API_KEY`, no per-token billing.

## What it feels like

The same context it has at your desk, now in your pocket: your files, your MCPs, your memory, your skills. Stuff you might text it:

- *"Research <topic> and send me three solid sources plus your take."*
- *"Summarize what's new in the #acme Slack channel and flag anything that needs a reply."*
- *"What's blocking the release? Check the open Linear issues."*
- *"Add the lead I just met to the Notion CRM."*
- *"Open the Q3 deck in my project folder and tighten the intro copy."*

It answers like someone who already has the context, not a search box you have to spoon-feed. Terse by default, opinionated, and willing to push back before doing anything destructive or built on a wrong premise. Quick question, quick answer. Real work, it either does it or tells you it's a desktop-sized job. The whole personality lives in [`system_prompt.example.md`](system_prompt.example.md); edit that file and the assistant changes on its next message.

## What you get

- DM the bot, get answers from Claude — with full access to your local MCPs, files, and skills.
- Group chat support (responds only when @-mentioned).
- Photo / document attachments — Claude sees them via local Read.
- Allowlist of user_ids and chat_ids; everyone else is silently dropped.
- Magic commands: `/restart` (respawn the bot from your phone) and `/diag` (run a Claude smoke test and return the result).
- Transcript history per chat (kept short to keep prompts tight).
- launchd (macOS) and systemd (Linux) install scripts for always-on operation.
- Zero pip dependencies. Stdlib-only. System Python 3.9+ is enough.

## Requirements

- Python 3.9+ (system Python is fine; no venv needed).
- [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI installed and authenticated via `claude /login` (subscription auth).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) — see [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md).

## Quickstart

```bash
# 1. Clone
git clone https://github.com/bigsheeb/claude-dispatch-bot.git ~/.dispatch-bot
cd ~/.dispatch-bot

# 2. Copy templates
cp config.example.json config.json
cp system_prompt.example.md system_prompt.md

# 3. Edit config.json — paste your bot token, username, and your Telegram user_id
$EDITOR config.json

# 4. Sanity-check Claude auth
claude -p "Reply with the single word pong"
# Should print: pong

# 5. Smoke-test the bot in the foreground
python3 dispatch_bot.py --dry-run
# Then send your bot a Telegram message. Check the log output. Ctrl-C to stop.

# 6. Install as a background service
#    macOS:
bash install/macos/install.sh
#    Linux:
bash install/linux/install.sh

# 7. Message your bot from Telegram. You're live.
```

## How it works

```
[ Telegram ]
     │  getUpdates long-poll
     ▼
[ dispatch_bot.py ]  ── allowlist check ──> drop if not authorized
     │
     │  build prompt = system_prompt.md + recent transcript + new turn
     ▼
[ claude -p "<prompt>" ]   ◄── runs in your claude_working_dir with full MCP access
     │
     │  stdout = response text
     ▼
[ Telegram sendMessage ]   ◄── split into ≤4096-char chunks if needed
```

Full walkthrough: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Configuration

See [`config.example.json`](config.example.json) — every field has an inline `_comment` explaining what it does.

Key choices you'll make:

- **`bot_token` / `bot_username`** — from BotFather.
- **`allowlisted_user_ids`** — your numeric Telegram user_id (get it from [@userinfobot](https://t.me/userinfobot)).
- **`allowlisted_chat_ids`** — group chat IDs (also negative integers).
- **`claude_working_dir`** — the folder Claude treats as its working dir. Setting this to a project folder gives Claude immediate access to those files.
- **`claude_args` / `claude_env`** — tune the model and runtime. Default is Sonnet; switch to Opus or enable 1M context here.

## Security model

This bot puts a phone-shaped interface in front of a headless Claude Code instance with `--dangerously-skip-permissions`. That means:

- **Anyone on the allowlist can run anything Claude can run on your machine.** Tool calls, file edits, MCP actions — all without interactive approval.
- **Keep your allowlist short.** Just you and people you fully trust.
- **Keep your `bot_token` secret.** A leaked token = anyone who finds it can talk to your bot. The allowlist still protects you from random users on Telegram, but the token itself shouldn't go on GitHub.

If you want tighter scoping, replace `--dangerously-skip-permissions` in `config.json` with `--allowedTools` listing specific MCP tool names. Less convenient, more locked-down.

## Magic commands

- **`/restart`** — bot replies "Restarting..." then exits cleanly. The supervisor (launchd / systemd) respawns it. Lets you pick up code patches without SSHing into your laptop.
- **`/diag`** — runs `claude --version` + two `claude -p` smoke tests and returns the full exit codes / stdout / stderr. Useful when normal messages stop coming back and you need to know whether the issue is Claude auth or the bot itself.

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). Common ones:

- **macOS: `Operation not permitted` reading the script** — `/usr/bin/python3` doesn't have Full Disk Access. Keep the install path out of `~/Documents`, `~/Desktop`, and other TCC-protected folders. The default `~/.dispatch-bot` is safe.
- **`claude -p` returns 401** — subscription auth expired. Run `claude` interactively, then `/login` in the REPL.
- **Bot doesn't reply** — check the log (`tail -f ~/.dispatch-bot/dispatch_bot.log`). Then `/diag` from Telegram.

## License

MIT — see [LICENSE](LICENSE).
