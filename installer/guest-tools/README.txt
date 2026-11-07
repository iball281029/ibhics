ibHiCS guest tools

Windows 10/11 and Server 2016 and later: run virtio-win-guest-tools.exe. It installs the
virtio drivers (disk, network, memory balloon, display) and the QEMU guest agent.

Windows Server 2012 R2, 2012, 2008 R2, 8.1 and 7: run legacy\install-legacy.cmd as
Administrator (the installer above needs Windows 10 or later). It removes Red Hat driver
packages a newer virtio-win left (they show Code 52 here), installs the QEMU guest agent,
the virtio-win 0.1.189 drivers for this Windows and the memory balloon service. After a
restart, "Switch disks to VirtIO" in the VM's menu moves its disks from SATA to VirtIO.

Windows Setup reads the disk and network drivers in $WinPEDriver$ by itself, so the disk
shows without "Load driver".

VMs imported with ibHiCS Migrate already have the drivers.

Linux: run "sh linux/install.sh" as root. Most distributions already include the virtio
drivers; the script adds the QEMU guest agent from the distribution's repositories.
