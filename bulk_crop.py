#!/usr/bin/env python3
"""
Bulk Crop - set crops for many videos in one sitting, encode them in the background.

  1. Pick the top folder that holds your videos (any depth of subfolders).
  2. Output goes to a parallel "<folder>_cropped" tree with identical subfolders.
     Non-video files, and videos you choose not to crop, are copied over unchanged.
  3. Draw a crop box and/or black-out boxes on each video and press Enter. Encoding
     (lossless H.264) starts right away in the background while you move on.
     Boxes carry over to the next video, and "Apply to all remaining" saves them
     on every undecided video with the same frame size in one go.
  4. If you close the window with work still pending, the rest can be handed
     off to a background process that keeps going without the window.

Progress lives in <output>/_bulk_crop_state.json, so you can close and reopen
on the same folder at any time and pick up where you left off.

Usage:
  pythonw bulk_crop.py                       choose folders with dialogs
  pythonw bulk_crop.py INPUT [-o OUTPUT]     skip the folder dialogs
  python  bulk_crop.py --process STATE.json  headless: finish queued jobs, then exit
"""

import argparse
import bisect
import itertools
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg",
              ".webm", ".flv", ".mts", ".m2ts", ".ts", ".3gp"}
STATE_NAME = "_bulk_crop_state.json"
LOG_NAME = "_bulk_crop_log.txt"
PID_NAME = "_bulk_crop_worker.pid"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROZEN = getattr(sys, "frozen", False)  # running as the packaged BulkCrop.exe
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent


def find_tool(name):
    """A copy of the tool next to the app (as shipped in the release zip), else one on PATH."""
    local = APP_DIR / f"{name}.exe"
    return str(local) if os.name == "nt" and local.exists() else shutil.which(name)


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
MIN_FREE_BYTES = 2 * 1024 ** 3  # don't start an encode with less than this free on the output drive
RETRY_DELAYS = (10, 60)          # seconds to wait before the 2nd and 3rd attempt at a job
THUMB_WIDTH = 400                # scrubbing previews: small frames this wide...
THUMB_COUNT = 240                # ...about this many spread over each video

# (text found in the error, plain-English explanation)
FRIENDLY_ERRORS = [
    ("moov atom not found", "File is incomplete; the recording was probably cut off"),
    ("Invalid data found", "File is corrupt or not a readable video"),
    ("does not contain any stream", "File has no video stream"),
    ("matches no streams", "File has no readable video stream (corrupt or cut off)"),
    ("Nothing was written into output file", "File is corrupt or not a readable video"),
    ("too big or non positive", "Crop box falls outside this video's frame"),
    ("No space left", "Output drive is full"),
    ("not enough space on the disk", "Output drive is full"),
    ("Permission denied", "File is locked or access was denied (open in another program, or OneDrive syncing)"),
    ("being used by another process", "File is locked by another program (often OneDrive syncing)"),
    ("cloud operation", "OneDrive couldn't download the file (offline?)"),
    ("output check failed", "Encoding finished but the result didn't match the original"),
]
# errors that won't go away by trying again
PERMANENT_ERRORS = ("moov atom not found", "Invalid data found", "does not contain any stream",
                    "matches no streams", "Nothing was written into output file", "too big or non positive")
DISK_FULL_ERRORS = ("No space left", "not enough space on the disk")
RGB_PREFIXES = ("rgb", "bgr", "argb", "abgr", "0rgb", "0bgr", "gbr", "pal8")

log = logging.getLogger("bulk_crop")


# ---------------------------------------------------------------------------
# helpers

def setup_logging(output_root):
    if log.handlers:
        return
    handler = logging.FileHandler(Path(output_root) / LOG_NAME, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def longpath(p):
    """Prefix Windows paths with \\\\?\\ so ones over 260 characters still work
    (Windows long-path support is off by default; ffmpeg copes, Python doesn't)."""
    s = os.path.abspath(str(p))
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return Path(s)
    if s.startswith("\\\\"):  # network share
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def friendly(error):
    for needle, text in FRIENDLY_ERRORS:
        if needle.lower() in error.lower():
            return f"{text}.  [{error[:200]}]"
    return error[:300]


def is_permanent(error):
    return any(n.lower() in error.lower() for n in PERMANENT_ERRORS)


def is_disk_full(error):
    return any(n.lower() in error.lower() for n in DISK_FULL_ERRORS)


def replace_with_retry(src, dst, tries=20):
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:  # OneDrive / antivirus can hold a file for a moment
            if i == tries - 1:
                raise
            time.sleep(0.5)


def probe(src):
    """Duration, size and pixel format of a file's first video stream (missing values are 0/None)."""
    info = {"duration": 0.0, "width": None, "height": None, "pix_fmt": None}
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,pix_fmt:format=duration", "-of", "json", str(src)],
            capture_output=True, text=True, errors="replace", timeout=300,
            creationflags=NO_WINDOW).stdout
        data = json.loads(out or "{}")
    except (ValueError, subprocess.SubprocessError):
        return info
    stream = (data.get("streams") or [{}])[0]
    info.update(width=stream.get("width"), height=stream.get("height"), pix_fmt=stream.get("pix_fmt"))
    try:
        info["duration"] = max(float(data["format"]["duration"]), 0.0)
    except (KeyError, ValueError, TypeError):
        pass
    return info


def extract_frame(src, t, out_img):
    """Write one frame of src at t seconds (falling back to the start) to out_img. Use .ppm:
    uncompressed, so much quicker than PNG on noisy (e.g. ultrasound) frames."""
    for when in (t, 0.0):
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-ss", f"{when:.3f}", "-i", str(src), "-frames:v", "1", "-update", "1", out_img],
            capture_output=True, text=True, errors="replace", timeout=300,
            creationflags=NO_WINDOW)
        if r.returncode == 0 and os.path.exists(out_img) and os.path.getsize(out_img) > 0:
            return
    raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "could not read a frame")


def read_ppm(f):
    """One binary PPM image (header included) from a pipe, or None at the end."""
    header = b""
    while header.count(b"\n") < 3:
        c = f.read(1)
        if not c:
            return None
        header += c
    w, h = map(int, header.split(b"\n")[1].split())
    data = f.read(w * h * 3)
    return header + data if len(data) == w * h * 3 else None


