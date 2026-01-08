import os
import shutil
import subprocess
import time
from datetime import datetime
import shutil

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

    def __init__(self, filename: Optional[str] = None, mode: str = "fullscreen", area: Optional[tuple] = None, fps: int = 60, preferred_backend: str = 'auto'):
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        self.mode = mode
        self.area = area
        self.fps = fps
        self.proc: Optional[subprocess.Popen] = None
        self.on_stop: Optional[Callable[[str], None]] = None
        self.preferred_backend = preferred_backend or 'auto'
        # optional callback to report startup errors to the UI: on_error(message)
        self.on_error: Optional[Callable[[str], None]] = None
        if filename:
            self.outfile = filename
        else:
            self.outfile = os.path.join(RECORDINGS_DIR, datetime.now().strftime("rec_%Y%m%d_%H%M%S.mp4"))

    def detect_display(self) -> str:
        xdg = os.environ.get("XDG_SESSION_TYPE", "")
        if xdg:
            return xdg.lower()
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
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
            self.proc = subprocess.Popen(cmd)
            return

        if backend == 'pipewire':
            cmd = self._ffmpeg_pipewire_cmd()
            print("Starting recorder with:", " ".join(cmd))
            self.proc = subprocess.Popen(cmd)
            return

        if backend == 'wf-recorder':
            cmd = self._wf_recorder_cmd()
            print("Starting recorder with:", " ".join(cmd))
            # start wf-recorder but monitor for quick failure (compositor missing wlr-screencopy)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            # give it a moment to fail fast
            try:
                time.sleep(0.5)
            except Exception:
                pass
            rc = proc.poll()
            if rc is not None and rc != 0:
                # process exited quickly; read stderr for reason
                try:
                    err = proc.stderr.read().decode(errors='ignore')
                except Exception:
                    err = ''
                print('wf-recorder failed to start:', err)
                # choose fallback based on display
                if disp == 'x11':
                    print('Falling back to ffmpeg x11grab')
                    cmd2 = self._ffmpeg_x11_cmd()
                    print('Starting recorder with:', ' '.join(cmd2))
                    self.proc = subprocess.Popen(cmd2)
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
                        p2 = subprocess.Popen(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
                            try:
                                err2 = p2.stderr.read().decode(errors='ignore')
                            except Exception:
                                err2 = ''
                            print('ffmpeg pipewire fallback failed:', err2)
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
        self.proc = subprocess.Popen(cmd)

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        if self.on_stop:
            self.on_stop(self.outfile)

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

    def _wf_recorder_cmd(self):
        # wf-recorder records to a file directly
        cmd = [
            "wf-recorder",
            "-f",
            self.outfile,
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
