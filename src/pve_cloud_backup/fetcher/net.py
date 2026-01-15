import asyncio
import logging
import pickle
import struct

import zstandard as zstd

from pve_cloud_backup.daemon.rpc import Command

logger = logging.getLogger("fetcher")


async def archive_init(reader, writer, request_dict):
    # intialize archive command
    writer.write(struct.pack("B", Command.ARCHIVE.value))
    await writer.drain()
    logger.debug("send command")

    # send the archive request dict
    req_dict_pickled = pickle.dumps(request_dict)
    writer.write(struct.pack("!I", len(req_dict_pickled)))
    await writer.drain()
    writer.write(req_dict_pickled)
    await writer.drain()
    logger.debug("send dict")

    # wait for go signal, server needs to aquire write lock
    # we dont
    logger.debug("waiting for go from bdd")
    signal = await reader.readexactly(1)
    if signal != b"\x01":
        logger.error("recieved incorrect go signal")
        raise Exception("Incorrect go signal!")
    logger.debug("received go")


async def send_cchunk(writer, compressed_chunk):
    # compress the chunk
    if compressed_chunk:  # only send if something actually got compressed
        # send size + chunk
        writer.write(struct.pack("!I", len(compressed_chunk)))
        await writer.drain()
        writer.write(compressed_chunk)
        await writer.drain()


async def archive_async(backup_addr, request_dict, chunk_generator):
    logger.info(request_dict)
    reader, writer = await asyncio.open_connection(backup_addr, 8085)

    await archive_init(reader, writer, request_dict)

    # initialize the synchronous generator and start reading chunks, compress and send
    # compressor = zlib.compressobj(level=1)
    compressor = zstd.ZstdCompressor(level=1, threads=6).compressobj()
    async for chunk in chunk_generator():
        await send_cchunk(writer, compressor.compress(chunk))

    # send rest in compressor, compress doesnt always return a byte array, see bdd.py doc
    # send size first again
    await send_cchunk(writer, compressor.flush())

    # send eof to server, signal that we are done
    logger.debug("sending eof")
    writer.write(struct.pack("!I", 0))
    await writer.drain()

    # close the writer here, stdout needs to be closed by caller
    writer.close()
    await writer.wait_closed()


async def archive(backup_addr, request_dict, chunk_generator):
    logger.info(request_dict)
    reader, writer = await asyncio.open_connection(backup_addr, 8085)

    await archive_init(reader, writer, request_dict)

    # initialize the synchronous generator and start reading chunks, compress and send
    # compressor = zlib.compressobj(level=1)
    compressor = zstd.ZstdCompressor(level=1, threads=6).compressobj()
    for chunk in chunk_generator():
        await send_cchunk(writer, compressor.compress(chunk))

    # send rest in compressor, compress doesnt always return a byte array, see bdd.py doc
    # send size first again
    await send_cchunk(writer, compressor.flush())

    # send eof to server, signal that we are done
    logger.debug("sending eof")
    writer.write(struct.pack("!I", 0))
    await writer.drain()

    # close the writer here, stdout needs to be closed by caller
    writer.close()
    await writer.wait_closed()


async def meta(backup_addr, cmd, meta_dict):
    reader, writer = await asyncio.open_connection(backup_addr, 8085)
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
    await writer.wait_closed()


async def volume_meta(backup_addr, meta_dict):
    await meta(backup_addr, Command.VOLUME_META, meta_dict)


async def namespace_secrets(backup_addr, meta_dict):
    await meta(backup_addr, Command.NAMESPACE_SECRETS, meta_dict)
