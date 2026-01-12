import argparse
import asyncio
import base64
import json
import logging
import os
import pickle
import struct

import yaml
from kubernetes import client
from kubernetes.client import (V1ConfigMapVolumeSource, V1Container, V1EnvVar,
                               V1Job, V1JobSpec, V1ObjectMeta, V1PodSpec,
                               V1PodTemplateSpec, V1SecretVolumeSource,
                               V1Volume, V1VolumeMount)
from kubernetes.config.kube_config import KubeConfigLoader
from pve_cloud.cli.pvclu import (get_cloud_domain, get_cluster_vars,
                                 get_ssh_master_kubeconfig)
from pve_cloud.lib.inventory import get_online_pve_host
from pve_cloud_backup._version import __version__ as bkp_version

from pve_cloud_backup.daemon.rpc import Command

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("brctl")


async def list_backup_details_remote(args):
    reader, writer = await asyncio.open_connection(args.bdd_host, 8085)
    writer.write(struct.pack("B", Command.LIST_BACKUP_DETAILS.value))
    await writer.drain()

    # send the timestamp string
    writer.write((args.timestamp + "\n").encode())
    await writer.drain()

    # read the archives
    dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
    metas = pickle.loads((await reader.readexactly(dict_size)))

    # first we group metas
    k8s_stacks = {}

    for meta in metas:
        if meta["type"] == "k8s":
            if meta["stack"] not in k8s_stacks:
                k8s_stacks[meta["stack"]] = []

            k8s_stacks[meta["stack"]].append(meta)
        else:
            raise Exception(f"Invalid meta type found - meta {meta}")

    for k8s_stack, k8s_metas in k8s_stacks.items():
        print(f"  - k8s stack {k8s_stack}:")

        # query the server for backup secrets
        writer.write((k8s_stack + "\n").encode())
        await writer.drain()

        # read the the meta information
        dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
        stack_meta = pickle.loads((await reader.readexactly(dict_size)))

        namespace_secret_dict = pickle.loads(
            base64.b64decode(stack_meta["namespace_secret_dict_b64"])
        )

        namespace_k8s_metas = {}

        # group metas by namespace
        for meta in k8s_metas:
            if meta["namespace"] not in namespace_k8s_metas:
                namespace_k8s_metas[meta["namespace"]] = []

            namespace_k8s_metas[meta["namespace"]].append(meta)

        for namespace, k8s_metas in namespace_k8s_metas.items():
            print(f"    - namespace {namespace}:")
            print(f"      - volumes:")
            for meta in k8s_metas:
                pvc_name = meta["pvc_name"]
                pool = meta["pool"]
                storage_class = meta["storage_class"]
                print(
                    f"        - {pvc_name}, pool {pool}, storage class {storage_class}"
                )

            print(f"      - secrets:")
            for secret in namespace_secret_dict[namespace]:
                secret_name = secret["metadata"]["name"]
                print(f"        - {secret_name}")

    # send a terminator
    writer.write("##BRCTL-DONE\n".encode())
    await writer.drain()


async def list_backups_remote(args):
    reader, writer = await asyncio.open_connection(args.bdd_host, 8085)
    writer.write(struct.pack("B", Command.LIST_BACKUPS.value))
    await writer.drain()

    # read the response archives size and then the archives
    dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
    archives = pickle.loads((await reader.readexactly(dict_size)))

    if args.json:
        print(json.dumps(sorted(archives)))
        return

    print("available backup timestamps (ids):")

    for timestamp in sorted(archives):
        print(f"- timestamp {timestamp}")


