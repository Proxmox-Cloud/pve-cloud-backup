import argparse
import json
import logging
import os
import subprocess
from pathlib import Path

from pve_cloud_backup.daemon.funcs import get_backup_base_dir
from pve_cloud_backup.daemon.db import (
    get_db_timestamps,
    prune_db_by_timestamps
)

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("bdd-prune")

os.environ["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "yes"
os.environ["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"


def prune_repo(repo_path: str, keep_daily: int, keep_weekly: int, keep_monthly: int):
    cmd = [
        "borg", "prune", "-v", "--list",
        f"--keep-daily={keep_daily}",
        f"--keep-weekly={keep_weekly}",
        f"--keep-monthly={keep_monthly}",
        repo_path
    ]
    subprocess.run(cmd, check=True)


def check_repo(repo_path: str, verify_data: bool):
    cmd = ["borg", "-v", "check"]
    if verify_data:
        cmd.append("--verify-data")

    cmd.append(repo_path)
    subprocess.run(cmd, check=True)


def borg_list_timestamps(archive_dir: str):
    list_result = subprocess.run(
        ["borg", "list", archive_dir, "--json"],
        capture_output=True,
        check=True
    )

    timestamps = []
    archives = json.loads(list_result.stdout)["archives"]
    for archive in archives:
        archive_name = archive["archive"]
        timestamp = archive_name.split("_", 1)[1]
        timestamps.add(timestamp)

    return timestamps

def collect_borg_timestamps(base_dir: str) -> set:
    borg_timestamps = set()

    k8s_base = Path(base_dir) / "borg-k8s"
    if k8s_base.exists():
        for ns_dir in k8s_base.iterdir():
            if ns_dir.is_dir():
                borg_timestamps.update(borg_list_timestamps(str(ns_dir)))

    flat_dirs = ["borg-nextcloud", "borg-git", "borg-postgres"]
    for dir_name in flat_dirs:
        dir_path = Path(base_dir) / dir_name
        if dir_path.exists():
            borg_timestamps.update(borg_list_timestamps(str(dir_path)))

    return borg_timestamps


def prune_database(borg_timestamps: set):
    db_timestamps = get_db_timestamps()
    timestamps_to_delete = db_timestamps - borg_timestamps

    logger.info(f"pruning {len(timestamps_to_delete)} timestamps from database!")
    prune_db_by_timestamps(timestamps_to_delete)


def run_maintenance(keep_daily: int, keep_weekly: int, keep_monthly: int, verify_data: bool):
    base_dir = get_backup_base_dir()

    logger.info("performing prune and check on borg archives...")
    k8s_base = Path(base_dir) / "borg-k8s"
    if k8s_base.exists():
        for ns_dir in k8s_base.iterdir():
            if ns_dir.is_dir():
                prune_repo(str(ns_dir), keep_daily, keep_weekly, keep_monthly)
                check_repo(str(ns_dir), verify_data)

    flat_dirs = ["borg-nextcloud", "borg-git", "borg-postgres"]
    for dir_name in flat_dirs:
        dir_path = Path(base_dir) / dir_name
        if dir_path.exists():
            prune_repo(str(dir_path), keep_daily, keep_weekly, keep_monthly)
            check_repo(str(dir_path), verify_data)

    borg_timestamps = collect_borg_timestamps(base_dir)

    prune_database(borg_timestamps)


def main():
    parser = argparse.ArgumentParser(description="Borg repository maintenance (prune + check)")
    parser.add_argument(
        "--keep-daily",
        type=int,
        default=int(os.environ.get("KEEP_DAILY", 7)),
    )
    parser.add_argument(
        "--keep-weekly",
        type=int,
        default=int(os.environ.get("KEEP_WEEKLY", 4)),
    )
    parser.add_argument(
        "--keep-monthly",
        type=int,
        default=int(os.environ.get("KEEP_MONTHLY", 6)),
    )
    parser.add_argument(
        "--verify-data",
        action="store_true",
        default=os.environ.get("VERIFY_DATA", "").lower() in ("1", "true", "yes"),
        help="Run borg check with --verify-data",
    )

    args = parser.parse_args()
    run_maintenance(args.keep_daily, args.keep_weekly, args.keep_monthly, args.verify_data)
