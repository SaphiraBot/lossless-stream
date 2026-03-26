#!/usr/bin/env python3
"""
lossless-stream relay v1.0.0

Production-hardened audio stream relay with integrated ffmpeg capture.
Captures ALSA audio via ffmpeg, encodes to FLAC, and serves it over HTTP
to any number of concurrent listeners with per-client backpressure.

Features:
  - FLAC header caching for instant client decoder initialisation
  - Per-client write queues with overflow protection
  - Source timeout detection and graceful disconnect handling
  - Configurable max listener limit (503 on overflow)
  - Rich /stats and /metadata JSON APIs
  - /health with degraded-state reporting
  - Optional stream-to-file recording
  - CORS, cache-busting, and custom HTTP headers
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from aiohttp import web

# ── Version ───────────────────────────────────────────────────────────

VERSION = "1.0.0"
SERVER_TOKEN = f"lossless-stream/{VERSION}"

# ── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[relay] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────

ALSA_DEVICE       = os.environ.get("ALSA_DEVICE", "hw:0,0")
SAMPLE_RATE       = os.environ.get("SAMPLE_RATE", "48000")
CHANNELS          = os.environ.get("CHANNELS", "2")
BIT_DEPTH         = os.environ.get("BIT_DEPTH", "24")
PORT              = int(os.environ.get("RELAY_PORT", "8100"))
MOUNT             = os.environ.get("MOUNT_POINT", "live.flac")
STREAM_NAME       = os.environ.get("STREAM_NAME", "Audio Source")
CHUNK_SIZE        = int(os.environ.get("CHUNK_SIZE", "4096"))
THREAD_QUEUE_SIZE = os.environ.get("THREAD_QUEUE_SIZE", "4096")

# Per-client write-queue hard cap (bytes).  Clients that fall behind are dropped.
MAX_QUEUE_SIZE    = int(os.environ.get("MAX_QUEUE_SIZE_KB", "512")) * 1024
# Seconds of silence from ffmpeg before the source is considered timed-out.
SOURCE_TIMEOUT    = int(os.environ.get("SOURCE_TIMEOUT", "10"))
# Maximum simultaneous listeners (new connections get HTTP 503 when full).
MAX_LISTENERS     = int(os.environ.get("MAX_LISTENERS", "10"))
# Idle-connection / keep-alive timeout (seconds).
HEADER_TIMEOUT    = int(os.environ.get("HEADER_TIMEOUT", "15"))
# Optional path — if set, every byte from ffmpeg is also appended here.
RECORD_FILE       = os.environ.get("RECORD_FILE", "")
# JSON string of extra HTTP headers to add to stream responses.
EXTRA_HEADERS_RAW = os.environ.get("EXTRA_HEADERS", "")

# Parse extra headers at startup
extra_headers: dict[str, str] = {}
if EXTRA_HEADERS_RAW:
    try:
        parsed = json.loads(EXTRA_HEADERS_RAW)
        if isinstance(parsed, dict):
            extra_headers = {str(k): str(v) for k, v in parsed.items()}
        else:
            log.warning("EXTRA_HEADERS must be a JSON object — ignored")
    except json.JSONDecodeError as exc:
        log.warning(f"EXTRA_HEADERS parse error: {exc}")

# ── ffmpeg command ────────────────────────────────────────────────────

FFMPEG_CMD = [
    "ffmpeg",
    "-probesize", "32",
    "-analyzeduration", "0",
    "-fflags", "nobuffer",
    "-f", "alsa",
    "-thread_queue_size", THREAD_QUEUE_SIZE,
    "-ac", CHANNELS,
    "-ar", SAMPLE_RATE,
    "-i", ALSA_DEVICE,
    "-c:a", "flac",
    "-compression_level", "0",
    "-sample_fmt", "s32",
    "-frame_size", "1152",
    "-f", "flac",
    "-flush_packets", "1",
    "-",
]

FLAC_MAGIC = b"fLaC"

# ── Common HTTP headers for stream responses ─────────────────────────

STREAM_HEADERS: dict[str, str] = {
    "Content-Type": "audio/flac",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Expires": "Mon, 26 Jul 1997 05:00:00 GMT",
    "Access-Control-Allow-Origin": "*",
    "Connection": "keep-alive",
    "Server": SERVER_TOKEN,
}
STREAM_HEADERS.update(extra_headers)


# ── Global state ──────────────────────────────────────────────────────

clients: set["ClientConnection"] = set()
stream_headers_cache = bytearray()   # cached FLAC metadata blocks
headers_complete = False              # True once all metadata blocks parsed
ffmpeg_proc = None

source_connected = False
source_connected_since: str | None = None    # ISO-8601
last_data_time: float = 0                     # monotonic clock
source_timeout_detected = False

# Cumulative statistics
server_start_time: float = 0   # monotonic
bytes_received_total: int = 0
bytes_sent_total: int = 0
peak_listeners: int = 0
total_connections: int = 0

# Dynamic metadata (mutable via /metadata PUT)
stream_metadata: dict[str, str] = {"title": STREAM_NAME}

# Recording file handle (opened per ffmpeg session)
_record_fh = None


# ── Per-client connection wrapper ─────────────────────────────────────

class ClientConnection:
    """Wraps a StreamResponse with a bounded async write queue.

    Each connected listener gets its own ClientConnection.  The relay's
    broadcast loop pushes chunks into the queue (non-blocking); a dedicated
    writer coroutine drains the queue and writes to the HTTP response.
    If a client falls behind and the queue exceeds MAX_QUEUE_SIZE bytes,
    the client is forcibly disconnected to prevent unbounded memory growth.
    """

    __slots__ = (
        "response", "remote", "queue",
        "pending_bytes", "bytes_sent", "disconnected",
    )

    def __init__(self, response: web.StreamResponse, remote: str):
        self.response = response
        self.remote = remote
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.pending_bytes = 0
        self.bytes_sent = 0
        self.disconnected = False

    async def writer_loop(self):
        """Drain the queue and write to the HTTP response until closed."""
        global bytes_sent_total
        try:
            while not self.disconnected:
                data = await self.queue.get()
                if data is None:             # shutdown sentinel
                    break
                await self.response.write(data)
                self.pending_bytes -= len(data)
                self.bytes_sent += len(data)
                bytes_sent_total += len(data)
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug(f"Writer error for {self.remote}: {exc}")
        finally:
            self.disconnected = True

    def enqueue(self, data: bytes) -> bool:
        """Non-blocking enqueue.  Returns False when the client's buffer
        has exceeded MAX_QUEUE_SIZE and the client should be disconnected."""
        if self.disconnected:
            return False
        if self.pending_bytes + len(data) > MAX_QUEUE_SIZE:
            return False
        self.queue.put_nowait(data)
        self.pending_bytes += len(data)
        return True

    async def close(self):
        """Signal the writer loop to stop."""
        self.disconnected = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


# ── Broadcast helpers ─────────────────────────────────────────────────

async def broadcast(data: bytes):
    """Enqueue *data* to every connected client; drop slow ones."""
    dead: set[ClientConnection] = set()
    for client in list(clients):
        if not client.enqueue(data):
            log.warning(
                f"Slow client {client.remote} exceeded queue limit "
                f"({MAX_QUEUE_SIZE // 1024} KB) — disconnecting"
            )
            await client.close()
            dead.add(client)
    clients.difference_update(dead)


async def disconnect_all_clients(reason: str = "source disconnected"):
    """Gracefully close every listener connection."""
    if not clients:
        return
    log.info(f"Disconnecting {len(clients)} listener(s): {reason}")
    for client in list(clients):
        await client.close()
    clients.clear()


# ── Source timeout watchdog ───────────────────────────────────────────

async def source_timeout_monitor():
    """Fires once per second; flags source-timeout when ffmpeg goes quiet."""
    global source_timeout_detected
    while True:
        await asyncio.sleep(1)
        if source_connected and last_data_time > 0:
            elapsed = time.monotonic() - last_data_time
            if elapsed > SOURCE_TIMEOUT and not source_timeout_detected:
                source_timeout_detected = True
                log.warning(
                    f"Source timeout: no data for {elapsed:.1f}s "
                    f"(threshold: {SOURCE_TIMEOUT}s)"
                )
                await disconnect_all_clients("source timeout")


# ── Stream processing ────────────────────────────────────────────────

async def process_stream(proc):
    """
    Read from ffmpeg stdout in native FLAC format.
    Phase 1 — Buffer fLaC magic + all metadata blocks as headers.
    Phase 2 — Broadcast audio frames to all connected listeners.
    """
    global stream_headers_cache, headers_complete, bytes_received_total
    global last_data_time, source_timeout_detected, _record_fh

    raw = bytearray()
    headers_complete = False
    stream_headers_cache = bytearray()

    log.info("Stream processing started")

    # Open recording file if configured (append for multi-session continuity)
    if RECORD_FILE:
        try:
            _record_fh = open(RECORD_FILE, "ab")
            log.info(f"Recording stream to: {RECORD_FILE}")
        except OSError as exc:
            log.error(f"Cannot open recording file {RECORD_FILE}: {exc}")
            _record_fh = None

    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                log.warning("ffmpeg stdout EOF — pipe closed")
                break

            last_data_time = time.monotonic()
            source_timeout_detected = False
            bytes_received_total += len(chunk)

            # Tee to recording file
            if _record_fh:
                try:
                    _record_fh.write(chunk)
                    _record_fh.flush()
                except OSError as exc:
                    log.error(f"Recording write error: {exc}")
                    _record_fh = None

            raw.extend(chunk)

            # ── Phase 1: parse FLAC metadata blocks ───────────────
            if not headers_complete:
                if len(raw) < 4:
                    continue
                if raw[:4] != FLAC_MAGIC:
                    log.error(
                        f"Expected fLaC magic, got: {raw[:4].hex()}"
                    )
                    break

                offset = 4
                while offset < len(raw):
                    if offset + 4 > len(raw):
                        break  # need more data for block header

                    is_last = bool(raw[offset] & 0x80)
                    block_type = raw[offset] & 0x7F
                    data_len = (
                        (raw[offset + 1] << 16)
                        | (raw[offset + 2] << 8)
                        | raw[offset + 3]
                    )
                    block_end = offset + 4 + data_len

                    if block_end > len(raw):
                        break  # need more data for block body

                    log.info(
                        f"FLAC metadata block: type={block_type}, "
                        f"size={data_len}, last={is_last}"
                    )

                    if is_last:
                        stream_headers_cache = bytearray(raw[:block_end])
                        headers_complete = True
                        log.info(
                            f"FLAC headers complete "
                            f"({len(stream_headers_cache)} bytes cached)"
                        )
                        remaining = bytes(raw[block_end:])
                        raw = bytearray()
                        if remaining:
                            await broadcast(remaining)
                        break

                    offset = block_end

            # ── Phase 2: relay audio frames ───────────────────────
            else:
                await broadcast(bytes(raw))
                raw = bytearray()

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.error(f"Stream processing error: {exc}")
    finally:
        if _record_fh:
            try:
                _record_fh.close()
            except OSError:
                pass
            _record_fh = None


async def start_ffmpeg():
    """Launch ffmpeg with exponential-backoff restarts."""
    global ffmpeg_proc, stream_headers_cache, headers_complete
    global source_connected, source_connected_since
    global last_data_time, source_timeout_detected

    backoff = 1
    max_backoff = 60

    while True:
        log.info(f"Starting ffmpeg: {' '.join(FFMPEG_CMD[:8])}...")
        try:
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                *FFMPEG_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Reset state on each (re)start
            stream_headers_cache = bytearray()
            headers_complete = False
            source_connected = True
            source_connected_since = datetime.now(timezone.utc).isoformat()
            last_data_time = time.monotonic()
            source_timeout_detected = False

            await process_stream(ffmpeg_proc)
            backoff = 1  # reset after a clean run
        except Exception as exc:
            log.error(f"ffmpeg error: {exc}")

        # Source is gone
        source_connected = False
        source_timeout_detected = False
        log.warning("ffmpeg exited — source disconnected")
        await disconnect_all_clients("ffmpeg exited")

        log.warning(f"Restarting ffmpeg in {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


# ── HTTP handlers ─────────────────────────────────────────────────────

async def handle_stream(request: web.Request) -> web.StreamResponse:
    """Serve the live FLAC stream to a single listener."""
    global peak_listeners, total_connections, bytes_sent_total

    # ── Gate: max listeners ───────────────────────────────────
    if len(clients) >= MAX_LISTENERS:
        log.warning(
            f"Listener rejected (limit {MAX_LISTENERS}): {request.remote}"
        )
        return web.json_response(
            {
                "error": "listener limit reached",
                "max": MAX_LISTENERS,
                "current": len(clients),
            },
            status=503,
        )

    # ── Gate: source must be connected with headers ready ─────
    if not source_connected or not headers_complete:
        return web.json_response(
            {"error": "source not connected", "status": "unavailable"},
            status=503,
        )

    log.info(f"Client connected: {request.remote}")
    resp = web.StreamResponse(status=200, headers=dict(STREAM_HEADERS))
    await resp.prepare(request)

    client = ClientConnection(resp, request.remote)

    # Replay cached FLAC metadata so the decoder can initialise immediately
    if stream_headers_cache:
        try:
            await resp.write(bytes(stream_headers_cache))
            client.bytes_sent += len(stream_headers_cache)
            bytes_sent_total += len(stream_headers_cache)
            log.info(
                f"Sent {len(stream_headers_cache)} header bytes "
                f"to {request.remote}"
            )
        except Exception as exc:
            log.warning(f"Header send failed for {request.remote}: {exc}")
            return resp

    # Register and update stats
    clients.add(client)
    total_connections += 1
    peak_listeners = max(peak_listeners, len(clients))
    log.info(
        f"Client streaming: {request.remote}, total: {len(clients)}"
    )

    try:
        await client.writer_loop()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.debug(f"Client {request.remote} error: {exc}")
    finally:
        clients.discard(client)
        log.info(
            f"Client gone: {request.remote}, "
            f"remaining: {len(clients)}"
        )

    return resp


async def handle_health(request: web.Request) -> web.Response:
    """Health-check endpoint with degraded-state awareness."""
    ffmpeg_alive = ffmpeg_proc is not None and ffmpeg_proc.returncode is None

    if source_timeout_detected:
        status_text = "degraded"
        status_code = 503
    elif ffmpeg_alive and source_connected:
        status_text = "ok"
        status_code = 200
    else:
        status_text = "ffmpeg_dead"
        status_code = 503

    body: dict = {
        "status": status_text,
        "clients": len(clients),
        "mount": f"/{MOUNT}",
        "device": ALSA_DEVICE,
        "ffmpeg_alive": ffmpeg_alive,
        "headers_cached": len(stream_headers_cache),
        "headers_complete": headers_complete,
    }
    if source_timeout_detected:
        body["ffmpeg"] = "timeout"

    return web.json_response(body, status=status_code)


async def handle_stats(request: web.Request) -> web.Response:
    """Rich statistics endpoint."""
    uptime = time.monotonic() - server_start_time if server_start_time else 0

    return web.json_response({
        "server": SERVER_TOKEN,
        "uptime_seconds": round(uptime),
        "source": {
            "connected": source_connected and not source_timeout_detected,
            "connected_since": source_connected_since,
            "device": ALSA_DEVICE,
            "format": "FLAC",
            "samplerate": int(SAMPLE_RATE),
            "bitdepth": int(BIT_DEPTH),
            "channels": int(CHANNELS),
            "bytes_received": bytes_received_total,
        },
        "stream": {
            "mount": f"/{MOUNT}",
            "content_type": "audio/flac",
        },
        "listeners": {
            "current": len(clients),
            "peak": peak_listeners,
            "total_connections": total_connections,
            "bytes_sent": bytes_sent_total,
        },
    })


async def handle_metadata(request: web.Request) -> web.Response:
    """
    GET  /metadata  — current stream metadata as JSON.
    PUT  /metadata  — update the title dynamically (in-memory only).
    """
    if request.method == "GET":
        return web.json_response({
            "title": stream_metadata["title"],
            "source": "active" if source_connected else "inactive",
        })

    # PUT — update title
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body"}, status=400
        )

    if "title" in data and isinstance(data["title"], str):
        stream_metadata["title"] = data["title"]
        log.info(f"Metadata updated: title={data['title']!r}")
        return web.json_response({
            "title": stream_metadata["title"],
            "updated": True,
        })

    return web.json_response(
        {"error": "missing or invalid 'title' field"}, status=400
    )


# ── Middleware ─────────────────────────────────────────────────────────

@web.middleware
async def server_header_middleware(request: web.Request, handler):
    """Attach the Server identification header to every response."""
    resp = await handler(request)
    resp.headers.setdefault("Server", SERVER_TOKEN)
    return resp


# ── Application bootstrap ────────────────────────────────────────────

async def on_startup(app: web.Application):
    global server_start_time
    server_start_time = time.monotonic()
    asyncio.create_task(start_ffmpeg())
    asyncio.create_task(source_timeout_monitor())
    log.info(f"Relay started on :{PORT}, mount=/{MOUNT}")


app = web.Application(
    middlewares=[server_header_middleware],
    client_max_size=64 * 1024,  # 64 KB max request body (for metadata PUT)
)
app.router.add_get(f"/{MOUNT}", handle_stream)
app.router.add_get("/health", handle_health)
app.router.add_get("/stats", handle_stats)
app.router.add_get("/metadata", handle_metadata)
app.router.add_put("/metadata", handle_metadata)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    log.info(
        f"Starting: ALSA={ALSA_DEVICE} SR={SAMPLE_RATE} "
        f"CH={CHANNELS} BD={BIT_DEPTH}"
    )
    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT,
        access_log=None,
        keepalive_timeout=HEADER_TIMEOUT,
    )
