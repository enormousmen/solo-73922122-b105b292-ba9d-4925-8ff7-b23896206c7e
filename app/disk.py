"""单卷虚拟磁盘：带空槽的本地镜像 + 崩溃安全的原地整理。

镜像布局
========
偏移 0            : 主头副本   （4 KiB）
偏移 4096         : 备头副本   （4 KiB）
偏移 8192 + i*4096: 物理槽 i  （4 KiB，共 slot_count 个）

头内槽表 slots[] 每项为 int32：0 表示空槽，否则是 (逻辑块号 + 1)。

崩溃安全
========
- 每个头带 16 位结构版本、单调递增世代号和整头 CRC32。
- 提交时只写“当前非活动”的那个副本，再把活动指针切过去。
  因此任何时刻至少有一个完整有效的旧头；重启时取世代号最大的有效头。
- 搬一个块：数据先完整写入目标槽并 fsync，再提交新槽表。
  提交前所有读仍命中旧位置；提交后新位置已有完整数据。
- 整理游标 cursor 随槽表一起持久化，进程中途退出后重启可从游标继续。

镜像外临时数据
==============
整理只使用一个暂存文件，且任何时刻最多含一个 4 KiB 块。
"""

from __future__ import annotations

import contextlib
import io
import os
import struct
import threading
import zlib

BLOCK_SIZE = 4096
HEADER_BLOCKS = 2
HEADER_SIZE = HEADER_BLOCKS * BLOCK_SIZE
DATA_OFFSET = HEADER_SIZE

MAGIC = b"VDEF"
HDR_VERSION = 1
# 32 个槽位，留足空槽；需要调整时重新生成镜像即可。
DEFAULT_SLOT_COUNT = 32

STATE_IDLE = "idle"
STATE_COMPACTING = "compacting"
STATE_ERROR = "error"
STATES = (STATE_IDLE, STATE_COMPACTING, STATE_ERROR)

# 头固定区：magic(4) version(H) generation(Q) state(B 16) slot_count(H)
# cursor(H) moves(I) errors(I) crc(I)，共 44 字节；其余留给槽表。
HDR_FIXED = struct.Struct("<4sHQ16sHHIII")
HDR_FIXED_SIZE = HDR_FIXED.size  # 44
HDR_PAYLOAD_CAP = BLOCK_SIZE - 4  # 末尾 4 字节放 crc32
SLOTS_MAX = (HDR_PAYLOAD_CAP - HDR_FIXED_SIZE) // 4
STATE_BYTES = 16


class DiskError(RuntimeError):
    """磁盘逻辑错误（损坏、非法状态等），与搬移/截短的注入错误区分开。"""


