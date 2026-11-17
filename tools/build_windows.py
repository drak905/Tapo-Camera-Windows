"""Gate 4 build: PyInstaller one-folder app + optional Inno Setup installer.

Output:
  dist/TapoDesktop/TapoDesktop.exe        (windowed app, ffmpeg.exe bundled next to it)
  dist/TapoDesktop-<version>-win64.zip    (portable)
  dist/TapoDesktop-<version>-setup.exe    (if Inno Setup's ISCC.exe is available)
Then runs the clean-room audit and a packaged --selftest.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP = "TapoDesktop"
sys.path.insert(0, str(ROOT / "src"))
from tapo_desktop import __version__  # noqa: E402


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def make_icon() -> Path:
    """Render the app icon (same drawing as the UI) into build/app.ico."""
    BUILD.mkdir(exist_ok=True)
    ico = BUILD / "app.ico"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from tapo_desktop.ui import _theme
    app = QApplication.instance() or QApplication([])
    pm = _theme.app_icon().pixmap(256, 256)
    if not pm.save(str(ico), "ICO"):
        pm.save(str(BUILD / "app.png"), "PNG")
        return BUILD / "app.png"
    del app
    return ico


def find_iscc() -> str | None:
    for p in (shutil.which("iscc"),
              os.path.expandvars(r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"),
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")):
        if p and Path(p).is_file():
            return p
    return None


def main() -> int:
    py = sys.executable
    icon = make_icon()
    ffmpeg = os.environ.get("TAPO_FFMPEG") or shutil.which("ffmpeg")
    args = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", APP,
            "--distpath", str(DIST), "--workpath", str(BUILD / "pyi"), "--specpath", str(BUILD),
            "--paths", str(ROOT / "src"), "--icon", str(icon),
            "--exclude-module", "tests", "--exclude-module", "pytest", "--exclude-module", "hypothesis",
            "--exclude-module", "PyQt6.QtWebEngineCore", "--exclude-module", "PyQt6.QtQml",
            "--exclude-module", "PyQt6.QtQuick", "--exclude-module", "PyQt6.Qt3DCore",
            "--exclude-module", "tkinter",
            str(ROOT / "tools" / "pyi_entry.py")]
    if ffmpeg:
        ffmpeg = str(Path(ffmpeg).resolve())
        args[-1:-1] = ["--add-binary", f"{ffmpeg};."]
    else:
        print("WARNING: ffmpeg not found; the package will need ffmpeg on PATH")
    run(args, cwd=ROOT)

    out = DIST / APP
    run([py, str(ROOT / "tools" / "audit_release.py"), str(out), "--json", str(ROOT / "artifacts" / "release_audit.json")])
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TAPO_DESKTOP_HOME=str(BUILD / "selftest_home"))
    run([str(out / f"{APP}.exe"), "--selftest"], env=env, timeout=60)

    zip_base = DIST / f"{APP}-{__version__}-win64"
    shutil.make_archive(str(zip_base), "zip", root_dir=DIST, base_dir=APP)
    print("portable zip:", zip_base.with_suffix(".zip"))

    iscc = find_iscc()
    if iscc:
        run([iscc, f"/DAppVersion={__version__}", f"/DSourceDir={out}", f"/DOutputDir={DIST}",
             f"/DIconFile={icon}", str(ROOT / "tools" / "installer.iss")])
    else:
        print("Inno Setup not found: skipped setup.exe (portable zip produced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
