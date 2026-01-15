import asyncio
import base64
import json
import logging
import os
import pickle
import subprocess
from pprint import pformat

import paramiko
import yaml
from kubernetes import client, config
from kubernetes.config.kube_config import KubeConfigLoader

import pve_cloud_backup.fetcher.net as net

logger = logging.getLogger("fetcher")


# collect pvc and pv information, aswell as secrets of namespaces
def collect_k8s_meta(backup_config):

    config.load_incluster_config()
    v1 = client.CoreV1Api()

    namespace_secrets = {}

    namespace_volume_meta = {}

    for namespace_item in v1.list_namespace().items:
        namespace = namespace_item.metadata.name

        if namespace not in backup_config["k8s_namespaces"]:
            continue
        
        volume_meta = []

        # collect secrets of namespace
        namespace_secrets[namespace] = [
            secret.to_dict()
            for secret in v1.list_namespaced_secret(namespace=namespace).items
        ]

        pvc_list = v1.list_namespaced_persistent_volume_claim(namespace=namespace)

        for pvc in pvc_list.items:
            pvc_name = pvc.metadata.name
            volume_name = pvc.spec.volume_name
            status = pvc.status.phase

            if volume_name:
                pv = v1.read_persistent_volume(name=volume_name)
                pv_dict_b64 = base64.b64encode(pickle.dumps(pv.to_dict())).decode(
                    "utf-8"
                )

                pvc_dict_b64 = base64.b64encode(pickle.dumps(pvc.to_dict())).decode(
                    "utf-8"
                )

                volume_meta.append(
                    {
                        "namespace": namespace,
                        "pvc_name": pvc_name,
                        "namespace": namespace,
                        "image_name": pv.spec.csi.volume_attributes["imageName"],
                        "pool": pv.spec.csi.volume_attributes["pool"],
                        "pvc_dict_b64": pvc_dict_b64,
                        "pv_dict_b64": pv_dict_b64,
                        "storage_class": pvc.spec.storage_class_name,
                    }
                )
            else:
                logger.debug(
                    f"PVC: {pvc_name} -> Not bound to a PV [Status: {status}]"
                )
        
        namespace_volume_meta[namespace] = volume_meta

    return namespace_secrets, namespace_volume_meta


