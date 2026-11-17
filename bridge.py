#!/usr/bin/env python3
"""T3-Code -> Telegram voice-note bridge daemon.

Primary completion signal: read-only SQLite tail of statev2.sqlite.
On newly completed run: assistant text -> sanitize -> kokoro TTS -> ffmpeg opus -> sendVoice.
Watermark advances ONLY after successful Telegram send (ok:true).
"""
import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sanitize import sanitize, chunk  # noqa: E402

TERMINAL = ("completed", "failed", "interrupted", "cancelled")

log = logging.getLogger("bridge")


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    defaults = {
        "t3_bearer": None,
        "thread_ids": [],
        "poll_interval": 5,
        "voice": "af_heart",
        "state_file": "./state.json",
        "temp_dir": "./tmp",
        "db_path": "/home/ubuntu/.t3/userdata/statev2.sqlite",
        "models_dir": "./models",
        "max_retries": 3,
        "transport": "telegram",
        "audio_dir": "./audio",
        "audio_base_url": None,
        "t3_web_url": "t3code://threads",
        "t3_environment_id": None,
        "routing_sync_ssh": None,
        "routing_sync_state": "~/repos/t3-voice-bridge/state.json",
    }
    defaults.update(cfg)
    # resolve relative paths against repo dir
    for k in ("state_file", "temp_dir", "models_dir", "audio_dir"):
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


def db_connect(db_path):
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    return con


def new_runs_since(con, watermark, thread_ids):
    """Rows with terminal status and completed_at > watermark, oldest first."""
    q = (
        "SELECT run_id, thread_id, status, completed_at FROM "
        "orchestration_v2_projection_runs "
        "WHERE status IN ('completed','failed','interrupted','cancelled') "
        "AND completed_at > ? "
    )
    params = [watermark]
    if thread_ids:
        q += "AND thread_id IN (%s) " % ",".join("?" for _ in thread_ids)
        params += list(thread_ids)
    q += "ORDER BY completed_at ASC LIMIT 20"
    return con.execute(q, params).fetchall()


def latest_completed_run(con, thread_id=None):
    q = (
        "SELECT run_id, thread_id, status, completed_at FROM "
        "orchestration_v2_projection_runs "
        "WHERE status IN ('completed','failed','interrupted','cancelled') "
    )
    params = []
    if thread_id:
        q += "AND thread_id = ? "
        params.append(thread_id)
    q += "ORDER BY completed_at DESC LIMIT 1"
    return con.execute(q, params).fetchone()


