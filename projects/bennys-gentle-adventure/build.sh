#!/usr/bin/env bash
# Builds "Benny's Gentle Adventure" end-to-end: narration + music -> frames -> final MP4.
# Requirements: python3 (numpy, soundfile, kokoro-onnx), node + playwright (Chromium), ffmpeg.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
export OUT="${OUT:-$HERE/out}"
export MODELS="${MODELS:-$HERE/models}"
mkdir -p "$OUT" "$MODELS"

# 1) Kokoro TTS model (open-weight, Apache-2.0) — downloaded once
KOKORO_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
[ -f "$MODELS/kokoro.onnx" ] || curl -fsSL -o "$MODELS/kokoro.onnx" "$KOKORO_URL/kokoro-v1.0.onnx"
[ -f "$MODELS/voices.bin" ] || curl -fsSL -o "$MODELS/voices.bin" "$KOKORO_URL/voices-v1.0.bin"

# 2) narration, music, mix, timeline, subtitles
python3 "$HERE/audio.py"

# 3) frames -> silent 1080p30 video
node "$HERE/render.mjs"

# 4) final mux: YouTube loudness target (-14 LUFS, -1.5 dBTP), AAC 192k, faststart
ffmpeg -y -loglevel error -i "$OUT/video.mp4" -i "$OUT/mix.wav" \
  -map 0:v -map 1:a -c:v copy \
  -af "loudnorm=I=-14:TP=-1.5:LRA=11" -c:a aac -b:a 192k -ar 48000 \
  -movflags +faststart -shortest \
  -metadata title="Benny's Gentle Adventure - A Calm & Joyful Journey" \
  "$OUT/Bennys_Gentle_Adventure_1080p.mp4"

# 5) YouTube thumbnail (1280x720) from the title card
ffmpeg -y -loglevel error -ss 3.2 -i "$OUT/video.mp4" -frames:v 1 -vf scale=1280:720 "$OUT/thumbnail.png"

echo "Done: $OUT/Bennys_Gentle_Adventure_1080p.mp4"
