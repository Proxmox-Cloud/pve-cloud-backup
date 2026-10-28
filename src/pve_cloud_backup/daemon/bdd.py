import asyncio
import logging
import os
import pickle
import ssl
import struct

import zstandard as zstd
from pve_cloud.lib.backup_rpc import Command

from pve_cloud_backup.daemon.db import (get_stack_secrets, get_volume_metas,
                                        init_db, insert_stack_secret,
                                        insert_volume_meta)
from pve_cloud_backup.daemon.funcs import (copy_backup_generic,
                                           init_backup_dir,
                                           get_backup_base_dir)
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

                    # todo: this also needs acks?
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

                        # send ack
                        writer.write(b"\x01")
                        await writer.drain()

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
                        raise RuntimeError(f"Borg failed with code {exit_code}")

                except (
                    asyncio.IncompleteReadError,
                    ConnectionResetError,
                    BrokenPipeError,
                ) as e:
                    logger.warning(
                        "Client error on transmission: %s",
                        e,
                        exc_info=True,
                    )

                    if borg_proc:
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

            case Command.STACK_SECRETS:
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                logger.debug(f"reading secret dict of size {dict_size}")
                meta_dict = pickle.loads((await reader.readexactly(dict_size)))

                await insert_stack_secret(meta_dict)

                writer.write(b"\x01")
                await writer.drain()

            case Command.VOLUME_META:
                dict_size = struct.unpack("!I", (await reader.readexactly(4)))[0]
                meta_dict = pickle.loads((await reader.readexactly(dict_size)))

                await insert_volume_meta(
                    meta_dict
                )

                writer.write(b"\x01")
                await writer.drain()

            # funcs called by brctl for restores
            case Command.LIST_BACKUPS:
                volume_metas = await get_volume_metas()

                # simply return all archives
                metas_pickled = pickle.dumps(volume_metas)
                writer.write(struct.pack("!I", len(metas_pickled)))
                await writer.drain()

                writer.write(metas_pickled)
                await writer.drain()

            case Command.LIST_BACKUP_DETAILS:
                timestamp = (await reader.readline()).decode().rstrip("\n")

                volume_metas = await get_volume_metas(timestamp_filter=timestamp)

                # return the volume metas
                metas_pickled = pickle.dumps(volume_metas[timestamp])
                writer.write(struct.pack("!I", len(metas_pickled)))
                await writer.drain()

                writer.write(metas_pickled)
                await writer.drain()

                # return the secrets
                stack_secrets = await get_stack_secrets(timestamp)

                meta_pickled = pickle.dumps(stack_secrets)
                writer.write(struct.pack("!I", len(meta_pickled)))
                await writer.drain()

                writer.write(meta_pickled)
                await writer.drain()

            case Command.INIT_RESTORE_PROCEDURE:
                timestamp = (await reader.readline()).decode().rstrip("\n")
                logger.info("init restore procedure for %s", timestamp)

                volume_metas = await get_volume_metas(timestamp_filter=timestamp)

                # return the archive
                metas_pickled = pickle.dumps(volume_metas[timestamp])
                writer.write(struct.pack("!I", len(metas_pickled)))
                await writer.drain()

                writer.write(metas_pickled)
                await writer.drain()

                stack_secrets = await get_stack_secrets(timestamp)

                if not stack_secrets:
                    raise RuntimeError(f"No stack secrets found for timestamp {timestamp}")

                meta_pickled = pickle.dumps(stack_secrets)
                writer.write(struct.pack("!I", len(meta_pickled)))
                await writer.drain()

                writer.write(meta_pickled)
                await writer.drain()

            case Command.REQUEST_ARCHIVE:
                # next the client requests the archives which we extract here and pipe via a stream
                # open the extract process and send the stream the output
                request_archive = (await reader.readline()).decode().rstrip("\n")
                request_artifact = (await reader.readline()).decode().rstrip("\n")
                logger.info(
                    "request archive: %s, request artifact: %s",
                    request_archive,
                    request_artifact,
                )

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

                    dbg_chunk_count = 0
                    compressor = zstd.ZstdCompressor(level=1, threads=6).compressobj()
                    while True:
                        chunk = await proc.stdout.read(4 * 1024 * 1024 * 10)  # 4MB
                        if not chunk:
                            break

                        cchunk = compressor.compress(chunk)

                        # debug friendly send trace
                        if cchunk:
                            dbg_chunk_count += 1

                            if dbg_chunk_count % 100 == 0:
                                logger.debug(f"sending chunk size {len(cchunk)}")

                        # compress and send the chunk
                        await send_cchunk(writer, reader, cchunk)

                    # send the rest in the compressor
                    await send_cchunk(writer, reader, compressor.flush())

                    logger.info("finished extracting, sending eof")
                    writer.write(struct.pack("!I", 0))
                    await writer.drain()

    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as e:
        logger.warning("Client disconnected: %s", e, exc_info=True)
    finally:
        writer.close()
        # dont await to avoid race conditions


async def run():
    await init_db()
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
    if ENV == "PRODUCTION":
        copy_backup_generic()

    asyncio.run(run())
