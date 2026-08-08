import asyncio
import logging
import os
from datetime import datetime
from pprint import pformat

import paramiko
import yaml
from kubernetes import client, config
from proxmoxer import ProxmoxAPI

import pve_cloud_backup.fetcher.funcs as funcs
from pve_cloud_backup.fetcher.git import backup_git
from pve_cloud_backup.fetcher.nextcloud import backup_nextcloud
from pve_cloud_backup.fetcher.patroni import backup_patroni

logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "DEBUG").upper()))
logger = logging.getLogger("fetcher-ext")


with open("/opt/backup-conf.yaml", "r") as file:
    backup_config = yaml.safe_load(file)

backup_addr = os.getenv("BDD_HOST")

# main is prod and always runs in cluster
config.load_incluster_config()
v1 = client.CoreV1Api()


async def run():
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    try:
        # backup zfs
        namespace_secrets_zfs, namespace_volume_meta_zfs = funcs.collect_k8s_meta(
            backup_config, provisioner="zfs.csi.openebs.io"
        )
        logger.debug(f"volume_meta zfs:\n{pformat(namespace_volume_meta_zfs)}")

        await funcs.zfs_snap_and_send(
            namespace_volume_meta_zfs,
            timestamp,
            backup_config["k8s_stack"],
            backup_addr,
            paramiko.Ed25519Key.from_private_key_file("/opt/id_ext"),
            pkey_path="/opt/id_ext",
        )

        # merge metas and secrets for single db entry on server side
        await funcs.post_volume_meta(
            namespace_volume_meta_zfs,
            timestamp,
            backup_config["k8s_stack"],
            backup_addr,
        )
        await funcs.post_k8s_namespace_secrets(
            namespace_secrets_zfs,
            timestamp,
            backup_config["k8s_stack"],
            backup_addr,
        )

    finally:
        funcs.cleanup_zfs(
            namespace_volume_meta_zfs,
            timestamp,
            backup_config["k8s_stack"],
            paramiko.Ed25519Key.from_private_key_file("/opt/id_ext"),
        )


def main():
    asyncio.run(run())
