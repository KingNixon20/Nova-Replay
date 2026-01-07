#!/usr/bin/env python3
"""Build a release AppImage on Arch-like systems.

Usage: python3 scripts/build_appimage_arch.py

What it does:
- Ensures AppDir contains AppRun and nova-replay.desktop
- Ensures an icon exists at AppDir/nova-replay.png (copies from AppDir/usr/share/icons/... or from img/)
- Downloads linuxdeploy and appimagetool if not present
- Runs linuxdeploy to populate AppDir (if present) and then appimagetool to create the AppImage

This script is intentionally conservative and prints commands it runs.
"""
import os
import sys
import json
import shutil
import subprocess
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
APPDIR = os.path.join(ROOT, 'AppDir')
LINUXDEPLOY = os.path.join(ROOT, 'linuxdeploy-x86_64.AppImage')
APPIMAGETOOL = os.path.join(ROOT, 'appimagetool-x86_64.AppImage')

def fetch_latest_asset(owner, repo, name_substr, out_path):
    api = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
    req = urllib.request.Request(api, headers={'User-Agent': 'nova-replay-builder'})
    with urllib.request.urlopen(req) as r:
        data = json.load(r)
    assets = data.get('assets', [])
    for a in assets:
        url = a.get('browser_download_url', '')
        name = a.get('name', '')
        if name_substr in name or name_substr in url:
            print('Found asset', name, '->', url)
            urllib.request.urlretrieve(url, out_path)
            os.chmod(out_path, 0o755)
            return out_path
    raise RuntimeError(f'No asset matching {name_substr} in {owner}/{repo} latest release')

def ensure_tools():
    if not os.path.exists(LINUXDEPLOY):
        print('Downloading linuxdeploy...')
        # try the continuous build path
        url = 'https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage'
        urllib.request.urlretrieve(url, LINUXDEPLOY)
        os.chmod(LINUXDEPLOY, 0o755)
    if not os.path.exists(APPIMAGETOOL):
        print('Downloading appimagetool (latest)...')
        try:
            fetch_latest_asset('AppImage', 'AppImageKit', 'appimagetool-x86_64.AppImage', APPIMAGETOOL)
        except Exception as e:
            print('Failed to fetch latest appimagetool:', e)
            print('Falling back to known URL (may 404)')
            fallback = 'https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage'
            urllib.request.urlretrieve(fallback, APPIMAGETOOL)
            os.chmod(APPIMAGETOOL, 0o755)

