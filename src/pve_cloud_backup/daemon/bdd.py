import asyncio
import logging
import os
import pickle
import struct
import time

import zstandard as zstd
from tinydb import Query, TinyDB

from pve_cloud_backup.daemon.funcs import (copy_backup_generic,
                                           get_backup_base_dir,
                                           get_volume_metas, init_backup_dir)
from pve_cloud_backup.daemon.rpc import Command
from pve_cloud_backup.fetcher.net import send_cchunk

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("bdd")

ENV = os.getenv("ENV", "TESTING")

BACKUP_TYPES = ["k8s", "nextcloud", "git", "postgres"]

lock_dict = {}


# to prevent from writing to the same borg archive parallel
def get_lock(backup_dir):
    if backup_dir not in lock_dict:
        lock_dict[backup_dir] = asyncio.Lock()

    return lock_dict[backup_dir]


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    logger.info(f"Connection from {addr}")

    command = Command(struct.unpack("B", await reader.read(1))[0])
    logger.info(f"{addr} send command: {command}")

    try:
        match command:
            case Command.ARCHIVE:
                # each archive request starts with a pickled dict containing parameters
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                req_dict = pickle.loads((await reader.readexactly(dict_size)))
                logger.info(req_dict)

                # extract the parameters
                borg_archive_type = req_dict["borg_archive_type"]  # borg locks
                archive_name = req_dict["archive_name"]
                timestamp = req_dict["timestamp"]

                if borg_archive_type not in BACKUP_TYPES:
                    raise Exception("Unknown backup type " + borg_archive_type)

                if borg_archive_type == "k8s":
                    backup_dir = init_backup_dir("k8s/" + req_dict["namespace"])
                else:
                    backup_dir = init_backup_dir(borg_archive_type)

                # lock locally, we have one borg archive per archive type
                async with get_lock(backup_dir):
                    borg_archive = f"{backup_dir}::{archive_name}_{timestamp}"
                    logger.info(f"accuired lock {backup_dir}")

                    # send continue signal, meaning we have the lock and export can start.
                    writer.write(b"\x01")  # signal = 0x01 means "continue"
                    await writer.drain()
                    logger.debug("send go")

                    # initialize the borg subprocess we will pipe the received content to
                    # decompressor = zlib.decompressobj()
                    decompressor = zstd.ZstdDecompressor().decompressobj()
                    borg_proc = await asyncio.create_subprocess_exec(
                        "borg",
                        "create",
                        "--compression",
                        "zstd,1",
                        "--stdin-name",
                        req_dict["stdin_name"],
                        borg_archive,
                        "-",
                        stdin=asyncio.subprocess.PIPE,
                    )

                    # read compressed chunks
                    while True:
                        # client first always sends chunk size
                        chunk_size = struct.unpack("!I", (await reader.readexactly(4)))[
                            0
                        ]
                        if chunk_size == 0:
                            break  # client sends 0 chunk size at the end to signal that its finished uploading
                        chunk = await reader.readexactly(chunk_size)

                        # decompress and write
                        decompressed_chunk = decompressor.decompress(chunk)
                        if decompressed_chunk:
                            borg_proc.stdin.write(decompressed_chunk)
                            await borg_proc.stdin.drain()

                    # the decompressor does not always return a decompressed chunk but might retain
                    # and return empty. at the end we need to call flush to get everything out
                    borg_proc.stdin.write(decompressor.flush())
                    await borg_proc.stdin.drain()

                    # close the proc stdin pipe, writer gets closed in finally
                    borg_proc.stdin.close()
                    exit_code = await borg_proc.wait()

                    if exit_code != 0:
                        raise Exception(f"Borg failed with code {exit_code}")

            case Command.NAMESPACE_SECRETS:
                # read meta dict size
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                meta_dict = pickle.loads((await reader.readexactly(dict_size)))

                db_path = f"{get_backup_base_dir()}/ns-secret-db.json"

                async with get_lock(db_path):
                    secret_db = TinyDB(db_path)
                    secret_db.insert(meta_dict)

            case Command.VOLUME_META:
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                meta_dict = pickle.loads((await reader.readexactly(dict_size)))
                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                async with get_lock(db_path):
                    secret_db = TinyDB(db_path)
                    secret_db.insert(meta_dict)

            # funcs called by brctl for restores
            case Command.LIST_BACKUPS:
                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                async with get_lock(db_path):
                    # we call borg on all our backups and send a return string that is strictly for display via the cli tool
                    timestamp_archives = get_volume_metas()

                # simply return all archives
                archives_pickled = pickle.dumps(timestamp_archives)
                writer.write(struct.pack("!I", len(archives_pickled)))
                await writer.drain()

                writer.write(archives_pickled)
                await writer.drain()
                logger.debug("send archives")

            case Command.LIST_BACKUP_DETAILS:
                timestamp = (await reader.readline()).decode().rstrip("\n")

                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                async with get_lock(db_path):
                    # we call borg on all our backups and send a return string that is strictly for display via the cli tool
                    # this time we need the filter for displaying details of a certain backup
                    timestamp_archives = get_volume_metas(timestamp_filter=timestamp)

                # return the archive
                archive_pickled = pickle.dumps(timestamp_archives[timestamp])
                writer.write(struct.pack("!I", len(archive_pickled)))
                await writer.drain()

                writer.write(archive_pickled)
                await writer.drain()

                # return k8s secret requests
                db_path = f"{get_backup_base_dir()}/ns-secret-db.json"

                async with get_lock(db_path):
                    secret_db = TinyDB(db_path)

                while True:
                    # listen for secret requests
                    stack = (await reader.readline()).decode().rstrip("\n")

                    if stack == "##BRCTL-DONE":
                        break  # done signal

                    Meta = Query()
                    ns_secrets = secret_db.get(
                        (Meta.timestamp == timestamp) & (Meta.stack == stack)
                    )

                    meta_pickled = pickle.dumps(ns_secrets)
                    writer.write(struct.pack("!I", len(meta_pickled)))
                    await writer.drain()

                    writer.write(meta_pickled)
                    await writer.drain()

            case Command.RESTORE_PROCEDURE:
                timestamp = (await reader.readline()).decode().rstrip("\n")
                logger.info(timestamp)

                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                async with get_lock(db_path):
                    # we call borg on all our backups and send a return string that is strictly for display via the cli tool
                    # this time we need the filter for displaying details of a certain backup
                    timestamp_archives = get_volume_metas(timestamp_filter=timestamp)

                # return the archive
                archive_pickled = pickle.dumps(timestamp_archives[timestamp])
                writer.write(struct.pack("!I", len(archive_pickled)))
                await writer.drain()

                writer.write(archive_pickled)
                await writer.drain()

                # client then queries secrets of the backup to restore
                # return k8s secret requests
                db_path = f"{get_backup_base_dir()}/ns-secret-db.json"

                async with get_lock(db_path):
                    secret_db = TinyDB(db_path)

                    # listen for secret requests
                    stack = (await reader.readline()).decode().rstrip("\n")

                    Meta = Query()
                    ns_secrets = secret_db.get(
                        (Meta.timestamp == timestamp) & (Meta.stack == stack)
                    )

                    meta_pickled = pickle.dumps(ns_secrets)
                    writer.write(struct.pack("!I", len(meta_pickled)))
                    await writer.drain()

                    writer.write(meta_pickled)
                    await writer.drain()

                # next the client requests the archives which we extract here and pipe via a stream
                while True:
                    # open the extract process and send the stream the output
                    request_archive = (await reader.readline()).decode().rstrip("\n")
                    logger.info(request_archive)

                    if request_archive == "##BRCTL-DONE":
                        break  # done signal

                    request_artifact = (await reader.readline()).decode().rstrip("\n")
                    logger.info(request_artifact)

                    backup_dir = f"{get_backup_base_dir()}/{request_archive}"

                    async with get_lock(backup_dir):
                        proc = await asyncio.create_subprocess_exec(
                            "borg",
                            "extract",
                            "--sparse",
                            "--stdout",
                            f"{backup_dir}::{request_artifact}",
                            stdout=asyncio.subprocess.PIPE,
                        )

                        compressor = zstd.ZstdCompressor(
                            level=1, threads=6
                        ).compressobj()
                        while True:
                            chunk = await proc.stdout.read(4 * 1024 * 1024 * 10)  # 4MB
                            if not chunk:
                                break

                            # compress and send the chunk
                            await send_cchunk(writer, compressor.compress(chunk))

                        # send the rest in the compressor
                        await send_cchunk(writer, compressor.flush())

                        logger.info("sending eof")
                        writer.write(struct.pack("!I", 0))
                        await writer.drain()

    except asyncio.IncompleteReadError as e:
        logger.error("Client disconnected", e)
    finally:
        writer.close()
        # dont await on server side


async def run():
    server = await asyncio.start_server(handle_client, "0.0.0.0", 8085)
    addr = server.sockets[0].getsockname()
    logger.info(f"Serving on {addr}")
    async with server:
        await server.serve_forever()


def main():
    # wait for drive to be available
    while True:
        try:
            get_backup_base_dir()
            logger.info("Backup drive is available!")
            break
        except FileNotFoundError as e:
            logger.debug(e)
            logger.info("Backup drive not found, startup delayed.")
            time.sleep(5)

    if ENV == "PRODUCTION":
        copy_backup_generic()

    backup_store_env_vars = ["PXC_BACKUP_BASE_DIR", "PXC_REMOVABLE_DATASTORES"]
    num_defined = len([var for var in backup_store_env_vars if os.getenv(var)])
    if num_defined != 1:
        raise Exception(
            f"Number of defined backup store vars is {num_defined} but should only be exactly 1 defined!"
        )

    asyncio.run(run())
