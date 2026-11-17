# T3-Code ↔ Telegram voice bridge

Two-way voice loop, two daemons:

- **Outbound** (`bridge.py`): watches T3 thread runs (read-only SQLite
  tail), synthesizes completed assistant replies with Kokoro TTS, delivers
  them as Telegram voice notes. Records which thread each voice note came
  from.
- **Inbound** (`inbound.py`): long-polls Telegram for voice notes,
  transcribes with faster-whisper, injects the text back into the thread
  the voice note came from (reply-aware routing, most-recent fallback).

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather): `/newbot` → copy the token.
2. Get your chat id: message your bot anything, then
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → `message.chat.id`.
3. Install:
   ```
   cd ~/repos/t3-voice-bridge
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
6. Run / enable (outbound; repeat with `t3-voice-bridge-inbound` for the
   inbound leg once `inbound_enabled: true` is set — see below):
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

## Inbound: Telegram voice message → T3 thread

Separate daemon (`inbound.py`, own systemd unit). Long-polls Telegram
`getUpdates` for voice/audio notes from `telegram_chat_id`, downloads the
`.ogg`, converts to 16k mono wav, transcribes with faster-whisper, and
injects the text with a 🎙 prefix via the local server
WebSocket RPC (`orchestration.dispatchCommand` / `message.dispatch`,
`deliveryIntent: auto`, `dispatchMode: start_immediately`). Telegram gets a
text confirmation (transcript + status, e.g. server sequence number).

Routing is reply-aware, not pinned to one thread: the outbound leg records
every delivered voice note (`state.json`: `outbound_msg_thread`
`{telegram_message_id: thread_id}`, capped at 200, plus
`outbound_last_thread`). Precedence per inbound note:
**reply** (its `reply_to_message` matches a mapped outbound voice note) →
**recent** (thread of the most recent outbound voice note) →
**fallback** (`inbound_thread_id` override; may be left empty, in which case
an unroutable note gets a Telegram explanation and is skipped). So a voice note sent
freestanding lands in the thread of the last voice note you received, and a
voice note sent as a reply to a specific voice note routes to that thread.

### One-time setup

No T3 Connect / webhook needed — the daemon talks to the local server over
`ws://127.0.0.1:3773` (ticket from `POST /api/auth/websocket-ticket/rpc`,
protocol params `orchestrationProtocol=2&clientCapabilities=settingsSecretReferences`).
It mints its own bearer via
`t3 auth session issue --scope orchestration:read --scope orchestration:operate`,
so nothing to configure there.

1. `./setup.sh` (adds `faster-whisper` + `websockets` to the venv, prefetches
   the tiny STT model into `~/.cache/huggingface`).
2. Find a thread id (copy the sqlite first — WAL + read-only reads go
   stale, so copy all three files):
   ```
   cp ~/.t3/userdata/statev2.sqlite{,-wal,-shm} /tmp/
   sqlite3 /tmp/statev2.sqlite "SELECT thread_id, title FROM orchestration_v2_projection_threads WHERE title LIKE '%Voice%';"
   ```
3. Edit `config.json`:
   `inbound_enabled: true`,
   optional `inbound_thread_id: "<uuid>"` safety-net override (normally
   leave `null` — routing follows the most recent voice note's thread),
   optional `stt_model` (default `"tiny"`) and `stt_language` (default `"en"`).
4. Test: send yourself a voice note, then
   `./bridge-venv/bin/python inbound.py --once` (real inject), or add
   `--dry-run` to only transcribe + print without injecting or messaging.
5. Enable: `cp t3-voice-bridge-inbound.service ~/.config/systemd/user/`,
   `systemctl --user daemon-reload`,
   `systemctl --user enable --now t3-voice-bridge-inbound`.

### Config keys (inbound)

| key | meaning | default |
|---|---|---|
| `inbound_enabled` | run the inbound poller | `false` |
| `inbound_thread_id` | explicit override thread uuid; when empty/null, routing is purely reply-aware (reply → most-recent outbound thread) | `null` |
| `stt_model` | faster-whisper model (`tiny`, `base`, …) | `"tiny"` |
| `stt_language` | transcription language (`en`, …; `null` = auto-detect) | `"en"` |

Shares `config.json` / `state.json` with the outbound bridge; the Telegram
offset lives under the namespaced `inbound_update_offset` key.

### Failure behavior

- Offset advances ONLY after a successful inject. Non-voice updates and
  wrong-chat messages are skipped (offset advances).
- Transcribe/inject failure → Telegram gets a ⚠️ error note and the offset
  is **held** for retry on the next poll.
- Empty transcript → Telegram note, offset advances (nothing to inject).
