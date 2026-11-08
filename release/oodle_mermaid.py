from __future__ import annotations

BLOCK_SIZE = 0x40000
SUBCHUNK_SIZE = 0x20000
LZ_HALF = 0x10000


class OodleError(Exception):
    pass


class _MsbReader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def peek(self, n: int) -> int:
        if n == 0:
            return 0
        byte = self.pos >> 3
        chunk = self.data[byte:byte + 6]
        if len(chunk) < 6:
            chunk = chunk + b"\0" * (6 - len(chunk))
        value = int.from_bytes(chunk, "big")
        return (value >> (48 - (self.pos & 7) - n)) & ((1 << n) - 1)

    def read(self, n: int) -> int:
        value = self.peek(n)
        self.pos += n
        return value

    def clz32(self) -> int:
        return 32 - self.peek(32).bit_length()

    def rice_lengths(self, count: int) -> list[int]:
        data, pos, end = self.data, self.pos, len(self.data) * 8
        out = []
        for _ in range(count):
            zeros = 0
            while True:
                if pos >= end:
                    raise OodleError("rice code ran past end of data")
                bit = (data[pos >> 3] >> (7 - (pos & 7))) & 1
                pos += 1
                if bit:
                    break
                zeros += 1
            out.append(zeros)
        self.pos = pos
        return out

    def bytes_used(self) -> int:
        return (self.pos + 7) >> 3


def _bsr(x: int) -> int:
    return x.bit_length() - 1


def _read_fluff(br: _MsbReader, num_symbols: int) -> int:
    if num_symbols == 256:
        return 0
    x = 257 - num_symbols
    if x > num_symbols:
        x = num_symbols
    x *= 2
    y = (x - 1).bit_length()
    v = br.peek(y)
    z = (1 << y) - x
    if (v >> 1) >= z:
        br.pos += y
        return v - z
    br.pos += y - 1
    return v >> 1


def _convert_to_ranges(br: _MsbReader, num_symbols: int, p: int, symlen: list[int]):
    num_ranges = p >> 1
    si = 0
    sym_idx = 0
    if p & 1:
        v = symlen[si]
        si += 1
        if v >= 8:
            raise OodleError("bad range header")
        sym_idx = br.read(v + 1) + (1 << (v + 1)) - 1
    syms_used = 0
    ranges = []
    for _ in range(num_ranges):
        v = symlen[si]
        if v >= 9:
            raise OodleError("bad range count")
        num = br.read(v) + (1 << v)
        v = symlen[si + 1]
        if v >= 8:
            raise OodleError("bad range space")
        space = br.read(v + 1) + (1 << (v + 1)) - 1
        ranges.append((sym_idx, num))
        syms_used += num
        sym_idx += num + space
        si += 2
    if sym_idx >= 256 or syms_used >= num_symbols or sym_idx + num_symbols - syms_used > 256:
        raise OodleError("range table out of bounds")
    ranges.append((sym_idx, num_symbols - syms_used))
    return ranges


def _huff_lengths_old(br: _MsbReader, lists: list[list[int]]) -> int:
    if br.read(1):
        sym = 0
        num_symbols = 0
        avg_bits_x4 = 32
        forced_bits = br.read(2)
        thres = 1 << (31 - (20 >> forced_bits))
        skip_zeros = bool(br.read(1))
        while True:
            if not skip_zeros:
                if not (br.peek(32) & 0xFF000000):
                    raise OodleError("huffman: bad zero run")
                sym += br.read(2 * (br.clz32() + 1)) - 2 + 1
                if sym >= 256:
                    break
            skip_zeros = False
            if not (br.peek(32) & 0xFF000000):
                raise OodleError("huffman: bad symbol count")
            n = br.read(2 * (br.clz32() + 1)) - 2 + 1
            if sym + n > 256:
                raise OodleError("huffman: symbol overflow")
            num_symbols += n
            for _ in range(n):
                if br.peek(32) < thres:
                    raise OodleError("huffman: gamma too large")
                lz = br.clz32()
                v = br.read(lz + forced_bits + 1) + ((lz - 1) << forced_bits)
                codelen = (-(v & 1) ^ (v >> 1)) + ((avg_bits_x4 + 2) >> 2)
                if codelen < 1 or codelen > 11:
                    raise OodleError("huffman: bad code length")
                avg_bits_x4 = codelen + ((3 * avg_bits_x4 + 2) >> 2)
                lists[codelen].append(sym)
                sym += 1
            if sym == 256:
                break
        if sym != 256 or num_symbols < 2:
            raise OodleError("huffman: bad symbol total")
        return num_symbols
    num_symbols = br.read(8)
    if num_symbols == 0:
        raise OodleError("huffman: no symbols")
    if num_symbols == 1:
        lists[0].append(br.read(8))
        return 1
    codelen_bits = br.read(3)
    if codelen_bits > 4:
        raise OodleError("huffman: bad codelen bits")
    for _ in range(num_symbols):
        sym = br.read(8)
        codelen = br.read(codelen_bits) + 1
        if codelen > 11:
            raise OodleError("huffman: bad code length")
        lists[codelen].append(sym)
    return num_symbols