def assistant_text(con, run_id):
    rows = con.execute(
        "SELECT payload_json FROM orchestration_v2_projection_messages "
        "WHERE run_id = ? AND role = 'assistant' AND streaming = 0 "
        "ORDER BY created_at",
        (run_id,),
    ).fetchall()
    parts = []
    for (pj,) in rows:
        try:
            t = json.loads(pj).get("text") or ""
        except (json.JSONDecodeError, AttributeError):
            t = ""
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def thread_title_v2(con, thread_id):
    for tbl in ("orchestration_v2_projection_threads", "projection_threads"):
        try:
            row = con.execute(
                f"SELECT title FROM {tbl} WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row and row[0]:
                return row[0]
        except sqlite3.OperationalError:
            continue
    return thread_id


class TTS:
    def __init__(self, models_dir, voice="af_heart"):
        from kokoro_onnx import Kokoro

        onnx = str(Path(models_dir) / "kokoro-v1.0.onnx")
        voices = str(Path(models_dir) / "voices-v1.0.bin")
        if not (os.path.exists(onnx) and os.path.exists(voices)):
            raise FileNotFoundError(
                f"model files missing in {models_dir}; run setup.sh"
            )
        self.kokoro = Kokoro(onnx, voices)
        self.voice = voice

    def synth(self, text, wav_path):
        import soundfile as sf

        samples, sr = self.kokoro.create(text, voice=self.voice, lang="en-us")
        sf.write(wav_path, samples, sr)
        return sr


def _bin(name):
    """Resolve a media binary robustly under launchd's minimal PATH."""
    found = shutil.which(name)
    if found:
        return found
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if os.path.exists(cand):
            return cand
    return name


def to_ogg(wav_path, ogg_path):
    r = subprocess.run(
        [_bin("ffmpeg"), "-y", "-v", "error", "-i", wav_path,
         "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
         "-ar", "24000", "-ac", "1", ogg_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-2000:]}")
    return ogg_path


def send_voice(token, chat_id, ogg_path, caption, max_retries=3):
    import requests

    url = f"https://api.telegram.org/bot{token}/sendVoice"
    cap = (caption or "")[:1024]
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            with open(ogg_path, "rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": chat_id, "caption": cap},
                    files={"voice": ("voice.ogg", f, "audio/ogg")},
                    timeout=180,
                )
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code == 200 and body.get("ok"):
                try:
                    return body["result"]["message_id"]
                except (KeyError, TypeError):
                    return True
            # rate limited?
            if r.status_code in (429,) or body.get("error_code") == 429:
                retry_after = 5 * attempt
                try:
                    retry_after = int(
                        body.get("parameters", {}).get("retry_after", retry_after)
                    )
                except (ValueError, TypeError, AttributeError):
                    pass
                log.warning("rate limited, sleeping %ss (attempt %d)", retry_after, attempt)
                time.sleep(retry_after)
                last = f"429: {body}"
                continue
            last = f"http={r.status_code} body={str(body)[:300]}"
            log.warning("sendVoice attempt %d failed: %s", attempt, last)
        except Exception as e:  # network error -> backoff + retry
            last = repr(e)
            log.warning("sendVoice attempt %d error: %s", attempt, last)
        time.sleep(2 * attempt)
    log.error("sendVoice failed after %d attempts: %s", max_retries, last)
    return False


SLACK_API = "https://slack.com/api"


def _slack_call(token, method, timeout=30, **data):
    import requests

    r = requests.post(f"{SLACK_API}/{method}",
                      headers={"Authorization": f"Bearer {token}"},
                      data=data, timeout=timeout)
    body = r.json()
    if r.status_code == 429:
        body = {"ok": False, "error": "ratelimited",
                "retry_after": int(r.headers.get("Retry-After", 5))}
    return body


def send_voice_slack(token, channel, ogg_path, caption, max_retries=3):
    """Upload ogg via files.getUploadURLExternal -> POST -> completeUploadExternal."""
    import requests

    size = os.path.getsize(ogg_path)
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            up = _slack_call(token, "files.getUploadURLExternal",
                             filename="voice.ogg", length=size)
            if not up.get("ok"):
                raise RuntimeError(f"getUploadURLExternal: {up}")
            with open(ogg_path, "rb") as f:
                r = requests.post(up["upload_url"],
                                  files={"file": ("voice.ogg", f, "audio/ogg")},
                                  timeout=180)
            if r.status_code != 200:
                raise RuntimeError(f"upload http={r.status_code}")
            done = _slack_call(
                token, "files.completeUploadExternal",
                files=json.dumps([{"id": up["file_id"], "title": (caption or "voice")[:250]}]),
                channel_id=channel, initial_comment=(caption or "")[:4000],
            )
            if done.get("ok"):
                return True
            raise RuntimeError(f"completeUploadExternal: {done}")
        except Exception as e:
            last = repr(e)
            log.warning("slack upload attempt %d failed: %s", attempt, last)
            if "ratelimited" in last:
                time.sleep(5 * attempt)
        time.sleep(2 * attempt)
    log.error("slack upload failed after %d attempts: %s", max_retries, last)
    return False


def send_text_slack(token, channel, text):
    try:
        return bool(_slack_call(token, "chat.postMessage",
                                channel=channel, text=text[:4000]).get("ok"))
    except Exception as e:
        log.warning("chat.postMessage failed: %r", e)
        return False


