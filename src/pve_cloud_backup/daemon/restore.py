import asyncio
import base64
import fnmatch
import json
import logging
import os
import pickle
import ssl
import struct
import subprocess
import time
import uuid
import paramiko
import asyncssh
from pprint import pformat

import zstandard as zstd
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.utils.quantity import parse_quantity
from tinydb import Query, TinyDB

from pve_cloud_backup.daemon.rpc import Command

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("pxc-restore")


# these functions are necessary to convert python k8s naming to camel case
def to_camel_case(snake_str):
    components = snake_str.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


# this one too
def convert_keys_to_camel_case(obj):
    if isinstance(obj, dict):
        new_dict = {}
        for key, value in obj.items():
            new_key = to_camel_case(key)
            new_dict[new_key] = convert_keys_to_camel_case(value)
        return new_dict
    elif isinstance(obj, list):
        return [convert_keys_to_camel_case(item) for item in obj]
    else:
        return obj


async def init_procedure_bdd(restore_args):

    # connect to the backup server and start the restore procedure
    # we simply trust the host here as the ca is only available from
    # postgres secrets
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    reader, writer = await asyncio.open_connection(
        restore_args["bdd_host"], 8085, ssl=ssl_ctx
    )
    writer.write(struct.pack("B", Command.RESTORE_PROCEDURE.value))
    await writer.drain()

    # first we send the timestamp and receive our meta information for the restore
    writer.write((restore_args["timestamp"] + "\n").encode())
    await writer.drain()

    # read the archives
    dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
    metas = pickle.loads((await reader.readexactly(dict_size)))

    metas_grouped_by_ns = {}

    for meta in metas:
        if meta["namespace"] not in metas_grouped_by_ns:
            metas_grouped_by_ns[meta["namespace"]] = []

        metas_grouped_by_ns[meta["namespace"]].append(meta)

    # query the server for backup secrets
    writer.write(
        (
            restore_args["stack_name"] + "." + restore_args["cloud_domain"] + "\n"
        ).encode()
    )
    await writer.drain()

    # read the the meta information
    dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
    stack_meta = pickle.loads((await reader.readexactly(dict_size)))

    namespace_secret_dict = pickle.loads(
        base64.b64decode(stack_meta["namespace_secret_dict_b64"])
    )

    return reader, writer, metas_grouped_by_ns, namespace_secret_dict


def clean_pvc_dict(pvc_dict):
    # clean the old pvc object so it can be submitted freshly
    pvc_dict["metadata"]["annotations"].pop(
        "pv.kubernetes.io/bind-completed", None
    )
    pvc_dict["metadata"]["annotations"].pop(
        "pv.kubernetes.io/bound-by-controller", None
    )
    pvc_dict["metadata"].pop("finalizers", None)
    pvc_dict["metadata"].pop("managed_fields", None)
    pvc_dict["metadata"].pop("resource_version", None)
    pvc_dict["metadata"].pop("uid", None)
    pvc_dict["metadata"].pop("creation_timestamp", None)
    pvc_dict.pop("status", None)
    pvc_dict.pop("kind", None)
    pvc_dict.pop("api_version", None)


def clean_pv_dict(pv_dict):
    # cleanup the old pv aswell for recreation
    pv_dict.pop("api_version", None)
    pv_dict.pop("kind", None)
    pv_dict["metadata"].pop("creation_timestamp", None)
    pv_dict["metadata"].pop("finalizers", None)
    pv_dict["metadata"].pop("managed_fields", None)
    pv_dict["metadata"].pop("resource_version", None)
    pv_dict["metadata"]["annotations"].pop(
        "volume.kubernetes.io/provisioner-deletion-secret-name", None
    )
    pv_dict["metadata"]["annotations"].pop(
        "volume.kubernetes.io/provisioner-deletion-secret-namespace", None
    )
    pv_dict.pop("status", None)
    pv_dict["spec"].pop("claim_ref", None)
    pv_dict["spec"].pop("volume_attributes_class_name", None)
    pv_dict["spec"].pop("scale_io", None)
    pv_dict["spec"]["csi"].pop("volume_handle", None)
    pv_dict["spec"]["csi"]["volume_attributes"].pop("imageName", None)
    pv_dict["spec"]["csi"]["volume_attributes"].pop("journalPool", None)
    pv_dict["spec"]["csi"]["volume_attributes"].pop("pool", None)


