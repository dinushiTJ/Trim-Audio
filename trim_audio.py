#!/usr/bin/env python3
"""AudioMultiCut — cut multiple segments out of a long recording.

Runs a small local web server and opens an editor in your browser: waveform,
playback, marking, and per-segment fine-tuning. Everything stays on this
machine; audio is never uploaded anywhere.

Usage:  python3 trim_audio.py
"""

import array
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

HERE = Path(__file__).resolve().parent

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".m4b", ".mp4"}

# Folders searched when resolving a dropped file to a real path on disk.
SEARCH_DIRS = [
    Path.home() / "Desktop",
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Movies",
    Path.home() / "Music",
    HERE,
]

# How deep to walk inside each search dir.
SEARCH_DEPTH = 3

# Sample rate the waveform is decoded at — plenty for drawing peaks.
PEAK_RATE = 8000

# Python's mimetypes guesses audio/mp4a-latm for .m4a, which browsers refuse to
# play. These are the types the <audio> element actually understands.
AUDIO_TYPES = {
    ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
}


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------

def check_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        print(f"Missing required tool(s): {', '.join(missing)}\n")
        print("Install them with:\n\n    brew install ffmpeg\n")
        sys.exit(1)


def probe_duration(path):
    """Return the duration of an audio file in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError("Could not read the duration of this file.")


def probe_info(path):
    """Duration, sample rate and channel count, for the header line."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels",
         "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        raise RuntimeError("Could not read the duration of this file.")
    return {
        "duration": duration,
        "sample_rate": int(streams[0].get("sample_rate") or 0),
        "channels": int(streams[0].get("channels") or 0),
    }