def _huff_lengths_new(br: _MsbReader, lists: list[list[int]]) -> int:
    forced_bits = br.read(2)
    num_symbols = br.read(8) + 1
    fluff = _read_fluff(br, num_symbols)
    code_len = br.rice_lengths(num_symbols + fluff)
    code_len.extend([0] * 16)
    if forced_bits:
        for i in range(num_symbols):
            code_len[i] = ((code_len[i] << forced_bits) + br.read(forced_bits)) & 0xFF
    running_sum = 0x1E
    for i in range(num_symbols):
        v = code_len[i]
        v = -(v & 1) ^ (v >> 1)
        length = v + (running_sum >> 2) + 1
        if length < 1 or length > 11 or running_sum < 0:
            raise OodleError("huffman: bad code length")
        code_len[i] = length
        running_sum += v
    ranges = _convert_to_ranges(br, num_symbols, fluff, code_len[num_symbols:])
    cp = 0
    for first, count in ranges:
        sym = first
        for _ in range(count):
            lists[code_len[cp]].append(sym)
            cp += 1
            sym += 1
    return num_symbols


def _reverse11(i: int) -> int:
    return int(format(i, "011b")[::-1], 2)

_REV11 = [_reverse11(i) for i in range(2048)]


def _huff_build_lut(lists: list[list[int]]):
    blen = bytearray(2048)
    bsym = bytearray(2048)
    cur = 0
    for length in range(1, 11):
        step = 1 << (11 - length)
        for s in lists[length]:
            if cur + step > 2048:
                raise OodleError("huffman: table overflow")
            blen[cur:cur + step] = bytes([length]) * step
            bsym[cur:cur + step] = bytes([s]) * step
            cur += step
    for s in lists[11]:
        if cur + 1 > 2048:
            raise OodleError("huffman: table overflow")
        blen[cur] = 11
        bsym[cur] = s
        cur += 1
    if cur != 2048:
        raise OodleError("huffman: incomplete table")
    rlen = bytearray(2048)
    rsym = bytearray(2048)
    for k in range(2048):
        r = _REV11[k]
        rlen[r] = blen[k]
        rsym[r] = bsym[k]
    return rlen, rsym


def _huff_stream(data: bytes, count: int, rlen, rsym):
    out = bytearray(count)
    acc = nb = idx = 0
    size = len(data)
    for i in range(count):
        if nb < 11:
            while nb < 24:
                if idx < size:
                    acc |= data[idx] << nb
                idx += 1
                nb += 8
        k = acc & 0x7FF
        n = rlen[k]
        out[i] = rsym[k]
        acc >>= n
        nb -= n
    return out, idx - (nb >> 3)


def _huff_three_streams(data: bytes, split_mid: int, count: int, rlen, rsym) -> bytearray:
    if split_mid > len(data):
        raise OodleError("huffman: bad split")
    region_a = data[:split_mid]
    region_me = data[split_mid:]
    n_a = len(range(0, count, 3))
    n_e = len(range(1, count, 3))
    n_m = len(range(2, count, 3))
    out_a, used_a = _huff_stream(region_a, n_a, rlen, rsym)
    out_m, used_m = _huff_stream(region_me, n_m, rlen, rsym)
    out_e, used_e = _huff_stream(region_me[::-1], n_e, rlen, rsym)
    if used_a != len(region_a) or used_m + used_e != len(region_me):
        raise OodleError("huffman: stream length mismatch")
    out = bytearray(count)
    out[0::3] = out_a
    out[1::3] = out_e
    out[2::3] = out_m
    return out


def _decode_huffman(src: bytes, dst_size: int, kind: int) -> bytes:
    br = _MsbReader(src)
    lists: list[list[int]] = [[] for _ in range(12)]
    if not br.read(1):
        num_syms = _huff_lengths_old(br, lists)
    elif not br.read(1):
        num_syms = _huff_lengths_new(br, lists)
    else:
        raise OodleError("huffman: bad header")
    if num_syms < 1:
        raise OodleError("huffman: no symbols")
    if num_syms == 1:
        raise OodleError("huffman: single-symbol tables are not supported")
    rlen, rsym = _huff_build_lut(lists)
    pos = br.bytes_used()
    end = len(src)
    if kind == 1:
        if pos + 3 > end:
            raise OodleError("huffman: truncated")
        split_mid = src[pos] | (src[pos + 1] << 8)
        pos += 2
        return bytes(_huff_three_streams(src[pos:end], split_mid, dst_size, rlen, rsym))
    if pos + 6 > end:
        raise OodleError("huffman: truncated")
    half = (dst_size + 1) >> 1
    split_mid = src[pos] | (src[pos + 1] << 8) | (src[pos + 2] << 16)
    pos += 3
    if split_mid > end - pos:
        raise OodleError("huffman: bad split")
    mid = pos + split_mid
    split_left = src[pos] | (src[pos + 1] << 8)
    pos += 2
    if mid - pos < split_left + 2 or end - mid < 3:
        raise OodleError("huffman: bad left split")
    split_right = src[mid] | (src[mid + 1] << 8)
    if end - (mid + 2) < split_right + 2:
        raise OodleError("huffman: bad right split")
    left = _huff_three_streams(src[pos:mid], split_left, half, rlen, rsym)
    right = _huff_three_streams(src[mid + 2:end], split_right, dst_size - half, rlen, rsym)
    return bytes(left + right)