async def procedure():
    restore_args = json.loads(base64.b64decode(os.getenv("PXC_RESTORE_ARGS")))
    logger.info(restore_args)

    reader, writer, metas_grouped_by_ns, namespace_secret_dict = await init_procedure_bdd(restore_args)

    # now we start the restore procedure
    config.load_incluster_config()
    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    storage_v1 = client.StorageV1Api()
    custom_api = client.CustomObjectsApi()

    # get ceph and zfs storage classes
    cluster_storage_classes = {
        sc.metadata.name: sc
        for sc in storage_v1.list_storage_class().items
        if sc.provisioner in ["rbd.csi.ceph.com", "zfs.csi.openebs.io"]
    }
    # zfs zpool can be read directly from the storageclass

    # load existing ceph pools and fetch their ids, needed for later pv restoring
    ls_call = subprocess.run(
        ["ceph", "osd", "pool", "ls", "detail", "-f", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    pool_details = json.loads(
        ls_call.stdout
    )  # load existing ceph pools and fetch their ids, needed for later pv restoring

    pool_name_id = {}
    for pool_detail in pool_details:
        pool_name_id[pool_detail["pool_name"]] = pool_detail["pool_id"]

    # get the cluster id from ceph ns
    ceph_csi_config = core_v1.read_namespaced_config_map(
        name="ceph-csi-config", namespace="ceph-csi"
    )

    if not ceph_csi_config:
        raise Exception(
            "Could not find ceph-csi-config config map in ceph-csi namespace"
        )

    ceph_cluster_id = json.loads(ceph_csi_config.data.get("config.json"))[0][
        "clusterID"
    ]

    filter_namespaces = (
        []
        if restore_args["namespaces"] == ""
        else restore_args["namespaces"].split(",")
    )

    logger.info("restoring namespaces")
    for orig_namespace, metas_group in metas_grouped_by_ns.items():
        if filter_namespaces and orig_namespace not in filter_namespaces:
            continue  # skip filtered out namespaces

        logger.info(f"restoring {orig_namespace}")
        restore_namespace = orig_namespace

        if restore_args["namespace_mapping"]:
            for namespace_mapping in restore_args["namespace_mapping"]:
                if namespace_mapping.startswith(orig_namespace):
                    restore_namespace = namespace_mapping.split(":")[1]
                    logger.info(f"namespace mapping matched {namespace_mapping}")

        logger.info(
            f"trying to restore volumes of {orig_namespace} into {restore_namespace}"
        )

        auto_scale_replicas = {}
        if restore_args["auto_scale"]:
            # auto downscale deployments and statefulsets of namespace
            deployments = apps_v1.list_namespaced_deployment(restore_namespace)
            for d in deployments.items:
                name = d.metadata.name
                auto_scale_replicas[f"dp-{name}"] = (
                    d.spec.replicas
                )  # save original replicas for upscale later
                logger.info(f"Scaling Deployment '{name}' to 0 replicas...")
                apps_v1.patch_namespaced_deployment_scale(
                    name=name,
                    namespace=restore_namespace,
                    body={"spec": {"replicas": 0}},
                )

            statefulsets = apps_v1.list_namespaced_stateful_set(restore_namespace)
            for s in statefulsets.items:
                name = s.metadata.name
                auto_scale_replicas[f"ss-{name}"] = s.spec.replicas
                logger.info(f"Scaling StatefulSet '{name}' to 0 replicas...")
                apps_v1.patch_namespaced_stateful_set_scale(
                    name=name,
                    namespace=restore_namespace,
                    body={"spec": {"replicas": 0}},
                )

            # wait for termination
            while True:
                pods = core_v1.list_namespaced_pod(restore_namespace)
                remaining = [
                    pod.metadata.name
                    for pod in pods.items
                    if pod.status.phase in ["Running", "Pending", "Terminating"]
                ]
                if not remaining:
                    logger.info("All pods have terminated.")
                    break
                logger.info(f"Still active pods: {remaining}")
                time.sleep(5)

        # check if namespace has pods => throw exeption and tell user to scale down any
        pods = core_v1.list_namespaced_pod(namespace=restore_namespace)

        existing_pvcs = set(
            pvc.metadata.name
            for pvc in core_v1.list_namespaced_persistent_volume_claim(
                restore_namespace
            ).items
        )
        logger.debug(f"existing pvcs {existing_pvcs}")

        # if any pending / running pods exist fail
        pod_phases = [pod for pod in pods.items if pod.status.phase != "Succeeded"]
        if pod_phases:
            raise Exception(
                f"found pods in {restore_namespace} - {pod_phases} - scale down all and force delete!"
            )

        # process secret overwrites
        if restore_args["secret_pattern"]:

            namespace_secrets = {
                secret["metadata"]["name"]: secret
                for secret in namespace_secret_dict[orig_namespace]
            }

            for secret_pattern in restore_args["secret_pattern"]:
                if secret_pattern.split("/")[0] == restore_namespace:
                    # arg that is meant for this namespace restore
                    pattern = secret_pattern.split("/")[1]

                    for secret in namespace_secrets:
                        if fnmatch.fnmatch(secret, pattern):
                            logger.info(
                                f"overwrite pattern matched {pattern}, trying to patch {secret}"
                            )
                            try:
                                core_v1.patch_namespaced_secret(
                                    name=secret,
                                    namespace=restore_namespace,
                                    body={"data": namespace_secrets[secret]["data"]},
                                )
                            except ApiException as e:
                                # if it doesnt exist we simply create it
                                if e.status == 404:
                                    core_v1.create_namespaced_secret(
                                        namespace=restore_namespace,
                                        body={
                                            "metadata": {"name": secret},
                                            "data": namespace_secrets[secret]["data"],
                                        },
                                    )
                                    logger.info(
                                        f"secret {secret} did not exist, created it instead!"
                                    )
                                else:
                                    raise

        if restore_args["auto_delete"]:
            pvcs = core_v1.list_namespaced_persistent_volume_claim(restore_namespace)
            for pvc in pvcs.items:
                name = pvc.metadata.name
                logger.info(f"Deleting PVC: {name}")
                core_v1.delete_namespaced_persistent_volume_claim(
                    name=name,
                    namespace=restore_namespace,
                    body=client.V1DeleteOptions(),
                )

            while True:
                leftover = core_v1.list_namespaced_persistent_volume_claim(
                    restore_namespace
                ).items
                if not leftover:
                    logger.info("All PVCs have been deleted.")
                    break
                logger.info(f"Still waiting on: {[p.metadata.name for p in leftover]}")
                time.sleep(5)

            # there are no more existing pvcs
            existing_pvcs = set()

        # extract raw rbd images, import and recreate pvc if necessary
        for meta in metas_group:
            logger.debug(f"restoring {meta}")

            image_name = meta["image_name"]

            type = meta["type"]

            pvc_dict = pickle.loads(base64.b64decode(meta["pvc_dict_b64"]))
            logger.debug(f"pvc_dict:\n{pvc_dict}")
            pv_dict = pickle.loads(base64.b64decode(meta["pv_dict_b64"]))
            logger.debug(f"pv_dict:\n{pv_dict}")

            # import the image into ceph
            # move to new pool if mapping is defined
            pool = meta["pool"]
            storage_class = pvc_dict["spec"]["storage_class_name"]

            if restore_args["pool_sc_mapping"]:
                for pool_mapping in restore_args["pool_sc_mapping"]:
                    old_pool = pool_mapping.split(":")[0]
                    new_pool_sc = pool_mapping.split(":")[1]
                    if pool == old_pool:
                        pool = new_pool_sc.split("/")[0]
                        storage_class = new_pool_sc.split("/")[1]
                        logger.debug(
                            f"new mapping specified old pool {old_pool}, new pool {pool}, new sc {storage_class}"
                        )
                        break

            old_provisioner = pv_dict["spec"]["csi"]["driver"]
            target_provisioner = cluster_storage_classes[storage_class].provisioner

            if old_provisioner != target_provisioner:
                raise NotImplementedError("Cross provider restore not yet implemented!")

            if target_provisioner == "zfs.csi.openebs.io":
                # todo: pick better target node for restoring / make configurable
                target_restore_zfs_node = cluster_storage_classes[storage_class].allowed_topologies[0].match_label_expressions[0].values[0]
                logger.debug(f"restoring zfs to node {target_restore_zfs_node}")

                # get the nodes ip via kubeapi
                node = core_v1.read_node(name=target_restore_zfs_node)

                internal_ip = next(
                    addr.address
                    for addr in node.status.addresses
                    if addr.type == "InternalIP"
                )

                async with asyncssh.connect(
                        internal_ip,
                        username="admin",
                        client_keys=["/opt/id_qemu"],
                        known_hosts=None
                ) as ssh:
                    restore_pvc_uuid = str(uuid.uuid4())
                    # first we create a zvol of identical size
                    # todo: remove -rest stub and replace with new id generation
                    # todo: write converter function to create zvol in exact size like k8s did zfs list -t volume -o name,volsize
                    cmd = f"sudo zfs create -s -V {parse_quantity(pvc_dict['spec']['resources']['requests']['storage'])} tank-pv/pvc-{restore_pvc_uuid}"
                    logger.info("Executing create: %s", cmd)

                    await ssh.run(cmd, check=True)

                    # then we pipe the compressed stream into dd zstd decompression pipeline
                    proc = await ssh.create_process(f"zstd --decompress --stdout | sudo dd of=/dev/zvol/tank-pv/pvc-{restore_pvc_uuid} bs=4M status=none", encoding=None)

                    # send to the bdd server what we want to request
                    request_archive = f"borg-{type}/{orig_namespace}\n"
                    request_artifact = f"{image_name}_{restore_args['timestamp']}\n"
                    logger.info(
                        f"requesting borg archive stream from bdd {request_archive} - {request_artifact} into dd zvol import {pool}/pvc-{restore_pvc_uuid}"
                    )

                    # bdd server does readline()
                    writer.write(request_archive.encode())
                    await writer.drain()

                    writer.write(request_artifact.encode())
                    await writer.drain()
                    logger.info("send request artifact / archive")

                    # read compressed chunks
                    while True:
                        # client first always sends chunk size
                        chunk_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                        if chunk_size == 0:
                            logger.debug("received eof")
                            break  # client sends 0 chunk size at the end to signal that its finished uploading

                        chunk = await reader.readexactly(chunk_size)

                        proc.stdin.write(chunk)
                        await proc.stdin.drain()

                    logger.info("done reading closing proc")
                    proc.stdin.close()
                    await proc.wait()

                    logger.info(
                        "dd exit code %s",
                        proc.exit_status
                    )

                    if proc.exit_status != 0:
                        raise Exception(f"DD zvol import failed with code {proc.exit_status}")

                    # create the new pvc based on the old - remove dynamic fields of old:
                    if pvc_dict["metadata"]["name"] in existing_pvcs:
                        pvc_name = pvc_dict["metadata"]["name"]
                        pvc_dict["metadata"]["name"] = f"test-restore-{pvc_name}"
                        logger.info(
                            f"pvc {pvc_name} exists, creating it with test-restore- prefix"
                        )

                    clean_pvc_dict(pvc_dict)

                    # set new values
                    new_pv_name = f"pvc-{restore_pvc_uuid}"
                    pvc_dict["spec"]["storage_class_name"] = storage_class
                    pvc_dict["metadata"]["namespace"] = restore_namespace

                    # we can give it a customized pv name so we know migrated ones - will still behave like a normal created pv
                    pvc_dict["spec"]["volume_name"] = new_pv_name

                    # creation call
                    logger.debug(f"creating new pvc:\n{pformat(pvc_dict)}")
                    core_v1.create_namespaced_persistent_volume_claim(
                        namespace=restore_namespace,
                        body=client.V1PersistentVolumeClaim(
                            **convert_keys_to_camel_case(pvc_dict)
                        ),
                    )

                    clean_pv_dict(pv_dict)

                    # update claimRef - same as pv name for openebs
                    pv_dict["spec"]["csi"]["volume_handle"] = new_pv_name

                    # set the node we restored to
                    pv_dict["spec"]["node_affinity"]["required"]["node_selector_terms"][0]["match_expressions"][0]["values"] = [
                        target_restore_zfs_node
                    ]

                    pv_dict["spec"]["storage_class_name"] = storage_class

                    pv_dict["metadata"]["name"] = new_pv_name

                    logger.debug(f"creating new pv:\n{pformat(pv_dict)}")
                    core_v1.create_persistent_volume(
                        body=client.V1PersistentVolume(**convert_keys_to_camel_case(pv_dict))
                    )

                    # openebs zfs also has a custom resource for zvols that needs to be created
                    custom_api.create_namespaced_custom_object(
                        group="zfs.openebs.io",
                        version="v1",
                        namespace="openebs",
                        plural="zfsvolumes",
                        body={
                            "apiVersion": "zfs.openebs.io/v1",
                            "kind": "ZFSVolume",
                            "metadata": {
                                "name": new_pv_name,
                                "namespace": "openebs",
                                "labels": {
                                    "kubernetes.io/nodename": target_restore_zfs_node
                                },
                            },
                            "spec": {
                                "capacity": str(parse_quantity(pvc_dict['spec']['resources']['requests']['storage'])),
                                "fsType": "ext4",
                                "ownerNodeID": target_restore_zfs_node,
                                "poolName": "tank-pv",
                                "quotaType": "quota",
                                "volumeType": "ZVOL",
                            },
                        }
                    )

            elif target_provisioner == "rbd.csi.ceph.com":

                new_csi_image_name = f"csi-vol-{uuid.uuid4()}"

                # send to the bdd server what we want to request
                request_archive = f"borg-{type}/{orig_namespace}\n"
                request_artifact = f"{image_name}_{restore_args['timestamp']}\n"

                logger.info(
                    f"requesting borg archive stream from bdd {request_archive} - {request_artifact} into rbd import {pool}/{new_csi_image_name}"
                )

                # bdd server does readline()
                writer.write(request_archive.encode())
                await writer.drain()

                writer.write(request_artifact.encode())
                await writer.drain()

                # pipe the resulting stream into rbd import
                rbd_import_proc = await asyncio.create_subprocess_exec(
                    "rbd",
                    "import",
                    "-",
                    f"{pool}/{new_csi_image_name}",
                    stdin=asyncio.subprocess.PIPE,
                )

                # read compressed chunks
                decompressor = zstd.ZstdDecompressor().decompressobj()
                while True:
                    # client first always sends chunk size
                    chunk_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                    if chunk_size == 0:
                        break  # client sends 0 chunk size at the end to signal that its finished uploading
                    chunk = await reader.readexactly(chunk_size)

                    # decompress and write
                    decompressed_chunk = decompressor.decompress(chunk)
                    if decompressed_chunk:
                        rbd_import_proc.stdin.write(decompressed_chunk)
                        await rbd_import_proc.stdin.drain()

                # the decompressor does not always return a decompressed chunk but might retain
                # and return empty. at the end we need to call flush to get everything out
                rbd_import_proc.stdin.write(decompressor.flush())
                await rbd_import_proc.stdin.drain()

                # close the proc stdin pipe, writer gets closed in finally
                rbd_import_proc.stdin.close()
                exit_code = await rbd_import_proc.wait()

                if exit_code != 0:
                    raise Exception(f"Rbd import failed with code {exit_code}")

                # restore from pickled pvc dicts
                new_pv_name = f"pvc-{uuid.uuid4()}"

                logger.debug(
                    f"restoring pv with new pv name {new_pv_name} and csi image name {new_csi_image_name}"
                )

                # create the new pvc based on the old - remove dynamic fields of old:
                if pvc_dict["metadata"]["name"] in existing_pvcs:
                    pvc_name = pvc_dict["metadata"]["name"]
                    pvc_dict["metadata"]["name"] = f"test-restore-{pvc_name}"
                    logger.info(
                        f"pvc {pvc_name} exists, creating it with test-restore- prefix"
                    )

                clean_pvc_dict(pvc_dict)

                # set new values
                pvc_dict["spec"]["storage_class_name"] = storage_class
                pvc_dict["metadata"]["namespace"] = restore_namespace

                # we can give it a customized pv name so we know migrated ones - will still behave like a normal created pv
                pvc_dict["spec"]["volume_name"] = new_pv_name

                # creation call
                logger.debug(f"creating new pvc:\n{pformat(pvc_dict)}")
                core_v1.create_namespaced_persistent_volume_claim(
                    namespace=restore_namespace,
                    body=client.V1PersistentVolumeClaim(
                        **convert_keys_to_camel_case(pvc_dict)
                    ),
                )

                clean_pv_dict(pv_dict)

                # get the storage class and set secrets from it
                ceph_storage_class = cluster_storage_classes[storage_class]
                pv_dict["metadata"]["annotations"][
                    "volume.kubernetes.io/provisioner-deletion-secret-name"
                ] = ceph_storage_class.parameters[
                    "csi.storage.k8s.io/provisioner-secret-name"
                ]
                pv_dict["metadata"]["annotations"][
                    "volume.kubernetes.io/provisioner-deletion-secret-namespace"
                ] = ceph_storage_class.parameters[
                    "csi.storage.k8s.io/provisioner-secret-namespace"
                ]

                pv_dict["spec"]["csi"]["node_stage_secret_ref"]["name"] = (
                    ceph_storage_class.parameters[
                        "csi.storage.k8s.io/node-stage-secret-name"
                    ]
                )
                pv_dict["spec"]["csi"]["node_stage_secret_ref"]["namespace"] = (
                    ceph_storage_class.parameters[
                        "csi.storage.k8s.io/node-stage-secret-namespace"
                    ]
                )

                pv_dict["spec"]["csi"]["controller_expand_secret_ref"]["name"] = (
                    ceph_storage_class.parameters[
                        "csi.storage.k8s.io/controller-expand-secret-name"
                    ]
                )
                pv_dict["spec"]["csi"]["controller_expand_secret_ref"]["namespace"] = (
                    ceph_storage_class.parameters[
                        "csi.storage.k8s.io/controller-expand-secret-namespace"
                    ]
                )

                pv_dict["spec"]["csi"]["volume_attributes"]["clusterID"] = ceph_cluster_id

                # reconstruction of volume handle that the ceph csi provisioner understands
                pool_id = format(pool_name_id[pool], "016x")
                trimmed_new_csi_image_name = new_csi_image_name.removeprefix("csi-vol-")
                pv_dict["spec"]["csi"][
                    "volumeHandle"
                ] = f"0001-0024-{ceph_cluster_id}-{pool_id}-{trimmed_new_csi_image_name}"

                pv_dict["spec"]["csi"]["volume_attributes"][
                    "imageName"
                ] = new_csi_image_name
                pv_dict["spec"]["csi"]["volume_attributes"]["journalPool"] = pool
                pv_dict["spec"]["csi"]["volume_attributes"]["pool"] = pool

                pv_dict["spec"]["storage_class_name"] = storage_class

                pv_dict["metadata"]["name"] = new_pv_name

                # creation call
                logger.debug(f"creating new pv:\n{pformat(pv_dict)}")
                core_v1.create_persistent_volume(
                    body=client.V1PersistentVolume(**convert_keys_to_camel_case(pv_dict))
                )

        # send the done signal to bdd server
        writer.write("##BRCTL-DONE\n".encode())
        await writer.drain()

        # close the writer here
        writer.close()
        await writer.wait_closed()

        # scale back up again
        if restore_args["auto_scale"]:
            # auto downscale deployments and statefulsets of namespace
            deployments = apps_v1.list_namespaced_deployment(restore_namespace)
            for d in deployments.items:
                name = d.metadata.name
                logger.info(f"Scaling Deployment '{name}' back up...")
                apps_v1.patch_namespaced_deployment_scale(
                    name=name,
                    namespace=restore_namespace,
                    body={"spec": {"replicas": auto_scale_replicas[f"dp-{name}"]}},
                )

            statefulsets = apps_v1.list_namespaced_stateful_set(restore_namespace)
            for s in statefulsets.items:
                name = s.metadata.name
                logger.info(f"Scaling StatefulSet '{name}' back up...")
                apps_v1.patch_namespaced_stateful_set_scale(
                    name=name,
                    namespace=restore_namespace,
                    body={"spec": {"replicas": auto_scale_replicas[f"ss-{name}"]}},
                )

        logger.info(
            f"restore of namespace {orig_namespace} into {restore_namespace} complete, you can now scale up your deployments again"
        )


def main():
    logger.info("running pxc restore job")
    asyncio.run(procedure())
