import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from kubernetes import client
from kubernetes.client.rest import ApiException
from tinydb import Query, TinyDB

logger = logging.getLogger("bdd")

os.environ["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = (
    "yes"  # we need this to stop borg cli from manual prompting
)
os.environ["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"

ENV = os.getenv("ENV", "TESTING")


def get_backup_base_dir():
    if os.getenv("PXC_REMOVABLE_DATASTORES"):
        # logic for selecting any of the removables and writing there
        datastore_cmd = subprocess.run(
            ["proxmox-backup-manager", "datastore", "list", "--output-format", "json"],
            stdout=subprocess.PIPE,
            text=True,
        )
        datastores = json.loads(datastore_cmd.stdout)

        target_datastores = os.getenv("PXC_REMOVABLE_DATASTORES").split(",")

        # find the first datastore that matches env var
        matching_online_datastore = None
        for datastore in datastores:
            if datastore["name"] in target_datastores:
                result = subprocess.run(
                    ["findmnt", f"/mnt/datastore/{datastore['name']}"],
                    stdout=subprocess.PIPE,
                    text=True,
                )

                # check if its mounted
                if result.stdout.strip():
                    matching_online_datastore = datastore
                    break

        if not matching_online_datastore:
            raise Exception("Could not find matching datastore!")

        return f"/mnt/datastore/{matching_online_datastore['name']}/pxc"
    elif os.getenv("PXC_BACKUP_BASE_DIR"):
        return os.getenv("PXC_BACKUP_BASE_DIR")
    else:
        raise FileNotFoundError("No env variables configured for any backup scenario!")


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


def get_image_metas(timestamp_filter=None):
    image_meta_db = TinyDB(f"{get_backup_base_dir()}/image-meta-db.json")

    archives = []

    # iterate all k8s namespaced borg repos
    k8s_base_path = f"{get_backup_base_dir()}/borg-k8s"
    namespace_repos = [
        name
        for name in os.listdir(k8s_base_path)
        if os.path.isdir(os.path.join(k8s_base_path, name))
    ]

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

    timestamp_archives = {}
    for archive in archives:
        image = archive["archive"].split("_", 1)[0]
        timestamp = archive["archive"].split("_", 1)[1]

        if timestamp_filter is not None and timestamp_filter != timestamp:
            continue  # skip filtered

        if timestamp not in timestamp_archives:
            timestamp_archives[timestamp] = []

        Meta = Query()
        image_meta = image_meta_db.get(
            (Meta.image_name == image) & (Meta.timestamp == timestamp)
        )

        if image_meta is None:
            logger.error(
                f"None meta found {timestamp}, image_name {image}, archive {archive}"
            )
            del timestamp_archives[timestamp]
            continue

        timestamp_archives[timestamp].append(image_meta)

    return timestamp_archives