def _tans_table(br: _MsbReader, l_bits: int):
    a_syms: list[int] = []
    b_syms: list[tuple[int, int]] = []
    if br.read(1):
        q = br.read(3)
        num_symbols = br.read(8) + 1
        if num_symbols < 2:
            raise OodleError("tans: too few symbols")
        fluff = _read_fluff(br, num_symbols)
        rice = br.rice_lengths(fluff + num_symbols)
        rice.extend([0] * 16)
        ranges = _convert_to_ranges(br, num_symbols, fluff, rice[num_symbols:])
        total = 1 << l_bits
        average = 6
        somesum = 0
        ri = 0
        for first, count in ranges:
            symbol = first
            for _ in range(count):
                nextra = q + rice[ri]
                ri += 1
                if nextra > 15:
                    raise OodleError("tans: bad extra bits")
                v = br.read(nextra) + (1 << nextra) - (1 << q)
                average_div4 = average >> 2
                limit = 2 * average_div4
                if v <= limit:
                    v = average_div4 + ((-(v & 1)) ^ (v >> 1))
                if limit > v:
                    limit = v
                v += 1
                average += limit - average_div4
                if v == 1:
                    a_syms.append(symbol)
                elif v >= 2:
                    b_syms.append((symbol, v))
                else:
                    raise OodleError("tans: bad weight")
                somesum += v
                symbol += 1
        if somesum != total:
            raise OodleError("tans: weights do not sum to table size")
        return a_syms, b_syms
    seen = set()
    total = 1 << l_bits
    count = br.read(3) + 1
    bits_per_sym = _bsr(l_bits) + 1
    max_delta_bits = br.read(bits_per_sym)
    if max_delta_bits == 0 or max_delta_bits > l_bits:
        raise OodleError("tans: bad delta bits")
    weight = 0
    total_weights = 0
    for _ in range(count):
        sym = br.read(8)
        if sym in seen:
            raise OodleError("tans: duplicate symbol")
        weight += br.read(max_delta_bits)
        if weight == 0:
            raise OodleError("tans: zero weight")
        seen.add(sym)
        if weight == 1:
            a_syms.append(sym)
        else:
            b_syms.append((sym, weight))
        total_weights += weight
    sym = br.read(8)
    if sym in seen:
        raise OodleError("tans: duplicate symbol")
    if total - total_weights < weight or total - total_weights <= 1:
        raise OodleError("tans: bad final weight")
    b_syms.append((sym, total - total_weights))
    a_syms.sort()
    b_syms.sort()
    return a_syms, b_syms


def _tans_build_lut(a_syms, b_syms, l_bits: int):
    size = 1 << l_bits
    lut: list = [None] * size
    slots_left = size - len(a_syms)
    sa = slots_left >> 2
    pointers = [0, 0, 0, 0]
    sb = sa + ((slots_left & 3) > 0)
    pointers[1] = sb
    sb += sa + ((slots_left & 3) > 1)
    pointers[2] = sb
    sb += sa + ((slots_left & 3) > 2)
    pointers[3] = sb
    single = ((1 << l_bits) - 1, l_bits)
    for i, sym in enumerate(a_syms):
        lut[slots_left + i] = (single[0], single[1], sym, 0)
    weights_sum = 0
    for symbol, weight in b_syms:
        if weight > 4:
            sym_bits = _bsr(weight)
            z = l_bits - sym_bits
            x = (1 << z) - 1
            bits_x = z
            w = (size - 1) & (weight << z)
            what_to_add = 1 << z
            big_x = (1 << (sym_bits + 1)) - weight
            for j in range(4):
                dst = pointers[j]
                y = (weight + ((weights_sum - j - 1) & 3)) >> 2
                if big_x >= y:
                    for _ in range(y):
                        lut[dst] = (x, bits_x, symbol, w & 0xFFFF)
                        dst += 1
                        w += what_to_add
                    big_x -= y
                else:
                    for _ in range(big_x):
                        lut[dst] = (x, bits_x, symbol, w & 0xFFFF)
                        dst += 1
                        w += what_to_add
                    z -= 1
                    what_to_add >>= 1
                    bits_x = z
                    w = 0
                    x >>= 1
                    for _ in range(y - big_x):
                        lut[dst] = (x, bits_x, symbol, w & 0xFFFF)
                        dst += 1
                        w += what_to_add
                    big_x = weight
                pointers[j] = dst
        else:
            bits = ((1 << weight) - 1) << (weights_sum & 3)
            bits |= bits >> 4
            ww = weight
            for _ in range(weight):
                idx = (bits & -bits).bit_length() - 1
                bits &= bits - 1
                dst = pointers[idx]
                pointers[idx] += 1
                weight_bits = _bsr(ww)
                bx = l_bits - weight_bits
                lut[dst] = ((1 << bx) - 1, bx, symbol, (size - 1) & (ww << bx))
                ww += 1
        weights_sum += weight
    if any(e is None for e in lut):
        raise OodleError("tans: lookup table has holes")
    return lut