class Thumbs:
    """Small frames spread across a whole video, made in the background so scrubbing is instant.
    Frames are PPM bytes (Tk loads those without extra libraries), kept sorted by time."""

    def __init__(self, src):
        self.times, self.frames = [], []
        self.duration = 0.0
        self.version = 0      # bumps whenever frames arrive
        self.done = False
        self.cancelled = False
        self.proc = None
        self.lock = threading.Lock()
        threading.Thread(target=self._run, args=(src,), daemon=True).start()

    def cancel(self):
        self.cancelled = True
        try:
            if self.proc:
                self.proc.kill()
        except OSError:
            pass

    def nearest(self, t):
        """(time, frame) closest to t, or None if nothing yet."""
        with self.lock:
            if not self.times:
                return None
            i = bisect.bisect(self.times, t)
            if i == len(self.times) or (i > 0 and t - self.times[i - 1] < self.times[i] - t):
                i -= 1
            return self.times[i], self.frames[i]

    def step(self, t, direction):
        """Time of the frame just after (direction 1) or before (-1) t, or None."""
        with self.lock:
            if direction > 0:
                i = bisect.bisect_right(self.times, t + 1e-3)
                return self.times[i] if i < len(self.times) else None
            i = bisect.bisect_left(self.times, t - 1e-3) - 1
            return self.times[i] if i >= 0 else None

    def _run(self, src):
        try:
            self.duration = probe(src)["duration"]
            d = self.duration
            # keyframes only: nothing else gets decoded, so even long videos take seconds
            gap = d / THUMB_COUNT if d > 0 else 0
            self._pass(["-skip_frame", "nokey", "-i", str(src)],
                       f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{gap:.3f})'")
            if not self.cancelled and d > 0 and len(self.times) < min(40, d / 3):
                # too few keyframes (some recorders write one every few minutes): decode it all instead
                self._pass(["-i", str(src)], f"fps={THUMB_COUNT / d:.6f}")
        except Exception as e:
            log.info("preview strip failed for %s: %s", src, e)
        finally:
            self.done = True
            self.version += 1

    def _pass(self, inputs, pick):
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", *inputs,
               "-map", "0:v:0", "-vf", f"{pick},scale={THUMB_WIDTH}:-2:flags=fast_bilinear,showinfo",
               "-fps_mode", "passthrough", "-f", "image2pipe", "-c:v", "ppm", "-"]
        self.proc = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        times = []  # from showinfo, one per frame, in output order

        def read_times():
            for line in proc.stderr:
                i = line.find(b" pts_time:")
                if i >= 0:
                    try:
                        times.append(float(line[i + 10:].split()[0]))
                    except (ValueError, IndexError):
                        times.append(None)
        reader = threading.Thread(target=read_times, daemon=True)
        reader.start()
        n = 0
        try:
            while not self.cancelled:
                frame = read_ppm(proc.stdout)
                if frame is None:
                    break
                while len(times) <= n and reader.is_alive():
                    time.sleep(0.005)
                t = times[n] if n < len(times) else None
                n += 1
                if t is None:
                    continue
                with self.lock:
                    i = bisect.bisect(self.times, t)
                    self.times.insert(i, t)
                    self.frames.insert(i, frame)
                    self.version += 1
        finally:
            proc.kill()
            proc.wait()


def pid_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


def launch_background(state):
    """Start a detached process that finishes every queued job in state."""
    if FROZEN:
        cmd = [sys.executable]
    else:
        exe = Path(sys.executable)
        if exe.with_name("pythonw.exe").exists():
            exe = exe.with_name("pythonw.exe")
        cmd = [str(exe), str(Path(__file__).resolve())]
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen(cmd + ["--process", str(state.path)],
                     creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=os.name != "nt")


# ---------------------------------------------------------------------------
# job state (shared by the window, the encoder threads and the headless worker)

class State:
    """Videos: action None (undecided) / "crop" (re-encode with "crop" [x, y, w, h] or None for the
    full frame, plus "masks", a list of [x, y, w, h] boxes to black out) / "copy"; status todo/queued/done/failed.
    Other files: always copied; status queued/done/failed. "rev" bumps whenever a
    decision changes, so a job that changed mid-encode gets redone."""

    def __init__(self, path, input_root, output_root, workers=2):
        self.path = Path(path)
        self.input_root = Path(input_root)
        self.output_root = Path(output_root)
        self.workers = workers
        self.videos = {}
        self.files = {}
        self.active = set()   # (kind, rel) currently being processed
        self.progress = {}    # rel -> 0..1 while cropping
        self.paused = None    # reason encoding is on hold (e.g. disk full), until retry_failed()
        self.low_space = None # reason encoding is waiting for free space; clears itself
        self.version = 0
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        state = cls(path, data["input_root"], data["output_root"], data.get("workers", 2))
        state.videos = data.get("videos", {})
        state.files = data.get("files", {})
        return state

    def save(self):
        with self.lock:
            data = {"input_root": str(self.input_root), "output_root": str(self.output_root),
                    "workers": self.workers, "videos": self.videos, "files": self.files}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            replace_with_retry(tmp, self.path)

    def changed(self):
        with self.lock:
            self.version += 1
            self.save()
            self.cond.notify_all()

    def src(self, rel):
        return longpath(self.input_root) / rel

    def dest(self, rel):
        return longpath(self.output_root) / rel

    def scan(self):
        """Walk the input tree, mirror every folder into the output, and merge with saved decisions."""
        videos, files = {}, {}
        in_root, out_root = longpath(self.input_root), longpath(self.output_root)
        for dirpath, dirnames, filenames in os.walk(in_root):
            dirnames.sort(key=str.lower)
            rel_dir = Path(dirpath).relative_to(in_root)
            (out_root / rel_dir).mkdir(parents=True, exist_ok=True)
            for name in sorted(filenames, key=str.lower):
                rel = (rel_dir / name).as_posix()
                if Path(name).suffix.lower() in VIDEO_EXTS:
                    job = self.videos.get(rel) or {"action": None, "crop": None, "status": "todo",
                                                   "error": None, "rev": 0}
                    videos[rel] = job
                else:
                    job = self.files.get(rel) or {"status": "queued", "error": None, "rev": 0}
                    files[rel] = job
                # retry failures, and redo anything whose output has gone missing
                if job["status"] == "failed" or (job["status"] == "done" and not self.dest(rel).exists()):
                    job["status"] = "queued"
                    job["error"] = None
        with self.lock:
            self.videos, self.files = videos, files
            self.changed()

    def decide(self, rel, action, crop=None, masks=None):
        masks = masks or []
        with self.lock:
            job = self.videos[rel]
            if job["action"] == action and job["crop"] == crop and job.get("masks", []) == masks \
                    and job["status"] != "failed":
                return
            job.update(action=action, crop=crop, masks=masks, status="queued", error=None, rev=job["rev"] + 1)
            job.pop("attempts", None)
            job.pop("retry_at", None)
            self.changed()

    def next_job(self):
        """Claim the next queued job: (kind, rel, job snapshot), or None."""
        with self.lock:
            now = time.time()
            for kind, jobs in (("file", self.files), ("video", self.videos)):
                for rel, job in jobs.items():
                    if job["status"] == "queued" and (kind, rel) not in self.active \
                            and job.get("retry_at", 0) <= now:
                        self.active.add((kind, rel))
                        self.version += 1
                        return kind, rel, dict(job)
        return None

    def finish(self, kind, rel, rev, error=None):
        with self.lock:
            self.active.discard((kind, rel))
            self.progress.pop(rel, None)
            job = (self.files if kind == "file" else self.videos).get(rel)
            if job is not None and job["rev"] == rev:  # otherwise the decision changed: stays queued
                job["status"] = "failed" if error else "done"
                job["error"] = error
                job.pop("attempts", None)
                job.pop("retry_at", None)
            self.changed()

    def defer(self, kind, rel, rev, delay):
        """Give a claimed job back to the queue to try again after delay seconds; other jobs go meanwhile."""
        with self.lock:
            self.active.discard((kind, rel))
            self.progress.pop(rel, None)
            job = (self.files if kind == "file" else self.videos).get(rel)
            if job is not None and job["rev"] == rev:
                job["attempts"] = job.get("attempts", 0) + 1
                job["retry_at"] = time.time() + delay
            self.changed()

    def release(self, kind, rel):
        """Give a claimed job back to the queue untouched."""
        with self.lock:
            self.active.discard((kind, rel))
            self.progress.pop(rel, None)
            self.version += 1
            self.cond.notify_all()

    def retry_failed(self):
        with self.lock:
            for job in itertools.chain(self.files.values(), self.videos.values()):
                if job["status"] == "failed":
                    job["status"], job["error"] = "queued", None
            self.paused = None
            self.changed()

    def has_work(self):
        with self.lock:
            return bool(self.active) or any(
                j["status"] == "queued" for j in itertools.chain(self.files.values(), self.videos.values()))

    def counts(self):
        with self.lock:
            c = {"todo": 0, "queued": 0, "running": 0, "done": 0, "failed": 0}
            for rel, job in self.videos.items():
                c["running" if ("video", rel) in self.active else job["status"]] += 1
            c["files_total"] = len(self.files)
            c["files_done"] = sum(j["status"] == "done" for j in self.files.values())
            c["files_failed"] = sum(j["status"] == "failed" for j in self.files.values())
            return c


