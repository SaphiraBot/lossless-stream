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
      MAX_LISTENERS: "10"
      MAX_QUEUE_SIZE_KB: "512"
      SOURCE_TIMEOUT: "10"
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
| `STREAM_NAME` | `Audio Source` | Stream name shown in logs and the `/metadata` endpoint |
| `CHUNK_SIZE` | `4096` | Broadcast chunk size in bytes |
| `THREAD_QUEUE_SIZE` | `4096` | ffmpeg thread queue size (increase if you see buffer overrun warnings) |
| `MAX_LISTENERS` | `10` | Maximum simultaneous listeners — new connections get HTTP 503 when full |
| `MAX_QUEUE_SIZE_KB` | `512` | Per-client write buffer limit in KB — slow clients exceeding this are dropped |
| `SOURCE_TIMEOUT` | `10` | Seconds of silence from ffmpeg before the source is flagged as timed-out |
| `HEADER_TIMEOUT` | `15` | Idle-connection / keep-alive timeout in seconds |
| `RECORD_FILE` | *(empty)* | If set, the raw FLAC stream is also written to this file path |
| `EXTRA_HEADERS` | *(empty)* | JSON object of extra HTTP headers added to stream responses (e.g. `'{"X-Custom": "value"}'`) |

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

## API Endpoints

### Health Check — `GET /health`

Returns server and source status. Used by Docker's built-in `HEALTHCHECK`.

```bash
curl http://localhost:8100/health
```

**Normal response (200):**

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

**Degraded response (503) — source timeout:**

When ffmpeg is running but has stopped producing audio data (e.g. ALSA device stalled):

```json
{
  "status": "degraded",
  "clients": 0,
  "mount": "/live.flac",
  "device": "hw:0,0",
  "ffmpeg_alive": true,
  "headers_cached": 278,
  "headers_complete": true,
  "ffmpeg": "timeout"
}
```

**Dead response (503) — ffmpeg not running:**

```json
{
  "status": "ffmpeg_dead",
  "clients": 0,
  "mount": "/live.flac",
  "device": "hw:0,0",
  "ffmpeg_alive": false,
  "headers_cached": 0,
  "headers_complete": false
}
```

### Statistics — `GET /stats`

Rich statistics about the server, source, stream, and listeners.

```bash
curl http://localhost:8100/stats
```

```json
{
  "server": "lossless-stream/1.0.0",
  "uptime_seconds": 86400,
  "source": {
    "connected": true,
    "connected_since": "2026-03-25T10:00:00.000000+00:00",
    "device": "hw:0,0",
    "format": "FLAC",
    "samplerate": 48000,
    "bitdepth": 24,
    "channels": 2,
    "bytes_received": 1234567890
  },
  "stream": {
    "mount": "/live.flac",
    "content_type": "audio/flac"
  },
  "listeners": {
    "current": 1,
    "peak": 3,
    "total_connections": 12,
    "bytes_sent": 987654321
  }
}
```

| Field | Description |
|---|---|
| `uptime_seconds` | Seconds since the relay process started |
| `source.connected` | Whether ffmpeg is actively producing audio data |
| `source.connected_since` | ISO-8601 timestamp of when the current ffmpeg session started |
| `source.bytes_received` | Cumulative bytes received from ffmpeg (across all sessions) |
| `listeners.current` | Currently connected listener count |
| `listeners.peak` | Highest simultaneous listener count since startup |
| `listeners.total_connections` | Total listener connections since startup |
| `listeners.bytes_sent` | Cumulative bytes sent to all listeners |

### Metadata — `GET /metadata` and `PUT /metadata`

Out-of-band metadata API for querying and dynamically updating the stream title. This is purely informational and does not affect the audio stream.

**Get current metadata:**

```bash
curl http://localhost:8100/metadata
```

```json
{
  "title": "Audio Source",
  "source": "active"
}
```

**Update the title dynamically:**

```bash
curl -X PUT http://localhost:8100/metadata \
  -H "Content-Type: application/json" \
  -d '{"title": "Vinyl — Abbey Road"}'
```

```json
{
  "title": "Vinyl — Abbey Road",
  "updated": true
}
```

The title defaults to the `STREAM_NAME` environment variable and resets on process restart (changes are in-memory only).

### Stream — `GET /<MOUNT_POINT>`

The live FLAC audio stream. Connect any HTTP audio client or player:

```bash
# Play with ffplay
ffplay http://localhost:8100/live.flac

# Play with VLC
vlc http://localhost:8100/live.flac
```

**Response headers include:**

| Header | Value |
|---|---|
| `Content-Type` | `audio/flac` |
| `Cache-Control` | `no-cache` |
| `Pragma` | `no-cache` |
| `Expires` | `Mon, 26 Jul 1997 05:00:00 GMT` |
| `Access-Control-Allow-Origin` | `*` |
| `Server` | `lossless-stream/1.0.0` |

Plus any custom headers from `EXTRA_HEADERS`.

**Error responses:**

- **503** — listener limit reached: `{"error": "listener limit reached", "max": 10, "current": 10}`
- **503** — source not connected: `{"error": "source not connected", "status": "unavailable"}`

## Stream Recording

To record the raw stream to a file while simultaneously serving it to listeners:

```yaml
environment:
  RECORD_FILE: "/data/recording.flac"
volumes:
  - ./recordings:/data
```

The recording file is opened in append mode — if ffmpeg restarts, new data is appended to the same file. To start a fresh recording, delete or rename the file before starting the container.

## Custom HTTP Headers

For reverse proxy setups or special requirements, inject custom headers into stream responses via the `EXTRA_HEADERS` environment variable:

```yaml
environment:
  EXTRA_HEADERS: '{"X-Forwarded-Proto": "https", "X-Custom": "value"}'
```

These are parsed as a JSON object at startup and added to all stream responses.

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

### Slow client disconnections

- If clients are being dropped with queue limit warnings, increase `MAX_QUEUE_SIZE_KB` (default 512 KB)
- Alternatively, investigate the client's network — slow/stalled connections are dropped to protect server memory

### Source timeout / degraded health

- The `/health` endpoint shows `"status": "degraded"` with `"ffmpeg": "timeout"` when ffmpeg stops producing data
- Check your ALSA device and audio source — the capture may have stalled
- The relay will automatically recover when ffmpeg restarts and data resumes

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
      MAX_LISTENERS: "10"
      MAX_QUEUE_SIZE_KB: "512"
      SOURCE_TIMEOUT: "10"
```

Then run `docker compose up -d --build`.

## License

[MIT](LICENSE) — Saphira