def pool_images(namespace_volume_meta):
    # initialize for images grouped by pool
    unique_pools = set()

    # collect pools from k8s volumes
    for volume_meta in namespace_volume_meta.values():
        for meta in volume_meta:
            unique_pools.add(meta["pool"])

    # create rbd groups
    for pool in unique_pools:
        try:
            # check for errors, capture stderr output as text
            subprocess.run(
                ["rbd", "group", "create", f"{pool}/backups"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.warning(
                e.stdout + e.stderr
            )  # no problem if group already exists, cleanup failed tho

    # add rbds from pvcs
    for volume_meta in namespace_volume_meta.values():
        for meta in volume_meta:
            pool = meta["pool"]
            image = meta["image_name"]
            try:
                subprocess.run(
                    [
                        "rbd",
                        "group",
                        "image",
                        "add",
                        f"{pool}/backups",
                        f"{pool}/{image}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                logger.error(e.stdout + e.stderr)  # proper error printing
                raise

    return unique_pools


def clone(pool, image, timestamp):
    try:
        command = subprocess.run(
            ["rbd", "snap", "ls", "--all", "--format", "json", f"{pool}/{image}"],
            check=True,
            capture_output=True,
            text=True,
        )
        snaps = json.loads(command.stdout)
        # doesnt logger.info anything on success
    except subprocess.CalledProcessError as e:
        logger.error(e.stdout + e.stderr)
        raise

    for snap in snaps:
        if (
            snap["namespace"]["type"] == "group"
            and snap["namespace"]["group snap"] == timestamp
        ):
            snap_id = snap["id"]
            break

    logger.debug(f"image {image} snap id {snap_id}")

    # create temporary clone
    try:
        subprocess.run(
            [
                "rbd",
                "clone",
                "--snap-id",
                str(snap_id),
                f"{pool}/{image}",
                f"{pool}/temp-clone-{timestamp}-{image}",
                "--rbd-default-clone-format",
                "2",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(e.stdout + e.stderr)
        raise


def snap_and_clone(namespace_volume_meta, timestamp, unique_pools):
    logger.info("creating snaps")
    for pool in unique_pools:
        try:
            subprocess.run(
                ["rbd", "group", "snap", "create", f"{pool}/backups@{timestamp}"],
                check=True,
                capture_output=True,
                text=True,
            )
            # doesnt logger.info anything on success
        except subprocess.CalledProcessError as e:
            logger.error(e.stdout + e.stderr)
            raise

    logger.info("creating clones")

    # clone all the snapshots into new images so we can export them
    # sadly there isnt yet a direct export function for group snapshots
    for volume_meta in namespace_volume_meta.values():
        for meta in volume_meta:
            pool = meta["pool"]
            image = meta["image_name"]
            clone(pool, image, timestamp)


async def send_export(send_command, semaphore):
    async with semaphore:
        backup_addr = send_command["backup_addr"]
        params = send_command["params"]

        request_dict = {
            "borg_archive_type": "k8s",
            "archive_name": params["image_name"],
            "timestamp": params["timestamp"],
            "stdin_name": params["image_name"] + ".raw",
            "namespace": params["namespace"],
        }
        logger.info(request_dict)

        # to get full performance we need to have the subprocess reading async aswell
        async def async_chunk_generator():
            proc = await asyncio.create_subprocess_exec(
                *send_command["subprocess_args"], stdout=asyncio.subprocess.PIPE
            )

            while True:
                chunk = await proc.stdout.read(4 * 1024 * 1024 * 10)  # 4MB
                if not chunk:
                    break
                yield chunk

            await proc.wait()

        await net.archive_async(backup_addr, request_dict, async_chunk_generator)


async def send_backups(namespace_volume_meta, timestamp, backup_addr):
    send_commands = []

    for volume_meta in namespace_volume_meta.values():
        for meta in volume_meta:
            pool = meta["pool"]
            image = meta["image_name"]

            params = {
                "timestamp": timestamp,
                "image_name": image,
                "pool": pool,
                "namespace": meta["namespace"],
            }

            send_commands.append(
                {
                    "params": params,
                    "backup_addr": backup_addr,
                    "subprocess_args": [
                        "rbd",
                        "export",
                        f"{pool}/temp-clone-{timestamp}-{image}",
                        "-",
                    ],
                }
            )

    semaphore = asyncio.Semaphore(int(os.getenv("SEND_PARALELLISM_NUM", "2")))

    # start one thread per type, since borg on bdd side is single threaded per archive
    export_tasks = [
        asyncio.create_task(send_export(command, semaphore))
        for command in send_commands
    ]

    await asyncio.gather(*export_tasks)


async def post_volume_meta(namespace_volume_meta, timestamp, k8s_stack, backup_addr):
    for volume_meta in namespace_volume_meta.values():
        for meta in volume_meta:
            pool = meta["pool"]
            image = meta["image_name"]
            body = {
                "timestamp": timestamp,
                "image_name": image,
                "pool": pool,
                "stack": k8s_stack,
                "type": "k8s",
                "namespace": meta["namespace"],
                "pvc_dict_b64": meta["pvc_dict_b64"],
                "pv_dict_b64": meta["pv_dict_b64"],
                "pvc_name": meta["pvc_name"],
                "storage_class": meta["storage_class"],
            }

            logger.debug(f"posting {body}")
            await net.volume_meta(backup_addr, body)


async def post_k8s_namespace_secrets(
    namespace_secrets, timestamp, k8s_stack, backup_addr
):

    namespace_secret_dict_b64 = base64.b64encode(
        pickle.dumps(namespace_secrets[k8s_stack])
    ).decode("utf-8")
    body = {
        "timestamp": timestamp,
        "stack": k8s_stack,
        "namespace_secret_dict_b64": namespace_secret_dict_b64,
    }
    logger.debug(f"posting {body}")

    await net.namespace_secrets(backup_addr, body)


def cleanup(namespace_volume_meta, timestamp, unique_pools):
    logger.info("cleanup")

    if namespace_volume_meta is not None:
        for volume_meta in namespace_volume_meta.values():
            for meta in volume_meta:
                pool = meta["pool"]
                image = meta["image_name"]
                try:
                    subprocess.run(
                        ["rbd", "rm", f"{pool}/temp-clone-{timestamp}-{image}"],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError as e:
                    logger.warning(e.stdout + e.stderr)

    if unique_pools is not None:
        # delete snaps
        for pool in unique_pools:
            logger.debug("removing snaps from pool " + pool)
            try:
                subprocess.run(
                    ["rbd", "group", "snap", "rm", f"{pool}/backups@{timestamp}"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                # doesnt logger.info anything on success
            except subprocess.CalledProcessError as e:
                logger.warning(e.stdout + e.stderr)

        # delete groups
        for pool in unique_pools:
            logger.debug("removing backup group from pool " + pool)
            try:
                subprocess.run(
                    ["rbd", "group", "rm", f"{pool}/backups"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                # doesnt logger.info anything on success
            except subprocess.CalledProcessError as e:
                logger.warning(e.stdout + e.stderr)
