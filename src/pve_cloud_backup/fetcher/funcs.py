import paramiko
import base64
import logging
import pickle
import subprocess
import json
import re
import yaml
import os
from pprint import pformat
from kubernetes import client
from kubernetes.config.kube_config import KubeConfigLoader
import pve_cloud_backup.fetcher.net as net
import asyncio


logger = logging.getLogger("fetcher")


# sshs into master k8s vms and fetches the there present kubeconfigs
def get_kubernetes_clients(backup_config, proxmox, pkey):
  logger.info(f"getting k8s clients")

  k8s_stacks = backup_config["k8s_stacks"].keys()

  k8s_masters = {}
  # collect one master node per stack
  for node in proxmox.nodes.get():
    node_name = node["node"]

    if node["status"] == "offline":
      logger.info(f"skipping offline node {node_name}")
      continue
    
    for qemu in proxmox.nodes(node_name).qemu.get():
      if "tags" in qemu and any(tag in k8s_stacks for tag in qemu["tags"].split(";")) and 'master' in qemu["tags"].split(";"):
        # found a master
        logger.debug(f"found master {pformat(qemu)}")
        
        # find the stack tag
        stack_tag = None
        for tag in qemu["tags"].split(";"):
          for k8s_stack_tag in k8s_stacks:
            if tag == k8s_stack_tag:
              stack_tag = tag
        
        if stack_tag is None:
          raise Exception(f"something went terribly wrong, stack tag should never be none - qemu:\n{pformat(qemu)}")
        
        if stack_tag in k8s_masters:
          continue # we already saved a master for this stack

        k8s_masters[stack_tag] = {"pve_host": node_name, "vmid": qemu["vmid"]}

  logger.debug(f"collected masters:\n{pformat(k8s_masters)}")

  k8s_kubeconfigs = {}

  # now we can connect to each master via ssh and fetch the kubeconfig
  for k8s_stack, master in k8s_masters.items():
    ifaces = proxmox.nodes(master["pve_host"]).qemu(master["vmid"]).agent("network-get-interfaces").get()
    logger.debug(f"k8s stack master {k8s_stack} interfaces {pformat(ifaces)}")

    master_ipv4 = None

    for iface in ifaces["result"]:
      if iface["name"] == "lo":
        continue # skip the first loopback device

      # after that comes the primary interface
      for ip_address in iface["ip-addresses"]:
        if ip_address["ip-address-type"] == "ipv4":
          master_ipv4 = ip_address["ip-address"]
          break
      
      if master_ipv4 is None:
        raise Exception(f"could not get ipv4 for master {master} stack {k8s_stack}")

      break

    # now we can use that address to connect via ssh
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    user = os.getenv("QEMU_ADMIN_USER", "admin")

    logger.info(f"connecting to master {master_ipv4} - {user}")
    ssh.connect(master_ipv4, username=user, pkey=pkey)

    # since we need root we cant use sftp and root via ssh is disabled
    _, stdout, _ = ssh.exec_command("sudo cat /etc/kubernetes/admin.conf")

    config = stdout.read().decode('utf-8')

    logger.debug(f"ssh sudo cat kubeconfig:\n{config}")

    k8s_kubeconfigs[k8s_stack] = { "raw_kubeconfig": config, "master_ip": master_ipv4 }
  
  return k8s_kubeconfigs