def is_slack(cfg):
    return cfg.get("transport") == "slack"


def thread_link(cfg, thread_id):
    env = cfg.get("t3_environment_id")
    if not env:
        try:
            env = Path("~/.t3/userdata/environment-id").expanduser().read_text().strip()
        except OSError:
            return None
    return f"{cfg['t3_web_url'].rstrip('/')}/{env}/{urllib.parse.quote(thread_id, safe='')}"


def send_voice_link_slack(cfg, ogg_path, caption, thread_id=None, words=None):
    """Copy ogg into audio_dir (served over tailnet) and post its link."""
    os.makedirs(cfg["audio_dir"], exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(4).hex() + ".ogg"
    shutil.copy(ogg_path, os.path.join(cfg["audio_dir"], name))
    url = cfg["audio_base_url"].rstrip("/") + "/" + name
    secs = round(ogg_duration(ogg_path))
    parts = [f"🔊 <{url}|{caption or 'voice'}>", f"{secs // 60}:{secs % 60:02d}"]
    if words is not None:
        parts.append(f"{words} words")
    link = thread_id and thread_link(cfg, thread_id)
    if link:
        parts.append(f"<{link}|Open thread>")
    return send_text_slack(cfg["slack_bot_token"], cfg["slack_channel"], " · ".join(parts))


def deliver_voice(cfg, ogg_path, caption, thread_id=None, words=None):
    if is_slack(cfg) and cfg.get("audio_base_url"):
        return send_voice_link_slack(cfg, ogg_path, caption, thread_id, words)
    if is_slack(cfg):
        return send_voice_slack(cfg["slack_bot_token"], cfg["slack_channel"],
                                ogg_path, caption, cfg.get("max_retries", 3))
    return send_voice(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                      ogg_path, caption, cfg.get("max_retries", 3))


def caption_for(status, title, run_id, n_chunks=1, idx=1):
    base = f"{title}"
    if status != "completed":
        base += f" [{status}]"
    if n_chunks > 1:
        base += f" ({idx}/{n_chunks})"
    if status != "completed":
        base += f" (run {status})"
    return base[:1024]


def process_run(cfg, con, tts, run_id, thread_id, status, tmpdir, dry_run=False,
                sent_ids=None):
    title = thread_title_v2(con, thread_id)
    if status != "completed":
        # speak nothing; text-mode note only
        cap = f"{title} (run {status}, no audio)".strip()[:1024]
        if dry_run:
            print(f"DRY: run {run_id} status={status} -> would send TEXT note, caption={cap!r}")
            return True
        if is_slack(cfg) and cfg.get("slack_bot_token"):
            return send_text_slack(cfg["slack_bot_token"], cfg["slack_channel"], cap)
        if not is_slack(cfg) and cfg.get("telegram_bot_token") and cfg["telegram_bot_token"] != "PASTE_FROM_BOTFATHER":
            return send_text_note(cfg, cap)
        log.info("run %s status=%s, skipping audio (no token -> skip text too)", run_id, status)
        return True  # advance watermark; nothing to speak
    raw = assistant_text(con, run_id)
    clean = sanitize(raw)
    if not clean.strip():
        log.info("run %s: empty assistant text, skipping", run_id)
        return True
    chunks = chunk(clean)
    if dry_run:
        total_bytes = 0
        for i, c in enumerate(chunks, 1):
            wav = os.path.join(tmpdir, f"dry-{i}.wav")
            ogg = os.path.join(tmpdir, f"dry-{i}.ogg")
            tts.synth(c, wav)
            to_ogg(wav, ogg)
            dur = ogg_duration(ogg)
            n = os.path.getsize(ogg)
            total_bytes += n
            print(f"DRY chunk {i}/{len(chunks)}: {dur:.1f}s, {n} bytes, text={c[:80]!r}...")
        print(f"would send {len(chunks)} voice note(s) ({total_bytes} bytes total) "
              f"to {'slack ' + str(cfg.get('slack_channel')) if is_slack(cfg) else 'chat ' + str(cfg.get('telegram_chat_id'))} "
              f"caption={title[:80]!r}")
        return True
    ok_all = True
    for i, c in enumerate(chunks, 1):
        wav = os.path.join(tmpdir, f"{re.sub(r'[^A-Za-z0-9_-]+', '_', run_id)}-{i}.wav")
        ogg = wav + ".ogg"
        try:
            tts.synth(c, wav)
            to_ogg(wav, ogg)
        except Exception:
            log.exception("run %s chunk %d: synth failed, skipping", run_id, i)
            note = f"{title}: couldn't voice this reply" + (f" (part {i}/{len(chunks)})" if len(chunks) > 1 else "")
            if is_slack(cfg):
                send_text_slack(cfg["slack_bot_token"], cfg["slack_channel"], note)
            else:
                send_text_note(cfg, note)
            continue
        cap = caption_for(status, title, run_id, len(chunks), i)
        mid = deliver_voice(cfg, ogg, cap, thread_id, len(c.split()))
        if not mid:
            ok_all = False
            break  # do not advance watermark; retry next poll
        if sent_ids is not None and isinstance(mid, int):
            sent_ids.append((mid, thread_id))
        for p in (wav, ogg):
            try:
                os.remove(p)
            except OSError:
                pass
    return ok_all


def send_text_note(cfg, text):
    import requests

    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": cfg["telegram_chat_id"], "text": text[:4000]},
                          timeout=30)
        return r.status_code == 200 and r.json().get("ok")
    except Exception as e:
        log.warning("sendMessage failed: %r", e)
        return False


