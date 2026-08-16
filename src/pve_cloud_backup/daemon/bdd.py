import asyncio
import logging
import os
import pickle
import ssl
import struct

import zstandard as zstd
from pve_cloud.lib.backup_rpc import Command
from tinydb import Query, TinyDB

from pve_cloud_backup.daemon.funcs import (copy_backup_generic,
                                           get_backup_base_dir,
                                           get_volume_metas, init_backup_dir)
from pve_cloud_backup.fetcher.net import send_cchunk

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(level=log_level)
logger = logging.getLogger("bdd")

ENV = os.getenv("ENV", "TESTING")

BACKUP_TYPES = ["k8s", "nextcloud", "git", "postgres"]

lock_dict = {}
lock_dict_lock = asyncio.Lock()


# to prevent from writing to the same borg archive parallel
async def get_lock(backup_dir):
    async with lock_dict_lock:
        if backup_dir not in lock_dict:
            lock_dict[backup_dir] = asyncio.Lock()

        return lock_dict[backup_dir]


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    logger.info(f"Connection from {addr}")

    command = Command(struct.unpack("B", await reader.readexactly(1))[0])
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

                # send ping pong while waiting on lock
                # maybe this is also needed in other rpc call types
                # todo: currently this is the most likely point where a disconnect might happen
                # this connection here is by far the longest running. It would however be nice that
                # the entire app has retries for connects and can handle connection outages well.
                # implement something like toxiproxy in e2e testing and make resilient.
                lock = await get_lock(backup_dir)
                # store ref to kill in case of failure => this will stop the archive from being committet
                borg_proc = None
                borg_archive = f"{backup_dir}::{archive_name}_{timestamp}"
                try:
                    # lock wait mechanism
                    while True:
                        try:
                            await asyncio.wait_for(lock.acquire(), timeout=5)
                            logger.info(f"accuired lock {backup_dir}")
                            break
                        except asyncio.TimeoutError:
                            writer.write(b"\x02")  # 0x02 byte means continue waiting
                            await writer.drain()
                            logger.debug(
                                "send keepalive waiting for lock, continueing..."
                            )

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
                    dbg_chunk_count = 0

                    while True:
                        # client first always sends chunk size
                        chunk_size = struct.unpack("!I", (await reader.readexactly(4)))[
                            0
                        ]

                        # log chunk size on every 100th chunk in dbg log level
                        dbg_chunk_count += 1
                        if dbg_chunk_count % 100 == 0:
                            logger.debug(f"received chunk size {chunk_size}")

                        if chunk_size == 0:
                            logger.debug("received chunk size 0, finished")
                            break  # client sends 0 chunk size at the end to signal that its finished uploading
                        chunk = await reader.readexactly(chunk_size)

                        # decompress and write
                        decompressed_chunk = decompressor.decompress(chunk)
                        if decompressed_chunk:
                            borg_proc.stdin.write(decompressed_chunk)
                            await borg_proc.stdin.drain()

                    # the decompressor does not always return a decompressed chunk but might retain
                    # and return empty. at the end we need to call flush to get everything out
                    logger.debug("flushing, draining and closing")
                    borg_proc.stdin.write(decompressor.flush())
                    await borg_proc.stdin.drain()

                    # close the proc stdin pipe, writer gets closed in finally
                    borg_proc.stdin.close()
                    exit_code = await borg_proc.wait()

                    if exit_code != 0:
                        raise RuntimeError(f"Borg failed with code {exit_code}")

                except (
                    asyncio.IncompleteReadError,
                    ConnectionResetError,
                    BrokenPipeError,
                ) as e:
                    logger.warning(
                        "Client error on transmission: %s, gracefully terminating borg...",
                        e,
                        exc_info=True,
                    )

                    if borg_proc:
                        logger.info("terminating borg process")
                        borg_proc.terminate()

                        try:
                            await asyncio.wait_for(borg_proc.wait(), timeout=30)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "terminate timed out, force killing borg subprocess!"
                            )
                            borg_proc.kill()
                            await borg_proc.wait()

                        logger.debug(f"cleaning up borg repo {borg_archive}")
                        await asyncio.create_subprocess_exec(
                            "borg",
                            "delete",
                            borg_archive,
                        )
                    # reraise exception for main close handler
                    raise
                finally:
                    lock.release()

            case Command.NAMESPACE_SECRETS:
                # read meta dict size
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                meta_dict = pickle.loads((await reader.readexactly(dict_size)))

                db_path = f"{get_backup_base_dir()}/ns-secret-db.json"

                lock = await get_lock(db_path)
                async with lock:
                    secret_db = TinyDB(db_path)
                    secret_db.insert(meta_dict)

            case Command.VOLUME_META:
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                meta_dict = pickle.loads((await reader.readexactly(dict_size)))
                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                lock = await get_lock(db_path)
                async with lock:
                    secret_db = TinyDB(db_path)
                    secret_db.insert(meta_dict)

            # funcs called by brctl for restores
            case Command.LIST_BACKUPS:
                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                lock = await get_lock(db_path)
                async with lock:
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

                lock = await get_lock(db_path)
                async with lock:
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

                lock = await get_lock(db_path)
                async with lock:
                    secret_db = TinyDB(db_path)

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

            case Command.INIT_RESTORE_PROCEDURE:
                timestamp = (await reader.readline()).decode().rstrip("\n")
                logger.info(timestamp)

                db_path = f"{get_backup_base_dir()}/volume-meta-db.json"

                lock = await get_lock(db_path)
                async with lock:
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

                lock = await get_lock(db_path)
                async with lock:
                    secret_db = TinyDB(db_path)

                    Meta = Query()
                    ns_secrets = secret_db.get(
                        (
                            Meta.timestamp == timestamp
                        )  # timestamp is our unique id for the backup
                    )

                    meta_pickled = pickle.dumps(ns_secrets)
                    writer.write(struct.pack("!I", len(meta_pickled)))
                    await writer.drain()

                    writer.write(meta_pickled)
                    await writer.drain()

                    logger.info(
                        "send initial config / secrets - waiting for archive requests"
                    )

            case Command.REQUEST_ARCHIVE:
                # next the client requests the archives which we extract here and pipe via a stream
                # open the extract process and send the stream the output
                request_archive = (await reader.readline()).decode().rstrip("\n")
                logger.info(request_archive)

                request_artifact = (await reader.readline()).decode().rstrip("\n")
                logger.info(request_artifact)

                backup_dir = f"{get_backup_base_dir()}/{request_archive}"
                lock = await get_lock(backup_dir)
                async with lock:
                    logger.info(
                        f"running borg extract on {backup_dir}::{request_artifact}"
                    )
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


    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as e:
        logger.warning("Client disconnected: %s", e, exc_info=True)
    finally:
        writer.close()
        # dont await on server side


async def run():
    certs_dir = "/opt/bdd/certs"
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(
        certfile=f"{certs_dir}/server_cert.crt",
        keyfile=f"{certs_dir}/server_private_key.key",
    )
    server = await asyncio.start_server(handle_client, "0.0.0.0", 8085, ssl=ssl_context)
    addr = server.sockets[0].getsockname()
    logger.info(f"Serving on {addr}")
    async with server:
        await server.serve_forever()


def main():
    # # wait for drive to be available
    # while True:
    #     try:
    #         get_backup_base_dir()
    #         logger.info("Backup drive is available!")
    #         break
    #     except FileNotFoundError as e:
    #         logger.debug(e)
    #         logger.info("Backup drive not found, startup delayed.")
    #         time.sleep(5)

    if ENV == "PRODUCTION":
        copy_backup_generic()

    # backup_store_env_vars = ["PXC_BACKUP_BASE_DIR", "PXC_REMOVABLE_DATASTORES"]
    # num_defined = len([var for var in backup_store_env_vars if os.getenv(var)])
    # if num_defined != 1:
    #     raise Exception(
    #         f"Number of defined backup store vars is {num_defined} but should only be exactly 1 defined!"
    #     )

    asyncio.run(run())