def _decode_tans(src: bytes, dst_size: int) -> bytes:
    if len(src) < 8 or dst_size < 5:
        raise OodleError("tans: stream too small")
    br = _MsbReader(src)
    if br.read(1):
        raise OodleError("tans: reserved bit set")
    l_bits = br.read(2) + 8
    a_syms, b_syms = _tans_table(br, l_bits)
    pos = br.bytes_used()
    if pos >= len(src):
        raise OodleError("tans: truncated")
    lut = _tans_build_lut(a_syms, b_syms, l_bits)
    lut_x = [e[0] for e in lut]
    lut_b = [e[1] for e in lut]
    lut_s = [e[2] for e in lut]
    lut_w = [e[3] for e in lut]

    body = src[pos:]
    fdata = body
    bdata = body[::-1]
    fsize = bsize = len(body)
    f_acc = f_nb = f_idx = 0
    b_acc = b_nb = b_idx = 0
    mask = (1 << l_bits) - 1

    def f_fill():
        nonlocal f_acc, f_nb, f_idx
        while f_nb < 24:
            if f_idx < fsize:
                f_acc |= fdata[f_idx] << f_nb
            f_idx += 1
            f_nb += 8

    def b_fill():
        nonlocal b_acc, b_nb, b_idx
        while b_nb < 24:
            if b_idx < bsize:
                b_acc |= bdata[b_idx] << b_nb
            b_idx += 1
            b_nb += 8

    states = [0] * 5
    f_fill()
    b_fill()
    states[0] = f_acc & mask
    f_acc >>= l_bits
    f_nb -= l_bits
    states[1] = b_acc & mask
    b_acc >>= l_bits
    b_nb -= l_bits
    states[2] = f_acc & mask
    f_acc >>= l_bits
    f_nb -= l_bits
    states[3] = b_acc & mask
    b_acc >>= l_bits
    b_nb -= l_bits
    f_fill()
    states[4] = f_acc & mask
    f_acc >>= l_bits
    f_nb -= l_bits

    n_syms = dst_size - 5
    out = bytearray(dst_size)
    s0, s1, s2, s3, s4 = states
    st = [s0, s1, s2, s3, s4]
    i = 0
    while i < n_syms:
        for backward in (False, True):
            for si in range(5):
                if backward:
                    if b_nb < 12:
                        b_fill()
                    e = st[si]
                    out[i] = lut_s[e]
                    nbits = lut_b[e]
                    st[si] = (b_acc & lut_x[e]) + lut_w[e]
                    b_acc >>= nbits
                    b_nb -= nbits
                else:
                    if f_nb < 12:
                        f_fill()
                    e = st[si]
                    out[i] = lut_s[e]
                    nbits = lut_b[e]
                    st[si] = (f_acc & lut_x[e]) + lut_w[e]
                    f_acc >>= nbits
                    f_nb -= nbits
                i += 1
                if i >= n_syms:
                    break
            if i >= n_syms:
                break
    if (f_idx - (f_nb >> 3)) + (b_idx - (b_nb >> 3)) != len(body):
        raise OodleError("tans: stream length mismatch")
    for k in range(5):
        if st[k] & ~0xFF:
            raise OodleError("tans: final state out of range")
        out[n_syms + k] = st[k]
    return bytes(out)


