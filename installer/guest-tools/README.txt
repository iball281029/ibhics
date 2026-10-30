ibHiCS guest tools

Windows: run virtio-win-guest-tools.exe. It installs the virtio drivers (disk, network,
memory balloon, display) and the QEMU guest agent. VMs imported with ibHiCS Migrate
already have them.

Linux: run "sh linux/install.sh" as root. Most distributions already include the virtio
drivers; the script adds the QEMU guest agent from the distribution's repositories.
