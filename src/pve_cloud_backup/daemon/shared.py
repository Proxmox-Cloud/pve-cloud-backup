import logging
import subprocess
import time
import os
import shutil
from tinydb import TinyDB, Query
import json
import paramiko
import base64
import re
import pickle
import base64
import uuid
from kubernetes import client
from kubernetes.client.rest import ApiException
from pprint import pformat
import fnmatch


logger = logging.getLogger("bdd")

os.environ["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "yes" # we need this to stop borg cli from manual prompting
os.environ["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"

ENV = os.getenv("ENV", "TESTING")

# constants
BACKUP_BASE_DIR = os.getenv("BACKUP_BASE_DIR", "/tmp/pve-cloud-test-backup")

IMAGE_META_DB_PATH = f"{BACKUP_BASE_DIR}/image-meta-db.json"
STACK_META_DB_PATH = f"{BACKUP_BASE_DIR}/stack-meta-db.json"

def group_image_metas(metas, type_keys, group_key, stack_filter=None):
  metas_grouped = {}

  # group metas by vmid
  for meta in metas:
    logger.debug(f"meta {meta}")

    # kind of redundant right now since we only process k8s
    if not meta["type"] in type_keys:
      continue # skip non fitting

    if stack_filter and meta["stack"] != stack_filter:
      continue # skip filtered out stack

    if meta[group_key] not in metas_grouped:
      metas_grouped[meta[group_key]] = []

    metas_grouped[meta[group_key]].append(meta)

  return metas_grouped


# these functions are necessary to convert python k8s naming to camel case
def to_camel_case(snake_str):
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])


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


