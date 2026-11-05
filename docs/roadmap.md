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

## v0.6 — New look, tasks, Compute part 2 🚧
- New ib monogram logo; brighter colours (cyan to violet) in light and dark ✅
- Tasks page: every job with progress, duration, errors and logs; operations log ✅
- Dashboard without the status header; theme in the user menu ✅
- Windows VMs get the virtio driver CD as a second drive ✅
- Clone VM (full and linked), VM templates (convert VM ⇄ template, deploy from template) ✅
- Guest OS customization on deploy: cloud-init for Linux, sysprep answer file for Windows ✅
- Snapshots (take, revert, delete) with a timeline on the VM page
- VM detail tabs: summary, hardware, snapshots, backups, tasks, alerts
- Import VM: OVA/OVF/VMDK/VHDX from the VM page (reusing ibHiCS Migrate)
- Recycle bin for deleted VMs (kept N days)

## v0.7 — Network, part 2 ⏳
- Edge settings per node: which ports carry VM traffic, bonds (LACP, active-backup)
- Virtual router: routed networks between VLANs, NAT to the LAN, DHCP for a network
- Distributed firewall: allow/deny rules between VM groups and networks
- Test connectivity (ping/trace between two VMs or a VM and an address)

## v0.8 — Storage, part 2 ⏳
- Physical disks page: health (SMART), SSD wear, which pool uses each disk, blink LED
- Expand a pool (add disks), storage forecast (days until full)
- Shared disks between VMs (clusters), iSCSI target serving VM disks
- Storage policy per VM disk (thin/thick, cache)

## v0.9 — Nodes ⏳
- Add node wizard, node summary (accessible storage, VMs), maintenance mode
- Communication interfaces: management, migration (overlay), storage, VM uplink
- Aggregate interfaces (bonds), static routes, MTU/flow control, system disk health

## v0.10 — System and reliability ⏳
- Administrators and permissions per VM group; login and password policy
- Alarms with e-mail/LINE notification; task center
- High availability and resource scheduling (balance VMs across nodes, hot-add)
- System backup and restore of the cluster configuration, upgrade from the web console
- Cluster health check and diagnostics bundle, UPS integration, time sync
