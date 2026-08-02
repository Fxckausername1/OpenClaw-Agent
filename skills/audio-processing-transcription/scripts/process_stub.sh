#!/usr/bin/env bash
set -euo pipefail
echo "audio-processing: process stub"
echo "Usage: process_stub.sh input.mp3 output_dir/"
if [ "$#" -lt 2 ]; then
  echo "provide input and output_dir"; exit 1
fi
in="$1"; out="$2"
echo "Would run: ffmpeg -i '$in' -ss 0 -t 30 -acodec copy '$out/preview.mp3'"

