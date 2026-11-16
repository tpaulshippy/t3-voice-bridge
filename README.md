# T3-Code → Telegram voice-note bridge

Daemon: watches T3 thread runs (read-only SQLite tail), synthesizes completed
assistant replies with Kokoro TTS, delivers them as Telegram voice notes.

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather): `/newbot` → copy the token.
2. Get your chat id: message your bot anything, then
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → `message.chat.id`.
3. Install:
   ```
   cd ~/t3-voice-bridge
   ./setup.sh
   ```
   Downloads fp32 `kokoro-v1.0.onnx` (~310MB) + `voices-v1.0.bin` into `models/`.
   Requires espeak-ng + ffmpeg (already on this box).
4. Configure:
   ```
   cp config.example.json config.json
   # edit config.json: token, chat id, thread list
   ```
5. Dry-run (no Telegram send):
   ```
   ./bridge-venv/bin/python bridge.py --once --dry-run
   ```
6. Run / enable:
   ```
   ./bridge-venv/bin/python bridge.py
   # systemd user unit:
   mkdir -p ~/.config/systemd/user
   cp t3-voice-bridge.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now t3-voice-bridge
   journalctl --user -u t3-voice-bridge -f
   ```

## Config reference (`config.json`)

| key | meaning | default |
|---|---|---|
| `telegram_bot_token` | BotFather token (required for real sends) | — |
| `telegram_chat_id` | numeric chat id (required for real sends) | — |
| `t3_bearer` | optional; if absent bridge mints one via `t3 auth session issue` at startup (cached in `~/.t3`, never repo) | null |
| `thread_ids` | list of thread ids to watch; **empty = all threads**, watermark starts at NOW (no backlog spam) | `[]` |
| `poll_interval` | seconds between polls | 5 |
| `voice` | kokoro voice | `af_heart` |
| `state_file` | watermark persistence | `./state.json` |
| `temp_dir` | wav/ogg scratch | `./tmp` |
| `db_path` | T3 sqlite (read-only) | `/home/ubuntu/.t3/userdata/statev2.sqlite` |
| `models_dir` | kokoro models | `./models` |
| `max_retries` | Telegram send retries with backoff | 3 |

## Watching only specific threads

Set `thread_ids` to the ids you care about, e.g.
`{"thread_ids": ["399acccd-..."]}`. Thread ids/titles live in
`orchestration_v2_projection_threads` (query read-only).
With a non-empty list the watermark starts at epoch (catches up recent
history); with an empty list it starts at startup time.

## Behavior notes

- Only `completed` runs get voice audio. `failed`/`interrupted`/`cancelled`
  runs send a short text note (or just advance the watermark when dry).
- Long replies are split into sequential voice notes (~2000 chars each).
- Watermark advances ONLY after Telegram confirms `ok:true`.
- Markdown is stripped for speech (code fences → "code omitted").
- Logs to stdout + `bridge.log` (rotated, 1MB × 3).

## Troubleshooting

- **Token expired / 401 from Telegram**: create a fresh token with BotFather,
  update `config.json`, `systemctl --user restart t3-voice-bridge`.
- **t3 auth expiry**: bridge mints via `t3 auth session issue --scope
  orchestration:read --label telegram-bridge --ttl 30d` automatically.
- **Disk full**: models are ~350MB; `df -h` first. Free space under `~/repos`.
- **ffmpeg errors**: check `bridge.log`; voice files must be opus/24000/mono
  (`ffprobe voice.ogg`). Re-run `--dry-run` to reproduce locally.
- **No messages arriving**: check the thread actually completed a run
  (`sqlite3 "file:...statev2.sqlite?mode=ro" "SELECT ... FROM
  orchestration_v2_projection_runs ORDER BY completed_at DESC LIMIT 3"`),
  and that `thread_ids` (if set) includes it.
