import argparse
import asyncio
import base64
import gzip
import json
import logging
import os
import pickle
import ssl
import struct

import socketio
import yaml
from kubernetes import client
from kubernetes.client import (ApiException, V1ConfigMapVolumeSource,
                               V1Container, V1EnvVar, V1Job, V1JobSpec,
                               V1ObjectMeta, V1PodSpec, V1PodTemplateSpec,
                               V1SecretVolumeSource, V1Volume, V1VolumeMount)
from kubernetes.config.kube_config import KubeConfigLoader
from pve_cloud.cli.pvclu import (get_cloud_domain, get_ssh_master_kubeconfig,
                                 get_ssh_remote_master_kubeconfig)
from pve_cloud.cli.pxrpc import launch_pxrpc
from pve_cloud.lib.backup_rpc import Command
from pve_cloud.lib.inventory import (get_cloud_domain, get_cluster_vars,
                                     get_online_pve_host,
                                     get_online_pve_host_from_target_pve,
                                     get_pve_inventory)
from pve_cloud.lib.ssh import connect_host
from pve_cloud_backup._version import __version__ as bkp_version

from pve_cloud_backup.daemon.funcs import get_backup_base_dir

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("brctl")


async def list_backup_details_remote(args):

    # get the connection to the clouds patroni database
    cloud_domain = get_cloud_domain(args.bdd_stack_fqdn)
    pve_inventory = get_pve_inventory(cloud_domain)

    # simply pick the first available cluster
    pve_cluster = next(iter(pve_inventory))
    pve_host, jump_host = get_online_pve_host(pve_inventory, pve_cluster)

    bdd_stack_name = args.bdd_stack_fqdn.removesuffix(f".{cloud_domain}")

    # launch our rpc service to read discovery secrets and prepare
    # acessing the backup server
    logger.info(f"connecting to {pve_host} via {jump_host}")
    metas = None
    stack_meta = None

    with launch_pxrpc(jump_host, pve_host) as (pxrpc, pve_host_conn):
        if args.use_mc_gw:
            # we will create a socket connection to the multi cloud gateway for that we need to fetch secrets
            ext_mc_raw = pxrpc.get_cloud_secret(cloud_domain, "external-mc-token")
            if not ext_mc_raw:
                raise RuntimeError(
                    f"No multi cloud services could be discovered for {pve_cluster} - {cloud_domain}!"
                )

            ext_mc = json.loads(ext_mc_raw)
            logger.info(ext_mc)

            # we connect via socketio to the gateway
            sio = socketio.Client(logger=True, engineio_logger=True)

            sio.connect(
                f"https://{ext_mc['mc_gw_host']}",
                auth={"token": ext_mc["token"], "bdd_stack_name": bdd_stack_name},
                transports=["websocket"],
            )
            result = sio.call("list_backup_details", args.timestamp, timeout=30)

            sio.disconnect()

            metas = result["metas"]
            stack_meta = result["stack_meta"]
        else:
            # we will connect directly to the backup server
            # todo: here we can also pass the correct tls config

            tls_disc_raw = pxrpc.get_cloud_secret(
                cloud_domain, f"{bdd_stack_name}-bdd-tls-discovery"
            )
            if not tls_disc_raw:
                raise RuntimeError(
                    "Could not find discovery secret for the provided bdd stack name!"
                )

            tls_disc = json.loads(tls_disc_raw)

            # cli trusts the server without verifying
            # fetching ca is too inconvinient
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            reader, writer = await asyncio.open_connection(
                tls_disc["server_int_ip"], 8085, ssl=ssl_ctx
            )
            writer.write(struct.pack("B", Command.LIST_BACKUP_DETAILS.value))
            await writer.drain()

            # send the timestamp string
            writer.write((args.timestamp + "\n").encode())
            await writer.drain()

            # read the archives
            dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
            metas = pickle.loads((await reader.readexactly(dict_size)))

            # first we group metas
            k8s_stack = metas[0]["stack"]

            print(f"k8s stack {k8s_stack}:")

            # query the server for backup secrets
            writer.write((k8s_stack + "\n").encode())
            await writer.drain()

            # read the the meta information
            dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
            stack_meta = pickle.loads((await reader.readexactly(dict_size)))

            # send a terminator
            # todo: probably not needed anymore
            writer.write("##BRCTL-DONE\n".encode())
            await writer.drain()

        namespace_secret_dict = pickle.loads(
            base64.b64decode(stack_meta["namespace_secret_dict_b64"])
        )

        namespace_k8s_metas = {}

        # group metas by namespace
        for meta in metas:
            if meta["namespace"] not in namespace_k8s_metas:
                namespace_k8s_metas[meta["namespace"]] = []

            namespace_k8s_metas[meta["namespace"]].append(meta)

        for namespace, k8s_metas in namespace_k8s_metas.items():
            print(f"- namespace {namespace}:")
            print(f"  - volumes:")
            for meta in k8s_metas:
                pvc_name = meta["pvc_name"]
                pool = meta["pool"]
                storage_class = meta["storage_class"]
                print(f"    - {pvc_name}, pool {pool}, storage class {storage_class}")

            helm_releases = {}

            print(f"  - secrets:")
            for secret in namespace_secret_dict[namespace]:
                secret_name = secret["metadata"]["name"]

                if secret_name.startswith("sh.helm.release.v1."):
                    release_split = secret_name.removeprefix(
                        "sh.helm.release.v1."
                    ).split(".")
                    release_name = release_split[0]
                    release_num = int(release_split[1].removeprefix("v"))
                    # collect the latest helm release
                    if (
                        not release_name in helm_releases
                        or int(
                            helm_releases[release_name]["metadata"][
                                "name"
                            ].removeprefix(f"sh.helm.release.v1.{release_name}.v")
                        )
                        < release_num
                    ):
                        helm_releases[release_name] = secret
                else:
                    print(f"    - {secret_name}")  # print non helm secrets

            if helm_releases:
                print("  - helm releases:")
                for release_name, release_secret in helm_releases.items():
                    release_info = json.loads(
                        gzip.decompress(
                            base64.b64decode(
                                base64.b64decode(release_secret["data"]["release"])
                            )
                        )
                    )
                    print(
                        f"    - {release_info['chart']['metadata']['name']} - version: {release_info['chart']['metadata']['version']}"
                    )


