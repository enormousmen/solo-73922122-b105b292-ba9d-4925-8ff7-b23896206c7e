"""disk.py 的单元/集成测试（不依赖 Flask，可直接 python -m unittest）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.disk import (BLOCK_SIZE, DATA_OFFSET, HEADER_SIZE, STATE_COMPACTING,
                      STATE_ERROR, STATE_IDLE, Disk, DiskError,
                      _pack_header, _unpack_header)
from app.seed import SEED_LAYOUT, block_content, build as build_seed


class DiskTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.img = os.path.join(self.tmp.name, "disk.img")
        build_seed(self.img)
        self.expected = {lbn: block_content(lbn)
                         for lbn in SEED_LAYOUT if lbn is not None}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def assertAllBlocksReadable(self, disk: Disk) -> None:
        for lbn, content in self.expected.items():
            self.assertEqual(disk.read_lbn(lbn), content,
                             f"lbn {lbn} 内容被破坏")

    def open(self) -> Disk:
        return Disk(self.img)


class TestSeedAndRead(DiskTestBase):
    def test_seed_layout(self):
        disk = self.open()
        st = disk.status()
        self.assertEqual(st["slot_count"], 20)
        self.assertEqual(st["used_count"], 14)
        self.assertEqual(st["used_lbns"], list(range(14)))
        self.assertEqual(st["file_size"], DATA_OFFSET + 20 * BLOCK_SIZE)
        self.assertAllBlocksReadable(disk)
        disk.close()

    def test_read_missing_lbn(self):
        disk = self.open()
        with self.assertRaises(KeyError):
            disk.read_lbn(99)
        disk.close()

    def test_file_size_holds_tail(self):
        # 整理前：尾部还有空槽，文件仍按全部 20 槽占位。
        disk = self.open()
        self.assertGreater(disk.file_size, disk.expected_compact_size())
        disk.close()


class TestCompaction(DiskTestBase):
    def test_full_compaction(self):
        disk = self.open()
        disk.start_compaction()
        while not disk.compact_once():
            pass
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        # 文件长度恰好容纳头 + 全部在用块。
        self.assertEqual(disk.file_size,
                         DATA_OFFSET + 14 * BLOCK_SIZE)
        self.assertEqual(disk.file_size, disk.expected_compact_size())
        # 前 14 槽依次是 lbn 0..13；头槽表定长，截短后尾部条目保持为 0。
        self.assertEqual(st["slots"][:14], [i + 1 for i in range(14)])
        self.assertEqual(set(st["slots"][14:]), {0})
        self.assertAllBlocksReadable(disk)
        # 镜像外临时数据峰值不超过 4 KiB。
        self.assertLessEqual(st["max_temp_bytes"], BLOCK_SIZE)
        disk.close()

    def test_idempotent_compact(self):
        disk = self.open()
        disk.start_compaction()
        while not disk.compact_once():
            pass
        moves_before = disk.moves
        # 已是压紧状态：再次整理只走空步，不产生搬移，长度不变。
        disk.start_compaction()
        while not disk.compact_once():
            pass
        self.assertEqual(disk.moves, moves_before)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        disk.close()

    def test_move_error_keeps_data_and_resumes(self):
        disk = self.open()
        disk.start_compaction()
        disk.inject_move_error = True
        failed = False
        while True:
            try:
                done = disk.compact_once()
            except RuntimeError:
                failed = True
                disk.record_error()
                break
            if done:
                break
        self.assertTrue(failed)
        self.assertEqual(disk.status()["state"], STATE_ERROR)
        # 报错时所有块依旧可读且内容不变。
        self.assertAllBlocksReadable(disk)
        cursor = disk.cursor
        disk.close()

        # 重启后续跑。
        disk = self.open()
        self.assertEqual(disk.status()["state"], STATE_ERROR)
        disk.start_compaction()
        while not disk.compact_once():
            pass
        self.assertEqual(disk.status()["state"], STATE_IDLE)
        self.assertGreaterEqual(disk.cursor, cursor)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        self.assertAllBlocksReadable(disk)
        disk.close()

    def test_truncate_error_keeps_data_and_resumes(self):
        disk = self.open()
        disk.start_compaction()
        # 先把数据全部压紧，再让截短失败。
        disk.inject_truncate_error = True
        while True:
            try:
                done = disk.compact_once()
            except RuntimeError:
                disk.record_error()
                break
            if done:
                break
        self.assertEqual(disk.status()["state"], STATE_ERROR)
        self.assertAllBlocksReadable(disk)
        # 截短失败，文件长度保持原样。
        self.assertEqual(disk.file_size, DATA_OFFSET + 20 * BLOCK_SIZE)
        disk.close()

        disk = self.open()
        disk.start_compaction()
        while not disk.compact_once():
            pass
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        self.assertAllBlocksReadable(disk)
        disk.close()

    def test_temp_never_exceeds_one_block(self):
        disk = self.open()
        disk.start_compaction()
        while not disk.compact_once():
            self.assertLessEqual(disk.max_temp_seen, BLOCK_SIZE)
        disk.close()


class TestConcurrentReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = os.path.join(self.tmp.name, "disk.img")
        build_seed(self.img)
        self.expected = {lbn: block_content(lbn)
                         for lbn in SEED_LAYOUT if lbn is not None}

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_during_compaction_always_complete(self):
        disk = Disk(self.img)
        disk.move_delay = 0.02
        errors: list[Exception] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                lbn = threading.get_ident() % 14
                try:
                    data = disk.read_lbn(lbn)
                    if data != self.expected[lbn]:
                        errors.append(AssertionError(f"lbn {lbn} 数据错误"))
                except Exception as exc:  # 整理期间任何异常都算失败
                    errors.append(exc)
                time.sleep(0.001)

        readers = [threading.Thread(target=reader) for _ in range(6)]
        disk.start_compaction()
        for t in readers:
            t.start()
        while not disk.compact_once():
            pass
        stop.set()
        for t in readers:
            t.join(timeout=5)
        self.assertEqual(errors, [])
        for lbn, content in self.expected.items():
            self.assertEqual(disk.read_lbn(lbn), content)
        disk.close()


class TestCrashRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = os.path.join(self.tmp.name, "disk.img")
        build_seed(self.img)

    def tearDown(self):
        self.tmp.cleanup()

    def _helper(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_crash_worker.py")

    def test_kill_mid_compaction_then_resume(self):
        helper = self._helper()
        env = dict(os.environ, PYTHONPATH=os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        # 第一轮：工作进程推进一个整理步骤后 os._exit 硬退出（模拟被 SIGKILL）。
        run1 = subprocess.run(
            [sys.executable, helper, self.img, "compact-hard"],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run1.returncode, 42)
        self.assertIn("COMPACT_STARTED", run1.stdout)

        # 第二轮：再次硬退出，验证多轮中断后仍可续作。
        run2 = subprocess.run(
            [sys.executable, helper, self.img, "compact-hard"],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run2.returncode, 42)

        # 崩溃后立即重开：头里应记录 compacting 态，且数据完整可读。
        disk = Disk(self.img)
        self.assertEqual(disk.status()["state"], STATE_COMPACTING)
        for lbn in range(14):
            self.assertEqual(disk.read_lbn(lbn), block_content(lbn))
        disk.close()

        # 第三轮：新进程自动续作直到完成。
        out = subprocess.run(
            [sys.executable, helper, self.img, "resume"],
            env=env, check=True, capture_output=True, text=True, timeout=30)
        self.assertIn("state=idle", out.stdout)
        disk = Disk(self.img)
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        for lbn in range(14):
            self.assertEqual(disk.read_lbn(lbn), block_content(lbn))
        disk.close()

    def test_torn_header_copy_is_ignored(self):
        disk = Disk(self.img)
        disk.start_compaction()
        for _ in range(3):
            disk.compact_once()
        disk.close()
        # 人为撕毁活动副本：非活动副本仍带完整旧世代，应被选中。
        with open(self.img, "r+b") as fh:
            active = self._active_copy(self.img)
            fh.seek(active * BLOCK_SIZE)
            fh.write(b"\xff" * BLOCK_SIZE)
            fh.flush()
            os.fsync(fh.fileno())
        disk = Disk(self.img)
        self.assertIn(disk.status()["state"],
                      (STATE_COMPACTING, STATE_IDLE))
        for lbn in range(14):
            self.assertEqual(disk.read_lbn(lbn), block_content(lbn))
        disk.close()

    @staticmethod
    def _active_copy(path: str) -> int:
        d = Disk(path)
        idx = d._active  # noqa: SLF001 - 测试需要内部指针
        d.close()
        return idx


class TestHeaderCodec(unittest.TestCase):
    def test_roundtrip(self):
        slots = [0, 1, 0, 5, 0]
        buf = _pack_header(7, STATE_COMPACTING, len(slots), 2, 3, 1, slots)
        self.assertEqual(len(buf), BLOCK_SIZE)
        h = _unpack_header(buf)
        self.assertEqual(h["generation"], 7)
        self.assertEqual(h["state"], STATE_COMPACTING)
        self.assertEqual(h["slots"], slots)
        self.assertEqual(h["cursor"], 2)

    def test_corruption_detected(self):
        buf = bytearray(_pack_header(1, STATE_IDLE, 3, 0, 0, 0, [0, 0, 0]))
        buf[100] ^= 0xFF
        with self.assertRaises(DiskError):
            _unpack_header(bytes(buf))


if __name__ == "__main__":
    unittest.main(verbosity=2)
