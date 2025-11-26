import paramiko
import logging
from pve_cloud_backup.fetcher.net import archive
import os

logger = logging.getLogger("fetcher")


async def backup_patroni(backup_addr, timestamp, proxmox, stack_fqdn, pkey):
  logger.info("looking for " + stack_fqdn)

  patroni_member = None

  for node in proxmox.nodes.get():
    node_name = node["node"]
    logger.info("collecting node " + node_name)

    if node["status"] == "offline":
      logger.info(f"skipping offline node {node_name}")
      continue

    for lxc in proxmox.nodes(node_name).lxc.get():
      if "tags" not in lxc:
        continue # non stack vm

      if stack_fqdn in lxc["tags"]:
        patroni_member = lxc
        break

  if not patroni_member:
    raise Exception("Couldnt find a patroni member for the specified stack " + stack_fqdn)

  stack_apex = ".".join(stack_fqdn.split(".")[1:])

  ssh = paramiko.SSHClient()
  ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

  logger.info("connecting to patroni member " + lxc["name"] + "." + stack_apex)
  ssh.connect(lxc["name"] + "." + stack_apex, username="root", pkey=pkey)

  # create pg pass file
  _, stdout, _ = ssh.exec_command("echo '*:*:*:*:" + os.getenv("PATRONI_PASS") + "' > ~/.pgpass && chmod 600 ~/.pgpass")

  def chunk_generator():
    stdin, stdout, stderr = ssh.exec_command(
      "pg_dumpall -h 127.0.0.1 -p 5432 -U postgres --no-role-passwords",
      bufsize=0
    )
    chunk_size = 4 * 1024 * 1024  
    channel = stdout.channel
    
    while not channel.exit_status_ready() or channel.recv_ready():
      data = channel.recv(chunk_size)
      if not data:
        continue
      yield data

  request_dict = {
    "borg_archive_type": "postgres",
    "archive_name": "patroni_dump.db",
    "timestamp": timestamp,
    "stdin_name": "patroni_dump.db"
  }

  await archive(backup_addr, request_dict, chunk_generator)

  # delete the password file again
  _, stdout, _ = ssh.exec_command("rm ~/.pgpass")