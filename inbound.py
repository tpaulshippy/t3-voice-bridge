#!/usr/bin/env python3
"""Telegram voice-message -> T3 thread inbound leg.

Long-polls Telegram getUpdates; for voice/audio notes from the allowlisted
chat: downloads the .ogg, converts to 16k mono wav, transcribes with
faster-whisper, and injects the text into a single T3 thread via the local
server WebSocket RPC (orchestration.dispatchCommand / message.dispatch).

Offset advances ONLY after successful inject. On inject failure an error
note is sent to Telegram and the offset is held for retry.
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent

log = logging.getLogger("inbound")

_STT_MODEL = None


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    defaults = {
        "inbound_enabled": False,
        "inbound_thread_id": None,
        "stt_model": "tiny",
        "stt_language": "en",
        "poll_interval": 5,
        "state_file": "./state.json",
        "temp_dir": "./tmp",
        "db_path": "/home/ubuntu/.t3/userdata/statev2.sqlite",
        "max_retries": 3,
    }
    defaults.update(cfg)
    for k in ("state_file", "temp_dir"):
        p = Path(defaults[k])
        if not p.is_absolute():
            defaults[k] = str(HERE / p)
    return defaults


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def tg_call(token, method, payload=None, timeout=45, files=None):
    import requests

    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        r = requests.post(url, data=payload or {}, files=files, timeout=timeout)
    elif payload is not None or method in ("sendMessage",):
        r = requests.post(url, json=payload or {}, timeout=timeout)
    else:
        r = requests.get(url, params=payload or {}, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def send_text_note(cfg, text):
    code, body = tg_call(cfg["telegram_bot_token"], "sendMessage",
                         {"chat_id": cfg["telegram_chat_id"], "text": text[:4000]},
                         timeout=30)
    ok = code == 200 and body.get("ok")
    if not ok:
        log.warning("sendMessage failed: http=%s body=%s", code, str(body)[:200])
    return ok


def get_updates(token, offset, timeout=30):
    import requests

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    r = requests.get(url, params={"offset": offset, "timeout": timeout,
                                  "allowed_updates": ["message"]},
                     timeout=timeout + 15)
    body = r.json()
    if r.status_code != 200 or not body.get("ok"):
        raise RuntimeError(f"getUpdates failed: http={r.status_code} {str(body)[:200]}")
    return body.get("result", [])


def download_voice(cfg, file_id, tmpdir):
    code, body = tg_call(cfg["telegram_bot_token"], "getFile",
                         {"file_id": file_id}, timeout=30)
    if code != 200 or not body.get("ok"):
        raise RuntimeError(f"getFile failed: http={code} {str(body)[:200]}")
    file_path = body["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{cfg['telegram_bot_token']}/{file_path}"
    ext = os.path.splitext(file_path)[1] or ".ogg"
    dest = os.path.join(tmpdir, f"voice{ext}")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


def to_wav_16k(src, dst):
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src,
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-1000:]}")
    return dst


def transcribe(wav_path, model_name, language):
    global _STT_MODEL
    from faster_whisper import WhisperModel
    import soundfile as sf
    import numpy as np

    key = (model_name, language)
    if _STT_MODEL is None or _STT_MODEL[0] != key:
        _STT_MODEL = (key, WhisperModel(model_name, device="cpu", compute_type="int8"))
    model = _STT_MODEL[1]
    # Pass float32 PCM directly: avoids faster-whisper's av-based decoder
    # (installed av build lacks the metadata_errors kwarg faster-whisper uses).
    audio, sr = sf.read(wav_path, dtype="float32", always_2d=False)
    if getattr(audio, "ndim", 1) > 1:
        audio = np.mean(audio, axis=1)
    assert sr == 16000, f"expected 16kHz wav, got {sr}"
    segments, _info = model.transcribe(np.ascontiguousarray(audio, dtype=np.float32),
                                       language=language or None,
                                       beam_size=5, vad_filter=True)
    return "".join(s.text for s in segments).strip()


def thread_title(cfg, thread_id):
    """Best-effort title lookup (WAL-safe copy first); falls back to short id."""
    try:
        with tempfile.TemporaryDirectory(dir=cfg["temp_dir"]) as tmpdir:
            for suffix in ("", "-wal", "-shm"):
                src = cfg.get("db_path", "") + suffix
                if os.path.exists(src):
                    shutil.copy(src, os.path.join(tmpdir, "s.sqlite" + suffix))
            con = sqlite3.connect(f"file:{tmpdir}/s.sqlite?mode=ro", uri=True, timeout=10)
            try:
                for tbl in ("orchestration_v2_projection_threads", "projection_threads"):
                    try:
                        row = con.execute(
                            f"SELECT title FROM {tbl} WHERE thread_id = ?",
                            (thread_id,)).fetchone()
                        if row and row[0]:
                            return row[0]
                    except sqlite3.OperationalError:
                        continue
            finally:
                con.close()
    except Exception as e:
        log.warning("thread title lookup failed: %r", e)
    return thread_id[:8]


def mint_bearer():
    import shutil

    t3_bin = shutil.which("t3") or "/home/ubuntu/.npm-global/bin/t3"
    r = subprocess.run(
        [t3_bin, "auth", "session", "issue",
         "--scope", "orchestration:read", "--scope", "orchestration:operate",
         "--label", "telegram-inbound", "--ttl", "1h", "--token-only"],
        capture_output=True, text=True, timeout=30,
    )
    tok = r.stdout.strip()
    if r.returncode != 0 or not tok:
        raise RuntimeError(f"t3 auth mint failed: {r.stderr[-300:]}")
    return tok


def fetch_ticket(bearer):
    req = urllib.request.Request(
        "http://127.0.0.1:3773/api/auth/websocket-ticket/rpc", data=b"{}",
        headers={"Authorization": f"Bearer {bearer}",
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["ticket"]


async def _ws_dispatch(ticket, payload):
    import websockets

    url = (f"ws://127.0.0.1:3773/ws?wsTicket={ticket}"
           "&orchestrationProtocol=2&clientCapabilities=settingsSecretReferences")
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps([{"_tag": "Request", "id": 0,
                                   "tag": "orchestration.dispatchCommand",
                                   "payload": payload, "headers": []}]))
        async with asyncio.timeout(30):
            async for msg in ws:
                data = json.loads(msg)
                if data.get("_tag") == "Exit" and data.get("requestId") == 0:
                    return data["exit"]
                # ignore streaming frames for other requestIds


def t3_inject(thread_id, text):
    """Inject user text into a T3 thread. Returns server sequence number."""
    payload = {
        "type": "message.dispatch",
        "createdBy": "user",
        "creationSource": "server",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "messageId": str(uuid.uuid4()),
        "text": text,
        "attachments": [],
        "deliveryIntent": "auto",
        "dispatchMode": {"type": "start_immediately"},
    }
    bearer = mint_bearer()
    ticket = fetch_ticket(bearer)
    exit_frame = asyncio.run(_ws_dispatch(ticket, payload))
    if exit_frame.get("_tag") == "Success":
        return exit_frame["value"].get("sequence")
    raise RuntimeError(f"dispatch failed: {str(exit_frame)[:500]}")


def resolve_target_thread(cfg, msg):
    """Reply-aware routing for an inbound Telegram message.

    Precedence: reply_to_message matching a mapped outbound voice note ->
    thread of the most recent outbound voice note -> configured
    inbound_thread_id override (may be empty/null, in which case routing
    is purely reply-aware). Re-reads state from disk so the outbound
    daemon's map is always fresh.
    Returns (thread_id, how) where how is 'reply', 'recent', 'fallback',
    or (None, 'none') when no target can be determined.
    """
    try:
        state = load_state(cfg["state_file"])
    except Exception:
        state = {}
    cmap = state.get("outbound_msg_thread") or {}
    reply = msg.get("reply_to_message") or {}
    rid = reply.get("message_id")
    if rid is not None and str(rid) in cmap:
        return cmap[str(rid)], "reply"
    recent = state.get("outbound_last_thread")
    if recent:
        return recent, "recent"
    override = cfg.get("inbound_thread_id")
    if override:
        return override, "fallback"
    return None, "none"


def process_update(cfg, update, tmpdir, dry_run=False):
    """Returns True if offset may advance past this update."""
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id != cfg["telegram_chat_id"]:
        return True  # not ours; skip
    voice = msg.get("voice") or msg.get("audio")
    if not voice:
        return True  # text/sticker/etc; nothing to inject
    file_id = voice.get("file_id")
    if not file_id:
        log.warning("voice message without file_id, skipping")
        return True
    try:
        src = download_voice(cfg, file_id, tmpdir)
        wav = os.path.join(tmpdir, "voice16k.wav")
        to_wav_16k(src, wav)
        transcript = transcribe(wav, cfg.get("stt_model", "tiny"),
                                cfg.get("stt_language", "en"))
    except Exception as e:
        log.exception("voice processing failed")
        send_text_note(cfg, f"⚠️ couldn't transcribe that voice note ({e}). "
                            "Holding it — I'll retry automatically.")
        return False  # hold offset
    if not transcript:
        log.info("empty transcript, nothing to inject")
        if dry_run:
            print("DRY empty transcript, nothing to inject")
            return True
        send_text_note(cfg, "🎙 I couldn't hear anything in that voice note — "
                            "nothing was sent to T3.")
        return True
    text = f"🎙 {transcript}"
    target_thread, route_how = resolve_target_thread(cfg, msg)
    if dry_run:
        print(f"DRY transcript: {transcript!r}")
        print(f"DRY route={route_how} target={target_thread} chars={len(text)}")
        return True
    if target_thread is None:
        log.info("no target thread known (route=none), skipping inject")
        send_text_note(cfg, "🎙 I heard you, but I don't know which T3 thread "
                            "this belongs to yet — reply to one of my voice "
                            "notes, or set inbound_thread_id.")
        return True  # advance: retrying wouldn't find a target either
    try:
        seq = t3_inject(target_thread, text)
    except Exception as e:
        log.exception("T3 inject failed")
        send_text_note(cfg, "⚠️ couldn't deliver that voice note to T3 "
                            f"({e}). Holding it — I'll retry automatically.")
        return False  # hold offset
    title = thread_title(cfg, target_thread)
    send_text_note(cfg, f"🎙 {transcript}\n\n→ delivered to “{title}” (seq {seq}).")
    log.info("injected voice note into %s (seq %s, %d chars, route=%s)",
             target_thread, seq, len(text), route_how)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(str(HERE / "inbound.log"),
                                      maxBytes=1_000_000, backupCount=3)],
    )

    cfg = load_config(args.config)
    if not cfg.get("inbound_enabled") and not args.dry_run:
        print("inbound_enabled is false; set it true in config.json to run.")
        return 0
    if not cfg.get("inbound_thread_id"):
        log.info("no inbound_thread_id override; using purely reply-aware routing")
    if not cfg.get("telegram_bot_token") or cfg["telegram_bot_token"] == "PASTE_FROM_BOTFATHER":
        print("ERROR: telegram_bot_token missing.", file=sys.stderr)
        return 2

    os.makedirs(cfg["temp_dir"], exist_ok=True)
    state_path = cfg["state_file"]
    state = load_state(state_path)
    offset = state.get("inbound_update_offset")
    log.info("inbound started, thread=%s model=%s lang=%s",
             cfg["inbound_thread_id"], cfg.get("stt_model"), cfg.get("stt_language"))

    while True:
        try:
            updates = get_updates(cfg["telegram_bot_token"], offset)
            for u in updates:
                uid = u["update_id"]
                with tempfile.TemporaryDirectory(dir=cfg["temp_dir"]) as tmpdir:
                    ok = process_update(cfg, u, tmpdir, dry_run=args.dry_run)
                if ok and not args.dry_run:
                    offset = uid + 1
                    state["inbound_update_offset"] = offset
                    save_state(state_path, state)
                elif not ok:
                    log.warning("holding offset at %s for retry", offset)
                    break
            if args.once or args.dry_run:
                break
        except Exception:
            log.exception("poll error")
            if args.once or args.dry_run:
                return 1
            time.sleep(cfg.get("poll_interval", 5))
    return 0


if __name__ == "__main__":
    sys.exit(main())