def _decode_rle(src: bytes, dst_size: int) -> bytes:
    if len(src) <= 1:
        if len(src) != 1:
            raise OodleError("rle: empty")
        return bytes([src[0]]) * dst_size
    if src[0]:
        cmds_dec, used = decode_bytes(src)
        buf = bytes(cmds_dec) + src[used:]
        cmd_ptr = 0
    else:
        buf = src
        cmd_ptr = 1
    end = len(buf)
    out = bytearray()
    rle_byte = 0
    while cmd_ptr < end:
        cmd = buf[end - 1]
        if cmd == 0 or cmd >= 0x30:
            end -= 1
            to_copy = (-1 - cmd) & 0xF
            to_rle = cmd >> 4
        elif cmd >= 0x10:
            data = (buf[end - 2] | (buf[end - 1] << 8)) - 4096
            end -= 2
            to_copy = data & 0x3F
            to_rle = data >> 6
        elif cmd == 1:
            rle_byte = buf[cmd_ptr]
            cmd_ptr += 1
            end -= 1
            continue
        elif cmd >= 9:
            to_rle = ((buf[end - 2] | (buf[end - 1] << 8)) - 0x8FF) * 128
            end -= 2
            to_copy = 0
        else:
            to_copy = ((buf[end - 2] | (buf[end - 1] << 8)) - 511) * 64
            end -= 2
            to_rle = 0
        if end - cmd_ptr < to_copy:
            raise OodleError("rle: literal overrun")
        out += buf[cmd_ptr:cmd_ptr + to_copy]
        cmd_ptr += to_copy
        if to_rle:
            out += bytes([rle_byte]) * to_rle
    if cmd_ptr != end or len(out) != dst_size:
        raise OodleError("rle: size mismatch")
    return bytes(out)


def _get_block_size(src: bytes) -> int:
    if len(src) < 2:
        raise OodleError("block: truncated")
    chunk_type = (src[0] >> 4) & 7
    if chunk_type == 0:
        if src[0] >= 0x80:
            return ((src[0] << 8) | src[1]) & 0xFFF
        if len(src) < 3:
            raise OodleError("block: truncated")
        return (src[0] << 16) | (src[1] << 8) | src[2]
    if chunk_type >= 6:
        raise OodleError("block: bad type")
    if src[0] >= 0x80:
        bits = (src[0] << 16) | (src[1] << 8) | src[2]
        return (bits & 0x3FF) + ((bits >> 10) & 0x3FF) + 1
    bits = (src[1] << 24) | (src[2] << 16) | (src[3] << 8) | src[4]
    return (((bits >> 18) | (src[0] << 14)) & 0x3FFFF) + 1


def decode_bytes(src: bytes):
    if len(src) < 2:
        raise OodleError("block: too few bytes")
    chunk_type = (src[0] >> 4) & 7
    if chunk_type == 0:
        if src[0] >= 0x80:
            size = ((src[0] << 8) | src[1]) & 0xFFF
            start = 2
        else:
            if len(src) < 3:
                raise OodleError("block: too few bytes")
            size = (src[0] << 16) | (src[1] << 8) | src[2]
            if size & ~0x3FFFF:
                raise OodleError("block: reserved bits set")
            start = 3
        if len(src) - start < size:
            raise OodleError("block: memcopy overrun")
        return src[start:start + size], start + size
    if src[0] >= 0x80:
        if len(src) < 3:
            raise OodleError("block: too few bytes")
        bits = (src[0] << 16) | (src[1] << 8) | src[2]
        src_size = bits & 0x3FF
        dst_size = src_size + ((bits >> 10) & 0x3FF) + 1
        start = 3
    else:
        if len(src) < 5:
            raise OodleError("block: too few bytes")
        bits = (src[1] << 24) | (src[2] << 16) | (src[3] << 8) | src[4]
        src_size = bits & 0x3FFFF
        dst_size = (((bits >> 18) | (src[0] << 14)) & 0x3FFFF) + 1
        if src_size >= dst_size:
            raise OodleError("block: bad sizes")
        start = 5
    if len(src) - start < src_size:
        raise OodleError("block: source overrun")
    body = src[start:start + src_size]
    if chunk_type in (2, 4):
        data = _decode_huffman(body, dst_size, chunk_type >> 1)
    elif chunk_type == 1:
        data = _decode_tans(body, dst_size)
    elif chunk_type == 3:
        data = _decode_rle(body, dst_size)
    elif chunk_type == 5:
        data, used = _decode_recursive(body, dst_size)
        if used != src_size:
            raise OodleError("recursive: size mismatch")
    else:
        raise OodleError(f"block: unsupported chunk type {chunk_type}")
    if len(data) != dst_size:
        raise OodleError("block: decoded size mismatch")
    return data, start + src_size


def _decode_recursive(src: bytes, output_size: int):
    if len(src) < 6:
        raise OodleError("recursive: too small")
    n = src[0] & 0x7F
    if n < 2:
        raise OodleError("recursive: bad count")
    if not (src[0] & 0x80):
        pos = 1
        out = bytearray()
        for _ in range(n):
            data, used = decode_bytes(src[pos:])
            out += data
            pos += used
        if len(out) != output_size:
            raise OodleError("recursive: size mismatch")
        return bytes(out), pos
    arrays, used = _decode_multi_array(src, 1)
    if len(arrays[0]) != output_size:
        raise OodleError("recursive: size mismatch")
    return arrays[0], used


