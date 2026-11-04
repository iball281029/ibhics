#!/bin/sh
# Runs in the Debian installer when the default (erase without asking) entry finds
# ibHiCS already on the disk it would erase: says so, erases nothing, and restarts.
. /usr/share/debconf/confmodule
debconf-loadtemplate ibhics /cdrom/ibhics/ibhics.templates
db_subst ibhics/already-installed DISK "$1"
db_fset ibhics/already-installed seen false
db_input critical ibhics/already-installed || true
db_go || true
reboot
