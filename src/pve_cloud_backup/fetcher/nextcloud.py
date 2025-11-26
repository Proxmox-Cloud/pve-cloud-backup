import requests
from requests.auth import HTTPBasicAuth
import os
import logging
from pve_cloud_backup.fetcher.net import archive


logger = logging.getLogger("fetcher")

password = None
if os.path.isfile("/opt/nextcloud.pass"):
  with open("/opt/nextcloud.pass", "r", encoding="utf-8") as file:
    password = file.read()
else:
  logger.info("no nextcloud pass mounted, skipping nextcloud backup.")

username = os.getenv("NEXTCLOUD_USER")

nextcloud_base = os.getenv("NEXTCLOUD_BASE")


async def backup_nextcloud(backup_addr, timestamp, nextcloud_files):
  if password is None:
    logger.info("no nextcloud pass mounted, skipping nextcloud backup.")
    return

  for file in nextcloud_files:
    request_dict = {
      "borg_archive_type": "nextcloud",
      "archive_name": file,
      "timestamp": timestamp,
      "stdin_name": file
    }

    def chunk_generator():
      response = requests.get(f"{nextcloud_base}/remote.php/dav/files/{username}/{file}", auth=HTTPBasicAuth(username, password), stream=True)
      for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
        if chunk:
          yield chunk

    await archive(backup_addr, request_dict, chunk_generator)