def _decode_multi_array(src: bytes, array_count: int):
    if len(src) < 4:
        raise OodleError("multi: too small")
    if not (src[0] & 0x80):
        raise OodleError("multi: bad header")
    num_arrays = src[0] & 0x3F
    pos = 1
    if num_arrays == 0:
        arrays = []
        for _ in range(array_count):
            data, used = decode_bytes(src[pos:])
            arrays.append(bytes(data))
            pos += used
        return arrays, pos
    entropy = []
    for _ in range(num_arrays):
        data, used = decode_bytes(src[pos:])
        entropy.append([bytes(data), 0])
        pos += used
    if len(src) - pos < 3:
        raise OodleError("multi: truncated")
    q = src[pos] | (src[pos + 1] << 8)
    pos += 2
    num_indexes = _get_block_size(src[pos:])
    num_lens = num_indexes - array_count
    if num_lens < 1:
        raise OodleError("multi: bad index count")
    if q & 0x8000:
        packed, used = decode_bytes(src[pos:])
        if len(packed) != num_indexes:
            raise OodleError("multi: bad packed index size")
        pos += used
        lenlog2 = [t >> 4 for t in packed]
        indexes = [t & 0xF for t in packed]
        num_lens = num_indexes
    else:
        indexes_b, used = decode_bytes(src[pos:])
        if len(indexes_b) != num_indexes:
            raise OodleError("multi: bad index size")
        pos += used
        lenlog2_b, used = decode_bytes(src[pos:])
        if len(lenlog2_b) != num_lens:
            raise OodleError("multi: bad length-log2 size")
        pos += used
        indexes = list(indexes_b)
        lenlog2 = list(lenlog2_b)
        if any(v > 16 for v in lenlog2):
            raise OodleError("multi: bad length-log2")
    varbits_len = q & 0x3FFF
    if len(src) - pos < varbits_len:
        raise OodleError("multi: varbits overrun")
    varbits = src[pos:pos + varbits_len]
    fwd = _MsbReader(varbits)
    bwd = _MsbReader(varbits[::-1])
    intervals = [0] * num_lens
    for i in range(num_lens):
        reader = fwd if (i & 1) == 0 else bwd
        n = lenlog2[i]
        intervals[i] = (1 << n) | reader.read(n)
    if indexes[num_indexes - 1]:
        raise OodleError("multi: bad final index")
    indi = leni = 0
    increment_leni = 1 if (q & 0x8000) else 0
    arrays = []
    for _ in range(array_count):
        if indi >= num_indexes:
            raise OodleError("multi: index overrun")
        out = bytearray()
        while True:
            source = indexes[indi]
            indi += 1
            if source == 0:
                break
            if source > num_arrays or leni >= num_lens:
                raise OodleError("multi: bad source")
            cur_len = intervals[leni]
            leni += 1
            data, offs = entropy[source - 1]
            if cur_len > len(data) - offs:
                raise OodleError("multi: interval overrun")
            out += data[offs:offs + cur_len]
            entropy[source - 1][1] = offs + cur_len
        leni += increment_leni
        arrays.append(bytes(out))
    if indi != num_indexes or leni != num_lens:
        raise OodleError("multi: leftover intervals")
    for data, offs in entropy:
        if offs != len(data):
            raise OodleError("multi: unused entropy data")
    return arrays, pos + varbits_len


def _decode_far_offsets(src: bytes, pos: int, count: int, offset: int):
    out = []
    if offset < 0xC00000 - 1:
        if len(src) - pos < 3 * count:
            raise OodleError("mermaid: far offsets truncated")
        for _ in range(count):
            out.append(src[pos] | (src[pos + 1] << 8) | (src[pos + 2] << 16))
            pos += 3
        return out, pos
    for _ in range(count):
        if len(src) - pos < 3:
            raise OodleError("mermaid: far offsets truncated")
        off = src[pos] | (src[pos + 1] << 8) | (src[pos + 2] << 16)
        pos += 3
        if off >= 0xC00000:
            if pos >= len(src):
                raise OodleError("mermaid: far offsets truncated")
            off += src[pos] << 22
            pos += 1
        out.append(off)
    return out, pos


class _LzTable:
    __slots__ = ("lit", "cmd", "cmd2_offs", "cmd2_offs_end", "off16", "off32_1", "off32_2",
                 "length")


