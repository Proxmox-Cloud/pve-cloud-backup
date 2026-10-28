import json
import sqlite3
from typing import Optional, Set

import aiosqlite

DB_PATH = "/mnt/bdd-zfs-data/backup-metadata.db"


async def init_db():
    # todo: this should be refactored to a deduplicated approach
    # as namespace secrets can get quite large and daily backups will
    # result in a big db
    async with aiosqlite.connect(DB_PATH) as db:
        # create table for storing kubernetes volume related information
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS volume_meta (
                image_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (image_name, timestamp)
            )
        """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_volume_meta_timestamp ON volume_meta(timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_volume_meta_image ON volume_meta(image_name)"
        )

        # table for storing secrets of multiple k8s namespaces per row (grouped by proxmox cloud stack)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS stack_secrets (
                timestamp TEXT PRIMARY KEY,
                stack TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
        """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stack_secrets_timestamp ON stack_secrets(timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stack_secrets_stack ON stack_secrets(stack)"
        )
        await db.commit()


async def insert_volume_meta(meta_dict: dict) -> int:
    image_name = meta_dict["image_name"]
    timestamp = meta_dict["timestamp"]

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR REPLACE INTO volume_meta (image_name, timestamp, data_json) VALUES (?, ?, ?)",
            (image_name, timestamp, json.dumps(meta_dict)),
        )
        await db.commit()
        return cursor.lastrowid


async def get_volume_meta(image_name: str, timestamp: str) -> Optional[dict]:
    """
    Get metadata of a single kubernetes pv + pvc.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT data_json FROM volume_meta WHERE image_name = ? AND timestamp = ?",
            (image_name, timestamp),
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row[0])

        return None


async def get_volume_metas(timestamp_filter: Optional[str] = None) -> dict:
    """
    Query the database for mutliple k8s pv + pvc volume metadata entries. Returns them in a map, grouped by their
    backup timestamp id.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if timestamp_filter:
            cursor = await db.execute(
                "SELECT timestamp, data_json FROM volume_meta WHERE timestamp = ?",
                (timestamp_filter,),
            )
        else:
            cursor = await db.execute("SELECT timestamp, data_json FROM volume_meta")

        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            timestamp = row[0]
            if timestamp not in result:
                result[timestamp] = []
            result[timestamp].append(json.loads(row[1]))
        return result


async def insert_stack_secret(data: dict) -> int:
    timestamp = data["timestamp"]
    stack = data["stack"]

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO stack_secrets (timestamp, stack, data_json) VALUES (?, ?, ?)",
            (timestamp, stack, json.dumps(data)),
        )
        await db.commit()
        return cursor.lastrowid


async def get_stack_secrets(timestamp: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT data_json FROM stack_secrets WHERE timestamp = ?", (timestamp,)
        )
        row = await cursor.fetchone()
        if row:
            return json.dumps(row[0])
        return None


def get_db_timestamps() -> Set[str]:
    timestamps = set()

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT timestamp FROM volume_meta")
        for row in cursor.fetchall():
            timestamps.add(row[0])

        cursor.execute("SELECT DISTINCT timestamp FROM stack_secrets")
        for row in cursor.fetchall():
            timestamps.add(row[0])

        return timestamps


def prune_db_by_timestamps(timestamps_to_delete: Set[str]):
    with sqlite3.connect(DB_PATH) as conn:
        # create temp table with timestamp ids that will be deleted
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS timestamps_to_delete (
                timestamp TEXT PRIMARY KEY
            )
        """
        )

        conn.executemany(
            "INSERT OR IGNORE INTO timestamps_to_delete (timestamp) VALUES (?)",
            ((ts,) for ts in timestamps_to_delete),
        )

        # prune the two tables
        conn.execute(
            """
            DELETE FROM volume_meta
            WHERE EXISTS (
                SELECT 1
                FROM timestamps_to_delete d
                WHERE d.timestamp = volume_meta.timestamp
            )
        """
        )

        conn.execute(
            """
            DELETE FROM stack_secrets
            WHERE EXISTS (
                SELECT 1
                FROM timestamps_to_delete d
                WHERE d.timestamp = stack_secrets.timestamp
            )
        """
        )
