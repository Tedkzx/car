# PyInstaller build: two programs sharing one folder.
#   SX200Tracker.exe      the app (no console window)
#   SX200Tracker-cli.exe  the same app with a console, for maintainer commands (--selftest, --capture)
# Build:  pyinstaller installer/sx200.spec --noconfirm
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
datas = [
    (str(ROOT / "recipes"), "recipes"),
    (str(ROOT / "web" / "templates"), "web/templates"),
    (str(ROOT / "web" / "static"), "web/static"),
]
datas += collect_data_files("playwright")  # the Playwright driver; browsers are not bundled (Edge is used)
hidden = collect_submodules("uvicorn") + collect_submodules("keyring.backends") + [
    "selectolax.lexbor", "pystray._win32", "winotify", "multipart", "win32ctypes.core"]

a = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "pytest", "matplotlib", "numpy.tests"], noarchive=False)
pyz = PYZ(a.pure)
icon = str(ROOT / "installer" / "sx200.ico") if (ROOT / "installer" / "sx200.ico").exists() else None
app = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SX200Tracker", console=False, icon=icon,
          upx=False, version=None)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SX200Tracker-cli", console=True, icon=icon, upx=False)
coll = COLLECT(app, cli, a.binaries, a.datas, strip=False, upx=False, name="SX200Tracker")
