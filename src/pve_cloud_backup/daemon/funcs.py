import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from kubernetes import client
from kubernetes.client.rest import ApiException

from pve_cloud_backup.daemon.db import get_volume_metas as db_get_volume_metas

logger = logging.getLogger("bdd")

os.environ["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = (
    "yes"  # we need this to stop borg cli from manual prompting
)
os.environ["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"

ENV = os.getenv("ENV", "TESTING")


def get_backup_base_dir():
    return "/mnt/bdd-zfs-data"


def init_backup_dir(backup_dir):
    backup_base_dir = get_backup_base_dir()

    full_backup_dir = f"{backup_base_dir}/borg-{backup_dir}"

    Path(full_backup_dir).mkdir(parents=True, exist_ok=True)

    # init borg repo, is ok to fail if it already exists
    subprocess.run(["borg", "init", "--encryption=none", full_backup_dir])

    return full_backup_dir


def copy_backup_generic():
    backup_base_dir = get_backup_base_dir()

    Path(backup_base_dir).mkdir(parents=True, exist_ok=True)

    source_dir = "/opt/bdd"
    for file in os.listdir(source_dir):
        if not file.startswith("."):
            full_source_path = os.path.join(source_dir, file)
            full_dest_path = os.path.join(backup_base_dir, file)

            if os.path.isfile(full_source_path):
                shutil.copy2(full_source_path, full_dest_path)


async def get_volume_metas(timestamp_filter=None):
    db_result = await db_get_volume_metas(timestamp_filter)

    k8s_base_path = f"{get_backup_base_dir()}/borg-k8s"
    namespace_repos = [
        name
        for name in os.listdir(k8s_base_path)
        if os.path.isdir(os.path.join(k8s_base_path, name))
    ]

    archives = []
    for repo in namespace_repos:
        list_result = subprocess.run(
            ["borg", "list", f"{get_backup_base_dir()}/borg-k8s/{repo}", "--json"],
            capture_output=True,
        )

        if list_result.returncode != 0:
            raise Exception(
                f"Borg list failed for repo {repo}: {list_result.stderr.decode()}"
            )

        archives.extend(json.loads(list_result.stdout)["archives"])

    result = {}
    for archive in archives:
        image = archive["archive"].split("_", 1)[0]
        timestamp = archive["archive"].split("_", 1)[1]

        if timestamp_filter is not None and timestamp_filter != timestamp:
            continue

        if timestamp not in result:
            result[timestamp] = []

        if timestamp in db_result:
            result[timestamp].extend(db_result[timestamp])

    return result
