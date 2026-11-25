# Container Backup and Restore

Container filesystem backup uses the existing backup repositories, compression, deduplication, retention, immutability and notification delivery settings.

## Back up now

Open **Containers → Backup**, select a repository, and start the job. A running container requires explicit permission to stop temporarily. It restarts after success, cancellation or failure; an already stopped container stays stopped. Follow read/stored bytes and percentage in **Data protection → Jobs**. **Cancel job** requests safe cleanup before the original restarts.

## Schedule

Open **Containers → Schedule**. Choose the container, repository, daily time or hourly interval, recent restore-point retention and whether temporary stops are permitted. Times use the manager timezone. Without permission, a running container is left untouched and the job fails safely. Manage, edit, run now or delete the schedule in **Data protection → Schedules**. Existing VM schedules retain their previous scope; container schedules are separate and explicitly labelled. Failed scheduled jobs use the existing retry settings.

## Restore

In **Data protection → Restore points**, choose a Container restore point and click **Restore**. Choose a new name, eligible node and storage pool. A local repository restores on its source node; shared repositories can use another eligible compute node. Restore writes directly into the selected pool, verifies repository chunks and publishes the container only when reconstruction succeeds. It assigns one persistent owner and a new MAC address, disables autostart and leaves the restored container stopped. Review networking, especially static IP addresses, before starting it.

Existing containers cannot be overwritten. Running originals remain separate. The original owner is checked before backup and lifecycle actions share the container task lock.

## Limits

This is a consistent, stopped filesystem backup, not live backup or runtime-memory checkpointing. It captures the managed rootfs image and container settings; snapshots and external mounts are not included. Custom external mounts are rejected where detected. VM isolated boot verification does not apply. Automatic restart failure is reported as a failure needing operator attention, even if the restore point was saved.

The repository still applies its configured immutability, so retention cannot delete a locked restore point until its lock expires. Large VM backup performance, VM hangs and container migration are separate tasks and were not resumed in this work.
