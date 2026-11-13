from __future__ import annotations

import bisect
import ctypes
import ctypes.wintypes as wt
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import oodle_mermaid

PARSER_VERSION = 5
FILE_MAGIC = 0x1CA2E27F
NETWORK_MAGIC = 0x2CF5A13D

REPLAY_STREAM_CHUNK_TIMES = 4
REPLAY_ENCRYPTION = 6

HISTORY_MULTIPLE_LEVELS = 6
HISTORY_HEADER_FLAGS = 9
HISTORY_LEVEL_STREAMING_FIXES = 10
HISTORY_SAVE_FULL_ENGINE_VERSION = 11
HISTORY_HEADER_GUID = 12
HISTORY_SAVE_PACKAGE_VERSION_UE = 17
HISTORY_CUSTOM_VERSIONS = 18

ENGINE_MAX_ACTOR_CHANNELS_CUSTOMIZATION = 3
ENGINE_NEW_ACTOR_OVERRIDE_LEVEL = 5
ENGINE_CHANNEL_NAMES = 6
ENGINE_CHANNEL_CLOSE_REASON = 7
ENGINE_ACKS_INCLUDED_IN_HEADER = 8
ENGINE_NETEXPORT_SERIALIZATION = 9
ENGINE_NETEXPORT_SERIALIZE_FIX = 10
ENGINE_OPTIONALLY_QUANTIZE_SPAWN_INFO = 13
ENGINE_SUBOBJECT_OUTER_CHAIN = 18

FLAG_HAS_STREAMING_FIXES = 1 << 1
FLAG_GAME_SPECIFIC_FRAME_DATA = 1 << 3

NAME_ACTOR = 102
NAME_CONTROL = 255
NAME_VOICE = 256

CHUNK_HEADER = 0
CHUNK_REPLAY_DATA = 1
_MISSING = object()

CHEST_GROUPS = {
    "/Game/Building/ActorBlueprints/Containers/Tiered_Chest_Athena.Tiered_Chest_Athena_C": "TieredChest",
    "/Game/Building/ActorBlueprints/Containers/AlwaysSpawn_NormalChest.AlwaysSpawn_NormalChest_C": "AlwaysSpawnNormalChest",
    "/Game/Building/ActorBlueprints/Containers/AlwaysSpawn_RareChest.AlwaysSpawn_RareChest_C": "AlwaysSpawnRareChest",
    "/Game/Athena/Items/ForagedItems/BuriedChest/B_Athena_BuriedChest_Chest.B_Athena_BuriedChest_Chest_C": "BuriedChest",
    "/RelicChest/Gameplay/B_TieredChest_Relics.B_TieredChest_Relics_C": "RelicChest",
    "/SpritePosters/Gameplay/RelicChests/B_Delilah_TieredChest_Relics_Variant1.B_Delilah_TieredChest_Relics_Variant1_C": "DelilahRelicChest",
    "/SpritePosters/Blueprints/Items/Booty/B_SpritePosters_Booty_Chest.B_SpritePosters_Booty_Chest_C": "SpriteBootyChest",
}
GAME_STATE_GROUP = "/Game/Athena/Athena_GameState.Athena_GameState_C"
PAWN_GROUP = "/Game/Athena/PlayerPawn_Athena.PlayerPawn_Athena_C"
PLAYER_STATE_GROUP = "/Script/FortniteGame.FortPlayerStateAthena"
TEAM_INFO_GROUP = "/Script/FortniteGame.FortTeamPrivateInfo"

WANTED = {
    "chest": {"bAlreadySearched": "bool", "bDestroyed": "bool", "SearchingPawn": "guid"},
    "gamestate": {"RecorderPlayerState": "guid", "PawnForReplayRelevancy": "guid"},
    "pawn": {"PlayerState": "guid", "On Death Controller": "guid", "ReplicatedMovement": "movement"},
    "playerstate": {"Owner": "guid", "bRespawningFromRespawnData": "bool", "TeamIndex": "int"},
    "teaminfo": {"PlayerState": "guid", "LastRepLocation": "vector"},
}
# Pawn positions are sampled at most this often (seconds) to keep memory small.
TRACK_INTERVAL = 0.25
# A chest opener counts as a teammate when within this distance (world units, 1 m = 100)
# of a squad mate's shared map location at the same moment.
SQUAD_RADIUS = 2500
SQUAD_WINDOW = 3.0


class ReplayError(Exception):
    pass


class _AesEcb:
    def __init__(self, key: bytes) -> None:
        self._bc = ctypes.WinDLL("bcrypt")
        self._provider = ctypes.c_void_p()
        self._key = ctypes.c_void_p()
        bc = self._bc
        if bc.BCryptOpenAlgorithmProvider(ctypes.byref(self._provider), "AES", None, 0) != 0:
            raise ReplayError("could not open the Windows AES provider")
        mode = "ChainingModeECB".encode("utf-16-le") + b"\0\0"
        if bc.BCryptSetProperty(self._provider, "ChainingMode", mode, len(mode), 0) != 0:
            raise ReplayError("could not select AES-ECB")
        if bc.BCryptGenerateSymmetricKey(self._provider, ctypes.byref(self._key), None, 0,
                                         key, len(key), 0) != 0:
            raise ReplayError("invalid replay encryption key")

    def decrypt(self, blob: bytes) -> bytes:
        if len(blob) % 16:
            raise ReplayError("encrypted chunk is not block aligned")
        if not blob:
            return b""
        out = ctypes.create_string_buffer(len(blob))
        written = wt.ULONG()
        status = self._bc.BCryptDecrypt(self._key, blob, len(blob), None, None, 0,
                                        out, len(blob), ctypes.byref(written), 0)
        if status != 0:
            raise ReplayError(f"AES decrypt failed (0x{status & 0xFFFFFFFF:08x})")
        return out.raw[:written.value]

    def close(self) -> None:
        if self._key:
            self._bc.BCryptDestroyKey(self._key)
        if self._provider:
            self._bc.BCryptCloseAlgorithmProvider(self._provider, 0)