# Matches ffmpeg's progress lines, e.g. "time=00:12:34.56"
TIME_RE = re.compile(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")


# --------------------------------------------------------------------------
# waveform peaks
# --------------------------------------------------------------------------

_peaks_cache = {}
_peaks_lock = threading.Lock()


def compute_peaks(path, buckets=2000):
    """Return `buckets` [min, max] pairs, each normalised to -1..1."""
    key = (str(path), path.stat().st_mtime, buckets)
    with _peaks_lock:
        if key in _peaks_cache:
            return _peaks_cache[key]

    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-ac", "1", "-ar", str(PEAK_RATE), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    raw = proc.stdout.read()
    err = proc.stderr.read().decode(errors="replace")
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(err.strip() or "ffmpeg could not decode this file")

    samples = array.array("h")
    samples.frombytes(raw[:len(raw) - (len(raw) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()

    total = len(samples)
    if total == 0:
        raise RuntimeError("This file contains no audio.")

    step = max(total // buckets, 1)
    peaks = []
    for i in range(0, total, step):
        window = samples[i:i + step]
        peaks.append([min(window) / 32768.0, max(window) / 32768.0])
    peaks = peaks[:buckets]

    with _peaks_lock:
        _peaks_cache.clear()  # only ever need the file currently open
        _peaks_cache[key] = peaks
    return peaks


# --------------------------------------------------------------------------
# silence detection
# --------------------------------------------------------------------------

SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def detect_silence(path, noise_db=-35.0, min_len=1.5):
    """Return [{start, end, duration}] for silent stretches in the file."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_len}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    regions, start = [], None
    for line in proc.stderr.splitlines():
        m = SILENCE_START_RE.search(line)
        if m:
            start = max(float(m.group(1)), 0.0)
            continue
        m = SILENCE_END_RE.search(line)
        if m and start is not None:
            end = float(m.group(1))
            if end > start:
                regions.append({"start": round(start, 3),
                                "end": round(end, 3),
                                "duration": round(end - start, 3)})
            start = None
    if start is not None:  # silence running to the end of the file
        total = probe_duration(path)
        if total > start:
            regions.append({"start": round(start, 3), "end": round(total, 3),
                            "duration": round(total - start, 3)})
    return regions


def invert_regions(total, regions, min_keep=0.5):
    """The gaps between `regions` — i.e. the audible parts."""
    out, pos = [], 0.0
    for r in sorted(regions, key=lambda x: x["start"]):
        s, e = max(r["start"], 0.0), min(r["end"], total)
        if s - pos >= min_keep:
            out.append({"start": round(pos, 3), "end": round(s, 3)})
        pos = max(pos, e)
    if total - pos >= min_keep:
        out.append({"start": round(pos, 3), "end": round(total, 3)})
    return out


# --------------------------------------------------------------------------
# cutting
# --------------------------------------------------------------------------

BAD_NAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_name(name, fallback="segment"):
    """Turn a user-typed segment name into a usable filename stem."""
    name = BAD_NAME_CHARS.sub("", str(name or "")).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def unique_names(segments):
    """Sanitised, de-duplicated stems, in order."""
    seen, out = {}, []
    for i, seg in enumerate(segments, start=1):
        stem = safe_name(seg.get("name"), f"segment {i}")
        n = seen.get(stem.lower(), 0) + 1
        seen[stem.lower()] = n
        out.append(stem if n == 1 else f"{stem}-{n}")
    return out


def codec_args(src, out_fmt):
    """Codec args and output extension for a cut."""
    if out_fmt == "mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"], ".mp3"
    ext = src.suffix.lower()
    if ext in (".m4a", ".m4b", ".mp4", ".aac"):
        return ["-c", "copy"], ".m4a"
    return ["-c", "copy"], ext


def cut_one(src, start, end, out_path, out_fmt):
    """Cut a single [start, end) range out of `src` into `out_path`."""
    args, _ = codec_args(src, out_fmt)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y",
           "-ss", f"{float(start):.3f}", "-to", f"{float(end):.3f}",
           "-i", str(src), *args, str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise RuntimeError(tail or "ffmpeg failed")
    return out_path


def cut_segments(src, segments, out_fmt, out_dir, emit=None):
    """Cut every segment into `out_dir`. Returns the list of paths written."""
    src = Path(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    _, ext = codec_args(src, out_fmt)
    names = unique_names(segments)
    written = []
    for i, (seg, stem) in enumerate(zip(segments, names)):
        if emit:
            emit({"stage": "cut", "index": i, "total": len(segments),
                  "name": stem, "progress": i / len(segments)})
        target = out_dir / f"{stem}{ext}"
        cut_one(src, seg["start"], seg["end"], target, out_fmt)
        written.append(target)
    if emit:
        emit({"stage": "cut", "progress": 1.0})
    return written


# --------------------------------------------------------------------------
# path resolution
# --------------------------------------------------------------------------

def find_by_name_size(name, size):
    """Find files matching a dropped file's name and byte size."""
    matches = []
    for root in SEARCH_DIRS:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= SEARCH_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if name in filenames:
                p = Path(dirpath) / name
                try:
                    if p.stat().st_size == size and p not in matches:
                        matches.append(p)
                except OSError:
                    pass
            if len(matches) > 1:
                return matches
    return matches


def resolve_audio(raw):
    """Expand and validate a path coming from the page."""
    path = Path(os.path.expanduser(str(raw or "").strip())).resolve()
    if not path.is_file():
        raise RuntimeError(f"No file at {path}")
    if path.suffix.lower() not in AUDIO_EXTS:
        raise RuntimeError(f"{path.suffix or 'That file'} isn't a supported audio format.")
    return path


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the terminal clean

    # -- helpers ----------------------------------------------------------
    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _param(self, name, default=""):
        return self._query().get(name, [default])[0]

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                self.serve_page()
            elif route == "/audio":
                self.serve_audio()
            elif route == "/peaks":
                self.serve_peaks()
            elif route == "/download":
                self.serve_download()
            elif route == "/zip":
                self.serve_zip()
            elif route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404)
        except Exception as exc:
            self._fail(exc)

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            if route == "/resolve":
                self.handle_resolve()
            elif route == "/probe":
                self.handle_probe()
            elif route == "/silence":
                self.handle_silence()
            elif route == "/cut":
                self.handle_cut()
            elif route == "/zip":
                self.handle_zip()
            elif route == "/reveal":
                self.handle_reveal()
            else:
                self.send_error(404)
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        try:
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            pass

    # -- pages and media --------------------------------------------------
    def serve_page(self):
        body = (HERE / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_audio(self):
        """Stream the source file, honouring Range requests so seeking works."""
        path = resolve_audio(self._param("path"))
        size = path.stat().st_size
        ctype = AUDIO_TYPES.get(path.suffix.lower()) \
            or mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        partial = bool(m and (m.group(1) or m.group(2)))
        if partial:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # suffix range: last N bytes
                start = max(size - int(m.group(2)), 0)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the browser seeked away; not an error
                remaining -= len(chunk)

    def serve_peaks(self):
        path = resolve_audio(self._param("path"))
        buckets = max(200, min(int(self._param("buckets", "2000")), 8000))
        self._send_json({"peaks": compute_peaks(path, buckets)})

    def serve_download(self):
        """Serve one already-cut file as a download."""
        path = Path(os.path.expanduser(self._param("path"))).resolve()
        if not path.is_file():
            raise RuntimeError("That file no longer exists.")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_zip(self):
        """Zip up a folder of cut segments and send it."""
        folder = Path(os.path.expanduser(self._param("dir"))).resolve()
        if not folder.is_dir():
            raise RuntimeError("That folder no longer exists.")
        files = sorted(p for p in folder.iterdir() if p.is_file()
                       and not p.name.startswith("."))
        if not files:
            raise RuntimeError("There's nothing in that folder to zip.")

        self._send_zip(files, folder.name)

    def _send_zip(self, files, stem):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                zf.write(f, f.name)
        data = buf.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(stem)}.zip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- json endpoints ---------------------------------------------------
    def handle_resolve(self):
        data = self._read_json()
        name, size = data.get("name", ""), int(data.get("size", -1))
        if Path(name).suffix.lower() not in AUDIO_EXTS:
            self._send_json({"error": f"{Path(name).suffix or 'That file'} isn't a "
                                      "supported audio format."}, status=400)
            return
        matches = find_by_name_size(name, size)
        if len(matches) == 1:
            self._send_json({"path": str(matches[0])})
        else:
            self._send_json({"needs_path": True, "ambiguous": len(matches) > 1})

    def handle_probe(self):
        path = resolve_audio(self._read_json().get("path"))
        info = probe_info(path)
        self._send_json({"path": str(path), "name": path.name,
                         "stem": path.stem, **info})

    def handle_silence(self):
        data = self._read_json()
        path = resolve_audio(data.get("path"))
        noise = float(data.get("noise_db", -35))
        min_len = float(data.get("min_len", 1.5))
        total = probe_duration(path)
        gaps = detect_silence(path, noise, min_len)
        self._send_json({"regions": gaps, "duration": total,
                         "speech": invert_regions(total, gaps)})

    def handle_cut(self):
        data = self._read_json()
        path = resolve_audio(data.get("path"))
        out_fmt = data.get("format", "mp3")
        segments = data.get("segments") or []
        total = probe_duration(path)

        clean = []
        for seg in segments:
            s = max(float(seg.get("start", 0)), 0.0)
            e = min(float(seg.get("end", 0)), total)
            if e - s > 0.01:
                clean.append({"name": seg.get("name"), "start": s, "end": e})
        if not clean:
            self._send_json({"error": "No segments to cut."}, status=400)
            return

        out_dir = Path(data.get("out_dir")) if data.get("out_dir") \
            else path.parent / f"{path.stem}_parts"

        # Stream newline-delimited JSON so the page can show live progress.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        lock = threading.Lock()

        def emit(obj):
            line = (json.dumps(obj) + "\n").encode()
            with lock:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()

        try:
            written = cut_segments(path, clean, out_fmt, out_dir, emit)
            emit({"done": True, "dir": str(out_dir),
                  "files": [{"name": p.name, "path": str(p)} for p in written]})
        except Exception as exc:
            try:
                emit({"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

    def handle_zip(self):
        """Zip an explicit list of files — not the whole folder, which may
        still contain segments from an earlier run."""
        data = self._read_json()
        paths = [Path(os.path.expanduser(p)).resolve() for p in data.get("files") or []]
        files = [p for p in paths if p.is_file()]
        if not files:
            self._send_json({"error": "Those files no longer exist."}, status=400)
            return
        self._send_zip(files, safe_name(data.get("name"), "segments"))

    def handle_reveal(self):
        target = Path(os.path.expanduser(self._read_json().get("path", "").strip()))
        if target.exists():
            subprocess.run(["open", "-R", str(target)])
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "Folder not found."}, status=400)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    check_tools()
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"AudioMultiCut running at {url}")
    print("Press Ctrl+C to stop.\n")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
