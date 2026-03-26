#!/bin/bash
set -euo pipefail

export ALSA_DEVICE="${ALSA_DEVICE:-hw:0,0}"
export SAMPLE_RATE="${SAMPLE_RATE:-48000}"
export CHANNELS="${CHANNELS:-2}"
export BIT_DEPTH="${BIT_DEPTH:-24}"
export RELAY_PORT="${RELAY_PORT:-8100}"
export MOUNT_POINT="${MOUNT_POINT:-live.flac}"
export STREAM_NAME="${STREAM_NAME:-Turntable}"

echo "[entrypoint] Starting lossless-stream"
echo "[entrypoint] ALSA=$ALSA_DEVICE SR=$SAMPLE_RATE CH=$CHANNELS BD=$BIT_DEPTH PORT=$RELAY_PORT"

# Run the relay (it manages ffmpeg internally)
exec python3 /relay.py