_U16, _U32, _I32, _U64, _F32 = (struct.Struct(f) for f in ("<H", "<I", "<i", "<Q", "<f"))


class ByteReader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def at_end(self) -> bool:
        return self.pos >= len(self.data)

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise EOFError("read past end of data")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def skip(self, n: int) -> None:
        if n < 0 or self.pos + n > len(self.data):
            raise EOFError("skip past end of data")
        self.pos += n

    def _fixed(self, fmt: struct.Struct):
        pos = self.pos
        if pos + fmt.size > len(self.data):
            raise EOFError("read past end of data")
        self.pos = pos + fmt.size
        return fmt.unpack_from(self.data, pos)[0]

    def u8(self) -> int:
        pos = self.pos
        if pos >= len(self.data):
            raise EOFError("read past end of data")
        self.pos = pos + 1
        return self.data[pos]

    def u16(self) -> int:
        return self._fixed(_U16)

    def u32(self) -> int:
        return self._fixed(_U32)

    def i32(self) -> int:
        return self._fixed(_I32)

    def u64(self) -> int:
        return self._fixed(_U64)

    def f32(self) -> float:
        return self._fixed(_F32)

    def packed(self) -> int:
        data, pos, end = self.data, self.pos, len(self.data)
        value = shift = 0
        while True:
            if pos >= end:
                raise EOFError("read past end of data")
            b = data[pos]
            pos += 1
            value += (b >> 1) << shift
            shift += 7
            if not b & 1:
                self.pos = pos
                return value

    def fstring(self) -> str:
        length = self.i32()
        if length == 0:
            return ""
        if length < 0:
            raw = self.read(-2 * length)
            return raw.decode("utf-16-le", "replace").strip(" \0")
        return self.read(length).decode("latin-1").strip(" \0")


