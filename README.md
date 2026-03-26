# lossless-stream

Stream any analog audio source to [Music Assistant](https://music-assistant.io/) as a lossless FLAC-over-HTTP stream. Uses ffmpeg for ALSA capture, a lightweight Python aiohttp relay to serve the stream, and connects to Music Assistant as a radio station URL — zero quality loss, bit-perfect audio from any line-in source to your whole-home system.

## Architecture

```
┌──────────────┐  analog/TOSLINK  ┌────────────┐    ALSA     ┌────────────┐
│ Audio Source │─────────────────►│ Sound Card │────────────►│   ffmpeg   │
└──────────────┘                  └────────────┘             └─────┬──────┘
                                                                   │ FLAC
                                                             ┌─────▼──────┐
                                                             │  aiohttp   │
                                                             │  relay.py  │
                                                             └─────┬──────┘
                                                                   │ HTTP
                                                       ┌───────────▼──────────┐
                                                       │   Music Assistant    │
                                                       │ http://host:8100/    │
                                                       │      live.flac       │
                                                       └──────────────────────┘
```

## Requirements

- **Linux host** *(required — macOS and Windows are not supported)* — audio capture uses ALSA, which is Linux-only
- ALSA-capable sound card (USB audio interface, TOSLINK card, etc.)
- **Docker** (or Podman)
- **Audio source** connected to the sound card (turntable via phono preamp, tape deck, etc.)

---

## Quick Start (Pre-built Image)

No cloning or building required — pull the image straight from GitHub Container Registry.

```bash
docker pull ghcr.io/saphirabot/lossless-stream:latest
```

### docker-compose.yml

Copy this snippet into a `docker-compose.yml` file, set `ALSA_DEVICE`, and you're done:

```yaml
services:
  lossless-stream:
    image: ghcr.io/saphirabot/lossless-stream:latest
    restart: unless-stopped
    network_mode: host          # lets Music Assistant reach the stream by LAN IP
    devices:
      - /dev/snd:/dev/snd       # pass ALSA hardware through to the container
    environment:
      ALSA_DEVICE: "hw:1,0"     # ← set this to your capture device (see arecord -l)
      SAMPLE_RATE: "48000"
      CHANNELS: "2"
      BIT_DEPTH: "24"
      RELAY_PORT: "8100"
      MOUNT_POINT: "live.flac"
      STREAM_NAME: "Audio Source"
      CHUNK_SIZE: "4096"
      THREAD_QUEUE_SIZE: "4096"
```

### Steps

1. **Copy the compose snippet** above into a new `docker-compose.yml` anywhere on your host.
2. **Set `ALSA_DEVICE`** — run `arecord -l` to find your card/device number, then update the value (e.g. `hw:0,0`, `hw:1,0`).
3. **Start the container:**
   ```bash
   docker compose up -d
   ```
4. **Verify:**
   ```bash
   curl http://localhost:8100/health
   ```

Your stream is now live at `http://<your-host-ip>:8100/live.flac`.

---

## Finding Your ALSA Device

Run `arecord -l` to list capture devices:

```
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
card 1: USB [Behringer UMC204HD], device 0: USB Audio [USB Audio]
```

The ALSA device string is `hw:<card>,<device>`:
- Built-in line-in → `hw:0,0`
- USB audio interface → `hw:1,0`

Set this as `ALSA_DEVICE` in your `docker-compose.yml` or `.env` file.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ALSA_DEVICE` | `hw:0,0` | ALSA capture device (find with `arecord -l`) |
| `SAMPLE_RATE` | `48000` | Sample rate in Hz (44100 for CD quality, 48000 for most USB interfaces) |
| `CHANNELS` | `2` | Number of audio channels (2 = stereo) |
| `BIT_DEPTH` | `24` | Bit depth (16 or 24) |
| `RELAY_PORT` | `8100` | HTTP port the stream is served on |
| `MOUNT_POINT` | `live.flac` | URL path for the stream (stream URL = `http://host:port/mount`) |
| `STREAM_NAME` | `Audio Source` | Name shown in logs |
| `CHUNK_SIZE` | `4096` | Broadcast chunk size in bytes |
| `THREAD_QUEUE_SIZE` | `4096` | ffmpeg thread queue size (increase if you see buffer overrun warnings) |

## Adding to Music Assistant

1. Open Music Assistant in your browser
2. Go to **Settings → Providers → Radio**
3. Add a new radio station with the URL:
   ```
   http://<your-host-ip>:8100/live.flac
   ```
4. Play it — audio from your source now streams to any MA player

> **Tip:** Use the host's LAN IP (e.g., `192.168.1.50`), not `localhost`, since Music Assistant runs in its own container.

## TOSLINK vs Analog Line-In

Both work. Choose based on your setup:

| Input | Pros | Cons |
|---|---|---|
| **TOSLINK (optical)** | Digital path — no analog noise, no ground loops | Requires a device with optical output (some preamps, CD players) |
| **Analog line-in** | Works with any source, simple cabling | Susceptible to ground loops / interference if cabling is poor |

If you hear hum or buzz with analog, try a ground loop isolator or switch to TOSLINK.

## Health Check

The relay exposes a health endpoint:

```bash
curl http://localhost:8100/health
```

```json
{
  "status": "ok",
  "clients": 1,
  "mount": "/live.flac",
  "device": "hw:0,0",
  "ffmpeg_alive": true,
  "headers_cached": 278,
  "headers_complete": true
}
```

## Troubleshooting

### Silent audio / no sound

- Verify the ALSA device is correct: `arecord -D hw:0,0 -d 5 -f cd test.wav && aplay test.wav`
- Check that your source is actually playing and connected
- Ensure the capture device isn't muted: `alsamixer` → F4 (Capture) → unmute and set levels

### Music Assistant can't connect

- Make sure you're using the host's LAN IP, not `localhost` or `127.0.0.1`
- Verify the container is running: `docker compose ps`
- Check the health endpoint: `curl http://<host-ip>:8100/health`
- Ensure no firewall is blocking the port

### Wrong ALSA device / "No such device"

- Re-check with `arecord -l` — device numbers can change after reboot if you have multiple sound cards
- USB devices may shift card numbers; consider using a udev rule for a stable name

### ffmpeg keeps restarting

- Check container logs: `docker compose logs -f`
- Usually means the ALSA device is busy (another process using it) or doesn't exist
- Verify with: `arecord -D hw:0,0 -d 1 -f cd /dev/null`

### Buffer overrun warnings

- Increase `THREAD_QUEUE_SIZE` in your `docker-compose.yml` or `.env` (try `8192` or `16384`)

---

## Building from Source

> For contributors or if you want to modify the code.

```bash
# 1. Clone the repo
git clone https://github.com/SaphiraBot/lossless-stream.git
cd lossless-stream

# 2. Find your ALSA device
arecord -l

# 3. Configure
cp .env.example .env
# Edit .env — at minimum, set ALSA_DEVICE to match your card

# 4. Build the image
docker build -t lossless-stream .

# 5. Run it
docker run -d \
  --name lossless-stream \
  --restart unless-stopped \
  --network host \
  --device /dev/snd:/dev/snd \
  --env-file .env \
  lossless-stream

# 6. Verify
curl http://localhost:8100/health
```

**Using docker-compose with a local build?** Copy the Quick Start compose snippet into a `docker-compose.yml` and replace the `image:` line with `build: .`:

```yaml
services:
  lossless-stream:
    build: .
    restart: unless-stopped
    network_mode: host
    devices:
      - /dev/snd:/dev/snd
    environment:
      ALSA_DEVICE: "hw:1,0"
      SAMPLE_RATE: "48000"
      CHANNELS: "2"
      BIT_DEPTH: "24"
      RELAY_PORT: "8100"
      MOUNT_POINT: "live.flac"
      STREAM_NAME: "Audio Source"
      CHUNK_SIZE: "4096"
      THREAD_QUEUE_SIZE: "4096"
```

Then run `docker compose up -d --build`.

## License

[MIT](LICENSE) — Saphira
