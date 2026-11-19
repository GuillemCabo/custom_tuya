"""Read Xiongmai (iCSee/XMEye) recording files (.h264 on the SD card, often H.265 inside).

The file is a sequence of frames, each behind a small header starting 00 00 01 Fx:
  FC  video keyframe  16-byte header: codec, fps, w/8, h/8, packed date-time, length
  FD  video frame      8-byte header: length
  FA  audio            8-byte header: codec, rate, 2-byte length
  F9  info             8-byte header: type, -, 2-byte length
"""
import struct
from datetime import datetime


def unpack_time(v):
    return datetime(2000 + (v >> 26 & 0x3F), v >> 22 & 0x0F, v >> 17 & 0x1F, v >> 12 & 0x1F, v >> 6 & 0x3F, v & 0x3F)


def frames(data):
    """Yield (kind, time or None, payload) with kind 'key', 'frame', 'audio' or 'info'."""
    i, n = 0, len(data)
    while i + 8 <= n:
        if data[i:i + 3] != b"\x00\x00\x01" or data[i + 3] not in (0xF9, 0xFA, 0xFC, 0xFD):
            j = data.find(b"\x00\x00\x01", i + 1)          # resync
            if j < 0:
                return
            i = j
            continue
        t = data[i + 3]
        if t == 0xFC:
            if i + 16 > n:
                return
            stamp, length = struct.unpack_from("<II", data, i + 8)
            try:
                when = unpack_time(stamp)
            except ValueError:
                when = None
            yield "key", when, data[i + 16:i + 16 + length]
            i += 16 + length
        elif t == 0xFD:
            (length,) = struct.unpack_from("<I", data, i + 4)
            yield "frame", None, data[i + 8:i + 8 + length]
            i += 8 + length
        else:
            (length,) = struct.unpack_from("<H", data, i + 6)
            yield ("audio" if t == 0xFA else "info"), None, data[i + 8:i + 8 + length]
            i += 8 + length


def keyframes(data):
    """Decode only the keyframes: yields (time, BGR image). Fast: no inter-frame decoding."""
    import av
    codec = av.CodecContext.create("hevc", "r")
    codec.thread_type = "AUTO"
    stamps = []   # the decoder may hold frames back: hand out times in order
    for kind, when, payload in frames(data):
        if kind != "key":
            continue
        stamps.append(when)
        for fr in codec.decode(av.Packet(payload)):
            yield stamps.pop(0), fr.to_ndarray(format="bgr24")
    for fr in codec.decode(None):
        yield (stamps.pop(0) if stamps else None), fr.to_ndarray(format="bgr24")
