import asyncio
import logging
import os
import pickle
import ssl
import struct
import inspect

import socketio
import zstandard as zstd
from pve_cloud.lib.backup_rpc import Command

logger = logging.getLogger("fetcher")

SIO_MAX_RETRIES = 3


def get_strict_client_ssl_ctx():
    ca_cert_path = os.getenv("BDD_CA_CERT_PATH")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(ca_cert_path)
    ctx.verify_mode = ssl.CERT_REQUIRED

    return ctx


async def archive_init(reader, writer, request_dict):
    # intialize archive command
    writer.write(struct.pack("B", Command.ARCHIVE.value))
    await writer.drain()

    # send the archive request dict
    req_dict_pickled = pickle.dumps(request_dict)
    writer.write(struct.pack("!I", len(req_dict_pickled)))
    await writer.drain()
    writer.write(req_dict_pickled)
    await writer.drain()

    # wait for go signal, server needs to aquire write lock
    # we dont
    logger.debug("waiting for go from bdd")
    while True:
        signal = await reader.readexactly(1)
        if signal == b"\x02":
            logger.debug("waiting for lock continues...")
            continue

        if signal != b"\x01":
            logger.error("recieved incorrect go signal")
            raise Exception("Incorrect go signal!")
        else:
            logger.debug("received go")
            break


# generic send and ack function
async def send_cchunk(writer, reader, compressed_chunk):
    if compressed_chunk:  # only send if something actually got compressed
        # send size + chunk
        writer.write(struct.pack("!I", len(compressed_chunk)))
        await writer.drain()
        writer.write(compressed_chunk)
        await writer.drain()

        ack = await reader.readexactly(1)
        if ack != b"\x01":
            raise RuntimeError("Expected x01 ack byte!")


async def get_sio_mc_client(backup_addr):
    if not os.getenv("MC_EXT_TOKEN"):
        raise RuntimeError(
            "Tried to initialize multicloud proxy without providing MC_EXT_TOKEN env var!"
        )

    sio = socketio.AsyncClient()
    await sio.connect(
        backup_addr,
        auth={
            "token": os.getenv("MC_EXT_TOKEN"),
            "bdd_stack_name": os.getenv("BDD_STACK_NAME"),
        },
        transports=["websocket"],
    )

    logger.debug(f"Connected sio client to {backup_addr}")

    return sio


# sio framework takes care of acking messages
async def sio_send_cchunk(sio, compressed_chunk):
    if compressed_chunk:
        await sio.call("backup_chunk", compressed_chunk)


async def wait_archive_init(sio, request_dict):
    # init archive request, this doesn't necessarily immediatly
    # lock and ready the server for writing
    initial = await sio.call(
        "archive_init",
        request_dict,
        timeout=30,
    )
    logger.debug("received init response: %s", initial)

    if initial["status"] == "ERR":
        raise RuntimeError(initial["error"])

    if initial["status"] == "ACQUIRED":
        return # server acquired lock for backup repo

    # ==> status WAIT
    logger.info("waiting for lock...")
    while True:
        wait_call = await sio.call("wait_archive", timeout=30)
        logger.debug("wait archive response: %s", wait_call)

        if wait_call["status"] == "ERR":
            raise RuntimeError(initial["error"])

        if wait_call["status"] == "ACQUIRED":
            return


