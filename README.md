# Nova Replay


<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start">
	<img src="githubassets/Screenshot_20260112_150145.png" alt="Thumbnails" style="width:32%;border-radius:6px;border:1px solid #ddd;margin-top:12px" />
	<img src="githubassets/Screenshot_20260112_150157.png" alt="Settings" style="width:32%;border-radius:6px;border:1px solid #ddd;margin-top:12px" />
	<img src="githubassets/Screenshot_20260112_150219.png" alt="Clips list" style="width:32%;border-radius:6px;border:1px solid #ddd;margin-top:12px" />
</div>

![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54) 	![FFmpeg](https://shields.io/badge/FFmpeg-%23171717.svg?logo=ffmpeg&style=for-the-badge&labelColor=171717&logoColor=5cb85c) ![Arch](https://img.shields.io/badge/Arch%20Linux-1793D1?logo=arch-linux&logoColor=fff&style=for-the-badge) ![GitHub](https://img.shields.io/badge/github-%23121011.svg?style=for-the-badge&logo=github&logoColor=white)
                          
### Nova Replay is a lightweight, local-first screen clipper for Linux. It's a developer-friendly prototype that focuses on capturing short clips with a polished GTK UI and simple clip management. ###

### About
Nova Replay is a lightweight, local-first screen clipper for Linux. It's a developer-focused prototype that emphasizes short clip capture, a polished GTK UI, and simple clip management.

### Highlights
- Modern GTK3 UI (Python + PyGObject)
- Uses native capture tools: `ffmpeg` (X11/pipewire) and `wf-recorder` (Wayland) when available — Wayland support is currently limited but improving.
- Thumbnail generation and clip management (play, delete, save-as)
- Basic trimming via `ffmpeg` (stream-copy trims with `-c copy`)
- Packable as an AppImage for easy distribution

### Requirements 
- Python 3.8+
- System tools: `ffmpeg` (required), `xdg-utils` (for opening folders), optional `mpv` for better playback
- Python deps: see `requirements.txt` (PyGObject, Pillow; `pynput` optional for global hotkeys)

### Quickstart (AppImage)
1. Download the latest AppImage from Releases (e.g. `Nova_Replay-x86_64.AppImage`).
2. Make the AppImage executable and run it:

```bash
chmod +x Nova_Replay-x86_64.AppImage
./Nova_Replay-x86_64.AppImage
```

You can also double-click the AppImage in a file manager if it respects executable bits.

### Quickstart (development)
1. Install system dependencies (Debian/Ubuntu example):

```bash
sudo apt update
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 ffmpeg xdg-utils mpv
# optional for Wayland recording
sudo apt install wf-recorder
```

2. Create and activate a virtual environment, then install Python deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

3. Run the app:

```bash
python3 main.py
```

Default recordings directory: `~/Videos/Nova` (created automatically). You can change this in Settings.

```bash
### Packaging (AppImage)
This repository includes `build_appimage.sh` to help create an AppImage using the included AppImage helpers (`linuxdeploy` and `appimagetool`). See the top of `build_appimage.sh` for required binaries and usage notes.
python3 -m venv .venv
### Design notes & limitations
- Global hotkeys are best-effort and may be restricted by some Wayland compositors.
- Recording behavior depends on the capture binaries and compositor; results vary across systems.
- Trimming is intentionally simple (stream-copy via `ffmpeg -c copy`). For frame-accurate or advanced editing, re-encoding or an external editor is recommended.

### Contributing
- Issues and PRs are welcome. For larger features (Wayland hotkey integration, settings migration), open an issue first to discuss design and compatibility.

```bash
python3 main.py
```

Default recordings directory: `~/Videos/Nova` (created automatically). Change this from Settings.

### Packaging (AppImage)

This repo includes `build_appimage_arch.sh` to help build an AppImage using linuxdeploy and appimagetool. Follow the script header for required binaries and usage notes.

### Design notes & limitations
- Global hotkeys are best-effort and may be restricted on some Wayland compositors.
- Recording relies on external capture tools — behavior varies by compositor and installed binaries.
- Trimming is implemented via `ffmpeg -c copy` and is intentionally simple; richer editing can be added later.
- Wayland support is partial, without wlroots you will not be able to record a wayland dekstop, but support is planned for future updates!

### Contributing
- Issues and PRs welcome. For larger features (Wayland hotkey integration, persistent settings migration), open an issue to discuss the approach first.

## Enjoy — and thanks for trying Nova Replay!
