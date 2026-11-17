# T3-Code ↔ Telegram voice bridge

Two-way voice loop, two daemons:

- **Outbound** (`bridge.py`): watches T3 thread runs (read-only SQLite
  tail), synthesizes completed assistant replies with Kokoro TTS, delivers
  them as Telegram voice notes. Records which thread each voice note came
  from.
- **Inbound** (`inbound.py`): long-polls Telegram for voice notes,
  transcribes with faster-whisper, injects the text back into the thread
  the voice note came from (reply-aware routing, most-recent fallback).

## Topology: cloud primary + Mac satellite

Telegram allows exactly **one** `getUpdates` holder per bot, so there is one
inbound poller total. Outbound senders (`sendVoice`) don't conflict, so each
machine voices its own local threads:

- **shipmate (cloud, primary)**: outbound + inbound via systemd user units.
  Watches shipmate threads, injects into shipmate threads.
- **Mac (satellite)**: outbound only, via a launchd agent. Watches Mac
  threads, voices them to the same Telegram chat.

Cross-machine reply routing: the Mac outbound syncs its
`message_id → thread` map into shipmate's `state.json` over SSH after every
send (see `routing_sync_ssh`), tagging values `mac:`. Shipmate's inbound
injects `mac:`-prefixed targets into the Mac's T3 server over Tailscale
(`mac_host:3773`, bearer in `mac_bearer`) instead of localhost.

## Setup (shipmate)

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

## Setup (Mac satellite, outbound only)

`setup.sh` is Linux-oriented; on macOS do its steps by hand:

```
brew install espeak-ng          # ffmpeg already present
python3 -m venv bridge-venv
./bridge-venv/bin/pip install --upgrade pip
./bridge-venv/bin/pip install kokoro-onnx soundfile numpy requests faster-whisper websockets
mkdir -p models && cd models
curl -fL -O https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx
curl -fL -O https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin
./bridge-venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"
```

Then `cp config.example.json config.json` and set `db_path` to
`$HOME/.t3/userdata/statev2.sqlite`, plus `routing_sync_ssh` (below).
The Mac needs SSH access to shipmate (`~/.ssh/config`: `Host shipmate`,
`User ubuntu`, `IdentityFile ~/.ssh/id_ed25519`).

Run under launchd (`~/Library/LaunchAgents/com.t3.voice-bridge.plist`
pointing at `bridge-venv/bin/python .../bridge.py`, `RunAtLoad` +
`KeepAlive`), then `launchctl load` it. Manage with
`launchctl kickstart -k gui/$(id -u)/com.t3.voice-bridge`;
logs in `bridge.log`.

## Config reference (`config.json`)

| key | meaning | default |
|---|---|---|
| `transport` | `telegram` or `slack` (outbound only; Slack has no inbound leg) | `telegram` |
| `slack_bot_token` | Slack bot token (`xoxb-`), scopes `files:write` + `chat:write`; bot must be in the channel | — |
| `slack_channel` | Slack channel id (e.g. `C0123…`) to post voice files into | — |
| `audio_base_url` | (Slack) if set, skip file upload: copy ogg into `audio_dir` and post a link `<audio_base_url>/<file>` instead (no `files:write` needed) | null |
| `audio_retention_days` | (Slack link mode) delete files in `audio_dir` older than this; old links stop working | 7 |
| `t3_web_url` | (Slack link mode) base for the "Open thread" link: `<t3_web_url>/<env id>/<thread id>`. Default opens the desktop app | `t3code://threads` |
| `t3_environment_id` | T3 environment id for that link; read from `~/.t3/userdata/environment-id` when unset | null |
| `audio_dir` | dir served at `audio_base_url` (e.g. `python3 -m http.server 3901 --bind 127.0.0.1 --directory audio` + `tailscale serve --bg --https=13901 http://localhost:3901`) | `./audio` |
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
| `routing_sync_ssh` | (Mac only) ssh host of the inbound machine; after each send, merge our routing map into its `state.json` so replies route back to our threads. Unset = no sync | null |
| `routing_sync_state` | remote `state.json` path for the sync above | `~/repos/t3-voice-bridge/state.json` |
| `mac_bearer` | (shipmate inbound only) long-lived bearer minted **on the Mac** (`t3 auth session issue --scope orchestration:read --scope orchestration:operate --label shipmate-mac-inject --ttl 30d --token-only`) enabling inject into Mac threads | null |
| `mac_host` | (shipmate inbound only) Mac Tailscale IP for ticket + WS-RPC | `100.84.1.93` |

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
- If the Mac is asleep (or its T3 app closed), Mac-thread replies fail to
  inject: Telegram gets a ⚠️ note and the offset is **held**, which also
  stalls notes queued behind it until the Mac is back.

## Troubleshooting

- **Token expired / 401 from Telegram**: create a fresh token with BotFather,
  update `config.json`, `systemctl --user restart t3-voice-bridge`
  (shipmate) or `launchctl kickstart -k ...` (Mac).
- **409 Conflict from getUpdates**: another poller is holding the connection —
  normally this daemon itself vs. a one-off `--once --dry-run` probe (harmless,
  it retakes the poll). If persistent, two inbound instances are running;
  stop one.
- **t3 auth expiry**: bridge mints via `t3 auth session issue --scope
  orchestration:read --label telegram-bridge --ttl 30d` automatically.
- **`mac_bearer` expiry** (~30d): Mac-thread replies fail with auth errors.
  Re-mint on the Mac (command above) and update shipmate's `config.json`,
  then `systemctl --user restart t3-voice-bridge-inbound`.
- **Disk full**: models are ~350MB; `df -h` first. Free space under `~/repos`.
- **ffmpeg errors**: check `bridge.log`; voice files must be opus/24000/mono
  (`ffprobe voice.ogg`). Re-run `--dry-run` to reproduce locally.
- **No messages arriving**: check the thread actually completed a run
  (`sqlite3 "file:...statev2.sqlite?mode=ro" "SELECT ... FROM
  orchestration_v2_projection_runs ORDER BY completed_at DESC LIMIT 3"`),
  and that `thread_ids` (if set) includes it.

## Inbound: Telegram voice message → T3 thread

Separate daemon (`inbound.py`, own systemd unit on shipmate). Long-polls Telegram
`getUpdates` for voice/audio notes from `telegram_chat_id`, downloads the
`.ogg`, converts to 16k mono wav, transcribes with faster-whisper, and
injects the text with a 🎙 prefix via the local server
WebSocket RPC (`orchestration.dispatchCommand` / `message.dispatch`,
`deliveryIntent: auto`, `dispatchMode: start_immediately`). Telegram gets a
text confirmation (transcript + status, e.g. server sequence number).

Routing is reply-aware, not pinned to one thread: the outbound legs record
every delivered voice note (`state.json`: `outbound_msg_thread`
`{telegram_message_id: thread_id}`, capped at 200, plus
`outbound_last_thread`; Mac entries arrive via the SSH sync tagged `mac:`).
Precedence per inbound note:
**reply** (its `reply_to_message` matches a mapped outbound voice note) →
**recent** (thread of the most recent outbound voice note) →
**fallback** (`inbound_thread_id` override; may be left empty, in which case
an unroutable note gets a Telegram explanation and is skipped). So a voice note sent
freestanding lands in the thread of the last voice note you received, and a
voice note sent as a reply to a specific voice note routes to that thread.
`mac:` targets are injected into the Mac's T3 server over Tailscale instead
of localhost.

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
