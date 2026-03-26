#!/bin/bash
set -euo pipefail

export ALSA_DEVICE="${ALSA_DEVICE:-hw:0,0}"
export SAMPLE_RATE="${SAMPLE_RATE:-48000}"
export CHANNELS="${CHANNELS:-2}"
export BIT_DEPTH="${BIT_DEPTH:-24}"
export RELAY_PORT="${RELAY_PORT:-8100}"
export MOUNT_POINT="${MOUNT_POINT:-live.flac}"
export STREAM_NAME="${STREAM_NAME:-Audio Source}"
export MAX_QUEUE_SIZE_KB="${MAX_QUEUE_SIZE_KB:-512}"
export SOURCE_TIMEOUT="${SOURCE_TIMEOUT:-10}"
export MAX_LISTENERS="${MAX_LISTENERS:-10}"
export HEADER_TIMEOUT="${HEADER_TIMEOUT:-15}"

echo "[entrypoint] Starting lossless-stream v1.0.0"
echo "[entrypoint] ALSA=$ALSA_DEVICE SR=$SAMPLE_RATE CH=$CHANNELS BD=$BIT_DEPTH PORT=$RELAY_PORT"

# Run the relay (it manages ffmpeg internally)
exec python3 /relay.py
