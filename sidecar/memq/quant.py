from __future__ import annotations

import math
from hashlib import blake2b
from typing import Iterable, List

import numpy as np


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n <= 1e-12:
        return v.astype(np.float32)
    return (v / n).astype(np.float32)


def embed_text(text: str, dim: int) -> np.ndarray:
    # Deterministic local embedding to avoid API dependency in sidecar core.
    vec = np.zeros((dim,), dtype=np.float32)
    tokens = [t for t in text.lower().replace("\n", " ").split(" ") if t]
    if not tokens:
        return vec
    for tok in tokens[:2048]:
        h = blake2b(tok.encode("utf-8", errors="ignore"), digest_size=16).digest()
        i1 = int.from_bytes(h[:4], "big") % dim
        i2 = int.from_bytes(h[4:8], "big") % dim
        s1 = 1.0 if (h[8] & 1) else -1.0
        s2 = 1.0 if (h[9] & 1) else -1.0
        vec[i1] += s1
        vec[i2] += 0.5 * s2
    return normalize(vec)


def quantize(vec: np.ndarray, bits: int = 8) -> bytes:
    bits = 8 if bits not in {6, 7, 8} else bits
    v = np.clip(vec.astype(np.float32), -1.0, 1.0)
    qmax = (1 << bits) - 1
    q = np.rint((v + 1.0) * 0.5 * qmax).astype(np.uint8)
    if bits == 8:
        return q.tobytes()
    return pack_bits(q.tolist(), bits)


def dequantize(blob: bytes, dim: int, bits: int = 8) -> np.ndarray:
    bits = 8 if bits not in {6, 7, 8} else bits
    qmax = (1 << bits) - 1
    if bits == 8:
        arr = np.frombuffer(blob, dtype=np.uint8, count=dim)
    else:
        vals = unpack_bits(blob, bits, dim)
        arr = np.asarray(vals, dtype=np.uint8)
    v = (arr.astype(np.float32) / float(qmax)) * 2.0 - 1.0
    return normalize(v)


def pack_bits(values: List[int], bits: int) -> bytes:
    out = bytearray()
    acc = 0
    acc_bits = 0
    mask = (1 << bits) - 1
    for value in values:
        v = int(value) & mask
        acc = (acc << bits) | v
        acc_bits += bits
        while acc_bits >= 8:
            shift = acc_bits - 8
            out.append((acc >> shift) & 0xFF)
            acc &= (1 << shift) - 1
            acc_bits = shift
    if acc_bits > 0:
        out.append((acc << (8 - acc_bits)) & 0xFF)
    return bytes(out)


def unpack_bits(blob: bytes, bits: int, count: int) -> List[int]:
    out: List[int] = []
    acc = 0
    acc_bits = 0
    mask = (1 << bits) - 1
    for b in blob:
        acc = (acc << 8) | int(b)
        acc_bits += 8
        while acc_bits >= bits and len(out) < count:
            shift = acc_bits - bits
            out.append((acc >> shift) & mask)
            acc &= (1 << shift) - 1
            acc_bits = shift
        if len(out) >= count:
            break
    if len(out) < count:
        out.extend([0] * (count - len(out)))
    return out


def dot(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a.astype(np.float32), b.astype(np.float32)))


def f16_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float16).tobytes()


def from_f16_blob(blob: bytes, dim: int) -> np.ndarray:
    arr = np.frombuffer(blob, dtype=np.float16, count=dim).astype(np.float32)
    return normalize(arr)