async def list_backups_remote(args):

    # get the connection to the clouds patroni database
    cloud_domain = get_cloud_domain(args.bdd_stack_fqdn)
    pve_inventory = get_pve_inventory(cloud_domain)

    # simply pick the first available cluster
    pve_cluster = next(iter(pve_inventory))
    pve_host, jump_host = get_online_pve_host(pve_inventory, pve_cluster)

    bdd_stack_name = args.bdd_stack_fqdn.removesuffix(f".{cloud_domain}")

    # launch our rpc service to read discovery secrets and prepare
    # acessing the backup server
    logger.info(f"connecting to {pve_host} via {jump_host}")
    archives = None

    with launch_pxrpc(jump_host, pve_host) as (pxrpc, pve_host_conn):
        if args.use_mc_gw:
            ext_mc_raw = pxrpc.get_cloud_secret(cloud_domain, "external-mc-token")
            if not ext_mc_raw:
                raise RuntimeError(
                    f"No multi cloud services could be discovered for {pve_cluster} - {cloud_domain}!"
                )

            ext_mc = json.loads(ext_mc_raw)
            logger.info(ext_mc)

            # we connect via socketio to the gateway
            sio = socketio.Client(logger=True, engineio_logger=True)

            sio.connect(
                f"https://{ext_mc['mc_gw_host']}",
                auth={"token": ext_mc["token"], "bdd_stack_name": bdd_stack_name},
                transports=["websocket"],
            )
            result = sio.call("list_backups", timeout=30)

            sio.disconnect()

            archives = result["archives"]

        else:
            tls_disc_raw = pxrpc.get_cloud_secret(
                cloud_domain, f"{bdd_stack_name}-bdd-tls-discovery"
            )
            if not tls_disc_raw:
                raise RuntimeError(
                    "Could not find discovery secret for the provided bdd stack name!"
                )

            tls_disc = json.loads(tls_disc_raw)

            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            reader, writer = await asyncio.open_connection(
                tls_disc["server_int_ip"], 8085, ssl=ssl_ctx
            )
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
    serializable_args = vars(args).copy()
    serializable_args["func"] = args.func.__name__

    with open(args.inventory, "r") as file:
        raw_pxc_inv = yaml.safe_load(file)

    if "plugin" not in raw_pxc_inv or raw_pxc_inv["plugin"] not in [
        "pxc.cloud.kubespray_inv",
        "pxc.cloud.ext_hosts_inv",
    ]:
        raise ValueError("Pxc incompatible inventory passed!")

    # todo: again this could be solved with a better generic schema handeling
    if raw_pxc_inv["plugin"] == "pxc.cloud.ext_hosts_inv":
        # validate that k0s_single host is there
        if (
            not "typed_host_groups" in raw_pxc_inv
            and not "k0s_edge" in raw_pxc_inv["typed_host_groups"]
        ):
            raise ValueError(
                "Unsuitable ext_hosts_inv passed! Needs k0s_edge typed host group."
            )

    kubeconfig_dict = None

    if raw_pxc_inv["plugin"] == "pxc.cloud.kubespray_inv":
        kubespray_inv = raw_pxc_inv

        # fetch the kubeconfig of the cluster we want to launch the restore job in
        online_pve_host, jump_host = get_online_pve_host_from_target_pve(
            kubespray_inv["target_pve"]
        )

        external_cp_defined = (
            "extra_control_plane_sans" in kubespray_inv
            and kubespray_inv["extra_control_plane_sans"]
        )

        if jump_host and not external_cp_defined:
            raise NotImplementedError(
                "Jump host functionality requires external san to be set for the kubernetes cluster!"
            )

        if jump_host:
            kubeconfig_dict = yaml.safe_load(
                get_ssh_remote_master_kubeconfig(
                    kubespray_inv["stack_name"],
                    kubespray_inv["extra_control_plane_sans"][0],
                    jump_host,
                    online_pve_host,
                )
            )
        else:
            cluster_vars = get_cluster_vars(online_pve_host)

            kubeconfig_dict = yaml.safe_load(
                get_ssh_master_kubeconfig(cluster_vars, kubespray_inv["stack_name"])
            )

        # todo: maybe parameterize?
        serializable_args["node_user"] = "admin"  # default for kubespray
        serializable_args["node_key_path"] = "/opt/id_qemu"

    elif raw_pxc_inv["plugin"] == "pxc.cloud.ext_hosts_inv":

        k0s_single = raw_pxc_inv["typed_host_groups"]["k0s_edge"]["k0s_single"]

        with connect_host(
            k0s_single["ansible_host"], user=k0s_single["ansible_user"]
        ) as ssh:
            _, stdout, _ = ssh.exec_command("sudo k0s kubeconfig admin")

            kubeconfig_dict = yaml.safe_load(stdout.read().decode("utf-8"))

        serializable_args["node_user"] = k0s_single[
            "ansible_user"
        ]  # works with passwordless sudo
        serializable_args["node_key_path"] = "/opt/id_ext"

    # next we prepare connection credentials the restore job will use to either
    # connect to the backup server directly or through our multicloud gateway
    cloud_domain = get_cloud_domain(args.bdd_stack_fqdn)
    pve_inventory = get_pve_inventory(cloud_domain)

    # simply pick the first available cluster
    pve_cluster = next(iter(pve_inventory))
    pve_host, jump_host = get_online_pve_host(pve_inventory, pve_cluster)

    bdd_stack_name = args.bdd_stack_fqdn.removesuffix(f".{cloud_domain}")

    # launch our rpc service to read discovery secrets and prepare
    # acessing the backup server
    logger.info(f"connecting to {pve_host} via {jump_host}")

    with launch_pxrpc(jump_host, pve_host) as (pxrpc, pve_host_conn):
        if args.use_mc_gw:
            ext_mc_raw = pxrpc.get_cloud_secret(cloud_domain, "external-mc-token")
            if not ext_mc_raw:
                raise RuntimeError(
                    f"No multi cloud services could be discovered for {pve_cluster} - {cloud_domain}!"
                )

            ext_mc = json.loads(ext_mc_raw)
            logger.info(ext_mc)

            serializable_args["mc_ext_token"] = ext_mc["token"]
            serializable_args["mc_gw_host"] = ext_mc["mc_gw_host"]
            serializable_args["bdd_stack_name"] = bdd_stack_name

        else:
            tls_disc_raw = pxrpc.get_cloud_secret(
                cloud_domain, f"{bdd_stack_name}-bdd-tls-discovery"
            )
            if not tls_disc_raw:
                raise RuntimeError(
                    "Could not find discovery secret for the provided bdd stack name!"
                )

            tls_disc = json.loads(tls_disc_raw)

            serializable_args["bdd_host"] = tls_disc["server_int_ip"]

    # init kube client for launching the restore job
    loader = KubeConfigLoader(config_dict=kubeconfig_dict)
    configuration = client.Configuration()
    loader.load_and_set(configuration)

    api_instance = client.ApiClient(configuration)
    core_v1 = client.CoreV1Api(api_instance)
    batch_v1 = client.BatchV1Api(api_instance)

    # check if ceph secrets exist
    ceph_secrets_available = True
    try:
        core_v1.read_namespaced_secret("ceph-secrets", namespace="pve-cloud-backup")
    except ApiException as e:
        if e.status == 404:
            ceph_secrets_available = False
        else:
            raise

    # env vars hold secrets for the job to run and auth
    env_vars = [
        V1EnvVar(
            name="PXC_RESTORE_ARGS",
            value=base64.b64encode(
                json.dumps(
                    serializable_args
                    # | {
                    #     "cloud_domain": cloud_domain,
                    #     "stack_name": kubespray_inv["stack_name"],
                    # }
                ).encode()
            ).decode(),
        ),
        V1EnvVar(name="LOG_LEVEL", value=args.log_level),
    ]

    volume_mounts = []
    if raw_pxc_inv["plugin"] == "pxc.cloud.kubespray_inv":
        volume_mounts.append(
            V1VolumeMount(
                name="fetcher-secrets", mount_path="/opt/id_qemu", sub_path="qemu-id"
            ),
        )

    elif raw_pxc_inv["plugin"] == "pxc.cloud.ext_hosts_inv":
        volume_mounts.append(
            V1VolumeMount(
                name="fetcher-secrets", mount_path="/opt/id_ext", sub_path="ext-id"
            ),
        )

    if ceph_secrets_available:
        volume_mounts.extend(
            [
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
            ]
        )

    container = V1Container(
        name="pxc-restore",
        image=(
            args.image if args.image else f"tobiashvmz/pve-cloud-backup:{bkp_version}"
        ),  # args.image gets injected by e2e tests
        args=["pxc-restore"],  # launch the job with our cli args as parameter
        env=env_vars,
        volume_mounts=volume_mounts,
    )

    volumes = [
        V1Volume(
            name="fetcher-secrets",
            secret=V1SecretVolumeSource(secret_name="fetcher-secrets"),
        ),
    ]

    if ceph_secrets_available:
        volumes.extend(
            [
                V1Volume(
                    name="ceph-config",
                    config_map=V1ConfigMapVolumeSource(name="ceph-config"),
                ),
                V1Volume(
                    name="ceph-secrets",
                    secret=V1SecretVolumeSource(secret_name="ceph-secrets"),
                ),
            ]
        )

    # todo: conditionally load ceph config and check restore type, zfs restores should work without
    # and only need ssh key / host info
    template = V1PodTemplateSpec(
        metadata=V1ObjectMeta(labels={"job": f"pxc-restore-{args.timestamp}"}),
        spec=V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=volumes,  # todo: should be more specific
        ),
    )

    job_spec = V1JobSpec(template=template, backoff_limit=0)

    job = V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=f"pxc-restore-job-{args.timestamp.replace('_', '-')}"
        ),
        spec=job_spec,
    )

    # Launch the Job in the default namespace
    resp = batch_v1.create_namespaced_job(body=job, namespace="pve-cloud-backup")

    logger.info("Job created. Status='%s'" % str(resp.status))