# ---------------------------------------------------------------------------
# background encoders

class Workers:
    def __init__(self, state, exit_when_idle=False):
        self.state = state
        self.exit_when_idle = exit_when_idle
        self.threads = []
        self.procs = {}
        self.stopping = False
        threading.Thread(target=self._keep_awake, daemon=True).start()

    def _keep_awake(self):
        """Stop Windows from going to sleep on idle while there's encoding to do
        (closing the lid or choosing Sleep still sleeps; work resumes on wake)."""
        if os.name != "nt":
            return
        import ctypes
        while not self.stopping:
            if self.state.has_work():
                ctypes.windll.kernel32.SetThreadExecutionState(0x00000001)  # ES_SYSTEM_REQUIRED: reset idle timer
            time.sleep(30)

    def _check_space(self):
        """True if the output drive has room to start another encode (caller holds the lock)."""
        st = self.state
        try:
            free = shutil.disk_usage(longpath(st.output_root)).free
        except OSError:
            return True
        reason = None if free >= MIN_FREE_BYTES else (
            f"Waiting: only {free / 1024 ** 3:.1f} GB free on the output drive. Free up space and it resumes.")
        if reason != st.low_space:
            st.low_space = reason
            st.version += 1
            if reason:
                log.info(reason)
        return reason is None

    def resize(self, n):
        with self.state.lock:
            self.state.workers = n
            self.state.cond.notify_all()
        while len(self.threads) < n:
            t = threading.Thread(target=self._run, args=(len(self.threads),), daemon=True)
            self.threads.append(t)
            t.start()

    def stop(self):
        self.stopping = True
        with self.state.lock:
            self.state.cond.notify_all()
        for proc in list(self.procs.values()):
            try:
                proc.kill()
            except OSError:
                pass

    def join(self, timeout=None):
        for t in self.threads:
            t.join(timeout)

    def _run(self, idx):
        st = self.state
        while True:
            with st.lock:
                while True:
                    if self.stopping:
                        return
                    if self.exit_when_idle and (st.paused or not st.has_work()):
                        return
                    if not st.paused and idx < st.workers and self._check_space():
                        claimed = st.next_job()
                        if claimed:
                            break
                    st.cond.wait(1.0)
            kind, rel, job = claimed
            try:
                error = self._do(idx, kind, rel, job)
            except Exception as e:  # keep the worker alive whatever one file does
                error = f"{type(e).__name__}: {e}"
            if self.stopping:  # killed mid-job: leave it queued for next time
                return
            attempt = job.get("attempts", 0)
            if error and not (is_permanent(error) or is_disk_full(error)) and attempt < len(RETRY_DELAYS):
                log.info("retrying  %s in %ds  (%s)", rel, RETRY_DELAYS[attempt], error)
                st.defer(kind, rel, job["rev"], RETRY_DELAYS[attempt])
                continue
            if error and is_disk_full(error):
                # not this file's fault: put it back and hold everything until space is freed
                st.release(kind, rel)
                with st.lock:
                    st.paused = ("Paused: the output drive filled up. Free up space, then click "
                                 "'Retry failed / resume'.")
                    st.version += 1
                log.info("PAUSED  output drive full while writing %s", rel)
                continue
            st.finish(kind, rel, job["rev"], friendly(error) if error else None)
            log.info("%s  %s%s", "FAILED" if error else "done  ", rel, f"  ({error})" if error else "")

    def _do(self, idx, kind, rel, job):
        src, dst = self.state.src(rel), self.state.dest(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if kind == "file" or job["action"] == "copy":
            return self._copy(src, dst)
        return self._encode(idx, rel, src, dst, job["crop"], job.get("masks") or [])

    def _copy(self, src, dst):
        s = src.stat()
        if dst.exists():
            d = dst.stat()
            if d.st_size == s.st_size and d.st_mtime >= s.st_mtime - 2:
                return None
        tmp = dst.with_name(dst.name + ".partial")
        shutil.copy2(src, tmp)
        replace_with_retry(tmp, dst)
        return None

    def _encode(self, idx, rel, src, dst, crop, masks):
        """Black out each mask box (in full-frame pixels), then crop (crop None = keep the full frame)."""
        info = probe(src)
        duration = info["duration"]
        filters = [f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill" for x, y, w, h in masks]
        if crop:
            x, y, w, h = crop
            filters.append(f"crop={w}:{h}:{x}:{y}")
        else:
            w, h = info["width"], info["height"]
        tmp = dst.with_name(f"{dst.stem}.partial{dst.suffix}")
        if dst.suffix.lower() == ".webm":
            vcodec = ["-c:v", "libvpx-vp9", "-lossless", "1", "-row-mt", "1"]
            acodec = ["-c:a", "libopus"]
        else:
            # RGB sources (e.g. uncompressed AVI) stay RGB; plain libx264 would convert them to YUV
            rgb = (info["pix_fmt"] or "").startswith(RGB_PREFIXES)
            # qp 0 is lossless at any preset; ultrafast just trades file size for speed
            vcodec = ["-c:v", "libx264rgb" if rgb else "libx264", "-preset", "ultrafast", "-qp", "0"]
            acodec = ["-c:a", "aac", "-b:a", "256k"]
        base = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats",
                "-progress", "pipe:1", "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
                "-vf", ",".join(filters), *vcodec, "-map_metadata", "0"]
        error = None
        # copy the audio untouched; if the container won't take it, re-encode it
        for audio in (["-c:a", "copy"], acodec):
            error = self._ffmpeg(idx, rel, base + audio + [str(tmp)], duration)
            if error is None or self.stopping or is_disk_full(error):
                break
        if error is None:
            error = self._verify(tmp, w, h, duration)
        if error is None:
            replace_with_retry(tmp, dst)
        else:
            tmp.unlink(missing_ok=True)
        return error

    @staticmethod
    def _verify(path, w, h, src_duration):
        """Make sure a finished encode has the expected size and (nearly) the original's length."""
        out = probe(path)
        if (out["width"], out["height"]) != (w, h):
            return f"output check failed: expected {w}x{h}, got {out['width']}x{out['height']}"
        if src_duration > 0 and out["duration"] < src_duration - max(1.0, 0.01 * src_duration):
            return f"output check failed: {out['duration']:.1f}s long, original is {src_duration:.1f}s"
        return None

    def _ffmpeg(self, idx, rel, cmd, duration):
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True,
                                    errors="replace", creationflags=NO_WINDOW)
            self.procs[idx] = proc
            try:
                for line in proc.stdout:
                    if line.startswith("out_time_us=") and duration > 0:
                        try:
                            self.state.progress[rel] = min(int(line[12:]) / 1e6 / duration, 1.0)
                        except ValueError:
                            pass
                proc.wait()
            finally:
                self.procs.pop(idx, None)
            if proc.returncode == 0:
                return None
            errf.seek(0)
            lines = errf.read().decode("utf-8", "replace").strip().splitlines()
            # the last line is often a generic "Error opening output files"; prefer one we recognise
            for needle, _ in FRIENDLY_ERRORS:
                for line in lines:
                    if needle.lower() in line.lower():
                        return line
            return lines[-1] if lines else f"ffmpeg exited with code {proc.returncode}"