def restore_pvcs(metas_grouped, namespace_secret_dict, args, api_client):
  core_v1 = client.CoreV1Api(api_client=api_client)
  apps_v1 = client.AppsV1Api(api_client=api_client)
  storage_v1 = client.StorageV1Api(api_client=api_client)

  # get ceph storage classes
  ceph_storage_classes = {sc.metadata.name: sc for sc in storage_v1.list_storage_class().items if sc.provisioner == 'rbd.csi.ceph.com'}

  # load existing ceph pools and fetch their ids, needed for later pv restoring
  ls_call = subprocess.run(["ceph", "osd", "pool", "ls", "detail", "-f", "json"], check=True, text=True, capture_output=True)
  pool_details = json.loads(ls_call.stdout)  # load existing ceph pools and fetch their ids, needed for later pv restoring

  pool_name_id = {}
  for pool_detail in pool_details:
    pool_name_id[pool_detail["pool_name"]] = pool_detail["pool_id"]

  # get the cluster id from ceph ns
  ceph_csi_config = core_v1.read_namespaced_config_map(name="ceph-csi-config", namespace="ceph-csi")

  if not ceph_csi_config:
    raise Exception("Could not find ceph-csi-config config map in ceph-csi namespace")

  ceph_cluster_id = json.loads(ceph_csi_config.data.get("config.json"))[0]["clusterID"]

  filter_namespaces = [] if args.namespaces == "" else args.namespaces.split(",")

  for orig_namespace, metas_group in metas_grouped.items():
    if filter_namespaces and namespace not in filter_namespaces:
      continue # skip filtered out namespaces

    restore_namespace = orig_namespace

    for namespace_mapping in args.namespace_mapping:
      if namespace_mapping.startswith(orig_namespace):
        restore_namespace = namespace_mapping.split(":")[1]
        logger.info(f"namespace mapping matched {namespace_mapping}")

    logger.info(f"trying to restore volumes of {orig_namespace} into {restore_namespace}")

    auto_scale_replicas = {}
    if args.auto_scale:
      # auto downscale deployments and statefulsets of namespace
      deployments = apps_v1.list_namespaced_deployment(restore_namespace)
      for d in deployments.items:
        name = d.metadata.name
        auto_scale_replicas[f"dp-{name}"] = d.spec.replicas # save original replicas for upscale later
        logger.info(f"Scaling Deployment '{name}' to 0 replicas...")
        apps_v1.patch_namespaced_deployment_scale(
          name=name,
          namespace=restore_namespace,
          body={"spec": {"replicas": 0}}
        )

      statefulsets = apps_v1.list_namespaced_stateful_set(restore_namespace)
      for s in statefulsets.items:
        name = s.metadata.name
        auto_scale_replicas[f"ss-{name}"] = s.spec.replicas
        logger.info(f"Scaling StatefulSet '{name}' to 0 replicas...")
        apps_v1.patch_namespaced_stateful_set_scale(
          name=name,
          namespace=restore_namespace,
          body={"spec": {"replicas": 0}}
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

    existing_pvcs = set(pvc.metadata.name for pvc in core_v1.list_namespaced_persistent_volume_claim(restore_namespace).items)
    logger.debug(f"existing pvcs {existing_pvcs}")

    # if any pending / running pods exist fail
    pod_phases = [pod for pod in pods.items if pod.status.phase != "Succeeded"]
    if pod_phases:
      raise Exception(f"found pods in {restore_namespace} - {pod_phases} - scale down all and force delete!")

    # process secret overwrites
    if args.secret_pattern:
      
      namespace_secrets = {secret["metadata"]["name"]: secret for secret in namespace_secret_dict[orig_namespace]}

      for secret_pattern in args.secret_pattern:
        if secret_pattern.split("/")[0] == restore_namespace:
          # arg that is meant for this namespace restore
          pattern = secret_pattern.split("/")[1]

          for secret in namespace_secrets:
            if fnmatch.fnmatch(secret, pattern):
              logger.info(f"overwrite pattern matched {pattern}, trying to patch {secret}")
              try:
                core_v1.patch_namespaced_secret(name=secret, namespace=restore_namespace, body={"data": namespace_secrets[secret]["data"]})
              except ApiException as e:
                # if it doesnt exist we simply create it
                if e.status == 404:
                  core_v1.create_namespaced_secret(
                      namespace=restore_namespace,
                      body={"metadata": {"name": secret}, "data": namespace_secrets[secret]["data"]}
                  )
                  logger.info(f"secret {secret} did not exist, created it instead!")
                else:
                    raise

    if args.auto_delete:
      pvcs = core_v1.list_namespaced_persistent_volume_claim(restore_namespace)
      for pvc in pvcs.items:
        name = pvc.metadata.name
        logger.info(f"Deleting PVC: {name}")
        core_v1.delete_namespaced_persistent_volume_claim(
          name=name,
          namespace=restore_namespace,
          body=client.V1DeleteOptions()
        )
      
      while True:
        leftover = core_v1.list_namespaced_persistent_volume_claim(restore_namespace).items
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

      # extract from borg archive
      if args.backup_path:
        # we can use the absolute path provided
        full_borg_archive = f"{args.backup_path}borg-{type}/{orig_namespace}::{image_name}_{args.timestamp}"
      else:
        full_borg_archive = f"{os.getcwd()}/borg-{type}/{orig_namespace}::{image_name}_{args.timestamp}"
      
      # import the image into ceph
      # move to new pool if mapping is defined
      pool = meta["pool"]
      storage_class = pvc_dict["spec"]["storage_class_name"]

      if args.pool_sc_mapping:
        for pool_mapping in args.pool_sc_mapping:
          old_pool = pool_mapping.split(":")[0]
          new_pool_sc = pool_mapping.split(":")[1]
          if pool == old_pool:
            pool = new_pool_sc.split("/")[0]
            storage_class = new_pool_sc.split("/")[1]
            logger.debug(f"new mapping specified old pool {old_pool}, new pool {pool}, new sc {storage_class}")
            break

      new_csi_image_name = f"csi-vol-{uuid.uuid4()}"

      logger.info(f"extracting borg archive {full_borg_archive} into rbd import {pool}/{new_csi_image_name}")

      with subprocess.Popen(["borg", "extract", "--sparse", "--stdout", full_borg_archive], stdout=subprocess.PIPE) as proc:
        subprocess.run(["rbd", "import", "-", f"{pool}/{new_csi_image_name}"], check=True, stdin=proc.stdout)

      # restore from pickled pvc dicts
      new_pv_name = f"pvc-{uuid.uuid4()}"

      logger.debug(f"restoring pv with new pv name {new_pv_name} and csi image name {new_csi_image_name}")
    
      # create the new pvc based on the old - remove dynamic fields of old:
      if pvc_dict['metadata']['name'] in existing_pvcs:
        pvc_name = pvc_dict['metadata']['name']
        pvc_dict['metadata']['name'] = f"test-restore-{pvc_name}"
        logger.info(f"pvc {pvc_name} exists, creating it with test-restore- prefix")

      # clean the old pvc object so it can be submitted freshly
      pvc_dict['metadata']['annotations'].pop('pv.kubernetes.io/bind-completed', None)
      pvc_dict['metadata']['annotations'].pop('pv.kubernetes.io/bound-by-controller', None)
      pvc_dict['metadata'].pop('finalizers', None)
      pvc_dict['metadata'].pop('managed_fields', None)
      pvc_dict['metadata'].pop('resource_version', None)
      pvc_dict['metadata'].pop('uid', None)
      pvc_dict['metadata'].pop('creation_timestamp', None)
      pvc_dict.pop('status', None)
      pvc_dict.pop('kind', None)
      pvc_dict.pop('api_version', None)

      # set new values
      pvc_dict['spec']['storage_class_name'] = storage_class
      pvc_dict['metadata']['namespace'] = restore_namespace

      # we can give it a customized pv name so we know migrated ones - will still behave like a normal created pv
      pvc_dict['spec']['volume_name'] = new_pv_name

      # creation call 
      logger.debug(f"creating new pvc:\n{pformat(pvc_dict)}")
      core_v1.create_namespaced_persistent_volume_claim(namespace=restore_namespace, body=client.V1PersistentVolumeClaim(**convert_keys_to_camel_case(pvc_dict)))

      # cleanup the old pv aswell for recreation
      pv_dict.pop('api_version', None)
      pv_dict.pop('kind', None)
      pv_dict['metadata'].pop('creation_timestamp', None)
      pv_dict['metadata'].pop('finalizers', None)
      pv_dict['metadata'].pop('managed_fields', None)
      pv_dict['metadata'].pop('resource_version', None)
      pv_dict['metadata']['annotations'].pop('volume.kubernetes.io/provisioner-deletion-secret-name', None)
      pv_dict['metadata']['annotations'].pop('volume.kubernetes.io/provisioner-deletion-secret-namespace', None)
      pv_dict.pop('status', None)
      pv_dict['spec'].pop('claim_ref', None)
      pv_dict['spec'].pop('volume_attributes_class_name', None)
      pv_dict['spec'].pop('scale_io', None)
      pv_dict['spec']['csi'].pop('volume_handle', None)
      pv_dict['spec']['csi']['volume_attributes'].pop('imageName', None)
      pv_dict['spec']['csi']['volume_attributes'].pop('journalPool', None)
      pv_dict['spec']['csi']['volume_attributes'].pop('pool', None)

      # set values

      # get the storage class and set secrets from it
      ceph_storage_class = ceph_storage_classes[storage_class]
      pv_dict['metadata']['annotations']['volume.kubernetes.io/provisioner-deletion-secret-name'] = ceph_storage_class.parameters['csi.storage.k8s.io/provisioner-secret-name']
      pv_dict['metadata']['annotations']['volume.kubernetes.io/provisioner-deletion-secret-namespace'] = ceph_storage_class.parameters['csi.storage.k8s.io/provisioner-secret-namespace']
      
      pv_dict['spec']['csi']['node_stage_secret_ref']['name'] = ceph_storage_class.parameters['csi.storage.k8s.io/node-stage-secret-name']
      pv_dict['spec']['csi']['node_stage_secret_ref']['namespace'] = ceph_storage_class.parameters['csi.storage.k8s.io/node-stage-secret-namespace']

      pv_dict['spec']['csi']['controller_expand_secret_ref']['name'] = ceph_storage_class.parameters['csi.storage.k8s.io/controller-expand-secret-name']
      pv_dict['spec']['csi']['controller_expand_secret_ref']['namespace'] = ceph_storage_class.parameters['csi.storage.k8s.io/controller-expand-secret-namespace']

      pv_dict['spec']['csi']['volume_attributes']['clusterID'] = ceph_cluster_id

      # reconstruction of volume handle that the ceph csi provisioner understands
      pool_id = format(pool_name_id[pool], '016x')
      trimmed_new_csi_image_name = new_csi_image_name.removeprefix('csi-vol-')
      pv_dict['spec']['csi']['volumeHandle'] = f"0001-0024-{ceph_cluster_id}-{pool_id}-{trimmed_new_csi_image_name}"

      pv_dict['spec']['csi']['volume_attributes']['imageName'] = new_csi_image_name
      pv_dict['spec']['csi']['volume_attributes']['journalPool'] = pool
      pv_dict['spec']['csi']['volume_attributes']['pool'] = pool

      pv_dict['spec']['storage_class_name'] = storage_class

      pv_dict['metadata']['name'] = new_pv_name

      # creation call
      logger.debug(f"creating new pv:\n{pformat(pv_dict)}")
      core_v1.create_persistent_volume(body=client.V1PersistentVolume(**convert_keys_to_camel_case(pv_dict)))

    # scale back up again
    if args.auto_scale:
      # auto downscale deployments and statefulsets of namespace
      deployments = apps_v1.list_namespaced_deployment(restore_namespace)
      for d in deployments.items:
        name = d.metadata.name
        logger.info(f"Scaling Deployment '{name}' back up...")
        apps_v1.patch_namespaced_deployment_scale(
          name=name,
          namespace=restore_namespace,
          body={"spec": {"replicas": auto_scale_replicas[f"dp-{name}"]}}
        )

      statefulsets = apps_v1.list_namespaced_stateful_set(restore_namespace)
      for s in statefulsets.items:
        name = s.metadata.name
        logger.info(f"Scaling StatefulSet '{name}' back up...")
        apps_v1.patch_namespaced_stateful_set_scale(
          name=name,
          namespace=restore_namespace,
          body={"spec": {"replicas": auto_scale_replicas[f"ss-{name}"]}}
        )

    logger.info(f"restore of namespace {orig_namespace} into {restore_namespace} complete, you can now scale up your deployments again")
    

def get_image_metas(args, timestamp_filter = None):
  image_meta_db = TinyDB(f"{args.backup_path}image-meta-db.json")

  archives = []

  # iterate all k8s namespaced borg repos
  k8s_base_path = f"{args.backup_path}/borg-k8s"
  namespace_repos = [name for name in os.listdir(k8s_base_path) if os.path.isdir(os.path.join(k8s_base_path, name))]

  for repo in namespace_repos:
    list_result = subprocess.run(["borg", "list", f"{args.backup_path}/borg-k8s/{repo}", "--json"], capture_output=True)

    if list_result.returncode != 0:
      raise Exception(f"Borg list failed for repo {repo}: {list_result.stderr.decode()}")

    archives.extend(json.loads(list_result.stdout)["archives"])

  timestamp_archives = {}
  for archive in archives:
    image = archive["archive"].split("_", 1)[0]
    timestamp = archive["archive"].split("_", 1)[1]

    if timestamp_filter is not None and timestamp_filter != timestamp:
      continue # skip filtered

    if timestamp not in timestamp_archives:
      timestamp_archives[timestamp] = []  

    Meta = Query()
    image_meta = image_meta_db.get((Meta.image_name == image) & (Meta.timestamp == timestamp))

    if image_meta is None:
      logger.error(f"None meta found {timestamp}, image_name {image}, archive {archive}")
      del timestamp_archives[timestamp]
      continue

    timestamp_archives[timestamp].append(image_meta)

  return timestamp_archives


def copy_backup_generic():
  source_dir = '/opt/bdd'
  for file in os.listdir(source_dir):
    if not file.startswith("."):
      full_source_path = os.path.join(source_dir, file)
      full_dest_path = os.path.join(BACKUP_BASE_DIR, file)

      if os.path.isfile(full_source_path):
        shutil.copy2(full_source_path, full_dest_path)
