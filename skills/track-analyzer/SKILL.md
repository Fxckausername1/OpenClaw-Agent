---
name: track-analyzer
description: Analyze a song's mix/master from a URL or audio file — loudness (LUFS), true peak, dynamics (LRA), clipping — and judge whether it needs mixing/mastering. Use whenever the user sends a song link (SoundCloud/Bandcamp/direct audio) or an audio file and asks to "listen to", analyze, or check a track.
---

# Track Analyzer

When the user sends a song link or audio file and wants it analyzed / "listened to", run from the workspace root:

```
./venv/bin/python analyze_track.py "<url-or-file>"
```

Then relay the printed report back to the user: loudness (LUFS), true peak (dBTP),
loudness range (LRA), sample peak/RMS, and the engineer's read plus a casual DM
hook for outreach. Add `--no-hook` to skip the DM line.

Notes:
- Works on **SoundCloud, Bandcamp, direct audio URLs (mp3/wav), and local files**.
- **YouTube** is bot-blocked from this datacenter IP. If a YouTube link fails, ask
  the user for a SoundCloud/Bandcamp/direct link, or set up YouTube cookies at
  `credentials/youtube_cookies.txt` (Netscape cookie file) — the script auto-uses it.
- **Spotify/Apple** links are DRM-protected and cannot be downloaded.
- FFmpeg *measures* audio (loudness, peaks, dynamics); it does not subjectively
  judge how "good" a song sounds.