def run_headless(state_path):
    state = State.load(state_path)
    setup_logging(state.output_root)
    pid_file = state.output_root / PID_NAME
    pid_file.write_text(str(os.getpid()))
    try:
        log.info("Background worker started (%d parallel encodes)", state.workers)
        workers = Workers(state, exit_when_idle=True)
        workers.resize(state.workers)
        workers.join()
        log.info("Background worker finished: %s", state.counts())
    finally:
        pid_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# the crop window

def fmt_time(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def describe_edit(crop, masks):
    parts = [f"crop {crop[2]}×{crop[3]} at ({crop[0]}, {crop[1]})" if crop else "full frame"]
    if masks:
        parts.append(f"{len(masks)} black-out box" + ("es" if len(masks) > 1 else ""))
    return " + ".join(parts)


class App:
    def __init__(self, root, state):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.state = state
        self.workers = Workers(state)
        self.workers.resize(state.workers)
        self.rels = list(state.videos)
        self.index = 0
        self.photo = self.shown = None
        self.src_size = (0, 0)
        self.scale = (1.0, 1.0)  # source pixels per screen pixel, x and y (boxes are kept in source pixels)
        self.thumb = None       # small PhotoImage shown while scrubbing, until the full frame loads
        self.thumb_t = None     # its time in seconds
        self.thumbs = {}        # rel -> Thumbs, for the current and next video
        self.full_load = None   # pending after() id: load the full frame once scrubbing pauses
        self.strip_imgs = []
        self.strip_key = None   # what the filmstrip currently shows, to skip needless redraws
        self.strip_span = (0, 1)  # x range of the filmstrip tiles
        self.origin = (0, 0)
        self.rect = None        # crop box [x0, y0, x1, y1] in source pixels
        self.masks = []         # black-out boxes, same form
        self.tool = tk.StringVar(value="crop")  # what dragging on empty frame draws: "crop" or "mask"
        self.drag = None
        last = next((j for j in reversed(list(state.videos.values())) if j["action"] == "crop"), None)
        self.last_edit = (last["crop"], last.get("masks") or []) if last else None  # carried to the next video
        self.carry = False      # fill in last_edit once the current frame's size is known
        self.applying = None    # result of "apply to all", handed from its thread to tick()
        self.frame_time = {}    # rel -> preview time in seconds
        self.futures = {}       # (rel, t) -> future of (rel, t, duration, image file)
        self.waiting = None     # (key, future) for the frame we're about to show
        self.cache_dir = tempfile.mkdtemp(prefix="bulk_crop_")
        self.img_ids = itertools.count()
        self.previews = ThreadPoolExecutor(max_workers=3)
        self.row_cache = {}
        self.flash_text, self.flash_until = "", 0.0
        self.ticks = 0
        self._build_ui()
        if self.rels:
            undecided = [i for i, r in enumerate(self.rels) if state.videos[r]["action"] is None]
            self.show(undecided[0] if undecided else 0)
        else:
            self.canvas.create_text(20, 20, anchor="nw", fill="white",
                                    text="No videos found. Other files are still being copied.")
        self._refresh()
        self.tick()

    # -- layout --------------------------------------------------------------

    def _build_ui(self):
        tk, ttk, r = self.tk, self.ttk, self.root
        r.title("Bulk Crop")
        r.geometry("1400x860")
        r.minsize(900, 600)
        r.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(r, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text=f"From:  {self.state.input_root}      To:  {self.state.output_root}").pack(side="left")
        ttk.Button(top, text="Open output folder",
                   command=lambda: os.startfile(self.state.output_root)).pack(side="right")
        ttk.Button(top, text="Retry failed / resume", command=self.retry_failed).pack(side="right", padx=6)
        self.n_workers = tk.IntVar(value=self.state.workers)
        ttk.Spinbox(top, from_=1, to=8, width=3, textvariable=self.n_workers, state="readonly",
                    command=self._workers_changed).pack(side="right", padx=(0, 16))
        ttk.Label(top, text="Parallel encodes:").pack(side="right", padx=4)

        self.status = ttk.Label(r, relief="sunken", padding=(8, 3))
        self.status.pack(side="bottom", fill="x")

        panes = ttk.PanedWindow(r, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("status",), selectmode="browse")
        self.tree.heading("#0", text="Video")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=320)
        self.tree.column("status", width=110, stretch=False)
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("queued", "#1f5fbf"), ("running", "#c06000"), ("done", "#1b7f2a"), ("failed", "#c00000")):
            self.tree.tag_configure(tag, foreground=color)
        for rel in self.rels:
            self.tree.insert("", "end", iid=rel, text=rel)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ttk.Frame(panes)
        panes.add(right, weight=4)
        self.info = ttk.Label(right, padding=(6, 4), font=("Segoe UI", 10, "bold"))
        self.info.pack(fill="x")
        self.canvas = tk.Canvas(right, bg="#202020", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonPress-3>", self._delete_box)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Configure>", lambda e: self.root.after_idle(self._render))

        timebar = ttk.Frame(right, padding=(6, 2))
        timebar.pack(fill="x")
        ttk.Label(timebar, text="Preview frame at:").pack(side="left")
        self.time_var = tk.DoubleVar()
        self.time_scale = ttk.Scale(timebar, from_=0, to=1, variable=self.time_var, takefocus=False,
                                    command=lambda _: self._scrub(self.time_var.get(), from_slider=True))
        self.time_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.time_scale.bind("<ButtonRelease-1>", self._time_released)
        self.time_label = ttk.Label(timebar, width=9)
        self.time_label.pack(side="left")
        ttk.Label(timebar, text="   Wheel or [ ] steps").pack(side="left")
        # filmstrip of the whole video: find the part you want at a glance, click or drag to jump there
        self.strip = tk.Canvas(right, height=54, bg="#151515", highlightthickness=0, cursor="hand2")
        self.strip.pack(fill="x", padx=6, pady=(0, 2))
        self.strip.bind("<ButtonPress-1>", self._strip_seek)
        self.strip.bind("<B1-Motion>", self._strip_seek)
        self.strip.bind("<ButtonRelease-1>", lambda e: self._load_full_soon(0))
        self.strip.bind("<Configure>", lambda e: self._draw_strip(force=True))
        for w in (self.strip, self.canvas):
            w.bind("<MouseWheel>", lambda e: self._step(-1 if e.delta > 0 else 1))

        self.crop_label = ttk.Label(right, padding=(6, 2))
        self.crop_label.pack(fill="x")
        tools = ttk.Frame(right, padding=(6, 2))
        tools.pack(fill="x")
        ttk.Label(tools, text="Draw:").pack(side="left")
        for value, text in (("crop", "Crop box"), ("mask", "Black-out box")):
            ttk.Radiobutton(tools, text=text, value=value, variable=self.tool, command=self._draw_rect,
                            takefocus=False).pack(side="left", padx=4)
        ttk.Label(tools, text="[B] switches.   Draw as many black-out boxes as you like; "
                              "right-click a box to delete it.").pack(side="left", padx=8)
        btns = ttk.Frame(right, padding=6)
        btns.pack(fill="x")
        ttk.Button(btns, text="◀ Prev", command=self.prev, takefocus=False).pack(side="left")
        ttk.Button(btns, text="Clear boxes  [Esc]", command=self.clear, takefocus=False).pack(side="left", padx=4)
        ttk.Button(btns, text="Reuse last boxes  [P]", command=self.reuse, takefocus=False).pack(side="left", padx=4)
        ttk.Button(btns, text="Copy unchanged  [U]", command=self.copy_as_is,
                   takefocus=False).pack(side="left", padx=4)
        ttk.Button(btns, text="Next ▶", command=self.next, takefocus=False).pack(side="right")
        ttk.Button(btns, text="✔  Save & next  [Enter]", command=self.confirm,
                   takefocus=False).pack(side="right", padx=4)
        ttk.Button(btns, text="Apply to all remaining  [A]", command=self.apply_all,
                   takefocus=False).pack(side="right", padx=4)

        def key(fn):
            return lambda e: None if isinstance(e.widget, (ttk.Spinbox, tk.Spinbox, ttk.Entry)) else fn()
        for seq, fn in (("<Return>", self.confirm), ("<KP_Enter>", self.confirm), ("<Escape>", self.clear),
                        ("<Right>", self.next), ("<Left>", self.prev), ("p", self.reuse), ("P", self.reuse),
                        ("u", self.copy_as_is), ("U", self.copy_as_is), ("b", self.switch_tool),
                        ("B", self.switch_tool), ("a", self.apply_all), ("A", self.apply_all),
                        ("[", lambda: self._step(-1)), ("]", lambda: self._step(1))):
            r.bind(seq, key(fn))

    # -- navigation ----------------------------------------------------------

    def current(self):
        return self.rels[self.index] if self.rels else None

    def show(self, i):
        self.index = i % len(self.rels)
        rel = self.current()
        job = self.state.videos[rel]
        self.rect, self.masks = None, []
        if job["action"] == "crop":
            self.rect = self._corners(job["crop"]) if job["crop"] else None
            self.masks = [self._corners(m) for m in job.get("masks") or []]
        self.carry = job["action"] is None and self.last_edit is not None
        if self.tree.selection() != (rel,):
            self.tree.selection_set(rel)
        self.tree.see(rel)
        self.photo = self.shown = self.thumb = self.thumb_t = None
        if self.full_load:
            self.root.after_cancel(self.full_load)
            self.full_load = None
        self.canvas.delete("all")
        self.canvas.create_text(20, 20, anchor="nw", fill="#bbbbbb", text="Loading preview…")
        key = (rel, self.frame_time.get(rel))
        self.waiting = (key, self._request(*key))
        for step in (1, 2, 3):  # warm up the next few so flipping through is instant
            nxt = self.rels[(self.index + step) % len(self.rels)]
            self._request(nxt, self.frame_time.get(nxt))
        self._thumbs_for(rel)
        self._draw_strip(force=True)
        self._update_labels()

    # -- fast scrubbing ------------------------------------------------------

    def _thumbs_for(self, rel):
        """The Thumbs of rel, starting it if needed; only the current and next video's are kept."""
        if rel not in self.thumbs:
            self.thumbs[rel] = Thumbs(self.state.src(rel))
        keep = {self.current(), self.rels[(self.index + 1) % len(self.rels)], rel}
        for r in [r for r in self.thumbs if r not in keep]:
            self.thumbs.pop(r).cancel()
        return self.thumbs[rel]

    def _scrub(self, t, from_slider=False):
        """Show the nearest small frame to t straight away; the full frame follows once you pause."""
        rel = self.current()
        if not (rel and self.photo):
            return
        th = self.thumbs.get(rel)
        hit = th.nearest(t) if th else None
        self.waiting = None  # a full frame still on its way is out of date now
        if hit:
            self.thumb_t, frame = hit
            self.thumb = self.tk.PhotoImage(data=frame, format="PPM")
            self._render()
        else:
            self.thumb_t = t
        if not from_slider:
            self.time_var.set(self.thumb_t)
        self.time_label.config(text=fmt_time(self.thumb_t))
        self._draw_strip_marker()
        self._load_full_soon(400 if not from_slider else None)

    def _load_full_soon(self, delay):
        """Load the full-resolution frame at the scrub position after delay ms (None: wait for release)."""
        if self.full_load:
            self.root.after_cancel(self.full_load)
            self.full_load = None
        if delay is not None:
            self.full_load = self.root.after(delay, self._load_full)

    def _load_full(self):
        self.full_load = None
        rel = self.current()
        if rel and self.photo and self.thumb_t is not None:
            t = round(self.thumb_t, 3)
            self.frame_time[rel] = t
            self.waiting = ((rel, t), self._request(rel, t))

    def _step(self, direction):
        rel = self.current()
        th = self.thumbs.get(rel)
        if not (th and self.photo):
            return
        now = self.thumb_t if self.thumb_t is not None else self.time_var.get()
        t = th.step(now, direction)
        if t is not None:
            self._scrub(t)

    def _strip_seek(self, e):
        th = self.thumbs.get(self.current())
        x0, x1 = self.strip_span
        if th and th.duration > 0 and x1 > x0:
            self._scrub(min(max((e.x - x0) / (x1 - x0), 0), 1) * th.duration)
            self._load_full_soon(None)  # wait for the button release

    def _draw_strip(self, force=False):
        """Tiles across the filmstrip, each the frame nearest the middle of its slice of the video."""
        th = self.thumbs.get(self.current())
        key = (self.current(), th.version if th else None, self.strip.winfo_width())
        if key == self.strip_key and not force:
            return
        self.strip_key = key
        self.strip.delete("all")
        self.strip_imgs = []
        if not th or not th.times or th.duration <= 0:
            self.strip.create_text(8, 27, anchor="w", fill="#888888",
                                   text="Building the filmstrip…" if not (th and th.done) else "No filmstrip for this video")
            return
        first = self.tk.PhotoImage(data=th.frames[0], format="PPM")
        k = max(1, round(first.height() / 50))
        tile_w = max(1, first.width() // k)
        width = max(self.strip.winfo_width(), 100)
        n = max(1, width // tile_w)
        x0 = (width - n * tile_w) // 2
        self.strip_span = (x0, x0 + n * tile_w)
        for i in range(n):
            hit = th.nearest((i + 0.5) / n * th.duration)
            img = self.tk.PhotoImage(data=hit[1], format="PPM").subsample(k)
            self.strip_imgs.append(img)
            self.strip.create_image(x0 + i * tile_w, 2, image=img, anchor="nw")
        self._draw_strip_marker()

    def _draw_strip_marker(self):
        self.strip.delete("marker")
        th = self.thumbs.get(self.current())
        x0, x1 = self.strip_span
        if th and th.duration > 0:
            t = self.thumb_t if self.thumb_t is not None else self.time_var.get()
            x = x0 + (x1 - x0) * min(max(t / th.duration, 0), 1)
            self.strip.create_line(x, 0, x, 54, fill="#ffd400", width=3, tags="marker")

    def next(self):
        if self.rels:
            self.show(self.index + 1)

    def prev(self):
        if self.rels:
            self.show(self.index - 1)

    def _advance(self):
        n = len(self.rels)
        for step in range(1, n + 1):
            j = (self.index + step) % n
            if self.state.videos[self.rels[j]]["action"] is None:
                self.show(j)
                return
        self._update_labels()
        self.flash("Every video has a decision. Encoding continues in the background.")

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if sel and sel[0] != self.current():
            self.show(self.rels.index(sel[0]))

    # -- decisions -----------------------------------------------------------

    def confirm(self):
        rel = self.current()
        if not rel:
            return
        if not ((self.rect or self.masks) and self.photo):
            self.flash("Draw a crop box or a black-out box first, or press U to copy this video unchanged.")
            return
        crop, masks = self._edit_values()
        self.state.decide(rel, "crop", crop, masks)
        self.last_edit = (crop, masks)
        self._advance()

    def copy_as_is(self):
        if self.current():
            self.state.decide(self.current(), "copy")
            self._advance()

    def clear(self):
        """Remove the boxes of the current drawing tool."""
        if self.tool.get() == "crop":
            self.rect = None
        else:
            self.masks = []
        self._draw_rect()

    def switch_tool(self):
        self.tool.set("mask" if self.tool.get() == "crop" else "crop")
        self._draw_rect()

    def reuse(self):
        if not (self.last_edit and self.photo):
            self.flash("No previous boxes to reuse yet.")
            return
        self._apply_last()
        self._draw_rect()

    def _fit(self, box):
        """An [x, y, w, h] box moved/shrunk to fit this frame, as corners."""
        x, y, w, h = box
        W, H = self.src_size
        w, h = min(w, W), min(h, H)
        x, y = min(x, W - w), min(y, H - h)
        return [x, y, x + w, y + h]

    def _apply_last(self):
        crop, masks = self.last_edit
        self.rect = self._fit(crop) if crop else None
        self.masks = [self._fit(m) for m in masks]

    def apply_all(self):
        """Save the current boxes on this video and every undecided video with the same frame size."""
        from tkinter import messagebox
        rel = self.current()
        if not ((self.rect or self.masks) and self.photo):
            self.flash("Draw a crop box or a black-out box first, then apply it to all remaining videos.")
            return
        if self.applying is not None:
            return
        targets = [r for r in self.rels if r == rel or self.state.videos[r]["action"] is None]
        if not messagebox.askyesno(
                "Bulk Crop", f"Apply these boxes to this video and the {len(targets) - 1} other videos "
                             "that don't have a decision yet?\n\nVideos with a different frame size are "
                             "skipped and left for you to do."):
            return
        crop, masks = self._edit_values()
        self.applying = "running"
        self.flash_text, self.flash_until = f"Checking frame sizes of {len(targets)} videos…", time.time() + 3600
        self._refresh()

        def frame_size(r):
            info = probe(self.state.src(r))
            return info["width"], info["height"]

        def work():
            same = []
            try:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    sizes = list(ex.map(frame_size, targets))
                size = sizes[targets.index(rel)]
                same = [r for r, s in zip(targets, sizes) if s == size and s[0]]
                for r in same:
                    if r == rel or self.state.videos[r]["action"] is None:  # may have changed meanwhile
                        self.state.decide(r, "crop", crop, masks)
            except Exception as e:
                log.info("apply to all failed: %s", e)
            finally:
                self.applying = (len(same), len(targets) - len(same), crop, masks)
        threading.Thread(target=work, daemon=True).start()

    def _applied(self):
        done, skipped, crop, masks = self.applying
        self.applying = None
        self.last_edit = (crop, masks)
        self.flash_until = 0
        self._advance()
        self.flash(f"Applied to {done} videos." +
                   (f"  {skipped} skipped (different frame size) and still need you." if skipped else ""))

    def retry_failed(self):
        self.state.retry_failed()
        self.flash("Failed items re-queued.")

    def _workers_changed(self):
        self.workers.resize(self.n_workers.get())
        self.state.save()

    # -- preview frames ------------------------------------------------------

    def _load(self, rel, t):
        src = self.state.src(rel)
        duration = probe(src)["duration"]
        if t is None:
            t = min(5.0, duration * 0.1)
        img = os.path.join(self.cache_dir, f"{next(self.img_ids)}.ppm")
        extract_frame(src, t, img)
        return rel, t, duration, img

    def _request(self, rel, t):
        key = (rel, t)
        if key not in self.futures:
            self.futures[key] = self.previews.submit(self._load, rel, t)
        return self.futures[key]

    def _time_released(self, _=None):
        if self.thumb_t is None:
            self.thumb_t = self.time_var.get()
        self._load_full_soon(0)

    def _display(self, key, fut):
        try:
            rel, t, duration, img = fut.result()
        except Exception as e:
            self.futures.pop(key, None)  # allow a retry next time
            if key[0] == self.current():
                self.canvas.delete("all")
                self.canvas.create_text(20, 20, anchor="nw", fill="#ff8080", width=800,
                                        text=f"Could not read this video:\n{friendly(str(e))}\n\n"
                                             "Press U to copy it as-is, or come back to it later.")
            return
        if rel != self.current():
            return
        self.frame_time[rel] = t
        self.time_scale.configure(to=max(duration, 0.1))
        self.time_var.set(t)
        self.time_label.config(text=fmt_time(t))
        self.photo = self.tk.PhotoImage(file=img)
        self.futures.pop(key, None)  # the file goes now (big, uncompressed); coming back re-extracts it
        try:
            os.remove(img)
        except OSError:
            pass
        self.src_size = (self.photo.width(), self.photo.height())
        self.thumb, self.thumb_t = None, t
        if self.carry:  # undecided video: start from the last saved boxes, so Enter accepts them
            self.carry = False
            self._apply_last()
        self._render()
        self._draw_strip_marker()

    def _render(self):
        """Show the full frame (shrunk to fit), or while scrubbing the small frame blown up to about the
        same size. Either way self.scale maps screen to source pixels, so boxes stay put."""
        if not self.photo:
            return
        self.canvas.delete("all")
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        W, H = self.src_size
        fit = min(cw / W, ch / H, 1.0)  # how big the full frame can be shown
        img = self.thumb or self.photo
        # Tk only scales by whole-number zoom/subsample, so pick the fraction closest below the fit
        p, q = max(((p, q) for p in range(1, 9) for q in range(1, 9)
                    if p / q <= fit * W / img.width()), key=lambda pq: pq[0] / pq[1], default=(1, 8))
        if (p, q) == (1, 1):
            self.shown = img
        else:
            self.shown = self.tk.PhotoImage()
            self.shown.tk.call(self.shown, "copy", img, "-zoom", p, p, "-subsample", q, q)
        self.scale = (W / self.shown.width(), H / self.shown.height())
        self.origin = ((cw - self.shown.width()) // 2, (ch - self.shown.height()) // 2)
        self.canvas.create_image(*self.origin, image=self.shown, anchor="nw")
        self._draw_rect()

    # -- drawing the crop box ------------------------------------------------

    def _to_src(self, ex, ey):
        (ox, oy), (W, H), (kx, ky) = self.origin, self.src_size, self.scale
        return min(max((ex - ox) * kx, 0), W), min(max((ey - oy) * ky, 0), H)

    def _to_canvas(self, sx, sy):
        (ox, oy), (kx, ky) = self.origin, self.scale
        return ox + sx / kx, oy + sy / ky

    @staticmethod
    def _norm(r):
        return [min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])]

    @staticmethod
    def _corners(box):
        x, y, w, h = box
        return [x, y, x + w, y + h]

    def _crop_values(self):
        """The box as ffmpeg crop values, snapped to even numbers (needed for 4:2:0 video)."""
        x0, y0, x1, y1 = self._norm(self.rect)
        W, H = self.src_size
        x, y = int(x0) // 2 * 2, int(y0) // 2 * 2
        w = min(int(x1 - x), W - x) // 2 * 2
        h = min(int(y1 - y), H - y) // 2 * 2
        return [x, y, max(w, 2), max(h, 2)]

    def _mask_values(self, r):
        """A black-out box as [x, y, w, h], grown outward to even numbers so nothing at its edge survives."""
        x0, y0, x1, y1 = self._norm(r)
        W, H = self.src_size
        x, y = int(x0) // 2 * 2, int(y0) // 2 * 2
        x1, y1 = min(math.ceil(x1 / 2) * 2, W), min(math.ceil(y1 / 2) * 2, H)
        return [x, y, x1 - x, y1 - y]

    def _edit_values(self):
        crop = self._crop_values() if self.rect else None
        return crop, [self._mask_values(m) for m in self.masks]

    def _box(self, target):
        return self.rect if target == "crop" else self.masks[target]

    def _set_box(self, target, r):
        if target == "crop":
            self.rect = r
        else:
            self.masks[target] = r

    def _hit(self, ex, ey):
        """(box, edges) under the pointer, for boxes of the current tool: box is "crop" or a mask
        index, edges a combo of "l", "r", "t", "b", or "move". None if over no box."""
        if not self.photo:
            return None
        if self.tool.get() == "crop":
            candidates = ["crop"] if self.rect else []
        else:
            candidates = list(reversed(range(len(self.masks))))  # topmost (latest) first
        for target in candidates:
            edges = self._edges(self._box(target), ex, ey)
            if edges:
                return target, edges
        return None

    def _edges(self, r, ex, ey):
        x0, y0, x1, y1 = self._norm(r)
        (cx0, cy0), (cx1, cy1) = self._to_canvas(x0, y0), self._to_canvas(x1, y1)
        tol = 8
        if not (cx0 - tol <= ex <= cx1 + tol and cy0 - tol <= ey <= cy1 + tol):
            return None
        edges = ""
        if abs(ex - cx0) <= tol:
            edges += "l"
        elif abs(ex - cx1) <= tol:
            edges += "r"
        if abs(ey - cy0) <= tol:
            edges += "t"
        elif abs(ey - cy1) <= tol:
            edges += "b"
        return edges or "move"

    def _hover(self, e):
        hit = self._hit(e.x, e.y)
        cursor = {None: "crosshair", "move": "fleur", "l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
                  "t": "sb_v_double_arrow", "b": "sb_v_double_arrow"}.get(hit and hit[1], "sizing")
        self.canvas.config(cursor=cursor)

    def _press(self, e):
        if not self.photo:
            return
        hit = self._hit(e.x, e.y)
        sx, sy = self._to_src(e.x, e.y)
        if hit is None:  # start a new box
            if self.tool.get() == "crop":
                self.rect, hit = [sx, sy, sx, sy], ("crop", "rb")
            else:
                self.masks.append([sx, sy, sx, sy])
                hit = (len(self.masks) - 1, "rb")
        else:
            self._set_box(hit[0], self._norm(self._box(hit[0])))
        self.drag = (*hit, sx, sy, list(self._box(hit[0])))

    def _drag(self, e):
        if not self.drag:
            return
        target, hit, sx0, sy0, orig = self.drag
        sx, sy = self._to_src(e.x, e.y)
        if hit == "move":
            (W, H), w, h = self.src_size, orig[2] - orig[0], orig[3] - orig[1]
            nx = min(max(orig[0] + sx - sx0, 0), W - w)
            ny = min(max(orig[1] + sy - sy0, 0), H - h)
            self._set_box(target, [nx, ny, nx + w, ny + h])
        else:
            r = list(orig)
            if "l" in hit:
                r[0] = sx
            if "r" in hit:
                r[2] = sx
            if "t" in hit:
                r[1] = sy
            if "b" in hit:
                r[3] = sy
            self._set_box(target, r)
        self._draw_rect()

    def _release(self, _):
        if not self.drag:
            return
        target, self.drag = self.drag[0], None
        x0, y0, x1, y1 = self._norm(self._box(target))
        if x1 - x0 < 8 or y1 - y0 < 8:  # just a click: drop the box
            self._remove_box(target)
        else:
            self._set_box(target, [x0, y0, x1, y1])
        self._draw_rect()

    def _remove_box(self, target):
        if target == "crop":
            self.rect = None
        else:
            del self.masks[target]

    def _delete_box(self, e):
        hit = self._hit(e.x, e.y)
        if hit and not self.drag:
            self._remove_box(hit[0])
            self._draw_rect()

    def _draw_rect(self):
        self.canvas.delete("crop")
        if self.photo:
            ix0, iy0 = self.origin
            ix1, iy1 = ix0 + self.shown.width(), iy0 + self.shown.height()
            editing = self.tool.get()

            def handles(cx0, cy0, cx1, cy1, color):
                for hx, hy in ((cx0, cy0), (cx1, cy0), (cx0, cy1), (cx1, cy1)):
                    self.canvas.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill=color, outline="", tags="crop")

            for m in self.masks:  # solid black, as they'll come out in the video
                x0, y0, x1, y1 = self._norm(m)
                (cx0, cy0), (cx1, cy1) = self._to_canvas(x0, y0), self._to_canvas(x1, y1)
                self.canvas.create_rectangle(cx0, cy0, cx1, cy1, fill="black", outline="#ff4040",
                                             width=2 if editing == "mask" else 1, tags="crop")
                if editing == "mask":
                    handles(cx0, cy0, cx1, cy1, "#ff4040")
            if self.rect:
                x0, y0, x1, y1 = self._norm(self.rect)
                (cx0, cy0), (cx1, cy1) = self._to_canvas(x0, y0), self._to_canvas(x1, y1)
                shade = dict(fill="black", stipple="gray50", outline="", tags="crop")
                for box in ((ix0, iy0, ix1, cy0), (ix0, cy1, ix1, iy1), (ix0, cy0, cx0, cy1), (cx1, cy0, ix1, cy1)):
                    self.canvas.create_rectangle(*box, **shade)
                self.canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="#ffd400",
                                             width=2 if editing == "crop" else 1, tags="crop")
                if editing == "crop":
                    handles(cx0, cy0, cx1, cy1, "#ffd400")
        self._update_labels()

    # -- status --------------------------------------------------------------

    def _update_labels(self):
        rel = self.current()
        if not rel:
            return
        job = self.state.videos[rel]
        size = f"     ({self.src_size[0]}×{self.src_size[1]})" if self.photo else ""
        self.info.config(text=f"{self.index + 1} / {len(self.rels)}     {rel}{size}")
        if (self.rect or self.masks) and self.photo:
            text = "Now: " + describe_edit(*self._edit_values())
        elif self.tool.get() == "crop":
            text = "Drag on the frame to draw a crop box. Drag inside it to move it, drag its edges to resize."
        else:
            text = "Drag on the frame to draw a black-out box. Drag inside one to move it, drag its edges to resize."
        if job["action"] == "crop":
            text += "          Saved: " + describe_edit(job["crop"], job.get("masks") or [])
        elif job["action"] == "copy":
            text += "          Saved: copy unchanged"
        if job["status"] == "failed":
            text += f"          ERROR: {job['error']}"
        self.crop_label.config(text=text)

    def _row(self, rel, job):
        if ("video", rel) in self.state.active:
            if job["action"] == "copy":
                return "copying…", "running"
            p = self.state.progress.get(rel)
            return (f"encoding {p:.0%}" if p is not None else "encoding…"), "running"
        copy = job["action"] == "copy"
        return {"todo": ("to do", "todo"),
                "queued": ("copy queued" if copy else "queued", "queued"),
                "done": ("copied ✓" if copy else "done ✓", "done"),
                "failed": ("failed ✗", "failed")}[job["status"]]

    def _refresh(self):
        with self.state.lock:
            rows = {rel: self._row(rel, job) for rel, job in self.state.videos.items()}
        for rel, row in rows.items():
            if self.row_cache.get(rel) != row:
                self.row_cache[rel] = row
                self.tree.item(rel, values=(row[0],), tags=(row[1],))
        if time.time() < self.flash_until:
            self.status.config(text=self.flash_text)
            return
        c = self.state.counts()
        text = (f"Videos:  {c['todo']} to do  ·  {c['queued']} waiting  ·  {c['running']} encoding  ·  "
                f"{c['done']} done  ·  {c['failed']} failed          "
                f"Other files copied: {c['files_done']}/{c['files_total']}")
        if c["files_failed"]:
            text += f"  ({c['files_failed']} failed, see {LOG_NAME})"
        hold = self.state.paused or self.state.low_space
        if hold:
            text = f"⚠ {hold}          {text}"
        self.status.config(text=text, foreground="#c00000" if hold else "")

    def flash(self, msg):
        self.flash_text, self.flash_until = msg, time.time() + 4
        self.root.bell()
        self._refresh()

    def tick(self):
        if self.waiting and self.waiting[1].done():
            (key, fut), self.waiting = self.waiting, None
            self._display(key, fut)
        if isinstance(self.applying, tuple):
            self._applied()
        self.ticks += 1
        if self.ticks % 5 == 0:
            self._refresh()
            if self.rels:
                self._draw_strip()  # only redraws when new frames have arrived
                th = self.thumbs.get(self.current())
                if th and th.done and len(self.rels) > 1:  # get the next video's filmstrip ready too
                    self._thumbs_for(self.rels[(self.index + 1) % len(self.rels)])
            if self.rels and self.state.videos[self.current()]["status"] == "failed":
                self._update_labels()
        self.root.after(100, self.tick)

    def on_close(self):
        from tkinter import messagebox
        busy = self.state.has_work()
        background = False
        if busy:
            background = messagebox.askyesnocancel(
                "Bulk Crop",
                "Some videos are still encoding or waiting.\n\n"
                "Yes:  keep going in the background after this window closes\n"
                "No:  stop now (it picks up where it left off next time you open this folder)\n"
                "Cancel:  go back")
            if background is None:
                return
        self.workers.stop()
        self.workers.join(timeout=15)
        self.state.save()
        pid_file = self.state.output_root / PID_NAME
        try:
            if pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
        if busy and background:
            launch_background(self.state)
        for th in self.thumbs.values():
            th.cancel()
        self.previews.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        self.root.destroy()


