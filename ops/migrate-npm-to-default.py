"""One-off authorized migration; keep source until destination is verified."""
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

import libvirt

UUID='50d7cc51-7d23-4ff0-99f0-01ab5324c15e'
conn=libvirt.open('qemu:///system')
dom=conn.lookupByUUIDString(UUID)
assert dom.name()=='npm-vm' and dom.isActive()
backup=Path('/root/ibhics-vstorage-backup-20261110')
backup.mkdir(mode=0o700,exist_ok=True)
original=dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
root=ET.fromstring(original)
disk=root.find("devices/disk[@device='disk']")
assert len(root.findall("devices/disk[@device='disk']"))==1
assert disk.find('source').get('pool')=='hybrid01'
assert not dom.snapshotNum(0)
source_pool=conn.storagePoolLookupByName('hybrid01')
source=source_pool.storageVolLookupByName('npm-vm-disk0.qcow2')
target_pool=conn.storagePoolLookupByName('default')
assert target_pool.isActive()
name='npm-vm-disk0.qcow2'
assert name not in target_pool.listVolumes()
size=source.info()[1]
assert target_pool.info()[3] > source.info()[2]*1.5+512*1024**2
for filename,content in [('npm-vm-before.xml',original),('npm-vm-before-live.xml',dom.XMLDesc(0)),('hybrid01-pool.xml',source_pool.XMLDesc(0))]:
 path=backup/filename
 assert not path.exists(), 'Backup already exists; review before retry'
 path.write_text(content)
 os.chmod(path,0o600)
new=target_pool.createXML(f"<volume><name>{name}</name><capacity unit='bytes'>{size}</capacity><allocation>0</allocation><target><format type='qcow2'/></target></volume>",0)
destination=new.path()
print(json.dumps({'step':'copy','source':source.path(),'destination':destination,'source_retained':True}),flush=True)
xml=f"<disk type='file'><source file='{destination}'/><driver type='qcow2'/></disk>"
dom.blockCopy('vda',xml,None,libvirt.VIR_DOMAIN_BLOCK_COPY_REUSE_EXT|libvirt.VIR_DOMAIN_BLOCK_COPY_TRANSIENT_JOB)
started=time.monotonic()
pivoted=False
try:
 while time.monotonic()-started<1200:
  job=dom.blockJobInfo('vda',0)
  if not job:raise RuntimeError('block copy stopped unexpectedly')
  if job.get('end') and job['cur']==job['end']:
   try:
    dom.blockJobAbort('vda',libvirt.VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT)
    pivoted=True
    break
   except libvirt.libvirtError:pass
  time.sleep(1)
 if not pivoted:raise RuntimeError('copy timed out')
except BaseException:
 if not pivoted:
  try:dom.blockJobAbort('vda',0)
  except libvirt.libvirtError:pass
 raise
# Persist immediately after the live pivot. Original image is deliberately retained.
source_element=disk.find('source')
source_element.clear()
source_element.set('pool','default')
source_element.set('volume',name)
for meta in root.findall('metadata/*/*'):
 if meta.tag.endswith('disk') and meta.get('dev')=='vda':
  meta.set('pool','default');meta.set('volume',name)
conn.defineXML(ET.tostring(root,encoding='unicode'))
for flags in [0,libvirt.VIR_DOMAIN_XML_INACTIVE]:
 check=ET.fromstring(dom.XMLDesc(flags)).find("devices/disk[@device='disk']/source")
 assert check.get('pool')=='default' or check.get('file')==destination, ET.tostring(check)
assert dom.isActive()
code=subprocess.run(['curl','-s','-o','/dev/null','-w','%{http_code}','--max-time','10','http://172.16.12.194:81/'],capture_output=True,text=True)
assert code.stdout=='200',code.stdout
(backup/'npm-migration-verified.json').write_text(json.dumps({'uuid':UUID,'destination':destination,'http':200,'source_retained':source.path(),'verified_at':time.time()},indent=2))
print('VERIFIED: live and persistent disk use default, NPM admin HTTP 200; original hybrid01 image retained.',flush=True)