def _mermaid_read_table(mode: int, src: bytes, out: bytearray, dst_pos: int,
                        dst_size: int, offset: int) -> _LzTable:
    if mode > 1:
        raise OodleError("mermaid: bad mode")
    if len(src) < 10:
        raise OodleError("mermaid: chunk too small")
    pos = 0
    if offset == 0:
        out[dst_pos:dst_pos + 8] = src[:8]
        pos = 8
    t = _LzTable()
    t.lit, used = decode_bytes(src[pos:])
    pos += used
    t.cmd, used = decode_bytes(src[pos:])
    pos += used
    t.cmd2_offs_end = len(t.cmd)
    if dst_size <= LZ_HALF:
        t.cmd2_offs = len(t.cmd)
    else:
        if len(src) - pos < 2:
            raise OodleError("mermaid: truncated")
        t.cmd2_offs = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if t.cmd2_offs > t.cmd2_offs_end:
            raise OodleError("mermaid: bad command split")
    if len(src) - pos < 2:
        raise OodleError("mermaid: truncated")
    off16_count = src[pos] | (src[pos + 1] << 8)
    if off16_count == 0xFFFF:
        pos += 2
        hi, used = decode_bytes(src[pos:])
        pos += used
        lo, used = decode_bytes(src[pos:])
        pos += used
        if len(hi) != len(lo):
            raise OodleError("mermaid: off16 size mismatch")
        t.off16 = [lo[i] | (hi[i] << 8) for i in range(len(lo))]
    else:
        raw = src[pos + 2:pos + 2 + off16_count * 2]
        if len(raw) != off16_count * 2:
            raise OodleError("mermaid: off16 truncated")
        t.off16 = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
        pos += 2 + off16_count * 2
    if len(src) - pos < 3:
        raise OodleError("mermaid: truncated")
    tmp = src[pos] | (src[pos + 1] << 8) | (src[pos + 2] << 16)
    pos += 3
    t.off32_1 = []
    t.off32_2 = []
    if tmp:
        size1 = tmp >> 12
        size2 = tmp & 0xFFF
        if size1 == 4095:
            size1 = src[pos] | (src[pos + 1] << 8)
            pos += 2
        if size2 == 4095:
            size2 = src[pos] | (src[pos + 1] << 8)
            pos += 2
        t.off32_1, pos = _decode_far_offsets(src, pos, size1, offset)
        t.off32_2, pos = _decode_far_offsets(src, pos, size2, offset + 0x10000)
    t.length = src[pos:]
    return t


