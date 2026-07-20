import asyncio
import base64
import json
import logging
import os
import pickle
import subprocess

import asyncssh
import paramiko
import yaml
from kubernetes import client, config
from kubernetes.config.kube_config import KubeConfigLoader

import pve_cloud_backup.fetcher.net as net

logger = logging.getLogger("fetcher")

SUPPORTED_PROVISIONERS = ["rbd.csi.ceph.com", "zfs.csi.openebs.io"]


# collect pvc and pv information, aswell as secrets of namespaces
def collect_k8s_meta(backup_config, provisioner):

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

        namespace_driver = (
            None  # check flag => backup currently only supports homogeneus namespaces
        )
        for pvc in pvc_list.items:
            pvc_name = pvc.metadata.name
            volume_name = pvc.spec.volume_name
            status = pvc.status.phase

            if volume_name:
                pv = v1.read_persistent_volume(name=volume_name)
                if not namespace_driver:
                    namespace_driver = pv.spec.csi.driver

                # make sure no mixed drivers in backup ns
                if pv.spec.csi.driver != namespace_driver:
                    raise RuntimeError(
                        f"Backup tool currently doesnt supported mixed csi driver namespaces: {namespace_driver} + {pv.spec.csi.driver} found!"
                    )

                if pv.spec.csi.driver not in SUPPORTED_PROVISIONERS:
                    raise ValueError(
                        f"Backup for unsupported provisioner on {namespace} {pvc_name}"
                    )

                if pv.spec.csi.driver != provisioner:
                    continue  # skip not targeted vols

                pv_dict_b64 = base64.b64encode(pickle.dumps(pv.to_dict())).decode(
                    "utf-8"
                )

                pvc_dict_b64 = base64.b64encode(pickle.dumps(pvc.to_dict())).decode(
                    "utf-8"
                )

                meta = {
                    "namespace": namespace,
                    "pvc_name": pvc_name,
                    "pv_name": pv.metadata.name,
                    "namespace": namespace,
                    "csi_spec": pv.spec.csi,
                    "pvc_dict_b64": pvc_dict_b64,
                    "pv_dict_b64": pv_dict_b64,
                    "storage_class": pvc.spec.storage_class_name,
                }

                if pv.spec.node_affinity and pv.spec.node_affinity.required:
                    meta["required_affinity"] = pv.spec.node_affinity.required

                volume_meta.append(meta)
            else:
                logger.debug(f"PVC: {pvc_name} -> Not bound to a PV [Status: {status}]")

        if volume_meta:  # only set on content
            namespace_volume_meta[namespace] = volume_meta
        else:
            # remove fetched secrets
            logger.debug(
                f"No secrets found for provisioner {provisioner} removing namespace {namespace} secrets"
            )
            namespace_secrets.pop(namespace)

    return namespace_secrets, namespace_volume_meta


def pool_images(namespace_volume_meta):
    # initialize for images grouped by pool
    unique_pools = set()

    # collect pools from k8s volumes
    for volume_meta in namespace_volume_meta.values():
        for meta in volume_meta:
            unique_pools.add(meta["csi_spec"].volume_attributes["pool"])

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
            pool = meta["csi_spec"].volume_attributes["pool"]
            image = meta["csi_spec"].volume_attributes["imageName"]
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
            pool = meta["csi_spec"].volume_attributes["pool"]
            image = meta["csi_spec"].volume_attributes["imageName"]
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
            pool = meta["csi_spec"].volume_attributes["pool"]
            image = meta["csi_spec"].volume_attributes["imageName"]

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
            pool = None
            image = None
            if meta["csi_spec"].driver == "rbd.csi.ceph.com":
                pool = meta["csi_spec"].volume_attributes["pool"]
                image = meta["csi_spec"].volume_attributes["imageName"]
            elif meta["csi_spec"].driver == "zfs.csi.openebs.io":
                pool = meta["csi_spec"].volume_attributes["openebs.io/poolname"]
                image = meta["pv_name"]
            else:
                raise RuntimeError(f"Unsupported csi driver {meta['csi_spec'].driver}")

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
        pickle.dumps(namespace_secrets)
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
                pool = meta["csi_spec"].volume_attributes["pool"]
                image = meta["csi_spec"].volume_attributes["imageName"]
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


