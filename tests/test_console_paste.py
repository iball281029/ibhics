# ruff: noqa: E501
"""Console "Paste text": paced key events and no modifier left held afterwards."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).parents[1] / "ibhics/manager/static/console.html"


def script():
    text = PAGE.read_text()
    return text[text.index("const US_KEYS") : text.index("if (uuid) connect()")]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_paste_paces_every_event_and_releases_modifiers():
    code = (
        "const sent = []; let rfb = { sendKey: (k, c, d) => sent.push([k, c, d]), focus() {} };\n"
        "const handlers = {};\n"
        "const el = (id) => ({ disabled: false, addEventListener: (_e, f) => { handlers[id] = f; } });\n"
        "const held = new Set(); const kbd = { querySelectorAll: () => [] };\n"
        "const setStatus = () => {};\n"
        "Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { readText: async () => 'Ab$\\n' } } });\n"
        "const document = { getElementById: el };\n"
        "let timers = 0; globalThis.setTimeout = (f) => { timers++; f(); };\n"
        + script()
        + "\nhandlers.paste().then(() => {\n"
        "  const ups = sent.filter((e) => !e[2]).map((e) => e[1]);\n"
        "  const downs = sent.filter((e) => e[2]).length;\n"
        "  if (!['ShiftLeft','ShiftRight','ControlLeft','AltLeft','MetaLeft'].every((c) => ups.includes(c))) throw 'no release';\n"
        "  if (timers < downs * 2) throw 'not paced ' + timers + ' ' + downs;\n"
        "  const shiftDowns = sent.filter((e) => e[1] === 'ShiftLeft' && e[2]).length;\n"
        "  if (shiftDowns !== 2) throw 'shift ' + shiftDowns;\n"
        "  console.log('ok');\n"
        "});\n"
    )
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


def test_release_keys_button_exists():
    assert re.search(r'id="release"[^>]*>Release keys<', PAGE.read_text())
