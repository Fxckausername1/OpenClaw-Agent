---
name: audio-processing-transcription
description: Extract frames, generate previews, and transcribe audio using ffmpeg + a transcription model (Whisper or similar). Scaffolding only; heavy workloads should run as isolated tasks.
user-invocable: true
---

Included:
- scripts/process_stub.sh — ffmpeg-based extraction stub.
- scripts/transcribe_stub.mjs — transcription stub (calls external model or local tool).

Notes: Transcription providers may incur cost; configure via env vars.

