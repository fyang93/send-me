#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""send-me: one-shot upload link via Cloudflare quick tunnel.

Run it, send the printed link to a friend. After they finish uploading
(or the link expires), the server exits and the tunnel URL dies with it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

CHUNK_SIZE = 8 * 1024 * 1024  # fixed chunk size the browser must use (Cloudflare caps bodies at 100 MB)
MAX_FILE = 1 << 42  # 4 TiB sanity bound
STAGING_TTL = 7 * 24 * 3600  # unfinished uploads are kept this long for resuming
HEX64 = re.compile(r"[0-9a-f]{64}")
COPY_BUF = 1024 * 1024
FID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


_out_lock = threading.Lock()
_progress_shown = False


def log(msg: str) -> None:
    global _progress_shown
    with _out_lock:
        if _progress_shown:  # wipe the progress line, print the message, progress redraws later
            sys.stdout.write("\r\x1b[2K")
            _progress_shown = False
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def show_progress(line: str) -> None:
    global _progress_shown
    with _out_lock:
        sys.stdout.write("\r\x1b[2K" + line)
        sys.stdout.flush()
        _progress_shown = True


def fmt_eta(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


class Progress(threading.Thread):
    """Single self-updating terminal line: bar, bytes, speed, ETA, current file."""

    def __init__(self, state: "State"):
        super().__init__(daemon=True)
        self.state = state
        self.speed = 0.0

    def run(self) -> None:
        st = self.state
        last_b, last_t = 0, time.monotonic()
        while not st.done.wait(0.5):
            if st.sid is None:
                continue
            now = time.monotonic()
            with st.lock:
                got, total, count, cur = st.received, st.expect_total, st.expect_count, st.current
                nfiles = len(st.files)
            inst = (got - last_b) / max(now - last_t, 1e-6)
            self.speed = inst if self.speed == 0 else self.speed * 0.8 + inst * 0.2
            last_b, last_t = got, now
            width = max(10, min(30, shutil.get_terminal_size((80, 20)).columns - 70))
            if total:
                frac = min(1.0, got / total)
                fill = int(frac * width * 8)
                bar = "█" * (fill // 8) + (" ▏▎▍▌▋▊▉"[fill % 8] if fill // 8 < width else "")
                bar = bar.ljust(width)
                eta = fmt_eta((total - got) / self.speed) if self.speed > 1024 else "--"
                head = f"\x1b[36m{bar}\x1b[0m {frac * 100:5.1f}%  {human(got)} / {human(total)}"
                tail = f"{human(self.speed)}/s  ETA {eta}"
            else:
                head, tail = f"{human(got)} received", f"{human(self.speed)}/s"
            files = f"[{min(nfiles, count or nfiles)}/{count or '?'}] {cur}" if cur else ""
            line = f"  {head}  {tail}  {files}"
            cols = shutil.get_terminal_size((80, 20)).columns
            if len(line) > cols + 9:  # +9 for the invisible color codes
                line = line[: cols + 8] + "…"
            show_progress(line)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    return name[:200] or "file"


def unique_path(directory: Path, name: str) -> Path:
    p = directory / name
    stem, suffix = p.stem, p.suffix
    i = 1
    while p.exists():
        p = directory / f"{stem} ({i}){suffix}"
        i += 1
    return p


class State:
    def __init__(self, token: str, out_dir: Path, staging: Path, max_bytes: int | None, parallel: int = 4):
        self.token = token
        self.staging = staging  # <fid>.part + <fid>.json survive restarts, so uploads resume across links
        self.parallel = parallel
        self.reserved = 0  # sum of declared sizes of all files started
        self.out_dir = out_dir
        self.max_bytes = max_bytes
        self.sid: str | None = None
        # fid -> {name, part, meta, size, nchunks, done: set[int], busy: set[int], sha: dict[int, str]}
        self.files: dict[str, dict] = {}
        self.total = 0
        self.received = 0  # bytes written incl. in-flight chunks (for progress display)
        self.expect_total: int | None = None  # as reported by the browser
        self.expect_count: int | None = None
        self.current = ""
        self.done = threading.Event()
        self.lock = threading.Lock()

    # caller holds self.lock
    def lookup(self, fid: str) -> dict | None:
        """In-memory entry, or a resumable one left on disk by an earlier run."""
        if (f := self.files.get(fid)) is not None:
            return f
        part, meta = self.staging / f"{fid}.part", self.staging / f"{fid}.json"
        if not (part.exists() and meta.exists()):
            return None
        try:
            m = json.loads(meta.read_text())
            size = int(m["size"])
            nchunks = max(1, -(-size // CHUNK_SIZE))
            done = {int(i) for i in m["done"] if 0 <= int(i) < nchunks}
            sha = {int(k): v for k, v in m.get("sha", {}).items()}
            if part.stat().st_size != size or int(m.get("chunk", CHUNK_SIZE)) != CHUNK_SIZE:
                raise ValueError("layout changed")
        except (OSError, ValueError, KeyError, TypeError):
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            return None
        f = {"name": safe_name(m.get("name", "")), "part": part, "meta": meta, "size": size, "nchunks": nchunks,
             "done": done, "busy": set(), "sha": sha}
        stored = sum(min(CHUNK_SIZE, size - i * CHUNK_SIZE) for i in done)
        self.files[fid] = f
        self.reserved += size
        self.received += stored
        log(f"resuming: {f['name']}  ({human(stored)} of {human(size)} already here)")
        return f


def save_meta(f: dict) -> None:
    """Persist which chunks are stored (atomic write)."""
    tmp = f["meta"].with_suffix(".tmp")
    tmp.write_text(json.dumps({"name": f["name"], "size": f["size"], "chunk": CHUNK_SIZE,
                               "done": sorted(f["done"]), "sha": {str(k): v for k, v in f["sha"].items()}}))
    tmp.replace(f["meta"])


def prune_staging(staging: Path) -> None:
    if not staging.is_dir():
        return
    cutoff = time.time() - STAGING_TTL
    for p in staging.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                log(f"pruned stale partial upload: {p.name}")
        except OSError:
            pass


def make_handler(state: State, page: bytes):
    prefix = f"/{state.token}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "send-me"
        sys_version = ""

        def log_message(self, format, *args):  # silence default access log
            pass

        # ---- helpers -------------------------------------------------
        def send(self, code: int, body: bytes = b"", ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def json(self, code: int, obj) -> None:
            self.send(code, json.dumps(obj).encode())

        def route(self):
            u = urlparse(self.path)
            if u.path != prefix and not u.path.startswith(prefix + "/"):
                return None, None
            return u.path[len(prefix):] or "/", {k: v[0] for k, v in parse_qs(u.query).items()}

        def drain(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            while n > 0:
                got = self.rfile.read(min(n, COPY_BUF))
                if not got:
                    break
                n -= len(got)

        def check_sid(self, q) -> bool:
            if state.done.is_set() or not state.sid or q.get("sid") != state.sid:
                self.drain()
                self.json(410, {"error": "link used or expired"})
                return False
            return True

        # ---- routes --------------------------------------------------
        def who(self) -> str:
            ip = self.headers.get("Cf-Connecting-Ip") or self.client_address[0]
            cc = self.headers.get("Cf-Ipcountry")
            return f"{ip}" + (f" [{cc}]" if cc else "")

        def do_GET(self):
            sub, _ = self.route()
            if sub != "/":
                # Chat apps sometimes glue trailing text/punctuation onto the URL
                # (e.g. "https://host/TOKEN，传完告诉我"); redirect to the clean link.
                raw = unquote(urlparse(self.path).path)
                if sub is None and raw.startswith(prefix):
                    log(f"redirecting mangled link from {self.who()}: {raw!r}")
                    self.send_response(302)
                    self.send_header("Location", prefix)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path not in ("/favicon.ico", "/robots.txt"):
                    log(f"404 from {self.who()}: {raw!r}  UA={self.headers.get('User-Agent', '')[:80]!r}")
                return self.send(404, b"not found", "text/plain")
            if state.done.is_set():
                return self.send(410, "此链接已失效 / This link has expired.".encode(), "text/plain; charset=utf-8")
            self.send(200, page, "text/html; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            sub, q = self.route()
            if sub == "/begin":
                return self.begin()
            if sub == "/chunk":
                return self.chunk(q)
            if sub == "/status":
                return self.status(q)
            if sub == "/finish":
                return self.finish_upload(q)
            # Unknown target (e.g. a tab still open from an older link): don't swallow its
            # body, just answer and hang up.
            log(f"404 POST from {self.who()}: {unquote(urlparse(self.path).path)!r}  "
                f"UA={self.headers.get('User-Agent', '')[:80]!r}")
            self.close_connection = True
            self.send_response(404)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def begin(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                body = {}
            with state.lock:
                if state.done.is_set():
                    return self.json(410, {"error": "link used or expired"})
                if state.sid is None:
                    state.sid = secrets.token_urlsafe(16)
                    state.out_dir.mkdir(parents=True, exist_ok=True)
                    state.staging.mkdir(parents=True, exist_ok=True)
                    log(f"session claimed by {self.who()}")
                elif body.get("sid") != state.sid:
                    return self.json(410, {"error": "link already in use"})
            self.json(200, {"sid": state.sid, "maxBytes": state.max_bytes,
                            "chunkSize": CHUNK_SIZE, "parallel": state.parallel})

        def read_json(self) -> dict:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                return body if isinstance(body, dict) else {}
            except ValueError:
                return {}

        def status(self, q):
            """Which chunks of the given files are already stored (for resume)."""
            if not self.check_sid(q):
                return
            fids = self.read_json().get("fids", [])
            with state.lock:
                out = {}
                for k in fids:
                    if isinstance(k, str) and FID_RE.fullmatch(k) and (f := state.lookup(k)) is not None:
                        out[k] = sorted(f["done"])
            self.json(200, {"files": out})

        def chunk(self, q):
            """Store chunk `index` of file `fid`. Chunks may arrive in any order and in parallel;
            re-sending a stored chunk is a no-op, so clients can retry freely."""
            if not self.check_sid(q):
                return
            try:
                fid = q["fid"]
                index = int(q["index"])
                size = int(q["size"])
                length = int(self.headers["Content-Length"])
                name = safe_name(unquote(q.get("name", "")))
                hint_total = int(q["total"]) if "total" in q else None
                hint_count = int(q["count"]) if "count" in q else None
                want_sha = q.get("sha", "").lower() or None
            except (KeyError, ValueError, TypeError):
                self.drain()
                return self.json(400, {"error": "bad request"})
            if want_sha is not None and not HEX64.fullmatch(want_sha):
                self.drain()
                return self.json(400, {"error": "bad checksum"})
            nchunks = max(1, -(-size // CHUNK_SIZE))
            offset = index * CHUNK_SIZE
            if (not FID_RE.fullmatch(fid) or not 0 <= size <= MAX_FILE or not 0 <= index < nchunks
                    or length != min(CHUNK_SIZE, size - offset)):
                self.drain()
                return self.json(400, {"error": "bad request"})

            with state.lock:
                f = state.lookup(fid)
                if f is None:
                    if len(state.files) >= 10000:
                        self.drain()
                        return self.json(413, {"error": "too many files"})
                    if state.max_bytes is not None and state.reserved + size > state.max_bytes:
                        self.drain()
                        return self.json(413, {"error": "total size limit exceeded"})
                    part, meta = state.staging / f"{fid}.part", state.staging / f"{fid}.json"
                    with open(part, "wb") as fh:
                        fh.truncate(size)  # sparse file; chunks fill it in any order
                    f = {"name": name, "part": part, "meta": meta, "size": size, "nchunks": nchunks,
                         "done": set(), "busy": set(), "sha": {}}
                    save_meta(f)
                    state.files[fid] = f
                    state.reserved += size
                    log(f"receiving: {name}  ({human(size)})")
                elif f["size"] != size:
                    self.drain()
                    return self.json(400, {"error": "size mismatch"})
                if index in f["done"]:
                    self.drain()
                    return self.json(200, {"done": len(f["done"]), "chunks": nchunks})
                if index in f["busy"]:  # a stale request for this chunk is still streaming
                    self.drain()
                    return self.json(409, {"error": "chunk in progress"})
                f["busy"].add(index)
                state.current = f["name"]
                if hint_total is not None and hint_total >= 0:
                    state.expect_total = hint_total
                if hint_count is not None and 0 < hint_count <= 10000:
                    state.expect_count = hint_count

            written, digest = 0, hashlib.sha256()
            try:
                fd = os.open(f["part"], os.O_WRONLY)
                try:
                    while written < length:
                        buf = self.rfile.read(min(COPY_BUF, length - written))
                        if not buf:
                            break  # client went away; this chunk stays missing and gets re-sent
                        os.pwrite(fd, buf, offset + written)
                        digest.update(buf)
                        written += len(buf)
                        with state.lock:
                            state.received += len(buf)
                finally:
                    os.close(fd)
            finally:
                ok = written == length and (want_sha is None or digest.hexdigest() == want_sha)
                with state.lock:
                    f["busy"].discard(index)
                    if ok:
                        f["done"].add(index)
                        f["sha"][index] = digest.hexdigest()
                        save_meta(f)
                    else:
                        state.received -= written
            if ok:
                self.json(200, {"done": len(f["done"]), "chunks": nchunks})
            elif written == length:  # arrived complete but corrupted: make the browser resend it
                log(f"checksum mismatch on {f['name']} chunk {index}; asking for resend")
                self.json(422, {"error": "checksum mismatch"})

        def finish_upload(self, q):
            if not self.check_sid(q):
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                keep = [k for k in body.get("fids", []) if isinstance(k, str)]
            except (ValueError, AttributeError):
                return self.json(400, {"error": "bad request"})
            with state.lock:
                if state.done.is_set():
                    return self.json(410, {"error": "link used or expired"})
                if any(f["busy"] for f in state.files.values()):
                    return self.json(409, {"error": "upload still in progress"})
                missing = [k for k in keep
                           if (f := state.files.get(k)) is None or len(f["done"]) < f["nchunks"]]
                if missing:
                    return self.json(409, {"error": "incomplete", "fids": missing})
                state.done.set()
                saved, kept = [], {}
                for fid in dict.fromkeys(keep):  # client order, deduped
                    if (f := state.files.get(fid)) is None:
                        continue
                    dest = unique_path(state.out_dir, f["name"])
                    f["part"].rename(dest)
                    f["meta"].unlink(missing_ok=True)
                    kept[fid] = f
                    saved.append({"name": dest.name, "size": f["size"]})
                    log(f"saved: {dest}  ({human(f['size'])})")
                for fid, f in state.files.items():  # removed in the browser: discard
                    if fid not in kept:
                        f["part"].unlink(missing_ok=True)
                        f["meta"].unlink(missing_ok=True)
                state.files = kept
                state.total = sum(f["size"] for f in kept.values())
            self.json(200, {"files": saved})

    return Handler


def print_qr(text: str) -> None:
    """Half-block QR with an even 1-module white border (qrencode's own utf8 output is lopsided)."""
    if not shutil.which("qrencode"):
        return
    try:
        out = subprocess.run(["qrencode", "-t", "ASCII", "-m", "1", text], capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return
    rows = [[c == "#" for c in line[::2]] for line in out.splitlines() if line]
    WHITE, BLACK = "97", "30"
    lines = []
    for y in range(0, len(rows), 2):
        top, bot = rows[y], rows[y + 1] if y + 1 < len(rows) else None
        cells = []
        for x, t in enumerate(top):
            fg = BLACK if t else WHITE
            if bot is None:
                cells.append(f"\x1b[{fg};49m▀")
            else:
                bg = "40" if bot[x] else "107"
                cells.append(f"\x1b[{fg};{bg}m▀")
        lines.append("    " + "".join(cells) + "\x1b[0m")
    print("\n".join(lines) + "\n", flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return  # client/tunnel dropped the connection; the browser retries on its own
        log(f"error handling request: {exc!r}")


def find_cloudflared() -> list[str]:
    if exe := shutil.which("cloudflared"):
        return [exe]
    sys.exit("cloudflared not found. Enter the dev shell (direnv allow / nix develop) or use --local.")


def start_tunnel(port: int, name: str | None = None, hostname: str | None = None) -> tuple[subprocess.Popen, str]:
    """Quick tunnel (random trycloudflare.com URL) or, with name+hostname, a named tunnel on your domain."""
    origin = f"http://127.0.0.1:{port}"
    if name:
        cmd = find_cloudflared() + ["tunnel", "--no-autoupdate", "run", "--url", origin, name]
        log(f"starting named tunnel '{name}' -> https://{hostname} ...")
    else:
        cmd = find_cloudflared() + ["tunnel", "--no-autoupdate", "--url", origin]
        log("starting Cloudflare quick tunnel ...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    found: list[str] = [f"https://{hostname}"] if name else []
    ready = threading.Event()
    tail: list[str] = []

    def pump():
        assert proc.stderr
        for line in proc.stderr:
            tail.append(line.rstrip())
            del tail[:-20]
            if not found and (m := url_re.search(line)):
                found.append(m.group(0))
            if found and "Registered tunnel connection" in line:
                ready.set()
        ready.set()

    threading.Thread(target=pump, daemon=True).start()
    ready.wait(timeout=120)
    if not found or proc.poll() is not None:
        stop(proc)
        sys.exit("failed to start tunnel:\n" + "\n".join(tail))
    return proc, found[0]


def stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description="One-shot upload link via Cloudflare quick tunnel.")
    ap.add_argument("-o", "--out", type=Path, default=Path.home() / "Downloads" / "send-me",
                    help="parent directory for received files (default: ~/Downloads/send-me)")
    ap.add_argument("-e", "--expire", type=float, default=24, help="hours until the link expires (default: 24)")
    ap.add_argument("-m", "--max-gb", type=float, default=50, help="max total upload size in GB, 0 = unlimited (default: 50)")
    ap.add_argument("-j", "--parallel", type=int, default=4, help="parallel chunk uploads per browser (default: 4)")
    ap.add_argument("-p", "--port", type=int, default=0, help="local port (default: random)")
    ap.add_argument("--local", action="store_true", help="no tunnel, serve on localhost only (for testing)")
    ap.add_argument("--tunnel", default=os.environ.get("SEND_ME_TUNNEL") or None,
                    help="named tunnel to use instead of a quick tunnel (env: SEND_ME_TUNNEL)")
    ap.add_argument("--hostname", default=os.environ.get("SEND_ME_HOSTNAME") or None,
                    help="public hostname routed to the named tunnel, e.g. send.example.com (env: SEND_ME_HOSTNAME)")
    ap.add_argument("--quick", action="store_true", help="force a quick tunnel even if a named tunnel is configured")
    args = ap.parse_args()
    if args.quick:
        args.tunnel = args.hostname = None
    if bool(args.tunnel) != bool(args.hostname):
        ap.error("--tunnel and --hostname must be given together")

    # letters+digits only: no '-'/'_' that chat apps might treat as link boundaries
    token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    out_dir = args.out.expanduser() / datetime.now().strftime("%Y%m%d-%H%M%S")
    max_bytes = int(args.max_gb * 1024**3) if args.max_gb > 0 else None
    staging = args.out.expanduser() / ".incoming"
    prune_staging(staging)
    state = State(token, out_dir, staging, max_bytes, max(1, min(args.parallel, 16)))
    page = (Path(__file__).with_name("upload.html")).read_bytes()

    httpd = Server(("127.0.0.1", args.port), make_handler(state, page))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    proc = None
    try:
        if args.local:
            base = f"http://127.0.0.1:{port}"
        else:
            proc, base = start_tunnel(port, args.tunnel, args.hostname)
        link = f"{base}/{token}"
        deadline = time.time() + args.expire * 3600
        print(f"\n  Upload link (expires in {args.expire:g}h):\n\n    {link}\n", flush=True)
        print_qr(link)
        log(f"files will be saved to {out_dir}")
        if sys.stdout.isatty():
            Progress(state).start()

        while not state.done.wait(timeout=1):
            if time.time() > deadline:
                log("link expired")
                state.done.set()
                break
            if proc and proc.poll() is not None:
                log("tunnel exited unexpectedly")
                break
        time.sleep(1.5)  # let the browser receive the final response
    except KeyboardInterrupt:
        print()
        log("interrupted")
    finally:
        httpd.shutdown()
        stop(proc)
        with state.lock:
            finished = state.done.is_set() and not any(f["part"].exists() for f in state.files.values())
            if not finished:  # stopped / expired mid-upload: partials stay in staging for the next link
                state.done.set()
                for f in state.files.values():
                    if f["part"].exists():
                        have = sum(min(CHUNK_SIZE, f["size"] - i * CHUNK_SIZE) for i in f["done"])
                        log(f"kept for resume: {f['name']}  ({human(have)} / {human(f['size'])})")
        if out_dir.exists() and not any(out_dir.iterdir()):
            out_dir.rmdir()
        if finished and state.files:
            log(f"done. {len(state.files)} file(s), {human(state.total)} in {out_dir}")
        elif not state.files:
            log("done. nothing received.")
        else:
            log("stopped before the upload finished. Send a new link (`just link`) and have them pick")
            log(f"the same files again: it resumes. Unfinished data lives in {state.staging} for {STAGING_TTL // 86400} days.")


if __name__ == "__main__":
    main()
