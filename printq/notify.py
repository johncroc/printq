"""Operator notifications and commands over Telegram.

printq uses its own bot (TELEGRAM_BOT_TOKEN) and one chat (TELEGRAM_CHAT_ID).
Without a token every message is only logged, and the same commands are
available from the CLI, so the queue works with no Telegram at all.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import config


def log(text: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", file=sys.stderr, flush=True)


def _api(method: str, data: bytes, content_type: str, timeout: float = 40) -> dict:
    url = f"https://api.telegram.org/bot{config.env('TELEGRAM_BOT_TOKEN')}/{method}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send(text: str, photo: Path | str | None = None) -> bool:
    """Send a message (with a photo if given). Returns True if Telegram accepted it."""
    log(f"notify: {text}" + (f" [photo {photo}]" if photo else ""))
    config.DATA.mkdir(parents=True, exist_ok=True)
    with open(config.DATA / "notify.log", "a") as f:  # the record of everything the operator was told
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}" + (f" [photo {photo}]" if photo else "") + "\n")
    token, chat = config.env("TELEGRAM_BOT_TOKEN"), config.env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    for attempt in range(3):
        try:
            if photo and Path(photo).is_file():
                boundary = uuid.uuid4().hex
                parts = b""
                for name, value in (("chat_id", chat), ("caption", text[:1000])):
                    parts += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
                parts += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"bed.jpg\"\r\n"
                          "Content-Type: image/jpeg\r\n\r\n").encode() + Path(photo).read_bytes()
                parts += f"\r\n--{boundary}--\r\n".encode()
                ok = _api("sendPhoto", parts, f"multipart/form-data; boundary={boundary}").get("ok")
            else:
                data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
                ok = _api("sendMessage", data, "application/x-www-form-urlencoded").get("ok")
            if ok:
                return True
        except Exception as exc:  # noqa: BLE001
            log(f"telegram attempt {attempt + 1} failed: {exc}")
        time.sleep(3 * 2 ** attempt)
    return False


def poll(offset: int) -> tuple[int, list[tuple[str, str]]]:
    """Long-poll for commands. Returns (next offset, [(command, argument), ...]) from the configured chat."""
    data = urllib.parse.urlencode({"offset": offset, "timeout": 50, "allowed_updates": '["message"]'}).encode()
    result = _api("getUpdates", data, "application/x-www-form-urlencoded", timeout=65).get("result", [])
    commands = []
    for u in result:
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if str((msg.get("chat") or {}).get("id")) != config.env("TELEGRAM_CHAT_ID") or not text.startswith("/"):
            continue
        head, _, arg = text[1:].partition(" ")
        head = head.split("@", 1)[0].lower()
        if "_" in head and not arg:  # the tappable form: /go_j12 means /go j12
            head, _, arg = head.partition("_")
        commands.append((head, arg.strip()))
    return offset, commands
