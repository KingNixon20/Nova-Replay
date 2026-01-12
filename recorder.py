import os
import shutil
import subprocess
import time
from datetime import datetime
import shutil
import tempfile

from typing import Optional, Callable

# Default recordings directory should be a writable user location.
# When running from an AppImage the package is read-only, so avoid
# placing recordings under the package tree. Prefer ~/Videos/Nova
DEFAULT_RECORDINGS_DIR = os.path.expanduser('~/Videos/Nova')
RECORDINGS_DIR = os.environ.get('NOVA_RECORDINGS_DIR', DEFAULT_RECORDINGS_DIR)
os.makedirs(RECORDINGS_DIR, exist_ok=True)


def set_recordings_dir(path: str):
    """Update the module-level recordings directory used by Recorder and helpers.

    This does not move existing files; it ensures the new directory exists.
    """
    global RECORDINGS_DIR
    RECORDINGS_DIR = os.path.abspath(path)
    os.makedirs(RECORDINGS_DIR, exist_ok=True)


def is_command_available(cmd: str) -> bool:
    return shutil.which(cmd) is not None


class Recorder:
    """Simple recorder wrapper that chooses a capture backend based on the environment.

    It currently supports:
    - X11: ffmpeg + x11grab
    - Wayland: wf-recorder (if available) or attempts ffmpeg pipewire input (best-effort)

    The implementation spawns external processes and provides start/stop methods.
    You can optionally pass `preferred_backend` to force a backend when starting.
    Valid values: 'auto', 'ffmpeg-x11', 'wf-recorder', 'pipewire'
    """

    def __init__(self, filename: Optional[str] = None, mode: str = "fullscreen", area: Optional[tuple] = None, fps: int = 60, preferred_backend: str = 'auto', settings: Optional[dict] = None):
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        self.mode = mode
        self.area = area
        self.fps = fps
        self.proc: Optional[subprocess.Popen] = None
        self.on_stop: Optional[Callable[[str], None]] = None
        self.preferred_backend = preferred_backend or 'auto'
        # optional callback to report startup errors to the UI: on_error(message)
        self.on_error: Optional[Callable[[str], None]] = None
        # encoder / recording settings supplied by UI
        self.settings = settings or {}
        # temporary file used by some backends (wf-recorder)
        self.tempfile: Optional[str] = None
        # last opened log file handle for subprocess output (so we can close it)
        self._log_handle = None
        if filename:
            self.outfile = filename
        else:
            container = (self.settings or {}).get('container', 'mp4')
            self.outfile = os.path.join(RECORDINGS_DIR, datetime.now().strftime(f"rec_%Y%m%d_%H%M%S.{container}"))

    def detect_display(self) -> str:
        # prefer explicit Wayland socket presence — some setups expose X11 variables
        # even when Wayland is available (XWayland). If a Wayland display socket
        # is present, treat the session as Wayland so Wayland-native capture
        # (wf-recorder / PipeWire) can be preferred.
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        xdg = os.environ.get("XDG_SESSION_TYPE", "")
        if xdg:
            return xdg.lower()
        if os.environ.get("DISPLAY"):
            return "x11"
        return "unknown"

    def start(self):
        if self.proc:
            return
        # Determine backend: honor preferred_backend when possible
        disp = self.detect_display()
        backend = getattr(self, 'preferred_backend', 'auto') or 'auto'

        def choose_auto():
            if disp == "x11":
                return 'ffmpeg-x11'
            if disp == "wayland":
                if is_command_available("wf-recorder"):
                    return 'wf-recorder'
                return 'pipewire'
            return 'ffmpeg-x11'

        if backend == 'auto':
            backend = choose_auto()
        # validate availability where applicable
        if backend == 'wf-recorder' and not is_command_available('wf-recorder'):
            backend = choose_auto()

        if backend == 'ffmpeg-x11':
            cmd = self._ffmpeg_x11_cmd()
            print("Starting recorder with:", " ".join(cmd))
            logp = self._open_log('ffmpeg')
            self._log_handle = logp
            self.proc = subprocess.Popen(cmd, stdout=logp, stderr=logp)
            return

        if backend == 'pipewire':
            cmd = self._ffmpeg_pipewire_cmd()
            print("Starting recorder with:", " ".join(cmd))
            logp = self._open_log('ffmpeg')
            self._log_handle = logp
            self.proc = subprocess.Popen(cmd, stdout=logp, stderr=logp)
            return

        if backend == 'wf-recorder':
            # wf-recorder: record to a temp file under RECORDINGS_DIR, then transcode/move on stop
            # Create a unique path for the tempfile but remove the zero-length
            # file so `wf-recorder` doesn't prompt to overwrite an existing file.
            tmpf = tempfile.NamedTemporaryFile(delete=False, dir=RECORDINGS_DIR, suffix='.mkv')
            tmpf_name = tmpf.name
            tmpf.close()
            try:
                os.remove(tmpf_name)
            except Exception:
                pass
            self.tempfile = tmpf_name
            # ensure no stale file at the path (avoid interactive overwrite prompt)
            try:
                if os.path.exists(self.tempfile):
                    os.remove(self.tempfile)
            except Exception:
                pass
            cmd = self._wf_recorder_cmd(output_override=self.tempfile)
            print("Starting recorder with:", " ".join(cmd))
            logp = self._open_log('wf-recorder')
            self._log_handle = logp
            proc = subprocess.Popen(cmd, stdout=logp, stderr=logp)
            # give it a moment to fail fast
            try:
                time.sleep(0.5)
            except Exception:
                pass
            rc = proc.poll()
            if rc is not None and rc != 0:
                # process exited quickly; check logs for reason
                print('wf-recorder failed to start; check logs in recordings dir')
                # choose fallback based on display
                if disp == 'x11':
                    print('Falling back to ffmpeg x11grab')
                    cmd2 = self._ffmpeg_x11_cmd()
                    print('Starting recorder with:', ' '.join(cmd2))
                    logp2 = self._open_log('ffmpeg')
                    self._log_handle = logp2
                    self.proc = subprocess.Popen(cmd2, stdout=logp2, stderr=logp2)
                    return
                elif disp == 'wayland':
                    # attempt ffmpeg pipewire fallback if ffmpeg available
                    if is_command_available('ffmpeg'):
                        # ensure ffmpeg supports the pipewire protocol
                        if not self._ffmpeg_supports_pipewire():
                            msg = ('ffmpeg on this system lacks PipeWire support; install an ffmpeg build with PipeWire support or enable PipeWire/xdg-desktop-portal.')
                            print(msg)
                            if callable(getattr(self, 'on_error', None)):
                                try:
                                    self.on_error(msg)
                                except Exception:
                                    pass
                            return
                        print('Attempting ffmpeg pipewire fallback (best-effort)')
                        cmd2 = self._ffmpeg_pipewire_cmd()
                        print('Starting recorder with:', ' '.join(cmd2))
                        logp2 = self._open_log('ffmpeg')
                        self._log_handle = logp2
                        p2 = subprocess.Popen(cmd2, stdout=logp2, stderr=logp2)
                        try:
                            time.sleep(0.5)
                        except Exception:
                            pass
                        rc2 = p2.poll()
                        if rc2 is None:
                            # ffmpeg seems to be running
                            self.proc = p2
                            return
                        else:
                            print('ffmpeg pipewire fallback failed; check logs in recordings dir')
                            print('Please ensure PipeWire and xdg-desktop-portal are installed and configured for Wayland (KDE).')
                            return
                    else:
                        print('ffmpeg not found; cannot fallback on Wayland. Install PipeWire/ffmpeg or use X11 backend.')
                        return
                else:
                    print('Unknown display type; cannot fallback automatically.')
                    return
            # wf-recorder started successfully
            self.proc = proc
            return

        # default fallback
        cmd = self._ffmpeg_x11_cmd()
        print("Starting recorder with:", " ".join(cmd))
        logp = self._open_log('ffmpeg')
        self._log_handle = logp
        self.proc = subprocess.Popen(cmd, stdout=logp, stderr=logp)

    def stop(self):
        if not self.proc:
            return
        proc = self.proc
        # If wf-recorder was used (we created a tempfile), prefer a graceful shutdown
        if self.tempfile:
            try:
                import signal
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        else:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.proc = None
        # If wf-recorder wrote to a tempfile, finalize it (wait for file to be closed, then transcode/move)
        if self.tempfile and os.path.exists(self.tempfile):
            # wait for the tempfile to finish being written: poll until size stabilizes
            try:
                prev = -1
                stable_count = 0
                for _ in range(100):
                    try:
                        cur = os.path.getsize(self.tempfile)
                    except Exception:
                        cur = -1
                    if cur == prev and cur > 0:
                        stable_count += 1
                        if stable_count >= 3:
                            break
                    else:
                        stable_count = 0
                    prev = cur
                    time.sleep(0.1)
            except Exception:
                pass
            final = self.outfile
            # helper: check for EBML header at file start (Matroska)
            def _has_ebml_hdr(path: str) -> bool:
                try:
                    with open(path, 'rb') as fh:
                        magic = fh.read(4)
                        return magic == b'\x1A\x45\xDF\xA3'
                except Exception:
                    return False
            # close any open log handle so buffers are flushed
            try:
                if self._log_handle and hasattr(self._log_handle, 'close'):
                    try:
                        self._log_handle.close()
                    except Exception:
                        pass
            finally:
                self._log_handle = None

            # try to transcode using ffmpeg if available, to respect encoder settings
            if shutil.which('ffmpeg'):
                # ensure file has an EBML header (MKV); wait a bit longer if not
                if not _has_ebml_hdr(self.tempfile):
                    waited = 0
                    while waited < 5 and not _has_ebml_hdr(self.tempfile):
                        time.sleep(0.5)
                        waited += 0.5
                if not _has_ebml_hdr(self.tempfile):
                    print('Temporary recording missing EBML header; skipping transcode and moving raw file.')
                    # fall through to move without transcoding
                    try:
                        # adjust final name to keep original container
                        base, _ = os.path.splitext(final)
                        final_mkv = base + '.mkv'
                        shutil.move(self.tempfile, final_mkv)
                        final = final_mkv
                        self.outfile = final_mkv
                        self.tempfile = None
                    except Exception:
                        pass
                    # invoke on_stop and return
                    if self.on_stop:
                        try:
                            self.on_stop(final)
                        except Exception:
                            pass
                    return
                vcodec = (self.settings or {}).get('video_codec', 'libx264')
                crf = (self.settings or {}).get('crf', 23)
                preset = (self.settings or {}).get('preset', 'medium')
                acodec = (self.settings or {}).get('audio_codec', 'aac')
                abitrate = (self.settings or {}).get('audio_bitrate_kbps', 128)
                cmd = [
                    'ffmpeg', '-y', '-i', self.tempfile,
                    '-c:v', vcodec,
                    '-preset', preset,
                    '-crf', str(crf),
                    '-c:a', acodec,
                    '-b:a', f"{abitrate}k",
                    final,
                ]
                try:
                    print('Transcoding recording to final file with ffmpeg')
                    subprocess.check_call(cmd)
                    try:
                        os.remove(self.tempfile)
                    except Exception:
                        pass
                except Exception as e:
                    print('Transcode failed:', e)
                    # try a short retry after small delay
                    try:
                        time.sleep(0.5)
                        subprocess.check_call(cmd)
                        try:
                            os.remove(self.tempfile)
                        except Exception:
                            pass
                    except Exception:
                        # fallback: move temp file into place (keep mkv if container mismatch)
                        try:
                            base, _ = os.path.splitext(final)
                            final_mkv = base + '.mkv'
                            shutil.move(self.tempfile, final_mkv)
                            final = final_mkv
                            self.outfile = final_mkv
                        except Exception:
                            pass
            else:
                try:
                    shutil.move(self.tempfile, final)
                except Exception:
                    pass
            self.tempfile = None

        if self.on_stop:
            try:
                self.on_stop(self.outfile)
            except Exception:
                pass

    def _open_log(self, prefix: str):
        """Open a rotating log file in the project's logs/ folder and return a file object for subprocess redirection."""
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        project_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.join(project_dir, 'logs')
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            log_dir = project_dir
        logname = os.path.join(log_dir, f"{prefix}_rec_{ts}.log")
        try:
            f = open(logname, 'ab')
            return f
        except Exception:
            # fallback to devnull
            return subprocess.DEVNULL

    def _ffmpeg_x11_cmd(self):
        # Default to :0.0 and capture full screen. Users can change area manually.
        display_env = os.environ.get("DISPLAY", ":0.0")
        geom = self._screen_geometry_x11()
        if self.area:
            w, h, x, y = self.area
            offset = f"+{x},{y}"
            size = f"{w}x{h}"
        else:
            size = f"{geom['width']}x{geom['height']}"
            offset = "+0,0"
        cmd = [
            "ffmpeg",
            "-y",
            "-video_size",
            size,
            "-framerate",
            str(self.fps),
            "-f",
            "x11grab",
            "-i",
            f"{display_env}{offset}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            self.outfile,
        ]
        return cmd

    def _ffmpeg_pipewire_cmd(self):
        # Best-effort; requires ffmpeg built with pipewire support.
        # Users may need to configure pipewire screen source first.
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "pipewire",
            "-framerate",
            str(self.fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            self.outfile,
        ]
        return cmd

    def _wf_recorder_cmd(self, output_override: Optional[str] = None):
        # wf-recorder records to a file directly
        out = output_override if output_override else self.outfile
        cmd = [
            "wf-recorder",
            "-f",
            out,
        ]
        if self.mode == "region" and self.area:
            w, h, x, y = self.area
            # wf-recorder accepts -g "WxH+X+Y"
            cmd += ["-g", f"{w}x{h}+{x}+{y}"]
        return cmd

    def _screen_geometry_x11(self):
        # Try to get geometry with xdpyinfo or fallback to 1920x1080
        try:
            out = subprocess.check_output(["xdpyinfo"]) .decode(errors="ignore")
            for line in out.splitlines():
                if "dimensions:" in line:
                    parts = line.strip().split()
                    dims = parts[1]
                    w, h = dims.split("x")
                    return {"width": int(w), "height": int(h)}
        except Exception:
            pass
        return {"width": 1920, "height": 1080}

    def _ffmpeg_supports_pipewire(self) -> bool:
        """Return True if system ffmpeg recognizes the 'pipewire' protocol/input."""
        try:
            p = subprocess.run(['ffmpeg', '-protocols'], capture_output=True, text=True, timeout=2)
            out = (p.stdout or '') + (p.stderr or '')
            if 'pipewire' in out:
                return True
        except Exception:
            pass
        try:
            p2 = subprocess.run(['ffmpeg', '-formats'], capture_output=True, text=True, timeout=2)
            out2 = (p2.stdout or '') + (p2.stderr or '')
            if 'pipewire' in out2:
                return True
        except Exception:
            pass
        return False


def trim_clip(input_path: str, start: float, end: float, out_path: Optional[str] = None) -> str:
    if not out_path:
        base = os.path.splitext(os.path.basename(input_path))[0]
        out_path = os.path.join(RECORDINGS_DIR, f"{base}_trim_{int(time.time())}.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-ss",
        str(start),
        "-to",
        str(end),
        "-c",
        "copy",
        out_path,
    ]
    subprocess.check_call(cmd)
    return out_path
