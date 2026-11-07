ibHiCS guest tools

Windows 10/11 and Server 2016 and later: run virtio-win-guest-tools.exe. It installs the
virtio drivers (disk, network, memory balloon, display) and the QEMU guest agent.

Windows Server 2012 R2, 2012, 2008 R2, 8.1 and 7: run legacy\virtio-win-guest-tools.exe
(virtio-win 0.1.229; the current installer needs Windows 10 or later). Drivers for these
are in the folders named 2k12R2, w8.1, ... After installing, "Switch disks to VirtIO" in
the VM's menu moves its disks from SATA to the faster VirtIO.

Windows Setup reads the disk and network drivers in $WinPEDriver$ by itself, so the disk
shows without "Load driver".

VMs imported with ibHiCS Migrate already have the drivers.

Linux: run "sh linux/install.sh" as root. Most distributions already include the virtio
drivers; the script adds the QEMU guest agent from the distribution's repositories.
