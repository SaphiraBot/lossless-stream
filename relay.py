#!/usr/bin/env python3
"""
Audio stream relay with integrated ffmpeg capture.
Caches FLAC stream headers and replays them to new clients.
No ICY metadata, no burst buffering beyond headers.
"""
import asyncio
import logging
import os
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="[relay] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# Config from environment
ALSA_DEVICE = os.environ.get("ALSA_DEVICE", "hw:0,0")
SAMPLE_RATE = os.environ.get("SAMPLE_RATE", "48000")
CHANNELS = os.environ.get("CHANNELS", "2")
BIT_DEPTH = os.environ.get("BIT_DEPTH", "24")
PORT = int(os.environ.get("RELAY_PORT", "8100"))
MOUNT = os.environ.get("MOUNT_POINT", "live.flac")
STREAM_NAME = os.environ.get("STREAM_NAME", "Turntable")
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "4096"))
THREAD_QUEUE_SIZE = os.environ.get("THREAD_QUEUE_SIZE", "4096")

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
    "-"
]

FLAC_MAGIC = b"fLaC"

clients = set()
stream_headers = bytearray()  # cached FLAC metadata to send to new clients
headers_complete = False       # True once all metadata blocks are cached
ffmpeg_proc = None


async def process_stream(proc):
    """
    Read from ffmpeg stdout (native FLAC format).
    - Buffer the fLaC magic + all metadata blocks as headers
    - Once last metadata block is found, broadcast audio frames to clients
    """
    global stream_headers, headers_complete, ffmpeg_proc

    raw_buffer = bytearray()
    headers_complete = False
    stream_headers = bytearray()

    log.info("Stream processing started")

    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                log.warning("ffmpeg stdout EOF")
                break

            raw_buffer.extend(chunk)

            if not headers_complete:
                # Need at least the fLaC magic
                if len(raw_buffer) < 4:
                    continue

                if raw_buffer[:4] != FLAC_MAGIC:
                    log.error(f"Expected fLaC magic, got: {raw_buffer[:4].hex()}")
                    break

                # Parse metadata blocks after the 4-byte magic
                offset = 4
                parsing_ok = True

                while offset < len(raw_buffer):
                    # Each metadata block: 4-byte header + data
                    if offset + 4 > len(raw_buffer):
                        parsing_ok = False
                        break  # need more data for block header

                    is_last = bool(raw_buffer[offset] & 0x80)
                    block_type = raw_buffer[offset] & 0x7F
                    data_len = (
                        (raw_buffer[offset + 1] << 16)
                        | (raw_buffer[offset + 2] << 8)
                        | raw_buffer[offset + 3]
                    )

                    block_end = offset + 4 + data_len
                    if block_end > len(raw_buffer):
                        parsing_ok = False
                        break  # need more data for block body

                    log.info(
                        f"FLAC metadata block: type={block_type}, "
                        f"size={data_len}, last={is_last}"
                    )

                    if is_last:
                        # Cache everything from start through end of last block
                        stream_headers = bytearray(raw_buffer[:block_end])
                        headers_complete = True
                        log.info(
                            f"FLAC headers complete "
                            f"({len(stream_headers)} bytes cached)"
                        )

                        # Broadcast leftover audio data after headers
                        remaining = bytes(raw_buffer[block_end:])
                        raw_buffer = bytearray()
                        if remaining:
                            await broadcast(remaining)
                        break

                    offset = block_end

                if not headers_complete and parsing_ok:
                    # All parsed but no last-block flag yet — wait for more
                    pass

            else:
                # Headers already captured — broadcast directly
                await broadcast(bytes(raw_buffer))
                raw_buffer = bytearray()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"Stream processing error: {e}")


async def broadcast(data):
    """Send data to all connected clients."""
    dead = set()
    for client in list(clients):
        try:
            await client.write(data)
        except Exception:
            dead.add(client)
    clients.difference_update(dead)


async def start_ffmpeg():
    global ffmpeg_proc, stream_headers, headers_complete
    backoff = 1
    max_backoff = 60

    while True:
        log.info(f"Starting ffmpeg: {' '.join(FFMPEG_CMD[:8])}...")
        try:
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                *FFMPEG_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            # Reset header cache on restart
            stream_headers = bytearray()
            headers_complete = False
            await process_stream(ffmpeg_proc)
            backoff = 1  # reset on clean exit
        except Exception as e:
            log.error(f"ffmpeg error: {e}")

        log.warning(f"ffmpeg exited, restarting in {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


async def handle_stream(request):
    log.info(f"Client connected: {request.remote}")
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/flac",
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
        }
    )
    await resp.prepare(request)

    # Send cached FLAC headers first so client can initialise decoder
    if stream_headers:
        try:
            await resp.write(bytes(stream_headers))
            log.info(
                f"Sent {len(stream_headers)} bytes of cached headers "
                f"to {request.remote}"
            )
        except Exception as e:
            log.warning(
                f"Failed to send headers to {request.remote}: {e}"
            )
            return resp

    clients.add(resp)
    log.info(
        f"Client streaming: {request.remote}, total: {len(clients)}"
    )

    try:
        await asyncio.sleep(3600 * 24)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.debug(f"Client {request.remote} error: {e}")
    finally:
        clients.discard(resp)
        log.info(
            f"Client gone: {request.remote}, "
            f"remaining: {len(clients)}"
        )

    return resp


async def handle_health(request):
    ffmpeg_alive = (
        ffmpeg_proc is not None and ffmpeg_proc.returncode is None
    )
    status_code = 200 if ffmpeg_alive else 503
    status_text = "ok" if ffmpeg_alive else "ffmpeg_dead"
    return web.Response(
        status=status_code,
        text=(
            f'{{"status":"{status_text}",'
            f'"clients":{len(clients)},'
            f'"mount":"/{MOUNT}",'
            f'"device":"{ALSA_DEVICE}",'
            f'"ffmpeg_alive":{str(ffmpeg_alive).lower()},'
            f'"headers_cached":{len(stream_headers)},'
            f'"headers_complete":{str(headers_complete).lower()}}}'
        ),
        content_type="application/json"
    )


async def on_startup(app):
    asyncio.create_task(start_ffmpeg())
    log.info(f"Relay started on port {PORT}, mount=/{MOUNT}")


app = web.Application()
app.router.add_get(f"/{MOUNT}", handle_stream)
app.router.add_get("/health", handle_health)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    log.info(
        f"Starting: ALSA={ALSA_DEVICE} SR={SAMPLE_RATE} "
        f"CH={CHANNELS} BD={BIT_DEPTH}"
    )
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
