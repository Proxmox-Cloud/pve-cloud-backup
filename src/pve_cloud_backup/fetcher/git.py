import os
import shutil
import subprocess
import tarfile
import logging
from pve_cloud_backup.fetcher.net import archive


logger = logging.getLogger("fetcher")


async def backup_git(backup_addr, timestamp, git_repos):
  for repo_url in git_repos:
    repo_name = os.path.splitext(os.path.basename(repo_url))[0]

    archive_path = f"{repo_name}.tar" 

    subprocess.run(["git", "clone", repo_url, repo_name], check=True)

    with tarfile.open(archive_path, "w") as tar:
      tar.add(repo_name, arcname=repo_name)

    shutil.rmtree(repo_name)

    logger.info(f"Repository archived successfully as {archive_path}")

    request_dict = {
      "borg_archive_type": "git",
      "archive_name": repo_name,
      "timestamp": timestamp,
      "stdin_name": archive_path
    }
    logger.info(request_dict)

    def chunk_generator():
      with open(archive_path, "rb") as file:
        while True:
          chunk = file.read(4 * 1024 * 1024) # 4mb
          if not chunk:
            break
          yield chunk

    await archive(backup_addr, request_dict, chunk_generator)

    os.remove(archive_path)