# openebs zfs localpv backup
async def zfs_snap_and_send(
    namespace_volume_meta, timestamp, k8s_stack, backup_addr, pkey
):
    logger.info("snap and sending zfs")

    stack_apex = ".".join(k8s_stack.split(".")[1:])

    for namespace, volume_meta in namespace_volume_meta.items():
        namespace_node = (
            volume_meta[0]["required_affinity"]
            .node_selector_terms[0]
            .match_expressions[0]
            .values[0]
        )  # check that only a single node contains all the pvs in the namespace
        logger.info(f"collecting metas for ns: {namespace}, node: {namespace_node}")

        datasets_to_snap = []

        for meta in volume_meta:
            meta_node = (
                meta["required_affinity"]
                .node_selector_terms[0]
                .match_expressions[0]
                .values[0]
            )
            meta_fstype = meta["csi_spec"].fs_type

            if meta_node != namespace_node:
                raise RuntimeError(
                    f"Found different node zfs local pvs for the same namespace {meta_node} / {namespace_node}"
                )

            if meta_fstype != "ext4":
                raise RuntimeError(
                    f"Only support zvol pvcs with ext4 filesystem type (no zfs dataset snapshots as they are not convertible to ceph rbd)!"
                )

            datasets_to_snap.append(
                f"{meta['csi_spec'].volume_attributes['openebs.io/poolname']}/{meta['pv_name']}"
            )

        async with asyncssh.connect(
            namespace_node + "." + stack_apex,
            username="admin",
            client_keys=["/opt/id_qemu"],
            known_hosts=None,
        ) as ssh:
            cmd = "sudo zfs snapshot " + " ".join(
                f"{dss}@{timestamp}" for dss in datasets_to_snap
            )
            logger.debug("executing: %s", cmd)

            await ssh.run(cmd, check=True)

            # backup the zvol through converting it to a raw disk image
            for meta in volume_meta:
                zpool = meta["csi_spec"].volume_attributes["openebs.io/poolname"]
                # first we need to mount the snapshot
                zvol_mount = f"{zpool}/{meta['pv_name']}-{timestamp}-export"

                cmd = (
                    f"sudo zfs clone {zpool}/{meta['pv_name']}@{timestamp} {zvol_mount}"
                )
                logger.debug("executing: %s", cmd)

                await ssh.run(cmd, check=True)

                # then we use qemu-img to export and upload
                request_dict = {
                    "borg_archive_type": "k8s",
                    "archive_name": meta["pv_name"],
                    "timestamp": timestamp,
                    "stdin_name": meta["pv_name"] + ".raw",
                    "namespace": meta["namespace"],
                }
                logger.info(request_dict)

                async def chunk_generator():
                    cmd = f"sudo dd if=/dev/zvol/{zvol_mount} bs=4M status=none | zstd -1 -T4 --stdout"
                    logger.debug("executing: %s", cmd)

                    proc = await ssh.create_process(cmd, encoding=None)

                    while True:
                        chunk = await proc.stdout.read(40 * 1024 * 1024)  # 40 MiB

                        if not chunk:
                            break

                        yield chunk

                    await proc.wait()

                    logger.info("dd exit code %s", proc.exit_status)

                await net.archive_async(
                    backup_addr, request_dict, chunk_generator, compress=False
                )


def cleanup_zfs(namespace_volume_meta, timestamp, k8s_stack, pkey):
    stack_apex = ".".join(k8s_stack.split(".")[1:])

    if namespace_volume_meta is not None:
        for volume_meta in namespace_volume_meta.values():
            namespace_node = (
                volume_meta[0]["required_affinity"]
                .node_selector_terms[0]
                .match_expressions[0]
                .values[0]
            )

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            logger.info(f"connecting to {namespace_node}.{stack_apex}")

            try:
                ssh.connect(
                    namespace_node + "." + stack_apex, username="admin", pkey=pkey
                )

                for meta in volume_meta:
                    zpool = meta["csi_spec"].volume_attributes["openebs.io/poolname"]

                    # delete zpool clones
                    logger.info(
                        f"running: sudo zfs destroy {zpool}/{meta['pv_name']}-{timestamp}-export"
                    )
                    _, stdout, _ = ssh.exec_command(
                        f"sudo zfs destroy {zpool}/{meta['pv_name']}-{timestamp}-export"
                    )

                    logger.info(
                        "zfs clone destroy exit code: %s",
                        stdout.channel.recv_exit_status(),
                    )

                    # delete snapshots
                    logger.info(
                        f"running: sudo zfs destroy {zpool}/{meta['pv_name']}@{timestamp}"
                    )
                    _, stdout, _ = ssh.exec_command(
                        f"sudo zfs destroy {zpool}/{meta['pv_name']}@{timestamp}"
                    )

                    logger.info(
                        "zfs destroy snapshot exit code: %s",
                        stdout.channel.recv_exit_status(),
                    )

            finally:
                ssh.close()
