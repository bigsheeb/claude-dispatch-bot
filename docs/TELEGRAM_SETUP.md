# Telegram bot setup

A 5-minute walkthrough from "no bot exists" to "I have a token and my user_id."

## 1. Create the bot

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. BotFather asks for a **display name** — what shows up in the chat title (e.g. `My Dispatch Bot`).
4. BotFather asks for a **username** — must end in `bot` (e.g. `my_dispatch_bot`). This is your `bot_username` for `config.json`.
5. BotFather replies with your bot token. It looks like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz0123456789` (a numeric ID, a colon, then a base64-ish string). This is your `bot_token` for `config.json`.

**Treat the token like a password.** Don't commit it, don't paste it in screenshots, don't share it. Anyone with it can act as your bot (allowlist still protects you from random users sending messages, but the token itself is sensitive).

## 2. Find your user_id

1. Start a chat with [@userinfobot](https://t.me/userinfobot).
2. Send any message.
3. It replies with your `Id: <number>`. That's your `user_id`. Put it in `allowlisted_user_ids`.

## 3. (Optional) Add the bot to a group

1. Open the group.
2. Add `@your_bot_username` as a member.
3. **Disable privacy mode** for the bot if you want it to see all messages (so it can detect @-mentions). Open BotFather → `/mybots` → pick your bot → Bot Settings → Group Privacy → Turn off.
4. Get the group's `chat_id`:
   - Easiest: send a message in the group, then visit `https://api.telegram.org/bot<your_token>/getUpdates` in a browser. Find your message in the JSON, copy the `chat.id` (it'll be a negative integer like `-1001234567890`).
   - Add that ID to `allowlisted_chat_ids`.

In groups, the bot only responds when @-mentioned. DMs from allowlisted users respond to every message.

## 4. Sanity check

After filling in `config.json` and running the bot:

- DM your bot anything. You should see a reply.
- If you don't see a reply but you see no errors in the log either, double-check:
  - `bot_token` is correct (no extra whitespace).
  - Your `user_id` is on `allowlisted_user_ids` — otherwise the bot silently drops your message and only logs `allowlist drop: ...`.

## Customizing the bot's profile

In BotFather:
- `/setdescription` — short bio shown in the contact card.
- `/setabouttext` — longer about text.
- `/setuserpic` — profile picture.
- `/setcommands` — register slash commands so they autocomplete (good ones to add: `restart - Restart the bot`, `diag - Run a diagnostic`).
