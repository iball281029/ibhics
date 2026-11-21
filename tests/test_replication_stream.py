import hashlib
import io

from ibhics.replication.ssh import SnapshotSSHProvider
from ibhics.replication.store import CHUNK


class Disk:
    key, size = "d0", 3 * CHUNK


class Proc:
    def __init__(self, data):
        self.stdout = io.BytesIO(data)

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self):
        pass


class FakeSSH:
    def __init__(self, data):
        self.data, self.calls, self.streams = data, [], 0

    def command(self, command, binary=False):
        self.calls.append(command)
        assert command.startswith("i=0; while")
        chunks = [self.data[i : i + CHUNK] for i in range(0, len(self.data), CHUNK)]
        return "".join(hashlib.sha256(c).hexdigest() + "  -\n" for c in chunks)

    def stream(self, command):
        self.streams += 1
        skip = int(command.split("skip=")[1].split()[0])
        return Proc(self.data[skip * CHUNK :])


def test_hashes_in_one_call_and_reads_through_one_stream():
    data = b"a" * CHUNK + b"b" * CHUNK + b"c" * CHUNK
    provider = object.__new__(SnapshotSSHProvider)
    provider.ssh, provider.paths = FakeSSH(data), {"d0": "/vmfs/volumes/ds/vm-flat.vmdk"}
    digests = [provider.hash(Disk, n * CHUNK, CHUNK) for n in range(3)]
    assert digests[1] == hashlib.sha256(b"b" * CHUNK).hexdigest()
    assert len(provider.ssh.calls) == 1
    assert provider.read(Disk, 0, CHUNK) == b"a" * CHUNK
    assert provider.read(Disk, 2 * CHUNK, CHUNK) == b"c" * CHUNK  # skips chunk 1 in-stream
    assert provider.ssh.streams == 1
    provider._close_stream()
