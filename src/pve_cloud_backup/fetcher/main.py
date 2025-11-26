from datetime import datetime
import logging
import pve_cloud_backup.fetcher.funcs as funcs
from pve_cloud_backup.fetcher.nextcloud import backup_nextcloud
from pve_cloud_backup.fetcher.git import backup_git
from proxmoxer import ProxmoxAPI
import os
import yaml
from kubernetes import client, config
from pprint import pformat
import paramiko
import asyncio
from pve_cloud_backup.fetcher.patroni import backup_patroni



logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "DEBUG").upper()))
logger = logging.getLogger("fetcher")

proxmox = ProxmoxAPI(
  os.getenv("PROXMOXER_HOST"), user=os.getenv("PROXMOXER_USER"), backend='ssh_paramiko', private_key_file='/opt/id_proxmox'
)

with open("/opt/backup-conf.yaml", "r") as file:
  backup_config = yaml.safe_load(file)

backup_addr = backup_config["backup_daemon_address"]

# main is prod and always runs in cluster
config.load_incluster_config()
v1 = client.CoreV1Api()


async def run():
  timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

  # backup to borg file repos

  # defined and not null
  if backup_config["git_repos"]:
    await backup_git(backup_addr, timestamp, backup_config["git_repos"])
  else:
    logger.info("No git repos to backup provided, skipping")

  # defined and not null
  if backup_config["nextcloud_files"]:
    await backup_nextcloud(backup_addr, timestamp, backup_config["nextcloud_files"])
  else:
    logger.info("No nextcloud files to backup provided, skipping")

  if backup_config["patroni_stack"]:
    await backup_patroni(backup_addr, timestamp, proxmox, backup_config["patroni_stack"], paramiko.Ed25519Key.from_private_key_file("/opt/id_qemu"))
  else:
    logger.info("No patroni stack provided, skipping pgdump.")

  # backup vms and k8s
  raw_k8s_meta = None
  raw_vm_meta = None
  unique_pools = None

  try:
    k8s_kubeconfigs = funcs.get_kubernetes_clients(backup_config, proxmox, paramiko.Ed25519Key.from_private_key_file("/opt/id_qemu"))
    logger.debug(f"k8s_kubeconfigs:\n{pformat(k8s_kubeconfigs)}")

    raw_k8s_meta, k8s_stack_namespace_secrets = funcs.collect_raw_k8s_meta(backup_config, k8s_kubeconfigs)
    logger.debug(f"k8s_meta:\n{pformat(raw_k8s_meta)}")

    # this simply adds all the images to groups inside of ceph
    unique_pools = funcs.pool_images(raw_k8s_meta)

    # create group snapshots
    funcs.snap_and_clone(raw_k8s_meta, timestamp, unique_pools)
    await funcs.send_backups(raw_k8s_meta, timestamp, backup_addr)

    await funcs.post_image_meta(raw_k8s_meta, timestamp, backup_config, backup_addr)
    await funcs.post_k8s_stack_meta(k8s_kubeconfigs, k8s_stack_namespace_secrets, timestamp, backup_addr)

  finally:
    # we always want to do the cleanup even if something failed
    funcs.cleanup(raw_vm_meta, raw_k8s_meta, timestamp, unique_pools)
    

def main():
  asyncio.run(run())

  
