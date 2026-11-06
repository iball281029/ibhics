"""The web UI must reach users whole: every file it loads has to be in git and the wheel."""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "ibhics" / "manager" / "static"
IMPORT_RE = re.compile(
    r"""(?:^|\s)(?:import|export)\b[^'"]*?from\s+['"](\.{1,2}/[^'"]+)['"]""", re.M
)


def tracked_static() -> set[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", str(STATIC)], cwd=REPO, capture_output=True, check=True
    ).stdout.decode()
    return {(REPO / name).resolve() for name in out.split("\0") if name}


def test_every_module_import_is_in_git():
    # A .gitignore "lib/" once dropped noVNC's pako, and the console hung at "Connecting...".
    tracked = tracked_static()
    missing = []
    for js in (p for p in tracked if p.suffix == ".js"):
        for target in IMPORT_RE.findall(js.read_text(encoding="utf-8")):
            path = (js.parent / target).resolve()
            if path not in tracked:
                missing.append(f"{js.relative_to(REPO)} -> {target}")
    assert not missing, "imported but not in git:\n" + "\n".join(missing)


def test_wheel_ships_the_console(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "-w", str(tmp_path), REPO],
        check=True,
    )
    (wheel,) = tmp_path.glob("ibhics-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    prefix = "ibhics/manager/static/"
    for name in ("index.html", "novnc/core/rfb.js", "novnc/vendor/pako/lib/zlib/inflate.js"):
        assert prefix + name in names
    shipped = {n for n in names if n.startswith(prefix)}
    expected = {str(p.relative_to(REPO)) for p in tracked_static()}
    assert expected <= shipped, sorted(expected - shipped)[:10]