# collect all pvc and pv information
def collect_raw_k8s_meta(backup_config, k8s_kubeconfigs):

  k8s_stack_meta = {}

  k8s_stack_namespace_secrets = {}

  for k8s_stack, k8s_backup_config in backup_config["k8s_stacks"].items():

    if k8s_backup_config["exclude_namespaces"] is not None and k8s_backup_config["include_namespaces"] is not None:
      raise Exception(f"cannot specify include and exclude for k8s_stack {k8s_stack}")
      
    kubeconfig = k8s_kubeconfigs[k8s_stack]
    master_ipv4 = kubeconfig["master_ip"]
    kubeconfig_dict = yaml.safe_load(kubeconfig["raw_kubeconfig"])

    # override the connection ip as it is set to localhost on the machines
    kubeconfig_dict["clusters"][0]["cluster"]["server"] = f"https://{master_ipv4}:6443"
    logger.debug(f"kubeconfig dict {pformat(kubeconfig_dict)}")

    # init kube client
    loader = KubeConfigLoader(config_dict=kubeconfig_dict)
    configuration = client.Configuration()
    loader.load_and_set(configuration)

    # Create a client from this configuration
    api_client = client.ApiClient(configuration)
    v1 = client.CoreV1Api(api_client=api_client)

    # Use it
    k8s_backup_meta = []

    # sub dict for secrets of each namespace
    k8s_stack_namespace_secrets[k8s_stack] = {}

    for namespace_item in v1.list_namespace().items:
      namespace = namespace_item.metadata.name

      if k8s_backup_config["exclude_namespaces"] is not None and namespace in k8s_backup_config["exclude_namespaces"]:
        continue

      if k8s_backup_config["include_namespaces"] is not None and namespace not in k8s_backup_config["include_namespaces"]:
        continue  
      
      # collect secrets of namespace
      k8s_stack_namespace_secrets[k8s_stack][namespace] = [secret.to_dict() for secret in v1.list_namespaced_secret(namespace=namespace).items]

      pvc_list = v1.list_namespaced_persistent_volume_claim(namespace=namespace)
      
      for pvc in pvc_list.items:
        pvc_name = pvc.metadata.name
        volume_name = pvc.spec.volume_name
        status = pvc.status.phase
    
        if volume_name:
          pv = v1.read_persistent_volume(name=volume_name)
          pv_dict_b64 = base64.b64encode(pickle.dumps(pv.to_dict())).decode('utf-8') 

          pvc_dict_b64 = base64.b64encode(pickle.dumps(pvc.to_dict())).decode('utf-8') 
          
          k8s_backup_meta.append({"namespace": namespace, "pvc_name": pvc_name, "namespace": namespace, 
                                  "image_name": pv.spec.csi.volume_attributes["imageName"], "pool": pv.spec.csi.volume_attributes["pool"],
                                  "pvc_dict_b64": pvc_dict_b64, "pv_dict_b64": pv_dict_b64, "storage_class": pvc.spec.storage_class_name})
        else:
          logger.debug(f"PVC: {pvc_name} -> Not bound to a PV [Status: {status}]")
    
    k8s_stack_meta[k8s_stack] = k8s_backup_meta
  
  return k8s_stack_meta, k8s_stack_namespace_secrets


