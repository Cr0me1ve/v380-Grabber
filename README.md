# V380 Grabber

V380 Grabber is a Telegram-operated, self-hosted receiver for authorized V380 Neo cameras. One container runs the private bot and up to two receiver child processes. The image contains Python, `cryptography`, Pillow/QR support, and FFmpeg. It does not require Wine or closed V380 binaries.

## Verified compatibility

The verified camera is V380 Neo A2K1310S with firmware `V2.1.4.7 (HSC_A2K1310S_20250528)`, secure mode 2, and H.264 video. The receiver produces 640×360 frames at about 15 fps. Other V380 models and firmware versions are unconfirmed; this is not a universal V380 client.

## First setup

The bot token is the one manual bootstrap. Create a bot with Telegram's BotFather, then on the server:

1. Clone this private repository and run `./scripts/init.sh` as the same non-root Unix user that will run Docker Compose. The script prompts without echoing and saves the token at `secrets/telegram_bot_token` with mode `0600`.
2. Build the image with `./scripts/compose build`.
3. Generate a one-time owner claim code with `./scripts/compose run --rm app issue-claim`. It expires after 15 minutes; only its hash is stored.
4. Start the service with `./scripts/compose up -d`.
5. In a private chat with your bot, send `/claim CODE` using the code printed by step 3.
6. Send `/login`. Scan the short-lived QR with your authorized V380 app and select one of the listed cameras. No Google password or manual device-key editing is required.

The bot accepts commands only from the paired account in that private chat. Group chats and other users are ignored. The QR image and `/claim` message are deleted on a best-effort basis after use.

### Bootstrap import fallback

Interactive QR sign-in is the primary flow. If it is unavailable for your account, send `/add` and upload an existing authorized bootstrap JSON as a document. Required values include the account token, device ID, device public key, SDK signature suffix, regional controller address, and cached relays. The bot validates the file without printing values. The illustrative file at [examples/bootstrap.example.json.example](examples/bootstrap.example.json.example) contains placeholders only; it is intentionally not a working credential file.

The receiver's camera-control path uses the vendor's native HTTP protocol, including `cloud.av380.net:8002` and cloud requests on port 80. The account token and camera authorization data sent on that path are not protected by TLS. Public-key refresh uses V380's signed HTTPS account API. Telegram Bot API calls also use HTTPS. Run the receiver only on a network you trust.

## Receiver and stream access

The receiver binds inside the container, while Docker publishes its ports on the server's loopback interface only. No public inbound port is required. The endpoints are:

- `http://127.0.0.1:8081/health.json`
- `http://127.0.0.1:8081/snapshot.jpg`
- `http://127.0.0.1:8081/video.mjpg`

Camera 2 uses port `8082`. To view a stream from your workstation, open an SSH tunnel such as `ssh -N -L 8081:127.0.0.1:8081 USER@SERVER`, then visit the local endpoint. `USER` and `SERVER` are placeholders for your own SSH details. Do not expose these ports publicly without adding authentication and network controls.

The receiver tries the regional controller addresses returned by V380 and its cached relay candidates. It retries alternate addresses when a controller or relay fails, keeps valid cached relays when refresh fails, and cools down a failed relay for 60 seconds. If the camera reports key-exchange error `1103`, the client refreshes the public key through V380's signed HTTPS account API and retries the exchange once; refresh attempts are rate-limited. The V380 dispatch service uses port 8001, and video relays use TCP port 32100. Outbound access to Telegram over HTTPS is also required.

The bot sends one owner notification after a camera stream has been unavailable for at least 10 seconds, and one more when that stream recovers. It does not repeat alerts for the same outage.

## Motion clips

Recording is off by default. Turn it on through the bot with `/recording on`. The local recorder watches the MJPEG stream, keeps a three-second pre-roll, and queues 30-second MP4 clips at 15 fps after it detects motion. Empty black or white scenes do not create clips. The durable queue survives restarts; queued clips are sent to the paired owner in Telegram and acknowledged only after Telegram accepts the upload. A network timeout after Telegram accepted a clip can cause a duplicate retry; unacknowledged clips are retained. Retention cleanup removes delivered clips only.

Use `/recording off` to stop and cancel an unfinished clip. `/sensitivity low|medium|high` changes the motion threshold; `/retention DAYS` keeps delivered clips for 1–30 days. These commands apply to all configured cameras unless a camera number is supplied, for example `/recording on 2`.

## Bot commands

- `/login` — sign in by V380 QR, then select a camera for an open slot.
- `/login 1` or `/login 2` — refresh or replace that receiver through QR login.
- `/add` — import an existing authorized bootstrap JSON as a fallback.
- `/status` or `/ping` — check receiver and live-frame health.
- `/check 1` — check one camera's local health endpoint.
- `/edit 1` — replace a camera's bootstrap JSON.
- `/restart 1` — restart a receiver.
- `/remove 1` — remove it after `/confirm`.
- `/recording`, `/recording on`, `/recording off` — view or change recording state.
- `/sensitivity low|medium|high` — set motion sensitivity.
- `/retention DAYS` — set clip retention.
- `/cancel` — cancel QR setup or a pending dialog.

## Local data

The bot token stays in the ignored `secrets/` directory. Camera credentials, owner pairing state, recording settings, clips, and the durable queue stay in the ignored `data/` directory. Camera JSON files use mode `0600`; private state and recording directories use mode `0700`. Logs are bounded to two 3 MiB files. The container runs as the invoking Unix user's numeric ID, has a read-only root filesystem, drops Linux capabilities, and does not mount `docker.sock`.
