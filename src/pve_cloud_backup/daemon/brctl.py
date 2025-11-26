import argparse
import logging
from kubernetes import client
from kubernetes.config.kube_config import KubeConfigLoader
import pve_cloud_backup.daemon.shared as shared
from proxmoxer import ProxmoxAPI
from tinydb import TinyDB, Query
from pprint import pformat
import yaml
import pickle
import base64
import os
import json


log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("brctl")


def list_backup_details(args):  
  print(f"listing details for {args.timestamp}")
  timestamp_archives = shared.get_image_metas(args, args.timestamp)

  metas = timestamp_archives[args.timestamp]

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

    # get stack meta and decode stack namespace secrets
    stack_meta_db = TinyDB(f"{args.backup_path}stack-meta-db.json")
    Meta = Query()

    stack_meta = stack_meta_db.get((Meta.timestamp == args.timestamp) & (Meta.stack == k8s_stack) & (Meta.type == "k8s"))
    
    namespace_secret_dict = pickle.loads(base64.b64decode(stack_meta["namespace_secret_dict_b64"]))

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
        print(f"        - {pvc_name}, pool {pool}, storage class {storage_class}")
      
      print(f"      - secrets:")
      for secret in namespace_secret_dict[namespace]:
        secret_name = secret["metadata"]["name"]
        print(f"        - {secret_name}")


def list_backups(args):
  timestamp_archives = shared.get_image_metas(args)

  if args.json:
    print(json.dumps(sorted(timestamp_archives)))
    return

  print("available backup timestamps (ids):")

  for timestamp in sorted(timestamp_archives):
    print(f"- timestamp {timestamp}")

  
# this assumes you first restored the virtual machines
# and extracted a fitting kubeconfig passing it via --kubeconfig
def restore_k8s(args):
  print(f"restoring {args.timestamp}")

  metas = shared.get_image_metas(args, args.timestamp)[args.timestamp]

  metas_grouped = shared.group_image_metas(metas, ["k8s"], "namespace", args.k8s_stack_name)

  stack_meta_db = TinyDB(f"{args.backup_path}stack-meta-db.json")
  Meta = Query()

  stack_meta = stack_meta_db.get((Meta.timestamp == args.timestamp) & (Meta.stack == args.k8s_stack_name) & (Meta.type == "k8s"))
  logger.debug(f"stack meta {stack_meta}")
  namespace_secret_dict = pickle.loads(base64.b64decode(stack_meta["namespace_secret_dict_b64"]))

  # user can manually specify it
  if args.kubeconfig_new:
    with open(args.kubeconfig_new, "r") as file:
      kubeconfig_dict = yaml.safe_load(file)
  else:
    # restore into original k8s cluster
    master_ipv4 = stack_meta["master_ip"]
    kubeconfig_dict = yaml.safe_load(stack_meta["raw_kubeconfig"])

    # override the connection ip as it is set to localhost on the machines
    kubeconfig_dict["clusters"][0]["cluster"]["server"] = f"https://{master_ipv4}:6443"

  logger.debug(f"kubeconfig dict {pformat(kubeconfig_dict)}")

  # init kube client
  loader = KubeConfigLoader(config_dict=kubeconfig_dict)
  configuration = client.Configuration()
  loader.load_and_set(configuration)

  # Create a client from this configuration
  api_client = client.ApiClient(configuration)

  # run the restore
  shared.restore_pvcs(metas_grouped, namespace_secret_dict, args, api_client)


# dynamic backup path function for the --backup-path argument
def backup_path(value):
  if value == "":
    return ""
  
  if value.endswith("/"):
    return value
  else:
    return value + "/"


# purpose of these tools is disaster recovery into an identical pve + ceph system
# assumes to be run on a pve system, but can be passed pve host and path to ssh key aswell
def main():
  parser = argparse.ArgumentParser(description="CLI for restoring backups.")

  base_parser = argparse.ArgumentParser(add_help=False)
  base_parser.add_argument("--backup-path", type=backup_path, default=".", help="Path of the mounted backup drive/dir.")
  base_parser.add_argument("--proxmox-host", type=str, help="Proxmox host, if not run directly on a pve node.")
  base_parser.add_argument("--proxmox-private-key", type=str, help="Path to pve root private key, for connecting to remote pve.")

  subparsers = parser.add_subparsers(dest="command", required=True)

  list_parser = subparsers.add_parser("list-backups", help="List available backups.", parents=[base_parser])
  list_parser.add_argument("--json", action="store_true", help="Outputs the available timestamps as json.")
  list_parser.set_defaults(func=list_backups)

  list_detail_parser = subparsers.add_parser("backup-details", help="List details of a backup.", parents=[base_parser])
  list_detail_parser.add_argument("--timestamp", type=str, help="Timestamp of the backup to list details of.", required=True)
  list_detail_parser.set_defaults(func=list_backup_details)

  k8s_restore_parser = subparsers.add_parser("restore-k8s", help="Restore k8s csi backups. If pvcs with same name exist, test-restore will be appended to pvc name.", parents=[base_parser])
  k8s_restore_parser.add_argument("--timestamp", type=str, help="Timestamp of the backup to restore.", required=True)
  k8s_restore_parser.add_argument("--k8s-stack-name", type=str, help="Stack name of k8s stack that will be restored into.", required=True)
  k8s_restore_parser.add_argument("--kubeconfig-new", type=str, help="Optional kubeconfig for new cluster restores.")
  k8s_restore_parser.add_argument("--namespaces", type=str, default="", help="Specific namespaces to restore, CSV, acts as a filter. Use with --pool-mapping for controlled migration of pvcs.")
  k8s_restore_parser.add_argument("--pool-sc-mapping", action="append", help="Define pool storage class mappings (old to new), for example old-pool:new-pool/new-storage-class-name.")
  k8s_restore_parser.add_argument("--namespace-mapping", action="append", help="Namespaces that should be restored into new namespace names old-namespace:new-namespace.")
  k8s_restore_parser.add_argument("--auto-scale", action="store_true", help="When passed deployments and stateful sets will automatically get scaled down and back up again for restore.")
  k8s_restore_parser.add_argument("--auto-delete", action="store_true", help="When passed existing pvcs in namespace will automatically get deleted before restoring.")
  k8s_restore_parser.add_argument("--secret-pattern", action="append", help="Define as many times as you need, for example namespace/deployment* (glob style). Will overwrite secret data of matching existing.")
  k8s_restore_parser.set_defaults(func=restore_k8s)

  args = parser.parse_args()
  args.func(args)


if __name__ == "__main__":
  main()