def pool_images(raw_k8s_meta):
  # initialize for images grouped by pool
  unique_pools = set()

  # collect pools from k8s volumes
  for k8s_stack, k8s_metas in raw_k8s_meta.items():
    for k8s_meta in k8s_metas:
      unique_pools.add(k8s_meta["pool"])

  # create rbd groups 
  for pool in unique_pools:
    try:
      # check for errors, capture stderr output as text
      subprocess.run(["rbd", "group", "create", f"{pool}/backups"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
      logger.warning(e.stdout + e.stderr) # no problem if group already exists, cleanup failed tho

  # add rbds from pvcs
  for k8s_metas in raw_k8s_meta.values():
    for k8s_meta in k8s_metas:
      pool = k8s_meta["pool"]
      image = k8s_meta["image_name"]
      try:
        subprocess.run(["rbd", "group", "image", "add", f"{pool}/backups", f"{pool}/{image}"], check=True, capture_output=True, text=True)
      except subprocess.CalledProcessError as e:
        logger.error(e.stdout + e.stderr) # proper error printing
        raise

  return unique_pools


def clone(pool, image, timestamp):
  try:
    command = subprocess.run(["rbd", "snap", "ls", "--all", "--format", "json", f"{pool}/{image}"], check=True, capture_output=True, text=True)
    snaps = json.loads(command.stdout)
    # doesnt logger.info anything on success
  except subprocess.CalledProcessError as e:
    logger.error(e.stdout + e.stderr)
    raise
  
  for snap in snaps:
    if snap["namespace"]["type"] == "group" and snap["namespace"]["group snap"] == timestamp: 
      snap_id = snap["id"]
      break

  logger.debug(f"image {image} snap id {snap_id}")

  # create temporary clone
  try:
    subprocess.run(["rbd", "clone", "--snap-id", str(snap_id), f"{pool}/{image}", f"{pool}/temp-clone-{timestamp}-{image}", "--rbd-default-clone-format", "2"], check=True, capture_output=True, text=True)
  except subprocess.CalledProcessError as e:
    logger.error(e.stdout + e.stderr)
    raise


def snap_and_clone(raw_k8s_meta, timestamp, unique_pools):
  logger.info("creating snaps")
  for pool in unique_pools:
    try:
      subprocess.run(["rbd", "group", "snap", "create", f"{pool}/backups@{timestamp}"], check=True, capture_output=True, text=True)
      # doesnt logger.info anything on success
    except subprocess.CalledProcessError as e:
      logger.error(e.stdout + e.stderr)
      raise

  logger.info("creating clones")

  # clone all the snapshots into new images so we can export them
  # sadly there isnt yet a direct export function for group snapshots
  for k8s_metas in raw_k8s_meta.values():
    for k8s_meta in k8s_metas:
      pool = k8s_meta["pool"]
      image = k8s_meta["image_name"]
      clone(pool, image, timestamp)


async def send_export(send_command, semaphore):
  async with semaphore:
    backup_addr = send_command["backup_addr"]
    params = send_command["params"]

    request_dict = {
      "borg_archive_type": params["type"],
      "archive_name": params["image_name"],
      "timestamp": params["timestamp"],
      "stdin_name": params["image_name"] + ".raw",
      "namespace": params["namespace"]
    }
    logger.info(request_dict)

    # to get full performance we need to have the subprocess reading async aswell
    async def async_chunk_generator():
        proc = await asyncio.create_subprocess_exec(
            *send_command["subprocess_args"],
            stdout=asyncio.subprocess.PIPE
        )

        while True:
            chunk = await proc.stdout.read(4 * 1024 * 1024 * 10)  # 4MB
            if not chunk:
                break
            yield chunk

        await proc.wait()

    await net.archive_async(backup_addr, request_dict, async_chunk_generator)


async def send_backups(raw_k8s_meta, timestamp, backup_addr):
  send_commands = []

  for k8s_metas in raw_k8s_meta.values():
    for k8s_meta in k8s_metas:
      pool = k8s_meta["pool"]
      image = k8s_meta["image_name"]

      params = {"timestamp": timestamp, "image_name": image, "pool": pool, "type": "k8s", "namespace": k8s_meta["namespace"]}

      send_commands.append({"params": params, "backup_addr": backup_addr, "subprocess_args": ["rbd", "export", f"{pool}/temp-clone-{timestamp}-{image}", "-"]})

  semaphore = asyncio.Semaphore(int(os.getenv("SEND_PARALELLISM_NUM", "4")))

  # start one thread per type, since borg on bdd side is single threaded per archive
  export_tasks = [asyncio.create_task(send_export(command, semaphore)) for command in send_commands]
   
  await asyncio.gather(*export_tasks)


async def post_image_meta(raw_k8s_meta, timestamp, backup_config, backup_addr):
  for k8s_stack, k8s_metas in raw_k8s_meta.items():
    for k8s_meta in k8s_metas:
      pool = k8s_meta["pool"]
      image = k8s_meta["image_name"]
      body = {"timestamp": timestamp, "image_name": image, "pool": pool, "stack": k8s_stack, "type": "k8s", "namespace": k8s_meta["namespace"],
              "pvc_dict_b64": k8s_meta['pvc_dict_b64'], "pv_dict_b64": k8s_meta['pv_dict_b64'], "pvc_name": k8s_meta["pvc_name"], "storage_class": k8s_meta["storage_class"]}

      logger.debug(f"posting {body}")
      await net.image_meta(backup_addr, body)


async def post_k8s_stack_meta(k8s_kubeconfigs, k8s_stack_namespace_secrets, timestamp, backup_addr):
  for k8s_stack, kubeconfig in k8s_kubeconfigs.items():
    namespace_secret_dict_b64 = base64.b64encode(pickle.dumps(k8s_stack_namespace_secrets[k8s_stack])).decode('utf-8') 
    body = {"timestamp": timestamp, "stack": k8s_stack, "type": "k8s", "raw_kubeconfig": kubeconfig["raw_kubeconfig"], "master_ip": kubeconfig["master_ip"], "namespace_secret_dict_b64": namespace_secret_dict_b64}
    logger.debug(f"posting {body}")

    await net.stack_meta(backup_addr, body)

    
def cleanup(raw_vm_meta, raw_k8s_meta, timestamp, unique_pools):
  logger.info("cleanup")
  # delete tmp images
  if raw_vm_meta is not None:
    for vm_meta in raw_vm_meta:
      for disk_conf in vm_meta["disk_confs"].values():
        image = disk_conf.split(",")[0].split(":")[1]
        pool = disk_conf.split(",")[0].split(":")[0]
        try:
          subprocess.run(["rbd", "rm", f"{pool}/temp-clone-{timestamp}-{image}"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
          logger.warning(e.stdout + e.stderr)


  if raw_k8s_meta is not None:
    for k8s_stack, k8s_metas in raw_k8s_meta.items():
      for k8s_meta in k8s_metas:
        pool = k8s_meta["pool"]
        image = k8s_meta["image_name"]
        try:
          subprocess.run(["rbd", "rm", f"{pool}/temp-clone-{timestamp}-{image}"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
          logger.warning(e.stdout + e.stderr)


  if unique_pools is not None:
    # delete snaps
    for pool in unique_pools:
      logger.debug("removing snaps from pool " + pool)
      try:
        subprocess.run(["rbd", "group", "snap", "rm", f"{pool}/backups@{timestamp}"], check=True, capture_output=True, text=True)
        # doesnt logger.info anything on success
      except subprocess.CalledProcessError as e:
        logger.warning(e.stdout + e.stderr)

    # delete groups
    for pool in unique_pools:
      logger.debug("removing backup group from pool " + pool)
      try:
        subprocess.run(["rbd", "group", "rm", f"{pool}/backups"], check=True, capture_output=True, text=True)
        # doesnt logger.info anything on success
      except subprocess.CalledProcessError as e:
        logger.warning(e.stdout + e.stderr)