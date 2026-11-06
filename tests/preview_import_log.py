"""Local fake-driver preview: python -m tests.preview_import_log [--log FILE].

Creates three in-memory jobs (running/done/failed), with no conversion or remote calls.
Default input is fixtures/v2v-example.txt. Refresh the running job by editing --log FILE.
This development app has no authentication; bind only to loopback.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from ibhics.cluster import Cluster
from ibhics.hypervisor.fake import FakeDriver
from ibhics.manager.app import create_app
from ibhics.manager.imports import ImportJob, ImportManager


def preview_app(log_path: Path):
    class PreviewLogs:
        def log(self, name):
            text = log_path.read_text()
            if name == "Zabbix-running":
                # Stop the sample before metadata/finish, to show a live copy bar.
                text = text.split("[ 165.0]", 1)[0]
            return text

    cluster = Cluster()
    cluster.add_local("preview-node", FakeDriver(), imports=PreviewLogs())
    imports = ImportManager(cluster)
    started = datetime.now().astimezone() - timedelta(seconds=180)
    for index, state in enumerate(("running", "done", "failed"), 1):
        imports._jobs.append(
            ImportJob(
                id=str(index),
                source="demo-esx (fixture only)",
                vm="Zabbix",
                name=f"Zabbix-{state}",
                node="preview-node",
                pool="hdd01",
                created=started.isoformat(),
                state=state,
                finished=None if state == "running" else datetime.now().astimezone().isoformat(),
                error="ConversionFailed: demo disk error" if state == "failed" else None,
                step="Copying disk 1/1" if state == "running" else "",
                percent=60 if state == "running" else (100 if state == "done" else None),
            )
        )
    return create_app(cluster, imports=imports)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log", type=Path, default=Path(__file__).parent / "fixtures/v2v-example.txt"
    )
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()
    uvicorn.run(preview_app(args.log), host="127.0.0.1", port=args.port)
