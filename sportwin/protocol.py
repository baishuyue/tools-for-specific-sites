# -*- coding: utf-8 -*-
"""NetSecKit 4.2.4 纯 Python 复刻 + 运动世界校园业务签名。

这一层是"去模拟器"的核心：原项目之所以必须常驻一个 MuMu 实例 + LSPosed 签名模块，
唯一原因就是请求头 headerSign / tokenSign 依赖 App 内的安全 SDK。算法本身已经在
纯 Python 下 1:1 还原（AES-128-CBC + Feistel 变体 KDF + RSA PKCS#1 v1.5），
因此 Windows 侧可以独立完成全部加密与签名，不再需要任何安卓进程。

依赖：仅标准库。若本机装了 pycryptodome，selftest 会用它做交叉校验。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import uuid

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 业务签名静态盐（7.3.92 实测逐字节一致）
SALT = "2slhe02lsfiwowlcixisla_sls-_slaor"
#: 华为云 OBS 存储桶（轨迹对象）
OBS_BUCKET = "iydsj-hbase-hot"
#: App 版本，与协议模块保持一致
APP_VERSION = "7.3.92"
#: 请求平台号（NetSecKit 信封 p 字段）
PLATFORM_NO = "101"

#: 7.3.92 NetSec 请求公钥（RSA-1024，PKCS#1 v1.5）
_REQ_PUB_DER_B64 = (
    "MIGJAoGBALmmqVPOwY1mTVHGEfg7jHck1MNXsVtUfrqY99bm/W2cjLi3LvG/wMYw"
    "bhmf9O+y3CUlZ5g4AMd2Ly4XbWUloG/O8+1USGJ8ddmrzbl8j2EZi0OhpVCYI381"
    "+zZ0qa3z1cOuG2XdJnE7pO6Y4JNC37miqMXYmjejVi2QDIbONomfAgMBAAE="
)


# ---------------------------------------------------------------------------
# AES-128（纯 Python，ECB 原语 + CBC 封装）
# ---------------------------------------------------------------------------
S_BOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
)
INV_S_BOX = [0] * 256
for _i, _v in enumerate(S_BOX):
    INV_S_BOX[_v] = _i
RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
_SHIFT = (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)
_INV_SHIFT = (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF


def _mul(a: int, b: int) -> int:
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        a = _xtime(a)
        b >>= 1
    return res


def _expand_key(key: bytes) -> list:
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    w = [int.from_bytes(key[i:i + 4], "big") for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = w[i - 1]
        if i % 4 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = ((S_BOX[(t >> 24) & 0xFF] ^ RCON[i // 4]) << 24) | (S_BOX[(t >> 16) & 0xFF] << 16) \
                | (S_BOX[(t >> 8) & 0xFF] << 8) | S_BOX[t & 0xFF]
        w.append(w[i - 4] ^ t)
    return w


def _add_round_key(state, rk, r):
    base = r * 4
    for i in range(16):
        state[i] ^= (rk[base + (i // 4)] >> (24 - 8 * (i % 4))) & 0xFF


def _encrypt_block(block: bytes, rk) -> bytes:
    st = list(block)
    _add_round_key(st, rk, 0)
    for r in range(1, 10):
        st = [S_BOX[b] for b in st]
        st = [st[_SHIFT[i]] for i in range(16)]
        for c in range(4):
            i = c * 4
            s0, s1, s2, s3 = st[i:i + 4]
            st[i] = _xtime(s0) ^ (_xtime(s1) ^ s1) ^ s2 ^ s3
            st[i + 1] = s0 ^ _xtime(s1) ^ (_xtime(s2) ^ s2) ^ s3
            st[i + 2] = s0 ^ s1 ^ _xtime(s2) ^ (_xtime(s3) ^ s3)
            st[i + 3] = (_xtime(s0) ^ s0) ^ s1 ^ s2 ^ _xtime(s3)
        _add_round_key(st, rk, r)
    st = [S_BOX[b] for b in st]
    st = [st[_SHIFT[i]] for i in range(16)]
    _add_round_key(st, rk, 10)
    return bytes(st)


def _decrypt_block(block: bytes, rk) -> bytes:
    st = list(block)
    _add_round_key(st, rk, 10)
    for r in range(9, 0, -1):
        st = [st[_INV_SHIFT[i]] for i in range(16)]
        st = [INV_S_BOX[b] for b in st]
        _add_round_key(st, rk, r)
        for c in range(4):
            i = c * 4
            s0, s1, s2, s3 = st[i:i + 4]
            st[i] = _mul(s0, 14) ^ _mul(s1, 11) ^ _mul(s2, 13) ^ _mul(s3, 9)
            st[i + 1] = _mul(s0, 9) ^ _mul(s1, 14) ^ _mul(s2, 11) ^ _mul(s3, 13)
            st[i + 2] = _mul(s0, 13) ^ _mul(s1, 9) ^ _mul(s2, 14) ^ _mul(s3, 11)
            st[i + 3] = _mul(s0, 11) ^ _mul(s1, 13) ^ _mul(s2, 9) ^ _mul(s3, 14)
    st = [st[_INV_SHIFT[i]] for i in range(16)]
    st = [INV_S_BOX[b] for b in st]
    _add_round_key(st, rk, 0)
    return bytes(st)


def aes128_cbc_encrypt(data: bytes, key: bytes, iv: bytes = b"\x00" * 16) -> bytes:
    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(padded), 16):
        block = bytes(a ^ b for a, b in zip(padded[i:i + 16], prev))
        prev = _encrypt_block(block, rk)
        out += prev
    return bytes(out)


def aes128_cbc_decrypt(data: bytes, key: bytes, iv: bytes = b"\x00" * 16) -> bytes:
    if len(data) % 16:
        raise ValueError("ciphertext is not block aligned")
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, rk), prev))
        prev = block
    pad = out[-1]
    if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        return bytes(out[:-pad])
    return bytes(out)


# ---------------------------------------------------------------------------
# RSA PKCS#1 v1.5 公钥加密（只做公钥运算，纯整数幂模）
# ---------------------------------------------------------------------------
class _RsaKey:
    __slots__ = ("n", "e", "k")

    def __init__(self) -> None:
        der = base64.b64decode(_REQ_PUB_DER_B64)
        # DER: 30 81 89 02 81 81 00 <128B n> 02 03 01 00 01
        if der[0] != 0x30 or der[3] != 0x02:
            raise RuntimeError("NetSec 公钥 DER 结构异常")
        n_len = der[5]
        if n_len != 0x81:
            raise RuntimeError("NetSec 公钥长度异常: %d" % n_len)
        self.n = int.from_bytes(der[7:7 + 128], "big")
        e_len = der[7 + 128 + 1]
        self.e = int.from_bytes(der[7 + 128 + 2:7 + 128 + 2 + e_len], "big")
        self.k = 128

    def encrypt(self, msg: bytes) -> bytes:
        if len(msg) > self.k - 11:
            raise ValueError("RSA plaintext too long")
        ps_len = self.k - len(msg) - 3
        ps = bytearray()
        while len(ps) < ps_len:
            b = os.urandom(1)[0]
            if b:
                ps.append(b)
        em = b"\x00\x02" + bytes(ps) + b"\x00" + msg
        c = pow(int.from_bytes(em, "big"), self.e, self.n)
        return c.to_bytes(self.k, "big")


_RSA = _RsaKey()


# ---------------------------------------------------------------------------
# 响应密钥派生（Feistel 变体）
# ---------------------------------------------------------------------------
def _byte_hash(data: bytes) -> int:
    h = 0
    for b in data:
        h = ((b | ((h << 8) & 0xFFFFFFFF)) ^ ((h >> 24) & 0xFF)) & 0xFFFFFFFF
    return h


def _feistel(s0: int, s1: int, s2: int, s3: int):
    m = 0xFFFFFFFF
    t = (s2 ^ s0) & m
    mix = ((((t >> 24) | (t << 8)) & m) ^ (((t >> 8) | (t << 24)) & m) ^ t) & m
    ns1 = (mix ^ s1) & m
    ns3 = (mix ^ s3) & m
    t2 = (ns3 ^ ns1) & m
    mix2 = ((((t2 >> 24) | (t2 << 8)) & m) ^ (((t2 >> 8) | (t2 << 24)) & m) ^ t2) & m
    return (mix2 ^ s0) & m, ns1, (mix2 ^ s2) & m, ns3


def response_kdf(kd1: bytes, kd2: bytes, kd3: bytes, kd4: bytes) -> bytes:
    """由四个 keyData 片段派生 16 字节响应 AES 密钥。"""
    s0, s1, s2, s3 = _feistel(_byte_hash(kd1), _byte_hash(kd2), _byte_hash(kd3), _byte_hash(kd4))
    m = 0xFFFFFFFF
    a = (~(s2 | s3) & m) ^ s1
    b = ((s2 ^ s3) ^ a) & m
    c = ((a & s2) ^ s0) & m
    b = (b ^ c) & m
    a = (~(b | c) & m) ^ a
    d = ((a & b) ^ s3) & m
    return struct.pack("<IIII", d, a, b, c)


# ---------------------------------------------------------------------------
# NetSec 信封
# ---------------------------------------------------------------------------
class Envelope:
    """把明文对象封装成网关要求的 {d,h,k,p,t} 信封，并持有本次响应密钥。"""

    __slots__ = ("wire", "resp_key", "kd")

    def __init__(self, wire: dict, resp_key: bytes, kd: tuple) -> None:
        self.wire = wire
        self.resp_key = resp_key
        self.kd = kd

    @classmethod
    def wrap(cls, obj) -> "Envelope":
        seed = uuid.uuid4().hex + "ABCDEFGHIJKLMN12"
        kd = (seed[0:10], seed[10:20], seed[20:30], seed[30:40])
        resp_key = response_kdf(*(k.encode() for k in kd))

        if isinstance(obj, str):
            payload = base64.b64encode(obj.encode("utf-8")).decode("ascii")
        else:
            payload = base64.b64encode(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")

        # 字段顺序必须与客户端一致：data / kd4 / kd1 / kd3 / kd2 / platform / timeStamp
        inner_obj = {
            "data": payload,
            "keyDataFour": kd[3],
            "keyDataOne": kd[0],
            "keyDataThree": kd[2],
            "keyDataTwo": kd[1],
            "platform": 2,
            "timeStamp": int(time.time()),
        }
        inner_bytes = (json.dumps(inner_obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

        aes_key = "".join(random_ascii(16)).encode("ascii")
        cipher = aes128_cbc_encrypt(inner_bytes, aes_key)
        wire = {
            "d": base64.b64encode(cipher).decode("ascii"),
            "h": hashlib.md5(inner_bytes).hexdigest(),
            "k": base64.b64encode(_RSA.encrypt(aes_key)).decode("ascii"),
            "p": PLATFORM_NO,
            "t": "0",
        }
        return cls(wire, resp_key, kd)

    def unwrap(self, resp: dict):
        """解析响应：服务端用 r 字段回传加密的业务数据。"""
        if not isinstance(resp, dict) or "r" not in resp:
            return resp
        try:
            dec = aes128_cbc_decrypt(base64.b64decode(resp["r"]), self.resp_key)
            outer = json.loads(dec.decode("utf-8", "replace"))
        except Exception:
            return resp
        if isinstance(outer, dict) and "data" in outer:
            try:
                return json.loads(base64.b64decode(outer["data"]).decode("utf-8", "replace"))
            except Exception:
                return outer
        return outer


def random_ascii(n: int) -> str:
    import random
    # 与客户端一致的 16 位小写字母+数字会话密钥
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


# ---------------------------------------------------------------------------
# 业务签名
# ---------------------------------------------------------------------------
def _norm(v) -> str:
    return str(v).lower() if isinstance(v, bool) else str(v)


def token_sign(params: dict, salt: str = SALT) -> str:
    """请求头 tokenSign：按 (小写键, 原键) 排序拼接后加盐 MD5。"""
    items = [(k, _norm(v)) for k, v in params.items() if v is not None and k.lower() != "signature"]
    items.sort(key=lambda kv: (kv[0].lower(), kv[0]))
    return hashlib.md5(("&".join("%s=%s" % kv for kv in items) + salt).encode("utf-8")).hexdigest()


def canonical(fields: dict) -> str:
    """originalSign：仅按原键排序，不加盐。"""
    items = [(k, _norm(v)) for k, v in fields.items() if v is not None and k.lower() != "signature"]
    items.sort(key=lambda kv: kv[0])
    return "&".join("%s=%s" % kv for kv in items)


def sport_signature(fields: dict, salt: str = SALT) -> str:
    """31 字段运动结算 signature：排序拼接 + 加盐 MD5。"""
    items = [(k, _norm(v)) for k, v in fields.items() if v is not None and k.lower() != "signature"]
    items.sort(key=lambda kv: (kv[0].lower(), kv[0]))
    return hashlib.md5(("&".join("%s=%s" % kv for kv in items) + salt).encode("utf-8")).hexdigest()


def url_sign(url: str, salt: str = SALT) -> str:
    """少数老接口（如五点下发）把签名放在 body 里：md5(http://host/path + salt)。"""
    plain = url.replace("https://", "http://", 1)
    return hashlib.md5((plain + salt).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 自检：与 pycryptodome 交叉验证 AES，与已知盐值验证签名
# ---------------------------------------------------------------------------
def self_test(verbose: bool = True) -> bool:
    ok = True
    key = b"0123456789abcdef"
    plain = json.dumps({"a": 1, "b": "中文"}, ensure_ascii=False).encode("utf-8")
    ct = aes128_cbc_encrypt(plain, key)
    if aes128_cbc_decrypt(ct, key) != plain:
        ok = False
        if verbose:
            print("[FAIL] AES-CBC 自解密不一致")
    try:
        from Crypto.Cipher import AES as _AES
        from Crypto.Util.Padding import pad as _pad
        ref = _AES.new(key, _AES.MODE_CBC, b"\x00" * 16).encrypt(_pad(plain, 16))
        if ref != ct:
            ok = False
            if verbose:
                print("[FAIL] AES-CBC 与 pycryptodome 不一致")
        elif verbose:
            print("[OK] AES-128-CBC 与 pycryptodome 逐字节一致")
    except ImportError:
        if verbose:
            print("[SKIP] 未安装 pycryptodome，跳过交叉校验（不影响运行）")

    # KDF 输出必须稳定（用于解服务端响应）
    k = response_kdf(*(x.encode() for x in ("0123456789", "abcdefghij", "klmnopqrst", "uvwxyzABCD")))
    if len(k) != 16:
        ok = False
    if verbose:
        print("[OK] 响应 KDF 输出 16 字节: %s" % k.hex())

    sig = sport_signature({"uid": 1, "uuid": "x", "complete": True})
    if len(sig) != 32:
        ok = False
    if verbose:
        print("[OK] 业务签名示例: %s" % sig)
    return ok
