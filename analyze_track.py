#!/usr/bin/env python3
"""Technical mix/master analysis of a song from a URL (or local file).

Pipeline: yt-dlp downloads the audio (YouTube/SoundCloud/etc.) -> FFmpeg measures
integrated loudness (LUFS), true peak (dBTP), loudness range (LRA), sample peak,
RMS, and clipping -> produces an audio-engineer's read on whether the track needs
mixing/mastering, plus a casual DM hook for outreach.

Usage: analyze_track.py <url-or-localfile> [--title "Name"]
Note: FFmpeg measures audio, it doesn't subjectively judge it. Spotify/Apple
links are DRM-protected and can't be downloaded — use a YouTube/SoundCloud link.
"""
import os
import re
import sys
import json
import shutil
import tempfile
import argparse
import subprocess
from pathlib import Path

_VYT = Path(__file__).resolve().parent / 'venv' / 'bin' / 'yt-dlp'
YTDLP = str(_VYT) if _VYT.exists() else 'yt-dlp'
STREAMING_LUFS = -14.0   # Spotify/Apple/YouTube normalization target
TP_CEILING = -1.0        # standard mastering true-peak ceiling


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def download(url, tmp):
    cmd = [YTDLP, "-f", "bestaudio/best", "--no-playlist", "--no-progress",
           "-o", f"{tmp}/track.%(ext)s", "--print", "after_move:filepath", "--print", "title"]
    ck = Path(__file__).resolve().parent / "credentials" / "youtube_cookies.txt"
    if ck.exists():
        cmd += ["--cookies", str(ck)]
    px = Path(__file__).resolve().parent / "credentials" / "proxy.txt"
    if px.exists():
        cmd += ["--proxy", px.read_text().strip(),
                "--extractor-args", "youtube:formats=missing_pot"]
    cmd.append(url)
    out = run(cmd, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"download failed: {out.stderr.strip().splitlines()[-1] if out.stderr else 'unknown'}")
    path, title = None, None
    for line in out.stdout.strip().splitlines():
        line = line.strip()
        if os.path.exists(line):
            path = line
        elif line:
            title = line
    if not path:
        raise RuntimeError("could not locate downloaded audio")
    return path, (title or "track")


def ffprobe(path):
    out = run(["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", path])
    try:
        d = json.loads(out.stdout)
    except Exception:
        return {}
    fmt = d.get("format", {})
    astream = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "duration": float(fmt.get("duration", 0) or 0),
        "bitrate": int(fmt.get("bit_rate", 0) or 0),
        "sample_rate": astream.get("sample_rate", "?"),
        "channels": astream.get("channels", "?"),
        "codec": astream.get("codec_name", "?"),
    }


def loudnorm(path):
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
               "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
               "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", out.stderr, re.S)
    if not m:
        return {}
    try:
        j = json.loads(m.group(0))
    except Exception:
        return {}
    g = lambda k: float(j.get(k)) if j.get(k) not in (None, "", "-inf") else None
    return {"lufs": g("input_i"), "tp": g("input_tp"), "lra": g("input_lra")}


def astats(path):
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
               "-af", "astats=metadata=0", "-f", "null", "-"])
    txt = out.stderr

    def last(label):
        vals = re.findall(rf"{label}:\s*(-?\d+\.?\d*)", txt)
        return float(vals[-1]) if vals else None
    return {"peak_db": last("Peak level dB"), "rms_db": last("RMS level dB"),
            "flat": last("Flat factor")}


def interpret(m):
    lufs, tp, lra = m.get("lufs"), m.get("tp"), m.get("lra")
    reads, flags = [], []

    if lufs is not None:
        if lufs > -9:
            reads.append(f"Loudness {lufs:.1f} LUFS — very hot. Streaming normalizes to {STREAMING_LUFS:.0f}, "
                         f"so this gets turned down ~{abs(lufs - STREAMING_LUFS):.0f} dB and loses punch.")
            flags.append("over-compressed")
        elif lufs > -12:
            reads.append(f"Loudness {lufs:.1f} LUFS — a bit hot vs the {STREAMING_LUFS:.0f} streaming target.")
        elif lufs >= -16:
            reads.append(f"Loudness {lufs:.1f} LUFS — right in the streaming sweet spot (~{STREAMING_LUFS:.0f}).")
        else:
            reads.append(f"Loudness {lufs:.1f} LUFS — quiet; could be brought up toward {STREAMING_LUFS:.0f} for competitive level.")
            flags.append("low level")

    if tp is not None:
        if tp > 0:
            reads.append(f"True peak {tp:+.1f} dBTP — CLIPPING (over 0). Adds distortion; needs a limiter ceiling at {TP_CEILING:.0f}.")
            flags.append("clipping")
        elif tp > TP_CEILING:
            reads.append(f"True peak {tp:+.1f} dBTP — no headroom; intersample-clipping risk. Master to {TP_CEILING:.0f} dBTP.")
            flags.append("no headroom")
        else:
            reads.append(f"True peak {tp:+.1f} dBTP — safe headroom.")

    if lra is not None:
        if lra < 4:
            reads.append(f"Loudness range {lra:.1f} LU — very compressed/flat, little dynamic movement.")
            flags.append("flat dynamics")
        elif lra <= 9:
            reads.append(f"Loudness range {lra:.1f} LU — moderate dynamics.")
        else:
            reads.append(f"Loudness range {lra:.1f} LU — wide, dynamic.")

    needs = [f for f in flags if f in ("over-compressed", "clipping", "no headroom", "flat dynamics")]
    verdict = ("Prime candidate for a proper master." if needs
               else "Technically solid — clean master already.")
    return reads, flags, verdict