# ---------------------------------------------------------------------------

def open_gui(input_arg, output_arg):
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.withdraw()
    if not (FFMPEG and FFPROBE):
        messagebox.showerror("Bulk Crop", "ffmpeg/ffprobe not found on PATH.\nInstall with:  winget install Gyan.FFmpeg")
        return
    inp = input_arg or filedialog.askdirectory(title="Choose the top folder that contains your videos")
    if not inp:
        return
    inp = Path(inp).resolve()
    if output_arg:
        out = Path(output_arg).resolve()
    else:
        out = inp.with_name(f"{inp.name or 'videos'}_cropped")
        if not messagebox.askyesno(
                "Bulk Crop", f"Cropped videos will be saved to:\n\n{out}\n\n"
                             "with exactly the same subfolders as the originals.\n\n"
                             "Use this location?  (No = choose another folder)"):
            chosen = filedialog.askdirectory(title="Choose where to save the cropped copies")
            if not chosen:
                return
            out = Path(chosen).resolve()
    if out == inp or inp in out.parents:
        messagebox.showerror("Bulk Crop", "The output folder can't be the input folder or inside it.")
        return
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(out)

    # another window, or a background job from an earlier session, may be working on this folder
    pid_file = out / PID_NAME
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        pid = None
    if pid and pid != os.getpid() and pid_alive(pid):
        if not messagebox.askyesno("Bulk Crop", "Bulk Crop is already working on this folder (another window, "
                                                "or a background job).\n\nStop it and continue here? (Videos "
                                                "it was in the middle of will restart.)"):
            return
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        time.sleep(1)
    pid_file.write_text(str(os.getpid()))

    state_path = out / STATE_NAME
    if state_path.exists():
        state = State.load(state_path)
        if state.input_root.resolve() != inp and not messagebox.askyesno(
                "Bulk Crop", f"This output folder was last used for:\n{state.input_root}\n\n"
                             f"Use it for\n{inp}\nanyway?"):
            return
        state.input_root, state.output_root = inp, out
    else:
        state = State(state_path, inp, out)
    state.scan()
    log.info("Opened %s -> %s: %d videos, %d other files", inp, out, len(state.videos), len(state.files))
    root.deiconify()
    App(root, state)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Bulk-crop videos in a folder tree (see module docstring).")
    ap.add_argument("input", nargs="?", help="top folder containing the videos")
    ap.add_argument("-o", "--output", help="where to write the mirrored, cropped tree")
    ap.add_argument("--process", metavar="STATE", help="headless: finish queued jobs in this state file")
    args = ap.parse_args()
    if args.process:
        if not (FFMPEG and FFPROBE):
            sys.exit("ffmpeg/ffprobe not found on PATH")
        run_headless(args.process)
    else:
        open_gui(args.input, args.output)


if __name__ == "__main__":
    main()
