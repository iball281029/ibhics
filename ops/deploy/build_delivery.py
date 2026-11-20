"""Build a standalone source update, preserving hashes of reviewed predecessors."""

import base64
import hashlib
import json
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT.parents[1] / "ibhics-delivery"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    runtime = ROOT / "ibhics"
    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "ibhics"], cwd=ROOT, text=True
    ).splitlines()
    expected, files, hashes = {}, {}, {}
    for path in tracked:
        original = subprocess.check_output(["git", "show", "HEAD:" + path], cwd=ROOT)
        relative = str(Path(path).relative_to("ibhics"))
        expected[relative] = [sha(original)]
    for path in runtime.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        relative = str(path.relative_to(runtime))
        data = path.read_bytes()
        current = sha(data)
        if relative not in expected or current not in expected[relative]:
            files[relative] = base64.b64encode(data).decode()
            hashes[relative] = current
            expected.setdefault(relative, [None])
            expected[relative].append(current)
    previous = json.loads((OUT / "ui-release-payload.json").read_text())
    for relative, content in previous["files"].items():
        expected.setdefault(relative, [None]).append(sha(content.encode()))
    for relative, value in previous["expected"].items():
        expected.setdefault(relative, []).append(value)
    # Exact content of the separately deployed dotted-name patch is also reviewed.
    relative = "importer/convert.py"
    expected[relative].append(sha((runtime / relative).read_bytes()))
    manifest = json.loads((OUT / "backup-stage-manifest.json").read_text())
    payload = {
        "files": files,
        "new_hashes": hashes,
        "expected": expected,
        "stage_sha": manifest["source_sha256"],
        "smoke_script": base64.b64encode((OUT / "test-backup-linux.py").read_bytes()).decode(),
    }
    encoded = base64.b64encode(zlib.compress(json.dumps(payload).encode(), 9)).decode()
    source = (
        (ROOT / "ops/deploy/backup_spatial.py")
        .read_text()
        .replace(
            "PAYLOAD = None  # substituted only in the standalone delivery",
            f"PAYLOAD = json.loads(zlib.decompress(base64.b64decode({encoded!r})))",
        )
    )
    compile(source, "install-backup-spatial.py", "exec")
    (OUT / "install-backup-spatial.py").write_text(source)
    receipt = {
        "release": "backup-spatial-20261119",
        "files": hashes,
        "stage_sha": manifest["source_sha256"],
        "installer_sha": sha(source.encode()),
        "method": "guarded source overlay, atomic file replacement, immediate failure rollback",
    }
    (OUT / "backup-spatial-manifest.json").write_text(json.dumps(receipt, indent=2))
    print("DELIVERY_READY:", len(files), "changed/new runtime files;", len(source), "bytes")
    print("INSTALLER_SHA256:", receipt["installer_sha"])


if __name__ == "__main__":
    main()