def dm_hook(title, flags, verdict):
    issues = ", ".join(flags) or "a couple small things"
    prompt = (f"Write ONE casual Instagram DM line (under 35 words, no emojis, no quotes) from an audio "
              f"engineer who just analyzed an artist's track '{title}'. Findings: {issues}. Offer a free "
              f"sample master. Sound like a real person, mention a specific finding.")
    try:
        r = run(["openclaw", "infer", "model", "run", "--prompt", prompt, "--json"], timeout=60)
        txt = (json.loads(r.stdout).get("outputs") or [{}])[0].get("text", "").strip()
        if txt:
            return txt
    except Exception:
        pass
    return (f"yo just ran your track '{title}' through my tools — it's {issues}. "
            f"i could master it to hit clean + loud for streaming, want a free sample?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--title", default=None)
    ap.add_argument("--no-hook", action="store_true")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="track_")
    cleanup = True
    try:
        if os.path.exists(args.src):
            path, title = args.src, (args.title or Path(args.src).stem)
            cleanup = False
        else:
            print("Downloading audio...", file=sys.stderr)
            path, title = download(args.src, tmp)
            title = args.title or title

        info = ffprobe(path)
        m = {**loudnorm(path), **astats(path)}
        reads, flags, verdict = interpret(m)

        dur = info.get("duration", 0)
        mins = f"{int(dur // 60)}m{int(dur % 60):02d}s" if dur else "?"
        line2 = (f"  {mins} · {info.get('sample_rate','?')}Hz · "
                 f"{info.get('channels','?')}ch · {info.get('codec','?')}")

        print(f"\U0001F39B️ Track analysis — {title}")
        if m.get("lufs") is not None:
            print(f"  Loudness {m['lufs']:.1f} LUFS | True peak {m['tp']:+.1f} dBTP | "
                  f"LRA {m['lra']:.1f} LU" if m.get("tp") is not None and m.get("lra") is not None
                  else f"  Loudness {m['lufs']:.1f} LUFS")
        if m.get("peak_db") is not None:
            print(f"  Sample peak {m['peak_db']:.1f} dB | RMS {m['rms_db']:.1f} dB")
        print(line2)
        print(f"\n  Read: {verdict}")
        for r in reads:
            print(f"   • {r}")
        if not args.no_hook:
            print(f"\n  DM hook: {dm_hook(title, flags, verdict)}")
    finally:
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()


# ---- programmatic API (used by the artist pipeline) ----
def is_analyzable(url):
    u = (url or "").lower()
    if not u:
        return False
    if any(x in u for x in ("spotify.com", "music.apple.com", "linktr.ee",
                            "lnk.to", "lynkify", "posh.vip", "instagram.com",
                            "tiktok.com", "facebook.com")):
        return False
    if any(x in u for x in ("youtube.com", "youtu.be", "soundcloud.com", "bandcamp.com")):
        return True
    return u.rsplit("?", 1)[0].endswith((".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"))


def short_summary(res):
    if not res:
        return ""
    m = res.get("metrics", {})
    bits = []
    if m.get("lufs") is not None:
        bits.append(f"{m['lufs']:.1f} LUFS")
    if m.get("tp") is not None:
        bits.append(f"{m['tp']:+.1f} dBTP")
    for f in ("clipping", "over-compressed", "no headroom", "flat dynamics", "low level"):
        if f in res.get("flags", []):
            bits.append(f)
    return ", ".join(bits)


def analyze_url(src, want_hook=False):
    tmp = tempfile.mkdtemp(prefix="track_")
    cleanup = True
    try:
        if os.path.exists(src):
            path, title = src, Path(src).stem
            cleanup = False
        else:
            path, title = download(src, tmp)
        m = {**loudnorm(path), **astats(path)}
        if m.get("lufs") is None and m.get("peak_db") is None:
            return None
        reads, flags, verdict = interpret(m)
        res = {"title": title, "metrics": m, "flags": flags,
               "verdict": verdict, "reads": reads, "info": ffprobe(path)}
        if want_hook:
            res["hook"] = dm_hook(title, flags, verdict)
        return res
    except Exception:
        return None
    finally:
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)