def _mermaid_run(out: bytearray, base: int, size: int, startoff: int, cmds: bytes,
                 t: _LzTable, state: list, off32: list, delta: bool) -> None:
    lit = t.lit
    lit_end = len(lit)
    length_stream = t.length
    len_end = len(length_stream)
    off16 = t.off16
    lit_pos, len_pos, o16, recent = state
    o32 = 0
    pos = base + startoff
    end = base + size

    def copy_lit(n: int) -> None:
        nonlocal pos, lit_pos
        if n <= 0:
            return
        if lit_pos + n > lit_end or pos + n > end:
            raise OodleError("mermaid: literal overrun")
        if delta:
            rp = pos + recent
            if rp < 0:
                raise OodleError("mermaid: bad delta reference")
            if -recent >= n:
                src_seg = out[rp:rp + n]
                out[pos:pos + n] = bytes(
                    [(a + b) & 0xFF for a, b in zip(lit[lit_pos:lit_pos + n], src_seg)])
            else:
                for k in range(n):
                    out[pos + k] = (lit[lit_pos + k] + out[rp + k]) & 0xFF
        else:
            out[pos:pos + n] = lit[lit_pos:lit_pos + n]
        pos += n
        lit_pos += n

    def copy_match(mpos: int, n: int) -> None:
        nonlocal pos
        if n == 0:
            return
        if mpos < 0 or pos + n > end:
            raise OodleError("mermaid: bad match")
        d = pos - mpos
        if d >= n:
            out[pos:pos + n] = out[mpos:mpos + n]
        elif d > 0:
            pattern = bytes(out[mpos:pos])
            out[pos:pos + n] = (pattern * (n // d + 1))[:n]
        else:
            raise OodleError("mermaid: bad match distance")
        pos += n

    for cmd in cmds:
        if cmd >= 24:
            copy_lit(cmd & 7)
            if cmd < 128:
                if o16 >= len(off16):
                    raise OodleError("mermaid: off16 overrun")
                recent = -off16[o16]
                o16 += 1
            copy_match(pos + recent, (cmd >> 3) & 0xF)
            continue
        if cmd > 2:
            length = cmd + 5
            if o32 >= len(off32):
                raise OodleError("mermaid: off32 overrun")
            mpos = base - off32[o32]
            o32 += 1
            recent = mpos - pos
            copy_match(mpos, length)
            continue
        if len_pos >= len_end:
            raise OodleError("mermaid: length stream overrun")
        length = length_stream[len_pos]
        if length > 251:
            if len_end - len_pos < 3:
                raise OodleError("mermaid: length stream overrun")
            length += (length_stream[len_pos + 1] | (length_stream[len_pos + 2] << 8)) * 4
            len_pos += 2
        len_pos += 1
        if cmd == 0:
            copy_lit(length + 64)
        elif cmd == 1:
            if o16 >= len(off16):
                raise OodleError("mermaid: off16 overrun")
            mpos = pos - off16[o16]
            o16 += 1
            recent = mpos - pos
            copy_match(mpos, length + 91)
        else:
            if o32 >= len(off32):
                raise OodleError("mermaid: off32 overrun")
            mpos = base - off32[o32]
            o32 += 1
            recent = mpos - pos
            copy_match(mpos, length + 29)
    copy_lit(end - pos)
    state[0], state[1], state[2], state[3] = lit_pos, len_pos, o16, recent


def _mermaid_decode_chunk(out: bytearray, dst_pos: int, dst_size: int, mode: int,
                          src: bytes) -> None:
    offset = dst_pos
    t = _mermaid_read_table(mode, src, out, dst_pos, dst_size, offset)
    state = [0, 0, 0, -8]
    delta = mode == 0
    remaining = dst_size
    pos = dst_pos
    for iteration in range(2):
        cur = min(remaining, LZ_HALF)
        if iteration == 0:
            cmds = t.cmd[:t.cmd2_offs]
            off32 = t.off32_1
        else:
            cmds = t.cmd[t.cmd2_offs:t.cmd2_offs_end]
            off32 = t.off32_2
        startoff = 8 if (offset == 0 and iteration == 0) else 0
        _mermaid_run(out, pos, cur, startoff, cmds, t, state, off32, delta)
        pos += cur
        remaining -= cur
        if remaining == 0:
            break
    if state[1] != len(t.length):
        raise OodleError("mermaid: length stream not fully consumed")


def _mermaid_quantum(out: bytearray, dst_pos: int, dst_count_total: int, src: bytes) -> int:
    pos = 0
    dst_end = dst_pos + dst_count_total
    while dst_pos < dst_end:
        dst_count = min(dst_end - dst_pos, SUBCHUNK_SIZE)
        if len(src) - pos < 4:
            raise OodleError("mermaid: quantum truncated")
        hdr = (src[pos] << 16) | (src[pos + 1] << 8) | src[pos + 2]
        if not (hdr & 0x800000):
            data, used = decode_bytes(src[pos:])
            if len(data) != dst_count:
                raise OodleError("mermaid: stored chunk size mismatch")
            out[dst_pos:dst_pos + dst_count] = data
        else:
            pos += 3
            used = hdr & 0x7FFFF
            mode = (hdr >> 19) & 0xF
            if len(src) - pos < used:
                raise OodleError("mermaid: chunk overrun")
            if used < dst_count:
                _mermaid_decode_chunk(out, dst_pos, dst_count, mode, src[pos:pos + used])
            elif used > dst_count or mode != 0:
                raise OodleError("mermaid: bad raw chunk")
            else:
                out[dst_pos:dst_pos + dst_count] = src[pos:pos + dst_count]
        pos += used
        dst_pos += dst_count
    return pos


def decompress(src: bytes, dst_len: int) -> bytes:
    out = bytearray(dst_len)
    offset = 0
    sp = 0
    uncompressed = False
    use_checksums = False
    while offset < dst_len:
        if (offset & (BLOCK_SIZE - 1)) == 0:
            if len(src) - sp < 2:
                raise OodleError("truncated block header")
            b0, b1 = src[sp], src[sp + 1]
            if (b0 & 0xF) != 0xC or ((b0 >> 4) & 3):
                raise OodleError("bad block header")
            uncompressed = bool((b0 >> 6) & 1)
            decoder_type = b1 & 0x7F
            use_checksums = bool(b1 >> 7)
            if decoder_type != 10:
                raise OodleError(f"unsupported Oodle codec {decoder_type} (only Mermaid=10)")
            sp += 2
        step = min(BLOCK_SIZE, dst_len - offset)
        if uncompressed:
            if len(src) - sp < step:
                raise OodleError("truncated raw block")
            out[offset:offset + step] = src[sp:sp + step]
            sp += step
            offset += step
            continue
        if len(src) - sp < 3:
            raise OodleError("truncated quantum header")
        v = (src[sp] << 16) | (src[sp + 1] << 8) | src[sp + 2]
        size = v & 0x3FFFF
        if size != 0x3FFFF:
            comp = size + 1
            hp = sp + (6 if use_checksums else 3)
            fill = None
        elif (v >> 18) == 1:
            comp = 0
            fill = src[sp + 3]
            hp = sp + 4
        else:
            raise OodleError("bad quantum header")
        if fill is not None:
            out[offset:offset + step] = bytes([fill]) * step
        else:
            if len(src) - hp < comp:
                raise OodleError("truncated quantum")
            if comp > step:
                raise OodleError("quantum larger than output")
            if comp == step:
                out[offset:offset + step] = src[hp:hp + step]
            else:
                used = _mermaid_quantum(out, offset, step, src[hp:hp + comp])
                if used != comp:
                    raise OodleError("quantum size mismatch")
        sp = hp + comp
        offset += step
    if sp != len(src):
        raise OodleError(f"{len(src) - sp} unread bytes after stream")
    return bytes(out)