def prepare_appdir():
    if not os.path.isdir(APPDIR):
        print('Creating AppDir...')
        os.makedirs(APPDIR, exist_ok=True)
    
    def copy_if_newer(src, dst, mode=None):
        """Copy `src` -> `dst` only if dst doesn't exist or src is newer.
        Returns True if copied, False if skipped."""
        if not os.path.exists(src):
            return False
        if os.path.exists(dst):
            try:
                if os.path.getmtime(src) <= os.path.getmtime(dst):
                    print('Skipping up-to-date:', dst)
                    return False
            except Exception:
                pass
        ddir = os.path.dirname(dst)
        if ddir and not os.path.isdir(ddir):
            os.makedirs(ddir, exist_ok=True)
        shutil.copy2(src, dst)
        if mode:
            try:
                os.chmod(dst, mode)
            except Exception:
                pass
        print('Copied', src, '->', dst)
        return True

    def copy_tree_if_newer(src_dir, dst_dir):
        """Recursively copy files from src_dir into dst_dir, skipping files
        that already exist and are newer than the source."""
        if not os.path.isdir(src_dir):
            return 0
        copied = 0
        for root, dirs, files in os.walk(src_dir):
            rel = os.path.relpath(root, src_dir)
            target_root = os.path.join(dst_dir, rel) if rel != '.' else dst_dir
            os.makedirs(target_root, exist_ok=True)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(target_root, f)
                if copy_if_newer(s, d):
                    copied += 1
        return copied
    # ensure desktop file exists at AppDir root
    desktop_src = os.path.join(ROOT, 'nova-replay.desktop')
    desktop_dst = os.path.join(APPDIR, 'nova-replay.desktop')
    if os.path.exists(desktop_src):
        copy_if_newer(desktop_src, desktop_dst)
    else:
        print('Warning: nova-replay.desktop not found at project root')
    # ensure AppRun executable exists (copy from repo AppDir/AppRun if present)
    apprun = os.path.join(APPDIR, 'AppRun')
    repo_apprun = os.path.join(ROOT, 'AppDir', 'AppRun')
    if os.path.exists(repo_apprun):
        copy_if_newer(repo_apprun, apprun, mode=0o755)
        try:
            os.chmod(apprun, 0o755)
        except Exception:
            pass
        print('Ensured AppRun at', apprun)
    else:
        if not os.path.exists(apprun):
            print('Error: AppRun missing. Create AppDir/AppRun first or add AppDir/AppRun in the repo.')
            sys.exit(1)
        os.chmod(apprun, 0o755)

    # ensure top-level icon AppDir/nova-replay.png exists
    icon_dst = os.path.join(APPDIR, 'nova-replay.png')
    # prefer AppDir/usr/share/icons/... path, else project img/logo2.png
    icon_src_candidate = os.path.join(APPDIR, 'usr', 'share', 'icons', 'hicolor', '256x256', 'apps', 'nova-replay.png')
    if os.path.exists(icon_src_candidate):
        copy_if_newer(icon_src_candidate, icon_dst)
    else:
        proj_icon = os.path.join(ROOT, 'img', 'logo2.png')
        if os.path.exists(proj_icon):
            copy_if_newer(proj_icon, icon_dst)
        else:
            print('Warning: no icon found to place at', icon_dst)

    # copy python source files into AppDir/usr/bin
    usr_bin = os.path.join(APPDIR, 'usr', 'bin')
    os.makedirs(usr_bin, exist_ok=True)
    print('Copying python sources to', usr_bin)
    for fn in os.listdir(ROOT):
        if fn.endswith('.py') and fn not in ('scripts/build_appimage_arch.py',):
            src = os.path.join(ROOT, fn)
            dst = os.path.join(usr_bin, fn)
            copy_if_newer(src, dst)
    # copy thumbnail_renderer if in a module
    # copy img assets into AppDir/usr/share
    src_img = os.path.join(ROOT, 'img')
    if os.path.isdir(src_img):
        dst_img = os.path.join(APPDIR, 'usr', 'share', 'nova-replay', 'img')
        copied = copy_tree_if_newer(src_img, dst_img)
        print(f'Copied/updated {copied} files to', dst_img)

    # sanitize desktop file in AppDir: set Exec=AppRun and clean Categories
    desktop_path = os.path.join(APPDIR, 'nova-replay.desktop')
    if os.path.exists(desktop_path):
        with open(desktop_path, 'r') as f:
            lines = f.readlines()
        out_lines = []
        for L in lines:
            if L.strip().startswith('Exec='):
                out_lines.append('Exec=AppRun\n')
            elif L.strip().startswith('Categories='):
                # allow only standard categories
                out_lines.append('Categories=AudioVideo;Video;\n')
            else:
                out_lines.append(L)
        with open(desktop_path, 'w') as f:
            f.writelines(out_lines)
        print('Sanitized desktop file at', desktop_path)

def run_cmd(cmd, env=None):
    print('RUN:', ' '.join(cmd))
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        raise SystemExit(f'Command failed: {cmd}')

def build():
    prepare_appdir()
    ensure_tools()

    # run linuxdeploy (optional) to populate AppDir
    if os.path.exists(LINUXDEPLOY):
        cmd = [LINUXDEPLOY, '--appdir', APPDIR, '--desktop-file', os.path.join(APPDIR, 'nova-replay.desktop'), '--icon-file', os.path.join(APPDIR, 'nova-replay.png'), '--executable', os.path.join(APPDIR, 'AppRun')]
        try:
            run_cmd(cmd)
        except SystemExit as e:
            print('linuxdeploy step failed, continuing to appimagetool (if AppDir is already valid).', e)

    # create appimage with explicit ARCH
    env = os.environ.copy()
    env['ARCH'] = 'x86_64'
    cmd2 = [APPIMAGETOOL, APPDIR]
    run_cmd(cmd2, env=env)

def main():
    try:
        build()
        print('\nBuild complete — check the current directory for the produced AppImage (look for *.AppImage or Nova_Replay-*-x86_64.AppImage).')
    except Exception as e:
        print('Build failed:', e)
        sys.exit(1)

if __name__ == '__main__':
    main()