class Bits:
    __slots__ = ("v", "n", "pos", "err")

    def __init__(self, v: int = 0, n: int = 0) -> None:
        self.v = v
        self.n = n
        self.pos = 0
        self.err = False

    @classmethod
    def from_bytes(cls, data: bytes, nbits: Optional[int] = None) -> "Bits":
        return cls(int.from_bytes(data, "little"), len(data) * 8 if nbits is None else nbits)

    def at_end(self) -> bool:
        return self.pos >= self.n

    def bits_left(self) -> int:
        return self.n - self.pos

    def bit(self) -> int:
        if self.pos >= self.n:
            self.err = True
            return 0
        b = (self.v >> self.pos) & 1
        self.pos += 1
        return b

    def bits(self, k: int) -> int:
        if k < 0 or self.pos + k > self.n:
            self.err = True
            self.pos = self.n
            return 0
        value = (self.v >> self.pos) & ((1 << k) - 1)
        self.pos += k
        return value

    def sub(self, k: int) -> "Bits":
        if k < 0 or self.pos + k > self.n:
            self.err = True
            self.pos = self.n
            return Bits(0, 0)
        out = Bits((self.v >> self.pos) & ((1 << k) - 1), k)
        self.pos += k
        return out

    def skip(self, k: int) -> None:
        if k < 0 or self.pos + k > self.n:
            self.err = True
            self.pos = self.n
        else:
            self.pos += k

    def u8(self) -> int:
        return self.bits(8)

    def u16(self) -> int:
        return self.bits(16)

    def u32(self) -> int:
        return self.bits(32)

    def i32(self) -> int:
        value = self.bits(32)
        return value - (1 << 32) if value & 0x80000000 else value

    def packed(self) -> int:
        value = shift = 0
        for _ in range(5):
            if self.pos + 8 > self.n:
                self.err = True
                self.pos = self.n
                return value
            b = (self.v >> self.pos) & 0xFF
            self.pos += 8
            value |= (b >> 1) << shift
            if not b & 1:
                break
            shift += 7
        return value & 0xFFFFFFFF

    def serialized_int(self, max_value: int) -> int:
        value, mask, pos, n, v = 0, 1, self.pos, self.n, self.v
        while value + mask < max_value:
            if pos >= n:
                self.err = True
                break
            if (v >> pos) & 1:
                value |= mask
            pos += 1
            mask <<= 1
        self.pos = pos
        return value

    def fstring(self) -> str:
        length = self.i32()
        if length == 0 or self.err:
            return ""
        if length < 0:
            nbytes = -2 * length
            if self.pos + nbytes * 8 > self.n:
                self.err = True
                return ""
            raw = self.bits(nbytes * 8).to_bytes(nbytes, "little")
            return raw.decode("utf-16-le", "replace").strip(" \0")
        if self.pos + length * 8 > self.n:
            self.err = True
            return ""
        raw = self.bits(length * 8).to_bytes(length, "little")
        return raw.decode("latin-1").strip(" \0")

    def append(self, other_v: int, other_n: int) -> None:
        self.v |= other_v << self.n
        self.n += other_n

    def quantized_vector(self, scale: int) -> tuple[float, float, float]:
        # UE5 large-world-coordinate packed vector.
        info = self.serialized_int(1 << 7)
        size, scaled = info & 63, info >> 6
        if size:
            sign = 1 << (size - 1)
            parts = [((self.bits(size) ^ sign) - sign) for _ in range(3)]
            return tuple(p / scale if scaled else float(p) for p in parts)
        width, fmt = (64, "<d") if scaled else (32, "<f")
        return tuple(struct.unpack(fmt, self.bits(width).to_bytes(width // 8, "little"))[0]
                     for _ in range(3))


@dataclass
class ReplayInfo:
    file_version: int
    length_ms: int
    timestamp_ticks: int
    compressed: bool
    encrypted: bool
    key: bytes
    live: bool


@dataclass
class ReplayHeader:
    network_version: int = 0
    engine_net_version: int = 0
    flags: int = 0
    branch: str = ""
    changelist: int = 0
    guid: str = ""


class Group:
    __slots__ = ("path", "index", "exports", "cleaned", "kind", "wanted")

    def __init__(self, path: str, index: int) -> None:
        self.path = path
        self.index = index
        self.exports: dict[int, str] = {}
        self.cleaned = _remove_path_prefixes(path)
        self.kind: Optional[str] = None
        self.wanted: dict[int, tuple[str, str]] = {}

    def refresh_wanted(self) -> None:
        if self.kind is None and "PlayerState" in self.exports.values():
            self.kind = "pawn"
        if self.kind is None:
            return
        wanted = WANTED[self.kind]
        self.wanted = {h: (n, wanted[n]) for h, n in self.exports.items() if n in wanted}


@dataclass
class Channel:
    index: int
    actor_guid: Optional[int] = None
    archetype: Optional[int] = None
    opened: bool = False
    partial: Optional[tuple] = None


@dataclass
class Chest:
    kind: str
    already_searched: bool = False
    searched: bool = False
    pawn: Optional[int] = None
    opened_by: Optional[int] = None
    opened_at: Optional[float] = None


@dataclass
class ChestEvent:
    actor: int
    kind: str
    pawn: int
    time: float
    x: Optional[float] = None
    y: Optional[float] = None


@dataclass
class ReplayResult:
    path: str
    recorder_state: Optional[int]
    recorder_pawns: set[int]
    opened: list[ChestEvent]
    all_opened: list[ChestEvent]
    packets: int
    bunches: int
    errors: int
    duration_ms: int
    guid: str = ""
    started: Optional[datetime] = None
    squad_opened: list[ChestEvent] = field(default_factory=list)
    squad_size: int = 1

    @property
    def count(self) -> int:
        return len(self.opened)


def _remove_path_prefixes(path: str) -> str:
    if path.startswith("Default__"):
        path = path[len("Default__"):]
    for i in range(len(path) - 1, -1, -1):
        c = path[i]
        if c == ".":
            return path[i + 1:]
        if c == "/":
            return path
    return path


def _clean_suffix(path: str) -> str:
    for i in range(len(path) - 1, -1, -1):
        if not (path[i].isdigit() or path[i] == "_"):
            return path[:i + 1]
    return path


class ChestReplayParser:
    def __init__(self) -> None:
        self.info: Optional[ReplayInfo] = None
        self.header = ReplayHeader()
        self._reset()

    def parse(self, path: str | Path, progress: Optional[Callable[[int, int], None]] = None) -> ReplayResult:
        data = Path(path).read_bytes()
        return self.parse_bytes(data, str(path), progress)

    def parse_bytes(self, data: bytes, name: str = "",
                    progress: Optional[Callable[[int, int], None]] = None) -> ReplayResult:
        self._reset()
        reader = ByteReader(data)
        self.info = self._read_info(reader)
        aes = _AesEcb(self.info.key) if self.info.encrypted else None
        try:
            while reader.pos + 8 <= len(data):
                chunk_type = reader.u32()
                size = reader.i32()
                start = reader.pos
                if size < 0 or start + size > len(data):
                    break
                if progress:
                    progress(start, len(data))
                if chunk_type == CHUNK_HEADER:
                    self._read_header(ByteReader(data[start:start + size]))
                elif chunk_type == CHUNK_REPLAY_DATA:
                    if self.header.network_version == 0:
                        raise ReplayError("replay data found before the header chunk")
                    self._read_replay_data(ByteReader(data[start:start + size]), aes)
                reader.pos = start + size
        finally:
            if aes:
                aes.close()
        return self._result(name)

    def _reset(self) -> None:
        self.guid_to_path: dict[int, str] = {}
        self.groups: dict[str, Group] = {}
        self.by_cleaned: dict[str, Group] = {}
        self.group_by_index: dict[int, Group] = {}
        self._path_group: dict[str, Optional[Group]] = {}
        self.channels: dict[int, Channel] = {}
        self.in_reliable: dict[int, int] = {}
        self.in_packet_id = 0
        self.packets = 0
        self.bunches = 0
        self.errors = 0
        self.frame_time = 0.0
        self.recorder_state: Optional[int] = None
        self.pawn_state: dict[int, int] = {}
        self.state_owner: dict[int, int] = {}
        self.view_pawns: set[int] = set()
        self.relevancy_pawn: Optional[int] = None
        self.awaiting_respawn_pawn = False
        self.recorder_died = False
        self.chests: dict[int, Chest] = {}
        self.actor_by_channel: dict[int, int] = {}
        # chest updates that arrived before their group's field exports, replayed once known
        self._held_chests: dict[Group, list[tuple]] = {}
        # pawn -> ([times], [(x, y)]) and squad player state -> same, from shared map markers
        self.tracks: dict[int, tuple[list[float], list[tuple[float, float]]]] = {}
        self.squad_tracks: dict[int, tuple[list[float], list[tuple[float, float]]]] = {}
        self.team_of: dict[int, int] = {}

    def _result(self, name: str) -> ReplayResult:
        pawns = set(self.view_pawns)
        if self.recorder_state:
            pawns |= {p for p, s in self.pawn_state.items() if s == self.recorder_state}
        all_opened = sorted(
            (ChestEvent(a, c.kind, c.opened_by, c.opened_at)
             for a, c in self.chests.items()
             if c.searched and c.opened_by is not None and c.opened_at is not None),
            key=lambda e: e.time)
        for event in all_opened:
            spot = _track_at(self.tracks.get(event.pawn), event.time)
            if spot:
                event.x, event.y = spot
        mine = [e for e in all_opened if e.pawn in pawns]
        team = self.team_of.get(self.recorder_state) if self.recorder_state else None
        mates = {s for s, t in self.team_of.items() if team is not None and t == team}
        squad_pawns = pawns | self._squad_pawns(all_opened, pawns, mates - {self.recorder_state})
        started = None
        if self.info and self.info.timestamp_ticks:
            started = datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(
                microseconds=self.info.timestamp_ticks // 10)
        return ReplayResult(name, self.recorder_state, pawns, mine, all_opened, self.packets,
                            self.bunches, self.errors, self.info.length_ms if self.info else 0,
                            self.header.guid, started,
                            [e for e in all_opened if e.pawn in squad_pawns], max(len(mates), 1))

    def _squad_pawns(self, events: list[ChestEvent], mine: set[int], states: set[int]) -> set[int]:
        # Pawn ownership is encrypted in replays, so a pawn is matched to a squad mate when it
        # opens chests where that mate's shared map marker was at the time (majority vote).
        mates = [self.squad_tracks[s] for s in states if s in self.squad_tracks]
        votes: dict[int, list[int]] = {}
        for event in events:
            if event.pawn in mine or event.x is None:
                continue
            near = any(
                (spot := _track_at(track, event.time, SQUAD_WINDOW)) is not None
                and (spot[0] - event.x) ** 2 + (spot[1] - event.y) ** 2 <= SQUAD_RADIUS ** 2
                for track in mates)
            tally = votes.setdefault(event.pawn, [0, 0])
            tally[0] += near
            tally[1] += 1
        return {pawn for pawn, (hits, total) in votes.items() if hits * 2 > total}

    @staticmethod
    def _read_info(r: ByteReader) -> ReplayInfo:
        if r.u32() != FILE_MAGIC:
            raise ReplayError("not a replay file")
        version = r.u32()
        if version >= 7:
            for _ in range(r.i32()):
                r.skip(20)
        length_ms = r.u32()
        r.skip(8)
        r.fstring()
        live = bool(r.u32())
        ticks = r.u64() if version >= 3 else 0
        compressed = bool(r.u32()) if version >= 2 else False
        encrypted = False
        key = b""
        if version >= REPLAY_ENCRYPTION:
            encrypted = bool(r.u32())
            key = r.read(r.u32())
        if encrypted and not key:
            raise ReplayError("replay is encrypted but has no key")
        return ReplayInfo(version, length_ms, ticks, compressed, encrypted, key, live)

    def _read_header(self, r: ByteReader) -> None:
        if r.u32() != NETWORK_MAGIC:
            raise ReplayError("bad network magic in the replay header")
        h = ReplayHeader()
        h.network_version = r.u32()
        if h.network_version >= HISTORY_CUSTOM_VERSIONS:
            for _ in range(r.u32()):
                r.skip(20)
        r.u32()
        h.engine_net_version = r.u32()
        r.u32()
        if h.network_version >= HISTORY_HEADER_GUID:
            h.guid = r.read(16).hex()
        if h.network_version >= HISTORY_SAVE_FULL_ENGINE_VERSION:
            r.skip(6)
            h.changelist = r.u32()
            h.branch = r.fstring()
        else:
            h.changelist = r.u32()
        if h.network_version >= HISTORY_SAVE_PACKAGE_VERSION_UE:
            r.skip(12)
        if h.network_version <= HISTORY_MULTIPLE_LEVELS:
            raise ReplayError("replay is too old to read")
        for _ in range(r.u32()):
            r.fstring()
            r.u32()
        if h.network_version >= HISTORY_HEADER_FLAGS:
            h.flags = r.u32()
        self.header = h

    def _read_replay_data(self, r: ByteReader, aes: Optional[_AesEcb]) -> None:
        info = self.info
        assert info is not None
        if info.file_version >= REPLAY_STREAM_CHUNK_TIMES:
            r.u32()
            r.u32()
            length = r.u32()
        else:
            length = r.u32()
        memory_size = length
        if info.file_version >= REPLAY_ENCRYPTION:
            memory_size = r.i32()
        blob = r.read(length)
        if info.encrypted and aes is not None:
            blob = aes.decrypt(blob)
        if info.compressed:
            decompressed_size, compressed_size = struct.unpack_from("<ii", blob, 0)
            payload = oodle_mermaid.decompress(blob[8:8 + compressed_size], decompressed_size)
        else:
            payload = blob[:memory_size]
        frames = ByteReader(payload)
        while not frames.at_end():
            try:
                self._read_demo_frame(frames)
            except (EOFError, struct.error):
                self.errors += 1
                break

    def _read_demo_frame(self, r: ByteReader) -> None:
        h = self.header
        if h.network_version >= HISTORY_MULTIPLE_LEVELS:
            r.i32()
        self.frame_time = r.f32()
        if h.network_version >= HISTORY_LEVEL_STREAMING_FIXES:
            self._read_net_field_exports(r)
            self._read_net_export_guids(r)
        streaming_fixes = bool(h.flags & FLAG_HAS_STREAMING_FIXES)
        if streaming_fixes:
            for _ in range(r.packed()):
                r.fstring()
            r.u64()
        else:
            if r.packed():
                raise ReplayError("legacy level streaming data is not supported")
        self._read_external_data(r)
        if h.flags & FLAG_GAME_SPECIFIC_FRAME_DATA:
            skip = r.u64()
            if skip:
                r.skip(skip)
        while True:
            seen_level = r.packed() if streaming_fixes else 0
            size = r.i32()
            if size == 0:
                return
            if size < 0 or size > 2048:
                self.errors += 1
                raise EOFError("bad packet size")
            payload = r.read(size)
            if streaming_fixes and seen_level == 0:
                continue
            self._received_raw_packet(payload)

    @staticmethod
    def _read_external_data(r: ByteReader) -> None:
        while True:
            num_bits = r.packed()
            if num_bits == 0:
                return
            r.packed()
            r.skip((num_bits + 7) >> 3)

    def _read_net_field_exports(self, r: ByteReader) -> None:
        for _ in range(r.packed()):
            index = r.packed()
            exported = r.packed() == 1
            if exported:
                path = r.fstring()
                r.packed()
                group = self.groups.get(path)
                if group is None:
                    group = self._add_group(path, index)
                self.group_by_index[index] = group
            else:
                group = self.group_by_index.get(index)
            handle, name = self._read_field_export_bytes(r)
            if group is not None and handle is not None:
                group.exports[handle] = name
                group.refresh_wanted()
        self._flush_held_chests()

    def _read_field_export_bytes(self, r: ByteReader):
        if not r.u8():
            return None, ""
        handle = r.packed()
        r.u32()
        ev = self.header.engine_net_version
        if ev < ENGINE_NETEXPORT_SERIALIZATION:
            name = r.fstring()
            r.fstring()
        elif ev < ENGINE_NETEXPORT_SERIALIZE_FIX:
            name = r.fstring()
        else:
            name = self._static_name_bytes(r)
        return handle, name

    def _static_name_bytes(self, r: ByteReader) -> str:
        if r.u8():
            index = r.u32() if self.header.engine_net_version < ENGINE_CHANNEL_NAMES else r.packed()
            return _hardcoded_name(index)
        name = r.fstring()
        r.i32()
        return name

    def _read_net_export_guids(self, r: ByteReader) -> None:
        for _ in range(r.packed()):
            size = r.i32()
            bits = Bits.from_bytes(r.read(size))
            self._load_object(bits, True)

    def _add_group(self, path: str, index: int) -> Group:
        group = Group(path, index)
        if path in CHEST_GROUPS:
            group.kind = "chest"
        elif path == GAME_STATE_GROUP:
            group.kind = "gamestate"
        elif path == PAWN_GROUP:
            group.kind = "pawn"
        elif path == PLAYER_STATE_GROUP:
            group.kind = "playerstate"
        elif path == TEAM_INFO_GROUP:
            group.kind = "teaminfo"
        self.groups[path] = group
        if group.cleaned:
            self.by_cleaned.setdefault(group.cleaned, group)
        self._path_group.clear()
        return group

    def _load_object(self, bits: Bits, exporting: bool, depth: int = 0) -> int:
        if depth > 16:
            return 0
        guid = bits.packed()
        if guid == 0 or bits.err:
            return guid
        flags = 0
        if guid == 1 or exporting:
            flags = bits.u8()
        if flags & 1:
            self._load_object(bits, True, depth + 1)
            path = bits.fstring()
            if flags & 4:
                bits.u32()
            if exporting:
                self.guid_to_path[guid] = _remove_path_prefixes(path)
        return guid

    def _group_for_guid(self, guid: int) -> Optional[Group]:
        path = self.guid_to_path.get(guid)
        if path is None:
            return None
        base = path.split("_UAID_", 1)[0]
        cached = self._path_group.get(base, _MISSING)
        if cached is not _MISSING:
            return cached
        found = None
        for candidate in (path, base, _clean_suffix(base)):
            found = self.by_cleaned.get(candidate)
            if found is not None:
                break
        if found is None:
            best = 0
            for group in self.groups.values():
                size = len(group.cleaned)
                if size > best and size >= 4 and group.cleaned in path:
                    found, best = group, size
        self._path_group[base] = found
        return found

    def _received_raw_packet(self, data: bytes) -> None:
        if not data or data[-1] == 0:
            self.errors += 1
            return
        last = data[-1]
        bit_size = len(data) * 8 - 1
        while not last & 0x80:
            last <<= 1
            bit_size -= 1
        self.packets += 1
        try:
            self._received_packet(Bits.from_bytes(data, bit_size))
        except Exception:
            self.errors += 1

    def _received_packet(self, bits: Bits) -> None:
        ev = self.header.engine_net_version
        self.in_packet_id += 1
        while not bits.at_end():
            if ev < ENGINE_ACKS_INCLUDED_IN_HEADER:
                bits.bit()
            control = bits.bit()
            is_open = bits.bit() if control else 0
            is_close = bits.bit() if control else 0
            if is_close:
                if ev < ENGINE_CHANNEL_CLOSE_REASON:
                    bits.skip(1)
                else:
                    bits.serialized_int(15)
            bits.skip(1)
            reliable = bits.bit()
            if ev < ENGINE_MAX_ACTOR_CHANNELS_CUSTOMIZATION:
                ch_index = bits.serialized_int(10240)
            else:
                ch_index = bits.packed()
            has_exports = bits.bit()
            has_must_be_mapped = bits.bit()
            partial = bits.bit()
            if reliable:
                ch_sequence = self.in_reliable.get(ch_index, 0) + 1
            elif partial:
                ch_sequence = self.in_packet_id
            else:
                ch_sequence = 0
            partial_initial = partial_final = 0
            if partial:
                partial_initial = bits.bit()
                bits.skip(1)
                partial_final = bits.bit()
            ch_name = ""
            if ev < ENGINE_CHANNEL_NAMES:
                if reliable or is_open:
                    ch_type = bits.serialized_int(4)
                    ch_name = {1: "Control", 2: "Voice", 3: "Actor"}.get(ch_type, "")
            elif reliable or is_open:
                ch_name = self._channel_name(bits)
            data_bits = bits.serialized_int(2048 * 8)
            if bits.err:
                raise ValueError("bunch header ran past end of packet")
            body = bits.sub(data_bits)
            if bits.err:
                raise ValueError("bunch body ran past end of packet")
            self.bunches += 1
            if has_exports:
                self._receive_guid_bunch(body)
            channel = self.channels.get(ch_index)
            if channel is None:
                if not reliable and not (is_open and (is_close or partial)):
                    continue
                channel = Channel(ch_index)
                self.channels[ch_index] = channel
            try:
                self._received_next_bunch(channel, body, reliable, ch_sequence, partial,
                                          partial_initial, partial_final, is_open, is_close,
                                          has_exports, has_must_be_mapped, ch_name)
            except Exception:
                self.errors += 1

    @staticmethod
    def _channel_name_from_index(index: int) -> str:
        return {NAME_CONTROL: "Control", NAME_VOICE: "Voice", NAME_ACTOR: "Actor"}.get(index, "")

    def _channel_name(self, bits: Bits) -> str:
        if bits.bit():
            if self.header.engine_net_version < ENGINE_CHANNEL_NAMES:
                index = bits.u32()
            else:
                index = bits.packed()
            return self._channel_name_from_index(index)
        name = bits.fstring()
        bits.i32()
        return name

    def _receive_guid_bunch(self, bits: Bits) -> None:
        if bits.bit():
            self._receive_net_field_exports_compat(bits)
            return
        count = bits.i32()
        if count > 2048 or count < 0:
            return
        for _ in range(count):
            self._load_object(bits, True)

    def _receive_net_field_exports_compat(self, bits: Bits) -> None:
        for _ in range(bits.u32()):
            index = bits.packed()
            if bits.bit():
                path = bits.fstring()
                bits.u32()
                group = self.groups.get(path)
                if group is None:
                    group = self._add_group(path, index)
                self.group_by_index[index] = group
            else:
                group = self.group_by_index.get(index)
            handle, name = self._read_field_export_bits(bits)
            if group is not None and handle is not None:
                group.exports[handle] = name
                group.refresh_wanted()
            if bits.err:
                self._flush_held_chests()
                return
        self._flush_held_chests()

    def _read_field_export_bits(self, bits: Bits):
        if not bits.bit():
            return None, ""
        handle = bits.packed()
        bits.u32()
        ev = self.header.engine_net_version
        if ev < ENGINE_NETEXPORT_SERIALIZATION:
            name = bits.fstring()
            bits.fstring()
        elif ev < ENGINE_NETEXPORT_SERIALIZE_FIX:
            name = bits.fstring()
        elif bits.bit():
            index = bits.u32() if ev < ENGINE_CHANNEL_NAMES else bits.packed()
            name = _hardcoded_name(index)
        else:
            name = bits.fstring()
            bits.i32()
        return handle, name

    def _received_next_bunch(self, channel: Channel, body: Bits, reliable: int, ch_sequence: int,
                             partial: int, partial_initial: int, partial_final: int,
                             is_open: int, is_close: int, has_exports: int, has_must_be_mapped: int,
                             ch_name: str) -> None:
        if reliable:
            self.in_reliable[channel.index] = ch_sequence
        if partial:
            if partial_initial:
                left = body.bits_left()
                if not has_exports and left > 0:
                    rest = body.sub(left)
                else:
                    rest = Bits(0, 0)
                channel.partial = (rest, ch_sequence, reliable, has_must_be_mapped, is_open, ch_name)
                return
            pending = channel.partial
            if pending is None:
                return
            acc, seq, p_reliable, p_must_be_mapped, p_open, p_name = pending
            seq_ok = (ch_sequence == seq + 1) if p_reliable else (ch_sequence in (seq, seq + 1))
            if not seq_ok or p_reliable != reliable:
                channel.partial = None
                return
            left = body.bits_left()
            if not has_exports and left > 0:
                piece = body.sub(left)
                acc.append(piece.v, piece.n)
            channel.partial = (acc, ch_sequence, p_reliable, p_must_be_mapped, p_open, p_name)
            if not partial_final:
                return
            channel.partial = None
            self._process_sequenced(channel, acc, p_name, bool(p_must_be_mapped), bool(p_open), bool(is_close))
            return
        self._process_sequenced(channel, body, ch_name, bool(has_must_be_mapped), bool(is_open), bool(is_close))

    def _process_sequenced(self, channel: Channel, body: Bits, ch_name: str, must_be_mapped: bool,
                           is_open: bool, is_close: bool) -> None:
        if ch_name != "Control":
            if must_be_mapped:
                for _ in range(body.u16()):
                    body.packed()
            self._process_bunch(channel, body, is_open)
        if is_close:
            self.channels.pop(channel.index, None)

    def _process_bunch(self, channel: Channel, body: Bits, is_open: bool) -> None:
        ev = self.header.engine_net_version
        if not channel.opened:
            if not is_open:
                return
            actor_guid = self._load_object(body, False)
            dynamic = actor_guid > 0 and not actor_guid & 1
            if body.at_end() and dynamic:
                return
            archetype = None
            if dynamic:
                archetype = self._load_object(body, False)
                if ev >= ENGINE_NEW_ACTOR_OVERRIDE_LEVEL:
                    self._load_object(body, False)
                self._skip_spawn_vector(body, ev, True)
                if body.bit():
                    for _ in range(3):
                        if body.bit():
                            body.u16()
                self._skip_spawn_vector(body, ev, False)
                self._skip_spawn_vector(body, ev, False)
            channel.actor_guid = actor_guid
            channel.archetype = archetype
            channel.opened = True
            self.actor_by_channel[channel.index] = actor_guid
            if archetype is not None:
                path = self.guid_to_path.get(archetype, "")
                if "PlayerController" in path:
                    body.u8()
        while not body.at_end() and not body.err:
            has_rep_layout = body.bit()
            is_actor = body.bit()
            if is_actor:
                rep_object = channel.archetype if channel.archetype is not None else channel.actor_guid
            else:
                guid = self._load_object(body, False)
                if body.bit():
                    rep_object = guid
                else:
                    class_guid = self._load_object(body, False)
                    if class_guid == 0:
                        continue
                    if ev >= ENGINE_SUBOBJECT_OUTER_CHAIN:
                        actor_is_outer = True if body.at_end() else bool(body.bit())
                        if not actor_is_outer:
                            self._load_object(body, False)
                    rep_object = class_guid
            num_bits = body.packed()
            payload = body.sub(num_bits)
            if body.err:
                return
            if rep_object == 0 or payload.at_end():
                continue
            group = self._group_for_guid(rep_object)
            if group is None or group.kind is None:
                continue
            if has_rep_layout:
                self._receive_properties(channel, group, payload)
            elif group.kind == "teaminfo":
                self._receive_team_info(group, payload)

    def _receive_team_info(self, group: Group, bits: Bits) -> None:
        # Custom delta (fast array) fields: each element carries a squad mate's map marker.
        cache = self.groups.get(group.path + "_ClassNetCache")
        handles = max(len(cache.exports) if cache else 0, 2)
        while not bits.at_end() and not bits.err:
            bits.serialized_int(handles)
            data = bits.sub(bits.packed())
            if bits.err:
                return
            data.skip(64)
            deletes, changed = data.i32(), data.i32()
            if not (0 <= deletes < 256 and 0 <= changed < 256):
                continue
            data.skip(32 * deletes)
            for _ in range(changed):
                data.i32()
                values = self._read_values(group, data)
                if data.err:
                    break
                state, spot = values.get("PlayerState"), values.get("LastRepLocation")
                if state and spot:
                    _track_add(self.squad_tracks.setdefault(state, ([], [])), self.frame_time, spot, 0)

    @staticmethod
    def _skip_spawn_vector(bits: Bits, ev: int, quantized_default: bool) -> None:
        if not bits.bit():
            return
        quantize = True if ev < ENGINE_OPTIONALLY_QUANTIZE_SPAWN_INFO else bool(bits.bit())
        if quantize:
            scale_bits = bits.serialized_int(24)
            limit = 1 << (scale_bits + 2)
            for _ in range(3):
                bits.serialized_int(limit)
        else:
            bits.skip(96)

    def _flush_held_chests(self) -> None:
        ready = [group for group in self._held_chests
                 if any(name == "bAlreadySearched" for name, _ in group.wanted.values())]
        for group in ready:
            for actor, index, value, width, pos in self._held_chests.pop(group):
                if actor is None:
                    continue
                previous = self.actor_by_channel.get(index, _MISSING)
                if actor is not None:
                    self.actor_by_channel[index] = actor
                clone = Bits(value, width)
                clone.pos = pos
                self._receive_properties(Channel(index), group, clone)
                if previous is _MISSING:
                    self.actor_by_channel.pop(index, None)
                else:
                    self.actor_by_channel[index] = previous

    def _receive_properties(self, channel: Channel, group: Group, bits: Bits) -> None:
        if (group.kind == "chest" and bits.bits_left() > 0
                and not any(name == "bAlreadySearched" for name, _ in group.wanted.values())):
            actor = self.actor_by_channel.get(channel.index)
            self._held_chests.setdefault(group, []).append(
                (actor, channel.index, bits.v, bits.n, bits.pos))
            return
        values = self._read_values(group, bits)
        if values:
            self._on_export(channel, group.kind, values)

    @staticmethod
    def _read_values(group: Group, bits: Bits) -> dict[str, object]:
        bits.bit()
        values: dict[str, object] = {}
        wanted = group.wanted
        while True:
            handle = bits.packed()
            if handle == 0 or bits.err:
                break
            handle -= 1
            num_bits = bits.packed()
            if num_bits == 0:
                continue
            spec = wanted.get(handle)
            if spec is None:
                bits.skip(num_bits)
                if bits.err:
                    break
                continue
            field_bits = bits.sub(num_bits)
            if bits.err:
                break
            name, kind = spec
            if kind == "bool":
                values[name] = bool(field_bits.bit())
            elif kind == "int":
                values[name] = field_bits.bits(num_bits)
            elif kind == "vector":  # FVector_NetQuantize100: integers in hundredths
                values[name] = tuple(v / 100 for v in field_bits.quantized_vector(1))
            elif kind == "movement":
                field_bits.skip(4)  # sleep / physics / server frame / server handle flags
                values[name] = field_bits.quantized_vector(100)
            else:
                values[name] = field_bits.packed()
        return values

    def _on_export(self, channel: Channel, kind: str, values: dict) -> None:
        actor = self.actor_by_channel.get(channel.index)
        if actor is None:
            return
        if kind == "gamestate":
            state = values.get("RecorderPlayerState")
            if state:
                self.recorder_state = state
            pawn = values.get("PawnForReplayRelevancy")
            if pawn:
                changed = pawn != self.relevancy_pawn
                self.relevancy_pawn = pawn
                if not self.recorder_died:
                    self.view_pawns.add(pawn)
                elif self.awaiting_respawn_pawn and changed:
                    self.view_pawns.add(pawn)
                    self.awaiting_respawn_pawn = False
                    self.recorder_died = False
        elif kind == "pawn":
            spot = values.get("ReplicatedMovement")
            if spot:
                _track_add(self.tracks.setdefault(actor, ([], [])), self.frame_time, spot, TRACK_INTERVAL)
            state = values.get("PlayerState")
            if state:
                self.pawn_state[actor] = state
            dead_controller = values.get("On Death Controller")
            if dead_controller and dead_controller == self.state_owner.get(self.recorder_state):
                self.recorder_died = True
                self.view_pawns.add(actor)
        elif kind == "playerstate":
            owner = values.get("Owner")
            if owner:
                self.state_owner[actor] = owner
            if "TeamIndex" in values:
                self.team_of[actor] = values["TeamIndex"]
            if (actor == self.recorder_state
                    and values.get("bRespawningFromRespawnData") is True):
                self.awaiting_respawn_pawn = True
        elif kind == "chest":
            pawn = values.get("SearchingPawn") or None
            searched = values.get("bAlreadySearched") is True or values.get("bDestroyed") is True
            chest = self.chests.get(actor)
            if chest is None:
                chest = Chest(CHEST_GROUPS.get(self._group_path(channel), "Chest"),
                              already_searched=searched and pawn is None)
                self.chests[actor] = chest
            if pawn is not None:
                chest.pawn = pawn
            if searched and not chest.searched and not chest.already_searched:
                chest.searched = True
                chest.opened_at = self.frame_time
                chest.opened_by = chest.pawn

    def _group_path(self, channel: Channel) -> str:
        guid = channel.archetype if channel.archetype is not None else channel.actor_guid
        group = self._group_for_guid(guid) if guid is not None else None
        return group.path if group else ""


def _track_add(track: tuple[list[float], list[tuple[float, float]]], time: float,
               spot: tuple[float, float, float], interval: float) -> None:
    times, spots = track
    if (times and time - times[-1] < interval) or (spot[0] == 0 and spot[1] == 0):
        return  # throttled, or an unset (origin) position
    times.append(time)
    spots.append((spot[0], spot[1]))


def _track_at(track: Optional[tuple[list[float], list[tuple[float, float]]]], time: float,
              window: float = 3.0) -> Optional[tuple[float, float]]:
    if not track or not track[0]:
        return None
    times, spots = track
    i = bisect.bisect_left(times, time)
    best = min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - time))
    return spots[best] if abs(times[best] - time) <= window else None


def _hardcoded_name(index: int) -> str:
    return {NAME_ACTOR: "Actor", NAME_CONTROL: "Control", NAME_VOICE: "Voice"}.get(index, f"#{index}")


def peek_replay_state(path: str | Path) -> Optional[tuple[str, bool]]:
    with open(path, "rb") as fh:
        head = fh.read(4096)
    reader = ByteReader(head)
    try:
        info = ChestReplayParser._read_info(reader)
        while reader.pos + 8 <= len(head):
            chunk_type = reader.u32()
            size = reader.i32()
            if chunk_type == CHUNK_HEADER:
                if reader.pos + size > len(head):
                    with open(path, "rb") as fh:
                        fh.seek(reader.pos)
                        body = fh.read(size)
                else:
                    body = head[reader.pos:reader.pos + size]
                parser = ChestReplayParser()
                parser._read_header(ByteReader(body))
                guid = parser.header.guid
                if not guid:
                    return None
                return guid, info.live
            reader.skip(size)
    except (EOFError, ReplayError, struct.error):
        return None
    return None


def peek_replay_guid(path: str | Path) -> Optional[str]:
    state = peek_replay_state(path)
    return state[0] if state else None


def count_chests(path: str | Path) -> ReplayResult:
    return ChestReplayParser().parse(path)
