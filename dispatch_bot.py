#!/usr/bin/env python3
"""
Claude Dispatch Bot — a Telegram bot that relays messages to Claude via the
Claude Code CLI on your local machine.

Architecture:
- Long-running service. Polls Telegram getUpdates with offset tracking.
- For each message:
    1. Allowlist check (user_id + chat_id). Silent drop on mismatch.
    2. In groups: require @<bot_username> mention. Strip it before processing.
    3. Load per-chat transcript history; append the new turn.
    4. Build a prompt that includes recent history + a system preamble.
    5. Invoke `claude -p "<prompt>"` (Claude Code CLI in non-interactive mode).
       Claude runs in the configured working directory with all your MCPs,
       memory, and skills wired in.
    6. Append response to transcript; save.
    7. Post response back to Telegram (split into multiple messages if long).

Run manually for testing:
    python3 dispatch_bot.py
    python3 dispatch_bot.py --dry-run    # don't call Claude, just log what would happen
    python3 dispatch_bot.py --once       # process one batch of updates and exit

For always-on operation, install under launchd (macOS) or systemd (Linux) —
see install/ for templates.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("DISPATCH_BOT_CONFIG") or os.path.join(SCRIPT_DIR, "config.json")
SYSTEM_PROMPT_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "system_prompt.md")
OFFSET_FILE = os.path.join(os.path.dirname(CONFIG_PATH), ".last_offset")
LOG_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "dispatch_bot.log")
DEFAULT_MEDIA_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "media", "incoming")
DEFAULT_TRANSCRIPT_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "transcripts")
DEFAULT_MEDIA_RETENTION_DAYS = 15
MEDIA_API_FILE_LIMIT = 20 * 1024 * 1024  # Telegram Bot API getFile cap
MEDIA_SWEEP_INTERVAL_S = 3600  # re-sweep at most once per hour
DEFAULT_BOT_DISPLAY_NAME = "the bot"


def load_system_preamble() -> str:
    """
    The system_prompt.md file is editable separately from this code.
    Anything before a '---' marker line is treated as a comment header and stripped;
    everything after is the live system preamble. Reloaded on every message.
    """
    try:
        with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""
    if "\n---\n" in content:
        _, body = content.split("\n---\n", 1)
        return body.strip()
    return content.strip()


# ----- logging -----

def log(line: str) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    msg = f"[{ts}] {line}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print(msg, file=sys.stderr)


# ----- config -----

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# ----- Telegram API -----

def tg_call(token: str, method: str, params: dict | None = None, timeout: int = 60) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    if params is not None:
        data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        log(f"telegram HTTPError {e.code} on {method}: {body}")
        return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:
        log(f"telegram exception on {method}: {type(e).__name__}: {e}")
        return {"ok": False, "description": str(e)}


def get_updates(token: str, offset: int) -> list[dict]:
    resp = tg_call(token, "getUpdates", {"offset": offset, "timeout": 25}, timeout=40)
    if not resp.get("ok"):
        return []
    return resp.get("result") or []


def send_message(token: str, chat_id: int, text: str, reply_to: int | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text}
    if reply_to is not None:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    return tg_call(token, "sendMessage", params)


def send_typing(token: str, chat_id: int) -> None:
    tg_call(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)


class TypingPinger:
    """
    Background thread that re-sends sendChatAction every 4 seconds while
    Claude Code is running, so the user sees a continuous "typing..." indicator
    in Telegram throughout the response generation.
    """
    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = chat_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        send_typing(self.token, self.chat_id)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(4.0):
            try:
                send_typing(self.token, self.chat_id)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


# ----- media handling -----

def telegram_file_url(token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{token}/{file_path}"


def download_telegram_file(
    token: str,
    file_id: str,
    dest_dir: str,
    suggested_name: str | None = None,
) -> str | None:
    """
    Resolve a Telegram file_id to a local absolute path. Returns None on any failure
    (including > 20 MB, which the Bot API can't deliver).
    """
    resp = tg_call(token, "getFile", {"file_id": file_id})
    if not resp.get("ok"):
        log(f"getFile fail {file_id}: {resp.get('description')!r}")
        return None
    info = resp.get("result") or {}
    remote_path = info.get("file_path")
    file_size = info.get("file_size") or 0
    if not remote_path:
        log(f"getFile no file_path for {file_id}")
        return None
    if file_size and file_size > MEDIA_API_FILE_LIMIT:
        log(f"file {file_id} is {file_size} bytes; over 20MB Bot API limit, skipping")
        return None

    base = suggested_name or os.path.basename(remote_path)
    safe = base.replace("/", "_").replace("\\", "_").strip() or "file"
    fname = f"{file_id[:16]}__{safe}"
    os.makedirs(dest_dir, exist_ok=True)
    local_path = os.path.join(dest_dir, fname)

    url = telegram_file_url(token, remote_path)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        with open(local_path, "wb") as f:
            f.write(data)
        log(f"downloaded {file_id} -> {local_path} ({len(data)} bytes)")
        return local_path
    except Exception as e:
        log(f"download fail for {file_id}: {type(e).__name__}: {e}")
        return None


def sweep_media(media_root: str, retention_days: int) -> None:
    """Delete files in media_root older than retention_days; prune empty subdirs."""
    if not os.path.isdir(media_root):
        return
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(media_root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.unlink(full)
                    removed += 1
            except Exception:
                pass
    for dirpath, dirnames, filenames in os.walk(media_root, topdown=False):
        if dirpath == media_root:
            continue
        if not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except Exception:
                pass
    if removed:
        log(f"media sweep: removed {removed} file(s) older than {retention_days}d under {media_root}")


# ----- offset persistence -----

def read_offset() -> int:
    try:
        with open(OFFSET_FILE, "r") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def write_offset(offset: int) -> None:
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except Exception as e:
        log(f"could not write offset: {e}")


# ----- transcripts -----

def transcript_path(transcript_dir: str, chat_id: int) -> str:
    safe = str(chat_id).replace("/", "_")
    return os.path.join(transcript_dir, f"chat_{safe}.json")


def load_transcript(transcript_dir: str, chat_id: int) -> list[dict]:
    path = transcript_path(transcript_dir, chat_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_transcript(transcript_dir: str, chat_id: int, turns: list[dict], cap: int) -> None:
    os.makedirs(transcript_dir, exist_ok=True)
    trimmed = turns[-cap:] if cap and len(turns) > cap else turns
    with open(transcript_path(transcript_dir, chat_id), "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


# ----- message processing -----

def is_bot_mentioned(text: str, entities: list[dict] | None, bot_username: str) -> tuple[bool, str]:
    """
    Returns (mentioned, text_with_mention_stripped).
    Detects @<bot_username> in either explicit "mention" entities or in
    bot_command entities (/command@botname style).
    """
    text = text or ""
    if not text:
        return False, text

    target = f"@{bot_username.lower()}"
    entities = entities or []

    for ent in entities:
        ent_type = ent.get("type")
        offset = ent.get("offset", 0)
        length = ent.get("length", 0)
        chunk = text[offset:offset + length]
        if ent_type == "mention" and chunk.lower() == target:
            stripped = (text[:offset] + text[offset + length:]).strip()
            return True, stripped
        if ent_type == "bot_command" and target in chunk.lower():
            stripped = (text[:offset] + text[offset + length:]).strip()
            return True, stripped

    if target in text.lower():
        idx = text.lower().find(target)
        stripped = (text[:idx] + text[idx + len(target):]).strip()
        return True, stripped

    return False, text


def build_prompt(transcript: list[dict], new_turn: dict) -> str:
    parts = [load_system_preamble(), ""]
    parts.append("--- recent conversation ---")
    for turn in transcript:
        who = turn["who"]
        ts = turn.get("ts", "")
        parts.append(f"[{who} @ {ts}]: {turn['text']}")
    parts.append(f"[{new_turn['who']} @ {new_turn['ts']}]: {new_turn['text']}")
    parts.append("--- end ---")
    parts.append("")
    parts.append("Respond to the last user message. Keep it short for mobile.")
    return "\n".join(parts)


def call_claude_code(cfg: dict, prompt: str, dry_run: bool) -> str:
    if dry_run:
        log(f"DRY-RUN claude call (prompt chars={len(prompt)})")
        return "(dry-run: claude would respond here)"

    claude_cmd = cfg["claude_code_command"]
    working_dir = cfg.get("claude_working_dir") or SCRIPT_DIR

    # claude_args is a list of CLI flags appended after `-p <prompt>`. Defaults
    # to a Sonnet call with --dangerously-skip-permissions (required for -p mode
    # to not block on MCP tool prompts). Override in config.json to change model
    # or pass --allowedTools for tighter permission control.
    claude_args = cfg.get("claude_args") or ["--model", "sonnet", "--dangerously-skip-permissions"]

    # claude_env merges into the subprocess environment. Use this to set model
    # aliases like ANTHROPIC_DEFAULT_OPUS_MODEL or thinking budgets.
    env = os.environ.copy()
    for k, v in (cfg.get("claude_env") or {}).items():
        env[str(k)] = str(v)

    timeout_s = int(cfg.get("claude_timeout_s") or 600)

    try:
        result = subprocess.run(
            [claude_cmd, "-p", prompt, *claude_args],
            cwd=working_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if result.returncode != 0:
            log(f"claude exit code {result.returncode}, stderr: {result.stderr[:500]}, stdout: {result.stdout[:500]}")
            err_parts = []
            stderr_clean = result.stderr.strip()
            stdout_clean = result.stdout.strip()
            if stderr_clean:
                err_parts.append(f"stderr: {stderr_clean[:300]}")
            if stdout_clean:
                err_parts.append(f"stdout: {stdout_clean[:300]}")
            if not err_parts:
                err_parts.append("both stdout and stderr empty (likely auth/credit/model issue)")
            return f"(Claude Code exit {result.returncode}. {' | '.join(err_parts)})"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"claude call timed out after {timeout_s}s")
        return f"(Claude Code timed out after {timeout_s}s — try a smaller request.)"
    except FileNotFoundError:
        log(f"claude binary not found at {claude_cmd!r}")
        return f"(Claude Code CLI not found at {claude_cmd!r} — check your config and PATH.)"
    except Exception as e:
        log(f"claude call exception: {type(e).__name__}: {e}")
        return f"(Bot error: {type(e).__name__}: {e})"


def split_for_telegram(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut == -1:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut == -1:
            cut = remaining.rfind(". ", 0, max_chars)
            if cut != -1:
                cut += 2
        if cut == -1 or cut < max_chars // 2:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def process_update(update: dict, cfg: dict, dry_run: bool) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user_id = sender.get("id")

    raw_text = msg.get("text") or ""
    raw_caption = msg.get("caption") or ""
    text = raw_text or raw_caption
    entities = msg.get("entities") if raw_text else msg.get("caption_entities")

    photos = msg.get("photo") or []
    document = msg.get("document") or {}
    has_media = bool(photos) or bool(document)

    if not chat_id or not user_id:
        return
    if not text and not has_media:
        return

    allowed_users = [u for u in cfg.get("allowlisted_user_ids", []) if u]
    allowed_chats = cfg.get("allowlisted_chat_ids", []) or []
    user_ok = user_id in allowed_users
    chat_ok = chat_id in allowed_chats
    if not (user_ok or chat_ok):
        log(f"allowlist drop: user={user_id} chat={chat_id} type={chat_type}")
        return

    bot_display = cfg.get("bot_display_name") or DEFAULT_BOT_DISPLAY_NAME

    # Magic commands — recognized BEFORE the Claude Code call so they work
    # even when Claude is broken. Useful for mobile-only recovery.
    text_stripped = (text or "").strip().lower()
    if text_stripped in ("/restart", "/reboot"):
        log(f"magic /restart from chat={chat_id} user={user_id}")
        if not dry_run:
            send_message(cfg["bot_token"], chat_id, f"Restarting {bot_display} — back in a few seconds.")
        sys.stdout.flush()
        sys.stderr.flush()
        # KeepAlive in launchd / Restart=always in systemd respawns the process,
        # which re-reads dispatch_bot.py from disk (picks up any patches).
        os._exit(0)
    if text_stripped in ("/diag", "/diagnose"):
        log(f"magic /diag from chat={chat_id} user={user_id}")
        if not dry_run:
            diag_parts = []
            claude_args = cfg.get("claude_args") or ["--model", "sonnet", "--dangerously-skip-permissions"]
            try:
                ver = subprocess.run(
                    [cfg["claude_code_command"], "--version"],
                    capture_output=True, text=True, timeout=20,
                )
                diag_parts.append(f"claude --version (exit {ver.returncode}): {(ver.stdout + ver.stderr).strip()[:200]}")
            except Exception as e:
                diag_parts.append(f"claude --version EXCEPTION: {type(e).__name__}: {e}")
            try:
                hello = subprocess.run(
                    [cfg["claude_code_command"], "-p", "Reply with the single word: pong", *claude_args],
                    capture_output=True, text=True, timeout=60,
                )
                diag_parts.append(
                    f"claude -p with configured args (exit {hello.returncode}) "
                    f"stdout: {hello.stdout.strip()[:200]!r} "
                    f"stderr: {hello.stderr.strip()[:200]!r}"
                )
            except subprocess.TimeoutExpired:
                diag_parts.append("claude -p TIMED OUT after 60s")
            except Exception as e:
                diag_parts.append(f"claude -p EXCEPTION: {type(e).__name__}: {e}")
            try:
                hello_simple = subprocess.run(
                    [cfg["claude_code_command"], "-p", "Reply with the single word: pong"],
                    capture_output=True, text=True, timeout=60,
                )
                diag_parts.append(
                    f"claude -p (no flags, exit {hello_simple.returncode}) "
                    f"stdout: {hello_simple.stdout.strip()[:200]!r} "
                    f"stderr: {hello_simple.stderr.strip()[:200]!r}"
                )
            except subprocess.TimeoutExpired:
                diag_parts.append("claude -p (no flags) TIMED OUT after 60s")
            except Exception as e:
                diag_parts.append(f"claude -p (no flags) EXCEPTION: {type(e).__name__}: {e}")
            send_message(cfg["bot_token"], chat_id, "Diag:\n\n" + "\n\n".join(diag_parts))
        return

    if chat_type in ("group", "supergroup"):
        mentioned, stripped = is_bot_mentioned(text, entities, cfg["bot_username"])
        if not mentioned:
            return
        text = stripped if stripped else text
    elif chat_type != "private":
        return

    media_dir_root = cfg.get("media_dir") or DEFAULT_MEDIA_DIR
    chat_media_dir = os.path.join(media_dir_root, str(chat_id))
    media_notes: list[str] = []
    if not dry_run:
        if photos:
            largest = photos[-1]
            local = download_telegram_file(cfg["bot_token"], largest["file_id"], chat_media_dir)
            if local:
                media_notes.append(f"[Image attached: {local}]")
            else:
                media_notes.append("[Image attached but download failed.]")
        if document:
            local = download_telegram_file(
                cfg["bot_token"],
                document["file_id"],
                chat_media_dir,
                suggested_name=document.get("file_name"),
            )
            if local:
                mime = document.get("mime_type") or "unknown"
                media_notes.append(f"[Document attached ({mime}): {local}]")
            else:
                media_notes.append(f"[Document attached ({document.get('file_name', '?')}) but download failed.]")

    parts = []
    if text:
        parts.append(text)
    if media_notes:
        parts.extend(media_notes)
    effective_text = "\n".join(parts) if parts else "(empty message)"

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    who_label = sender.get("first_name") or f"user_{user_id}"
    new_turn = {"who": who_label, "ts": ts, "text": effective_text, "user_id": user_id}

    transcript_dir = cfg.get("transcript_dir") or DEFAULT_TRANSCRIPT_DIR
    transcript = load_transcript(transcript_dir, chat_id)
    prompt = build_prompt(transcript, new_turn)

    log(f"processing: chat={chat_id} user={user_id} who={who_label} chars={len(effective_text)} type={chat_type} media={len(media_notes)}")

    pinger = TypingPinger(cfg["bot_token"], chat_id) if not dry_run else None
    if pinger:
        pinger.start()
    try:
        response = call_claude_code(cfg, prompt, dry_run)
    finally:
        if pinger:
            pinger.stop()

    # Wrap save in try/except so a write failure (TCC permission errors on
    # macOS, disk-full, etc.) doesn't prevent the response from being sent.
    # Transcript history is convenience; the reply is the product.
    transcript.append(new_turn)
    transcript.append({"who": bot_display, "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "text": response})
    try:
        save_transcript(transcript_dir, chat_id, transcript, cfg.get("transcript_turn_cap", 20))
    except Exception as e:
        log(f"transcript save failed (continuing to send reply): {type(e).__name__}: {e}")

    max_chars = cfg.get("max_response_chars", 3800)
    chunks = split_for_telegram(response, max_chars)
    reply_to = msg.get("message_id") if chat_type in ("group", "supergroup") else None
    for idx, chunk in enumerate(chunks):
        if not dry_run:
            resp = send_message(cfg["bot_token"], chat_id, chunk, reply_to=reply_to if idx == 0 else None)
            if not resp.get("ok"):
                log(f"send fail chat={chat_id} chunk={idx+1}/{len(chunks)}: {resp.get('description')!r}")
            else:
                log(f"sent chat={chat_id} chunk={idx+1}/{len(chunks)} chars={len(chunk)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Dispatch Bot — Telegram → Claude Code relay.")
    parser.add_argument("--dry-run", action="store_true", help="Don't invoke Claude or send to Telegram; just log.")
    parser.add_argument("--once", action="store_true", help="Process one batch of updates and exit.")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except Exception as e:
        log(f"FATAL: config load failed from {CONFIG_PATH}: {e}")
        return 2

    if cfg.get("bot_token", "").startswith("<") or not cfg.get("bot_token"):
        log("FATAL: bot_token is missing or still a placeholder. Update config.json.")
        return 2
    if cfg.get("bot_username", "").startswith("<") or not cfg.get("bot_username"):
        log("FATAL: bot_username is missing or still a placeholder. Update config.json.")
        return 2

    offset = read_offset()
    media_root = cfg.get("media_dir") or DEFAULT_MEDIA_DIR
    retention_days = int(cfg.get("media_retention_days") or DEFAULT_MEDIA_RETENTION_DAYS)
    log(f"start: offset={offset} bot=@{cfg['bot_username']} dry_run={args.dry_run} once={args.once} media_root={media_root} retention={retention_days}d")

    sweep_media(media_root, retention_days)
    last_sweep = time.monotonic()

    while True:
        if time.monotonic() - last_sweep > MEDIA_SWEEP_INTERVAL_S:
            sweep_media(media_root, retention_days)
            last_sweep = time.monotonic()

        try:
            updates = get_updates(cfg["bot_token"], offset)
        except Exception as e:
            log(f"getUpdates loop error: {type(e).__name__}: {e}; sleeping 10s")
            time.sleep(10)
            if args.once:
                break
            continue

        for u in updates:
            try:
                process_update(u, cfg, dry_run=args.dry_run)
            except Exception as e:
                log(f"process_update error: {type(e).__name__}: {e}")
            new_offset = u.get("update_id", offset) + 1
            if new_offset > offset:
                offset = new_offset
                write_offset(offset)

        if args.once:
            break

        time.sleep(0.5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