def record_outbound(state_path, pairs, cfg=None):
    """Persist telegram message_id -> thread_id for inbound reply routing.

    Re-reads state from disk first so the inbound daemon's offset key
    (and vice versa) isn't clobbered. Map capped at 200 entries.
    When cfg has routing_sync_ssh, the same pairs are merged into the
    (single) inbound daemon's state file over ssh so reply-routing works
    across machines. Sync failure never blocks the local save.
    """
    try:
        state = load_state(state_path)
    except Exception:
        state = {}
    m = state.get("outbound_msg_thread") or {}
    for mid, tid in pairs:
        m[str(mid)] = tid
    while len(m) > 200:
        m.pop(next(iter(m)))
    state["outbound_msg_thread"] = m
    if pairs:
        state["outbound_last_thread"] = pairs[-1][1]
    try:
        save_state(state_path, state)
    except Exception as e:
        log.warning("record_outbound failed: %r", e)
    if pairs and cfg and cfg.get("routing_sync_ssh"):
        sync_routing_map(cfg, dict(m))


REMOTE_MERGE = (
    "import json,sys,os;"
    "pairs=json.loads(sys.argv[1]);"
    "sp=os.path.expanduser(sys.argv[2]);"
    "st=json.load(open(sp)) if os.path.exists(sp) else {};"
    "m=st.get('outbound_msg_thread') or {};"
    "m.update({str(k):v for k,v in pairs.items()});"
    "[m.pop(k) for k in list(m)[:max(0,len(m)-200)]];"
    "st['outbound_msg_thread']=m;"
    "st['outbound_last_thread']=list(pairs.values())[-1];"
    "tmp=sp+'.tmp';json.dump(st,open(tmp,'w'));os.replace(tmp,sp)"
)


def sync_routing_map(cfg, full_map):
    """Merge our outbound map into the inbound host's state file via ssh."""
    import shlex

    try:
        # ship only recent entries to keep the command small; tag values
        # mac: so the inbound side knows to inject via the Mac T3 server
        items = {k: (v if str(v).startswith("mac:") else "mac:" + str(v))
                 for k, v in list(full_map.items())[-200:]}
        remote_cmd = " ".join([
            "python3", "-c", shlex.quote(REMOTE_MERGE),
            shlex.quote(json.dumps(items)),
            shlex.quote(cfg.get("routing_sync_state", "state.json")),
        ])
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", cfg["routing_sync_ssh"], remote_cmd],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log.warning("routing sync failed: %s", r.stderr[-200:])
    except Exception as e:
        log.warning("routing sync error: %r", e)