class RWLock:
    """多读单写锁：读之间不互斥，写独占。

    写者在条件变量上与后来的读者一同竞争；本项目只有单个整理工作线程，
    且每次持锁时间极短（磁盘 I/O 在锁外），不会出现写者饥饿。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writers = 0

    @contextlib.contextmanager
    def read_lock(self):
        with self._cond:
            while self._writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write_lock(self):
        with self._cond:
            while self._readers or self._writers:
                self._cond.wait()
            self._writers += 1
        try:
            yield
        finally:
            with self._cond:
                self._writers -= 1
                self._cond.notify_all()


def _pack_header(generation: int, state: str, slot_count: int,
                 cursor: int, moves: int, errors: int,
                 slots: list[int]) -> bytes:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    if len(slots) != slot_count or slot_count > SLOTS_MAX:
        raise DiskError("bad slot table length")
    if not 0 <= cursor <= slot_count:
        raise DiskError("cursor out of range")
    payload = HDR_FIXED.pack(
        MAGIC, HDR_VERSION, generation,
        state.encode("ascii").ljust(STATE_BYTES, b"\x00"),
        slot_count, cursor, moves, errors, 0,  # crc 占位
    )
    payload += struct.pack(f"<{slot_count}i", *slots)
    payload += b"\x00" * (HDR_PAYLOAD_CAP - len(payload))
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return payload + struct.pack("<I", crc)


def _unpack_header(buf: bytes) -> dict:
    if len(buf) != BLOCK_SIZE:
        raise DiskError("header block size mismatch")
    payload, crc_bytes = buf[:HDR_PAYLOAD_CAP], buf[HDR_PAYLOAD_CAP:]
    (crc_stored,) = struct.unpack("<I", crc_bytes)
    if (zlib.crc32(payload) & 0xFFFFFFFF) != crc_stored:
        raise DiskError("header crc mismatch")
    (magic, version, generation, state_raw, slot_count,
     cursor, moves, errors, _crc_zero) = HDR_FIXED.unpack(
         payload[:HDR_FIXED_SIZE])
    if magic != MAGIC or version != HDR_VERSION:
        raise DiskError("bad magic/version")
    if slot_count > SLOTS_MAX:
        raise DiskError("slot_count too large")
    slots = list(struct.unpack(f"<{slot_count}i",
                               payload[HDR_FIXED_SIZE:
                                       HDR_FIXED_SIZE + slot_count * 4]))
    state = state_raw.rstrip(b"\x00").decode("ascii")
    if state not in STATES or not 0 <= cursor <= slot_count:
        raise DiskError("bad state/cursor in header")
    return {
        "generation": generation,
        "state": state,
        "slot_count": slot_count,
        "cursor": cursor,
        "moves": moves,
        "errors": errors,
        "slots": slots,
    }


def lbn_to_slot(slots: list[int]) -> dict[int, int]:
    """逻辑块号 -> 物理槽号；返回重复映射会报错。"""
    mapping: dict[int, int] = {}
    for phys, entry in enumerate(slots):
        if entry:
            lbn = entry - 1
            if lbn in mapping:
                raise DiskError(f"duplicate mapping for lbn {lbn}")
            mapping[lbn] = phys
    return mapping


class Disk:
    """打开/创建一个镜像文件并提供读块、整理、状态查询。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR)
        self.lock = RWLock()
        self._active = -1
        self._staging: io.BytesIO | None = None
        # 一次性故障注入：下一次对应操作抛 RuntimeError，不改变持久状态。
        self.inject_move_error = False
        self.inject_truncate_error = False
        self.move_delay = 0.0
        self.max_temp_seen = 0
        self._load_header()

    # ---------- 打开 / 创建 ----------

    @classmethod
    def create(cls, path: str, slot_count: int = DEFAULT_SLOT_COUNT) -> "Disk":
        if os.path.exists(path):
            raise FileExistsError(path)
        slots = [0] * slot_count
        hdr = _pack_header(1, STATE_IDLE, slot_count, 0, 0, 0, slots)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.pwrite(fd, hdr, 0)
            os.pwrite(fd, hdr, BLOCK_SIZE)
            # 预分配槽位（稀疏/零填充均可，ftruncate 不实际写数据）。
            os.ftruncate(fd, DATA_OFFSET + slot_count * BLOCK_SIZE)
            os.fsync(fd)
        finally:
            os.close(fd)
        return cls(path)

    def _load_header(self) -> None:
        candidates: list[tuple[int, dict]] = []
        for idx in range(HEADER_BLOCKS):
            raw = os.pread(self._fd, BLOCK_SIZE, idx * BLOCK_SIZE)
            try:
                candidates.append((idx, _unpack_header(raw)))
            except DiskError:
                continue
        if not candidates:
            raise DiskError("no valid header copy; image unusable")
        # 取世代号最大的有效副本（世代号单调递增，不会打平）。
        self._active, best = max(candidates, key=lambda item: item[1][
            "generation"])
        self._gen = best["generation"]
        self.state = best["state"]
        self.slot_count = best["slot_count"]
        self.cursor = best["cursor"]
        self.moves = best["moves"]
        self.errors = best["errors"]
        self.slots = list(best["slots"])

    def close(self) -> None:
        with self.lock.write_lock():
            if self._staging is not None:
                self._staging.close()
                self._staging = None
            os.close(self._fd)
            self._fd = -1

    # ---------- 持久化 ----------

    def _commit(self) -> None:
        """把当前内存状态写进非活动头副本，fsync 后切换活动指针。"""
        if self._fd < 0:
            raise DiskError("disk closed")
        buf = _pack_header(self._gen + 1, self.state, self.slot_count,
                           self.cursor, self.moves, self.errors, self.slots)
        target = 1 - self._active
        os.pwrite(self._fd, buf, target * BLOCK_SIZE)
        os.fsync(self._fd)
        self._active = target
        self._gen += 1

    # ---------- 槽位读写 ----------

    def _slot_offset(self, phys: int) -> int:
        if not 0 <= phys < self.slot_count:
            raise DiskError("physical slot out of range")
        return DATA_OFFSET + phys * BLOCK_SIZE

    def _read_slot(self, phys: int) -> bytes:
        data = os.pread(self._fd, BLOCK_SIZE, self._slot_offset(phys))
        if len(data) != BLOCK_SIZE:
            raise DiskError(f"short read at slot {phys}")
        return data

    def _write_slot(self, phys: int, data: bytes) -> None:
        if len(data) != BLOCK_SIZE:
            raise DiskError("block must be exactly 4 KiB")
        os.pwrite(self._fd, data, self._slot_offset(phys))
        os.fsync(self._fd)

    # ---------- 对外：读逻辑块 ----------

    def read_lbn(self, lbn: int) -> bytes:
        """按逻辑块号读 4 KiB；整理过程中始终返回完整原数据。"""
        if lbn < 0:
            raise DiskError("lbn must be non-negative")
        with self.lock.read_lock():
            entry = lbn + 1
            if entry not in self.slots:
                raise KeyError(lbn)
            phys = self.slots.index(entry)
        # 数据位置在提交新头之前不会被覆盖，pread 放到锁外不影响正确性，
        # 也避免整理的磁盘 I/O 长时间挡住读请求。
        return self._read_slot(phys)

    def has_lbn(self, lbn: int) -> bool:
        with self.lock.read_lock():
            return (lbn + 1) in self.slots

    def used_lbns(self) -> list[int]:
        with self.lock.read_lock():
            return sorted(e - 1 for e in self.slots if e)

    # ---------- 建镜像用：写块（仅 idle 态，供种子脚本调用） ----------

    def seed_write(self, lbn: int, data: bytes, phys: int) -> None:
        with self.lock.write_lock():
            if self.state != STATE_IDLE:
                raise DiskError("seed write only allowed in idle state")
            if lbn < 0 or (lbn + 1) in self.slots:
                raise DiskError(f"lbn {lbn} already mapped")
            if not 0 <= phys < self.slot_count or self.slots[phys] != 0:
                raise DiskError(f"slot {phys} not free")
            self._write_slot(phys, data)
            self.slots[phys] = lbn + 1
            self._commit()

    # ---------- 整理 ----------

    @property
    def file_size(self) -> int:
        return os.fstat(self._fd).st_size

    def expected_compact_size(self) -> int:
        return DATA_OFFSET + len(self.used_lbns()) * BLOCK_SIZE

    def start_compaction(self) -> bool:
        """进入整理态并持久化；已在整理/错误态时返回 False（可续跑）。"""
        with self.lock.write_lock():
            if self.state == STATE_COMPACTING:
                return False
            if self.state == STATE_ERROR:
                # 错误态保留游标，允许值班员重试续作。
                self.state = STATE_COMPACTING
                self._commit()
                return True
            self.state = STATE_COMPACTING
            self.cursor = 0
            self._commit()
            return True

    def _get_staging(self) -> io.BytesIO:
        if self._staging is None:
            # 内存暂存即“镜像外临时块”，生命周期内最多 4 KiB。
            self._staging = io.BytesIO()
        return self._staging

    def _temp_hold(self, data: bytes) -> None:
        st = self._get_staging()
        st.seek(0)
        st.truncate(0)
        st.write(data)
        st.seek(0)
        self.max_temp_seen = max(self.max_temp_seen, st.getbuffer().nbytes)

    def _temp_release(self) -> bytes:
        st = self._get_staging()
        data = st.getvalue()
        st.seek(0)
        st.truncate(0)
        return data

    def _move_block(self, src: int, dst: int) -> None:
        """把 src 槽的数据经镜像外暂存搬到 dst，再原子提交槽表。"""
        if self.inject_move_error:
            self.inject_move_error = False
            raise RuntimeError("injected move failure")
        if self.move_delay:
            threading.Event().wait(self.move_delay)
        with self.lock.write_lock():
            if self.slots[src] == 0:
                raise DiskError(f"source slot {src} is empty")
            if self.slots[dst] != 0:
                raise DiskError(f"dest slot {dst} not free")
            entry = self.slots[src]
        # I/O 放在写锁外：读路径仍可通过旧映射读到完整数据。
        data = self._read_slot(src)
        self._temp_hold(data)                 # 镜像外临时数据 ≤ 4 KiB
        self._write_slot(dst, data)           # 先落盘新位置并 fsync
        with self.lock.write_lock():
            # 再次确认，期间只有本工作线程能改槽表（单工作线程模型）。
            if self.slots[src] != entry or self.slots[dst] != 0:
                raise DiskError("slot table changed unexpectedly")
            self.slots[src] = 0
            self.slots[dst] = entry
            self.moves += 1
            self._commit()                     # 提交后旧槽才算空出
        self._temp_release()

    def _find_free_from(self, start: int) -> int:
        for phys in range(start, self.slot_count):
            if self.slots[phys] == 0:
                return phys
        return -1

    def compact_once(self) -> bool:
        """推进整理一步；返回 True 表示全部完成（已截短并回到 idle）。

        不变量：进入游标 i 时，物理槽 0..i-1 已依次压紧 used[0..i-1]，
        其余 U-i 个在用块分布在槽 i..S-1 中。因 S>U，该区间恒有空槽，
        枢轴永远可得。每一步结束后槽 0..i 压紧、映射自洽。
        """
        with self.lock.write_lock():
            if self.state != STATE_COMPACTING:
                if self.state == STATE_IDLE:
                    return True
                raise DiskError(f"cannot compact in state {self.state}")
            used = sorted(e - 1 for e in self.slots if e)
            slot_count = self.slot_count
            cursor = self.cursor

        if cursor < len(used):
            target_lbn = used[cursor]
            with self.lock.read_lock():
                mapping = lbn_to_slot(self.slots)
                target_phys = mapping[target_lbn]
                dest_is_free = self.slots[cursor] == 0
            if target_phys == cursor:
                # 目标块已在正确槽位。
                with self.lock.write_lock():
                    self.cursor += 1
                    self._commit()
                return False
            if dest_is_free:
                # 目标槽是空槽：目标块直接迁入。
                self._move_block(target_phys, cursor)
            else:
                # 目标槽被别的在用块占着：先借一个 ≥ cursor 的空槽当枢轴。
                pivot = self._find_free_from(cursor)
                if pivot < 0:
                    raise DiskError("no free slot available for pivoting")
                self._move_block(cursor, pivot)
                with self.lock.read_lock():
                    target_phys = lbn_to_slot(self.slots)[target_lbn]
                self._move_block(target_phys, cursor)
            with self.lock.write_lock():
                self.cursor += 1
                self._commit()
            return False

        # 所有在用块已压紧到前 len(used) 个槽，截短尾部。
        target_size = DATA_OFFSET + len(used) * BLOCK_SIZE
        if self.inject_truncate_error:
            self.inject_truncate_error = False
            raise RuntimeError("injected truncate failure")
        with self.lock.write_lock():
            # 截短前再校验一次，防止截掉在用数据。
            for phys in range(len(used), slot_count):
                if self.slots[phys] != 0:
                    raise DiskError("refuse to truncate allocated slot")
            os.ftruncate(self._fd, target_size)
            os.fsync(self._fd)
            # 游标停在在用块数：前 cursor 个槽均已压紧，idle 态再触发时
            # compact_once 的首分支会立即返回完成。
            self.cursor = len(used)
            self.state = STATE_IDLE
            self._commit()
        return True

    def record_error(self) -> None:
        with self.lock.write_lock():
            self.errors += 1
            self.state = STATE_ERROR
            self._commit()

    # ---------- 状态快照 ----------

    def status(self) -> dict:
        with self.lock.read_lock():
            used = sorted(e - 1 for e in self.slots if e)
            slots = list(self.slots)
            state = self.state
            cursor = self.cursor
            moves = self.moves
            errors = self.errors
            gen = self._gen
        return {
            "path": self.path,
            "state": state,
            "slot_count": self.slot_count,
            "used_count": len(used),
            "used_lbns": used,
            "slots": slots,  # 0=空槽，否则为 lbn+1
            "cursor": cursor,
            "moves": moves,
            "errors": errors,
            "generation": gen,
            "file_size": self.file_size,
            "block_size": BLOCK_SIZE,
            "expected_compact_size":
                DATA_OFFSET + len(used) * BLOCK_SIZE,
            "max_temp_bytes": self.max_temp_seen,
        }


def run_compaction_worker(disk: Disk, stop_event: threading.Event | None =
                          None) -> None:
    """后台工作线程：循环推进整理，出错置 error 态并保留游标。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            done = disk.compact_once()
        except Exception:
            disk.record_error()
            raise
        if done:
            return
