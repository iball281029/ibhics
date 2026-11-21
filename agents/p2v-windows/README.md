# Windows P2V agent

Build with Go 1.22 or later (standard library only; `go.sum` is intentionally empty):

```sh
go test ./...
go vet ./...
GOOS=windows GOARCH=amd64 go vet ./...
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o ../../ibhics/manager/static/p2v-agent.exe .
```

The CI workflow publishes `p2v-agent-exe`. The ISO workflow's packages job builds
that same artifact; the ISO job downloads it before `build-iso.sh` creates the
wheel. The binary is ignored by git. No Go installation is needed on the manager
or the source Windows machine.

Create a session under Migrate → Physical machines and choose Windows (.exe).
Download, right-click **Run as administrator**, stop databases and busy services,
then type `y`. Double-clicking also requests UAC elevation via ShellExecute
`runas` (no embedded manifest). Accepting UAC starts the elevated copy. A console
created for the agent waits for Enter at completion or failure. `--yes` bypasses
the copy confirmation; UAC still applies. The binary is unsigned: SmartScreen
may warn. This initial target is Windows amd64 (Windows 10 / Server 2016 or newer); actual
Windows testing is required. Go 1.22 inherits this runtime minimum from
[Go 1.21](https://go.dev/doc/go1.21#windows), so Server 2012 R2 and earlier cannot
run this executable.

This is a **live raw disk copy without VSS**, like the Linux agent. There is no
application consistency guarantee. Quiesce writers first; prefer offline copying
when a consistent disk image is required. BitLocker/encrypted disks must be
decrypted before conversion. All discovered PhysicalDrive0–255 disks are listed,
including USB/removable disks that expose a size; review the list carefully.
Disk reads use buffered Windows handles with sector-multiple offsets and lengths,
in chunks up to 4 MiB (including 4Kn disks). The agent never writes source disks.
There is no resume or automatic retry; interrupted copies need to be started again.

Pass `--replicate` to send a repeatable delta round instead of a one-shot upload.
The OS-independent protocol in `delta.go` hashes each 4 MiB chunk with SHA256,
posts inventory and hash lists to `/api/v1/p2v/upload/{sid}/delta/begin`, uploads
only requested chunks, and commits the round through the manager's ReplicaStore.
An incomplete round leaves the previous generation intact. A second full hash
pass rejects changes to skipped chunks before commit. These are still live disk
reads without VSS; the repository has no VSS implementation to reuse. Keep
writers quiesced. Reuse the same session and embedded credentials for later rounds
within its 24-hour TTL. Import consumes the session and its replica; scheduled
pull replication and token renewal are not implemented for physical machines.
Run `go test ./...` and `go vet ./...` to check the protocol without Windows.

The manager only stores the session token's SHA256 hash. The create response
returns the original token once (`agent_token`); the UI holds it in memory and
sends it in `X-P2V-Token` to the authenticated GET
`/api/v1/p2v/sessions/{sid}/agent.exe`. The endpoint verifies both login and the
existing token/state/24-hour TTL before returning the executable plus
`\n#IBHICS-P2V#` + JSON + `#END#`. It does not rotate the token or store plaintext,
and uses `Cache-Control: no-store`. Credentials never appear in a download URL.
The downloaded file contains upload credentials: keep it private. Refreshing the
page loses the original token; create a new session to generate a new download.

The config JSON contains `server`, `session`, `token`, and `pin`. Without a trailer,
use `--server https://manager:8888 --session ID --token TOKEN --pin BASE64`.
Embedded credentials take precedence over flags. HTTPS and a valid SHA256 SPKI
pin are required. CA/hostname checks are replaced by a constant-time comparison
against the presented leaf certificate's SubjectPublicKeyInfo hash, matching curl
`--pinnedpubkey sha256//PIN -k`. Redirects are rejected. No untrusted response bodies
or credentials are printed. A manager must have `p2v_pin` configured for this agent.

Windows OS and BIOS/UEFI inventory pass through the existing P2V source and
virt-v2v import path. The ISO's existing virtio-win/virt-tools payload is needed
for Windows conversion. This code does not install drivers on the source.