def ogg_duration(ogg_path):
    try:
        r = subprocess.run(
            [_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", ogg_path],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return 0.0


def ensure_token(cfg):
    """If t3_bearer absent, mint one via t3 CLI (cached in ~/.t3 state, never repo)."""
    if cfg.get("t3_bearer"):
        return cfg["t3_bearer"]
    try:
        r = subprocess.run(
            ["t3", "auth", "session", "issue", "--scope", "orchestration:read",
             "--label", "telegram-bridge", "--ttl", "30d", "--token-only"],
            capture_output=True, text=True, timeout=30,
        )
        tok = r.stdout.strip()
        if r.returncode == 0 and tok:
            log.info("minted t3 session token via CLI")
            return tok
        log.warning("t3 auth mint failed: %s", r.stderr[-500:])
    except Exception as e:
        log.warning("t3 auth mint error: %r", e)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--thread", default=None, help="dry-run: specific thread id")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(str(HERE / "bridge.log"),
                                      maxBytes=1_000_000, backupCount=3)],
    )

    cfg = load_config(args.config)
    os.makedirs(cfg["temp_dir"], exist_ok=True)
    state_path = cfg["state_file"]
    state = load_state(state_path)

    con = db_connect(cfg["db_path"])
    tts = TTS(cfg["models_dir"], cfg.get("voice", "af_heart"))

    if args.dry_run:
        row = latest_completed_run(con, args.thread)
        if not row:
            print("DRY: no completed runs found" + (f" for thread {args.thread}" if args.thread else ""))
            return 0
        run_id, thread_id, status, completed_at = row
        print(f"DRY: latest run {run_id} thread={thread_id} status={status} completed_at={completed_at}")
        with tempfile.TemporaryDirectory(dir=cfg["temp_dir"]) as tmpdir:
            ok = process_run(cfg, con, tts, run_id, thread_id, status, tmpdir, dry_run=True)
        return 0 if ok else 1

    _token = ensure_token(cfg)  # currently unused; SQLite is primary signal

    # init watermarks to NOW when watching all threads (no backlog spam)
    watching_all = not cfg.get("thread_ids")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def wm(thread_id):
        if thread_id in state:
            return state[thread_id]
        if watching_all:
            return now
        return "1970-01-01T00:00:00.000Z"

    # per-thread watermarks: query global max then filter; simpler: single global watermark
    global_wm = state.get("_global", now if watching_all else "1970-01-01T00:00:00.000Z")

    log.info("bridge started, watching=%s interval=%ss",
             "all threads" if watching_all else cfg["thread_ids"], cfg["poll_interval"])
    while True:
        try:
            con2 = con  # reuse ro connection
            rows = new_runs_since(con2, global_wm, cfg.get("thread_ids") or [])
            for run_id, thread_id, status, completed_at in rows:
                with tempfile.TemporaryDirectory(dir=cfg["temp_dir"]) as tmpdir:
                    sent = []
                    ok = process_run(cfg, con2, tts, run_id, thread_id, status,
                                     tmpdir, sent_ids=sent)
                if ok:
                    if sent:
                        record_outbound(state_path, sent, cfg)
                    global_wm = max(global_wm, completed_at)
                    state["_global"] = global_wm
                    state[thread_id] = max(state.get(thread_id, ""), completed_at)
                    save_state(state_path, state)
                else:
                    log.warning("send failed for %s, watermark held at %s", run_id, global_wm)
                    break
        except Exception:
            log.exception("poll error")
        if args.once:
            break
        time.sleep(cfg.get("poll_interval", 5))


if __name__ == "__main__":
    sys.exit(main())
