# V380 Grabber

V380 Grabber is a self-hosted Telegram-managed receiver for one authorized V380 Neo camera. It runs without Wine or closed V380 desktop software. The verified device is the V380 Neo A2K1310S on firmware `V2.1.4.7 (HSC_A2K1310S_20250528)`, secure mode 2, H.264 video. Other models and firmware versions are unconfirmed.

## Install

Install this private repository on the server in a path without spaces, such as `/opt/v380-grabber`. Create a Telegram bot with BotFather, then run:

```sh
sudo ./scripts/install.sh
```

The installer prompts for the bot token without echoing it, saves it under `secrets/`, builds the Docker images, initializes private state under `data/`, installs and starts the `v380-grabber.service` host systemd unit, then prints a one-time owner claim code. In a private chat with the new bot, send `/claim CODE` with that code. It expires after 15 minutes and is stored only as a hash.

Next, send `/login`. Scan the short-lived QR code with your authorized V380 app, approve the login, and select the camera. The bot keeps account credentials locally and does not send them in chat. If QR setup is unavailable for an account, `/add` can import an existing authorized bootstrap JSON instead.

Bot commands are accepted only from the paired owner in the same private chat. Group messages and other users are ignored. Claim messages, QR images, and uploaded bootstrap documents are deleted on a best-effort basis.

## Services and video

The deployment supports one camera and runs one to four parallel receiver containers for it. A selector publishes the healthiest verified stream and maintains a 12-second rolling frame buffer capped at 24 MiB. The current defaults are 30-second clips, two receivers, and three seconds of pre-roll.

Docker Compose runs the receiver replicas, selector, and MotionEye. The host systemd bot supervises the stack through the Docker CLI and checks service health; Docker's restart policy and the host watchdog recover failed processes. The bot runs on the host with the privileges needed to manage Compose. No container mounts `docker.sock`.

MotionEye provides the local web viewer and retained motion archives. While motion continues, Motion closes each recording at the configured clip length and starts the next. The bot queues each closed recording as it becomes available, including a shorter final recording when the event ends. A recording that runs past the clip length is split so its remaining footage can be sent separately. The durable queue retries delivery and marks a clip delivered after Telegram acknowledges `sendVideo`. Clock and scene checks reject bad video and suppress empty black footage. Archive retention and storage limits are configurable from Telegram.

The web interface is bound to server loopback at `http://127.0.0.1:8765`. Send `/web` to the bot for the current URL and login. To open it from a workstation, forward the loopback port over SSH, for example:

```sh
ssh -N -L 8765:127.0.0.1:8765 USER@SERVER
```

Then open `http://127.0.0.1:8765` locally. Replace `USER` and `SERVER` with your own SSH details. Set a new web password with `/web_password PASSWORD` (12–128 characters). Do not publish the viewer port directly to the Internet.

The selector exposes `http://127.0.0.1:8081/health.json`, `/snapshot.jpg`, `/video.mjpg`, and event-buffer endpoints. These ports are bound to loopback. The MotionEye interface is the supported browser UI.

## Telegram settings

Send `/settings` to see the current configuration. Settings are saved locally and take effect through the running stack.

| Command | Allowed values |
| --- | --- |
| `/duration SECONDS` | 5–120; current default 30 |
| `/receivers COUNT` | 1–4 parallel receivers; current default 2 |
| `/pre_roll SECONDS` | 0–10; current default 3 |
| `/recording on` or `/recording off` | Enable or disable motion capture |
| `/sensitivity low`, `medium`, or `high` | Motion threshold |
| `/retention DAYS` | 1–30 days |
| `/storage_gb GB` | 1–1000 GB archive limit |
| `/reserve_gb GB` | 0.5–100 GB free-space reserve |
| `/notifications on` or `/notifications off` | Motion-clip notifications |
| `/outage_alerts on` or `/outage_alerts off` | Camera outage and recovery alerts |
| `/web` | Show the loopback viewer URL and login |
| `/web_password PASSWORD` | Set a 12–128 character web password |

Other useful commands are `/ping` for a fresh frame and receiver status, `/restart` to reconnect the camera receivers, `/login` to configure or refresh the camera, and `/cancel` to cancel a pending QR selection.

## Network and credentials

The camera-control stream uses V380's native cloud protocol, including `cloud.av380.net:8002` and HTTP requests on port 80. The account token and camera authorization fields sent on that path are not protected by TLS. Public-key refresh uses V380's signed HTTPS account API. Telegram Bot API requests use HTTPS. Run the receiver only on a network you trust.

Outbound network access to V380 and Telegram is required. The V380 dispatch service uses port 8001 and camera video relays use TCP port 32100. The installer does not change VPN or firewall settings.

The bot token is stored under the ignored `secrets/` directory. Camera credentials, owner pairing, runtime settings, MotionEye configuration, logs, archives, and the durable queue are stored under the ignored `data/` directory. Do not add these files to Git or send them to anyone. The illustrative bootstrap file at [examples/bootstrap.example.json.example](examples/bootstrap.example.json.example) contains placeholders only and is not a working configuration.