# compress parameter exists for chunk generators that already do the compression
# the receiving side ALWAYS expects a compressed stream
async def archive(backup_addr, request_dict, chunk_generator, compress=True):
    logger.info("sending archive request: %s", request_dict)
    logger.debug("generator async: %s", inspect.isasyncgen(chunk_generator))

    # we assume that we are sending to a multicloud gateway if the addr starts with https://
    # direct connects via tcp are if the backup_addr is a hostname / ip address
    if backup_addr.startswith("https://"):
        logger.info(f"sending archive to mc gateway {backup_addr}")

        for attempt in range(1, SIO_MAX_RETRIES + 1):
            sio = None
            try:
                # connection to mc gw
                sio = await get_sio_mc_client(backup_addr)

                await wait_archive_init(sio, request_dict)

                if compress:
                    compressor = zstd.ZstdCompressor(
                        level=1,
                        threads=6,
                    ).compressobj()

                    if inspect.isasyncgen(chunk_generator):
                        async for chunk in chunk_generator():
                            await sio_send_cchunk(sio, compressor.compress(chunk))
                    else:
                        for chunk in chunk_generator():
                            await sio_send_cchunk(sio, compressor.compress(chunk))

                    await sio_send_cchunk(sio, compressor.flush())

                else:
                    if inspect.isasyncgen(chunk_generator):
                        async for chunk in chunk_generator():
                            await sio.call("backup_chunk", chunk)
                    else:
                        for chunk in chunk_generator():
                            await sio.call("backup_chunk", chunk)

                # signal the server that we are done
                await sio.call("backup_eof")

                break  # finished successfully, break try loop

            except socketio.exceptions.TimeoutError:
                logger.warn(f"Error on attempt {attempt}")
                if attempt == SIO_MAX_RETRIES:
                    raise

                logger.info("Retrying...")
                await asyncio.sleep(10)

            finally:
                # always close the sio connection
                if sio:
                    await sio.disconnect()

    else:
        # direct connection to backup server
        reader, writer = await asyncio.open_connection(
            backup_addr, 8085, ssl=get_strict_client_ssl_ctx()
        )

        await archive_init(reader, writer, request_dict)

        # initialize the synchronous generator and start reading chunks, compress and send
        # compressor = zlib.compressobj(level=1)
        if compress:
            compressor = zstd.ZstdCompressor(level=1, threads=6).compressobj()
            if inspect.isasyncgen(chunk_generator):
                async for chunk in chunk_generator():
                    await send_cchunk(writer, reader, compressor.compress(chunk))
            else:
                for chunk in chunk_generator():
                    await send_cchunk(writer, reader, compressor.compress(chunk))

            # send rest in compressor, compress doesnt always return a byte array, see bdd.py doc
            # send size first again
            await send_cchunk(writer, reader, compressor.flush())

        else:
            if inspect.isasyncgen(chunk_generator):
                async for chunk in chunk_generator():
                    writer.write(struct.pack("!I", len(chunk)))
                    await writer.drain()
                    writer.write(chunk)
                    await writer.drain()
            else:
                for chunk in chunk_generator():
                    writer.write(struct.pack("!I", len(chunk)))
                    await writer.drain()
                    writer.write(chunk)
                    await writer.drain()

        # send eof to server, signal that we are done
        logger.debug("sending eof")
        writer.write(struct.pack("!I", 0))
        await writer.drain()

        # close the writer here, stdout needs to be closed by caller
        writer.close()


async def meta(backup_addr, cmd, meta_dict):
    if backup_addr.startswith("https://"):
        sio = await get_sio_mc_client(backup_addr)

        await sio.call("bdd_meta", {"command": cmd.value, "meta_dict": meta_dict})

        await sio.disconnect()

    else:
        reader, writer = await asyncio.open_connection(
            backup_addr, 8085, ssl=get_strict_client_ssl_ctx()
        )
        writer.write(struct.pack("B", cmd.value))
        await writer.drain()

        meta_pickled = pickle.dumps(meta_dict)

        # send size first
        writer.write(struct.pack("!I", len(meta_pickled)))
        await writer.drain()

        # now send the dict
        writer.write(meta_pickled)
        await writer.drain()

        writer.close()


async def volume_meta(backup_addr, meta_dict):
    await meta(backup_addr, Command.VOLUME_META, meta_dict)


async def namespace_secrets(backup_addr, meta_dict):
    await meta(backup_addr, Command.NAMESPACE_SECRETS, meta_dict)
