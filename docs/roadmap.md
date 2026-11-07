# ibHiCS roadmap

Each release brings one area of the console up to what an admin expects from a
mature HCI platform. The areas follow the usual HCI manual structure (install, home,
compute, network, storage, nodes, system, backup), using the concepts only: the look
and the words are ibHiCS's own.

✅ done · 🚧 in this release · ⏳ planned

## v0.4 — storage and first wizards ✅
- Installer: no reinstall loop, cabled port found, version in the boot menu
- Add storage wizard: local disks (SSD/HDD, several disks, old Ceph/LVM disks), NFS, iSCSI, FC, folder
- Storage summary and file browser (folders, ISO uploads into a pool)
- Wizards: New VM, backup schedule, restore
- Network as its own menu; VLAN IDs always shown (VLAN 1 native)

## v0.5 — Compute (VM page) and network topology ✅
- Network topology: uplinks → virtual switches (VLANs) → VMs; click to configure, drag a VM to change its VLAN ✅
- VM groups: folder tree beside the VM list, with counts; new group, rename, delete, move VMs ✅
- VM list with metric views: Resources (CPU cores and usage, memory and usage, storage and usage), Basics (OS, IP, group, description), Node, Throughput, IO speed ✅
- Live CPU, memory, disk and network usage per VM ✅
- Select several VMs: power on, shut down, restart, power off, move to group, delete ✅
- Alerts bar: VMs that are short of CPU, memory or disk, or run without the guest agent ✅
- List or panel (cards) view ✅

## v0.6 — New look, tasks, Compute part 2 ✅
- New ib monogram logo; brighter colours (cyan to violet) in light and dark ✅
- Tasks page: every job with progress, duration, errors and logs; operations log ✅
- Dashboard without the status header; theme in the user menu ✅
- Windows VMs get the virtio driver CD as a second drive ✅
- Clone VM, VM templates (convert VM ⇄ template, Deploy VM with independent disk copies) ✅
- Guest OS customization on deploy: cloud-init for Linux, sysprep answer file for Windows ✅
- Snapshots (take, revert, delete) with a timeline on the VM page ✅
- VM detail tabs: summary, hardware, snapshots, backups, tasks, alerts ✅
- Import VM: OVA/OVF/VMDK/VHDX from the VM page (reusing ibHiCS Migrate) ✅
- Recycle bin for deleted VMs (kept N days) ✅

## v0.7 — Network, part 2 ✅
- Edge settings per node: which ports carry VM traffic, bonds (LACP, active-backup)
- Virtual router: routed networks between VLANs, NAT to the LAN, DHCP for a network
- Distributed firewall: allow/deny rules between VM groups and networks
- Test connectivity (ping/trace between two VMs or a VM and an address)

## v0.8 — Storage, part 2 ⏳
Beta 1–3 (0.8.0b1–b3) ✅
- Terminal page: run diagnostic commands on a node (ping, traceroute, ip, ss, DNS, …), admins only, audited; replaces Network → Test connectivity
- Snapshots of running UEFI VMs; default snapshot name is the VM's name and the time
- Backups of running VMs fixed (QEMU may write the scratch folder; waits while QEMU is busy)
- Snapshots: listed per VM and on Backup → Snapshots, restore any of them, shown in Tasks; one backup or snapshot at a time per VM
- VM names in Thai; ISO files named .ISO; a longer guest OS list (Windows 7–11, Server 2008 R2–2025, more Linux, BSD) with the right firmware and devices

Still to do:
- Physical disks page: health (SMART), SSD wear, which pool uses each disk, blink LED
- Expand a pool (add disks), storage forecast (days until full)
- Shared disks between VMs (clusters), iSCSI target serving VM disks
- Storage policy per VM disk (thin/thick, cache)

## v1.1
Beta 2 (1.1.0b2): Restart applies saved CPU/memory/boot changes (power off and on, not a guest reboot)
Beta 1 (1.1.0b1) ✅: rename a VM (Thai names too), even while it runs (shown name; the VM itself is renamed when off), snapshots and backup policies follow; rename containers; every image on images.linuxcontainers.org as a container template (search, grouped by distribution, offline falls back to the curated list); 18 app templates (Nginx, MariaDB, PostgreSQL, Redis, Docker, Zabbix, Grafana, WordPress, Nextcloud, …); on-screen keyboard on the VM console (Esc, Tab, F1–F12, sticky Ctrl/Alt/Shift/Win, Ctrl+Alt+Del)

## v1.0 — Containers ⏳
Beta 1 (1.0.0b1) ✅: LXC system containers beside VMs: one-click template catalog (Debian, Ubuntu, Rocky, Alma, Alpine) and app templates (Nginx, MariaDB, Docker host, Zabbix agent, WordPress), 3-step wizard (template → CPU/RAM/disk/network with IP at create → root password/SSH key), unprivileged by default, disk as an image file in any folder pool, live resize of CPU/memory/disk (grow), console (commands as root, admins), snapshots/restore, everything in Tasks

Still to do:
- Backup and restore of containers with the VM backup repositories and policies
- Clone, convert to template, migrate to another node
- OCI images from a registry (Docker Hub) as containers
- Interactive console (full TTY), live CPU/memory charts, firewall per container, topology view

## v0.9 — Nodes ⏳
Beta 2 (0.9.0b2) ✅: VM migration data over the migration interface, flow control per port, VMs listed on the node summary
Beta 1 (0.9.0b1) ✅: maintenance mode (no VMs placed/started/moved here, evacuate first), interface roles (management, migration, storage, VM uplink), static routes, system disk health (SMART, wear, temperature)

Still to do:
- Add node wizard, node summary (accessible storage, VMs), maintenance mode
- Communication interfaces: management, migration (overlay), storage, VM uplink
- Aggregate interfaces (bonds), static routes, MTU/flow control, system disk health

## v0.10 — System and reliability ⏳
- Administrators and permissions per VM group; login and password policy
- Alarms with e-mail/LINE notification; task center
- High availability and resource scheduling (balance VMs across nodes, hot-add)
- System backup and restore of the cluster configuration, upgrade from the web console
- Cluster health check and diagnostics bundle, UPS integration, time sync