async def print_backup_base_dir(args):
    print(get_backup_base_dir(), end="")


def get_parser():
    parser = argparse.ArgumentParser(description="CLI for restoring backups.")

    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        "--bdd-stack-fqdn",
        type=str,
        help="Stack name + pve cloud domain of the backup server in the target cloud from the k8s inventory. Needed for all operations. You need to be connected to the cloud using pvcli connect commands.",
        required=True,
    )
    base_parser.add_argument(
        "--inventory",
        type=str,
        help="PVE cloud kubespray inventory yaml file or pxc external hosts k0s conform inventory file, in this cluster the restore job will be launched.",
        # required=True,
    )
    base_parser.add_argument(
        "--use-mc-gw",
        action="store_true",
        help="Configures the backup job with the clouds external gateway instead of the internal bdd servers ip.",
    )
    # todo: implement bdd-host-address

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
        help="Restore pxc k8s csi backups (zfs/ceph origin) into ceph storage class backed pvcs. If pvcs with same name exist, test-restore will be appended to pvc name.",
        parents=[base_parser],
    )
    k8s_restore_parser.add_argument(
        "--timestamp",
        type=str,
        help="Timestamp of the backup to restore.",
        required=True,
    )
    k8s_restore_parser.add_argument(
        "--namespaces",
        type=str,
        default="",
        help="Specific namespaces to restore, CSV, acts as a filter. Use with --sc-mapping for controlled migration of pvcs.",
    )
    k8s_restore_parser.add_argument(
        "--sc-mapping",
        action="append",
        help='Map a storage classe in the backup to one in the target cluster, for example "csi-rbd-sc-ssd:openebs-zfspv-zvol". Can be provided multiple times.',
    )
    k8s_restore_parser.add_argument(
        "--namespace-mapping",
        action="append",
        help="Namespace that should be restored into a new namespace names old-namespace:new-namespace. Can also be provided multiple times.",
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

    k8s_restore_parser.add_argument("--log-level", default="INFO")

    k8s_restore_parser.set_defaults(func=launch_restore_job)

    base_dir_parser = subparsers.add_parser(
        "get-base-dir",
        help="Returns the base dir assuming the correct env variables are set (needed for cron cleanup script).",
    )
    base_dir_parser.set_defaults(func=print_backup_base_dir)

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