async def launch_restore_job(args):
    # fetch the kubeconfig of the cluster we want to launch the restore job in
    online_pve_host = get_online_pve_host(args.target_pve)
    cluster_vars = get_cluster_vars(online_pve_host)
    cloud_domain = get_cloud_domain(args.target_pve)

    kubeconfig_dict = yaml.safe_load(
        get_ssh_master_kubeconfig(cluster_vars, args.stack_name)
    )

    print(kubeconfig_dict)

    # init kube client for launching the restore job
    loader = KubeConfigLoader(config_dict=kubeconfig_dict)
    configuration = client.Configuration()
    loader.load_and_set(configuration)

    api_instance = client.ApiClient(configuration)
    batch_v1 = client.BatchV1Api(api_instance)

    serializable_args = vars(args).copy()
    serializable_args["func"] = args.func.__name__

    # env vars hold secrets for the job to run and auth
    env_vars = [
        V1EnvVar(
            name="PXC_RESTORE_ARGS",
            value=base64.b64encode(
                json.dumps(serializable_args | {"cloud_domain": cloud_domain}).encode()
            ).decode(),
        ),
    ]

    container = V1Container(
        name="pxc-restore",
        image=(
            args.image if args.image else f"tobiashvmz/pve-cloud-backup:{bkp_version}"
        ),  # args.image gets injected by e2e tests
        args=["pxc-restore"],  # launch the job with our cli args as parameter
        env=env_vars,
        volume_mounts=[
            V1VolumeMount(
                name="ceph-config",
                mount_path="/etc/ceph/ceph.conf",
                sub_path="ceph.conf",
            ),
            V1VolumeMount(
                name="ceph-secrets",
                mount_path="/etc/pve/priv/ceph.client.admin.keyring",
                sub_path="ceph-admin-keyring",
            ),
        ],
    )

    template = V1PodTemplateSpec(
        metadata=V1ObjectMeta(labels={"job": f"pxc-restore-{args.timestamp}"}),
        spec=V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=[
                V1Volume(
                    name="ceph-config",
                    config_map=V1ConfigMapVolumeSource(name="ceph-config"),
                ),
                V1Volume(
                    name="ceph-secrets",
                    secret=V1SecretVolumeSource(secret_name="ceph-secrets"),
                ),
            ],
        ),
    )

    job_spec = V1JobSpec(template=template, backoff_limit=0)

    job = V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=f"pxc-restore-job-{args.timestamp.replace("_", "-")}"
        ),
        spec=job_spec,
    )

    # Launch the Job in the default namespace
    resp = batch_v1.create_namespaced_job(body=job, namespace="pve-cloud-backup")

    logger.info("Job created. Status='%s'" % str(resp.status))


def get_parser():
    parser = argparse.ArgumentParser(description="CLI for restoring backups.")

    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        "--bdd-host",
        type=str,
        help="The target bdd server that hosts our backups. Needed for all operations.",
        required=True,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-backups", help="List available backups.", parents=[base_parser]
    )
    list_parser.add_argument(
        "--json", action="store_true", help="Outputs the available timestamps as json."
    )
    list_parser.set_defaults(func=list_backups_remote)

    list_detail_parser = subparsers.add_parser(
        "backup-details", help="List details of a backup.", parents=[base_parser]
    )
    list_detail_parser.add_argument(
        "--timestamp",
        type=str,
        help="Timestamp of the backup to list details of.",
        required=True,
    )
    list_detail_parser.set_defaults(func=list_backup_details_remote)

    k8s_restore_parser = subparsers.add_parser(
        "restore-k8s",
        help="Restore k8s csi backups. If pvcs with same name exist, test-restore will be appended to pvc name.",
        parents=[base_parser],
    )
    k8s_restore_parser.add_argument(
        "--timestamp",
        type=str,
        help="Timestamp of the backup to restore.",
        required=True,
    )
    k8s_restore_parser.add_argument(
        "--stack-name",
        type=str,
        help="Proxmox cloud k8s stack name where restore job will be launched. Requires pve-cloud-backup namespace installed via terraform-pxc-backup.",
        required=True,
    )
    k8s_restore_parser.add_argument(
        "--target-pve",
        type=str,
        help="Proxmox cluster where the k8s cluster we want to restore in is running.",
        required=True,
    )
    k8s_restore_parser.add_argument(
        "--namespaces",
        type=str,
        default="",
        help="Specific namespaces to restore, CSV, acts as a filter. Use with --pool-mapping for controlled migration of pvcs.",
    )
    k8s_restore_parser.add_argument(
        "--pool-sc-mapping",
        action="append",
        help="Define pool storage class mappings (old to new), for example old-pool:new-pool/new-storage-class-name.",
    )
    k8s_restore_parser.add_argument(
        "--namespace-mapping",
        action="append",
        help="Namespaces that should be restored into new namespace names old-namespace:new-namespace.",
    )
    k8s_restore_parser.add_argument(
        "--auto-scale",
        action="store_true",
        help="When passed deployments and stateful sets will automatically get scaled down and back up again for restore.",
    )
    k8s_restore_parser.add_argument(
        "--auto-delete",
        action="store_true",
        help="When passed existing pvcs in namespace will automatically get deleted before restoring.",
    )
    k8s_restore_parser.add_argument(
        "--secret-pattern",
        action="append",
        help="Define as many times as you need, for example namespace/deployment* (glob style). Will overwrite secret data of matching existing.",
    )
    k8s_restore_parser.add_argument(
        "--image",
        type=str,
        help="Custom image for launching restore job (e2e test arg).",
    )
    k8s_restore_parser.set_defaults(func=launch_restore_job)

    return parser


# purpose of these tools is disaster recovery into an identical pve + ceph system
# assumes to be run on a pve system, but can be passed pve host and path to ssh key aswell
def main():
    args = get_parser().parse_args()
    asyncio.run(
        args.func(args)
    )  # all funcs are async since we only communicate with bdd


if __name__ == "__main__":
    main()
