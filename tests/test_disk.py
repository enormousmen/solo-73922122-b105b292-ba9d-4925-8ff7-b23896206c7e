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
                      _pack_header, _unpack_header, lbn_to_slot,
                      run_compaction_worker)
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


class TestSwap(DiskTestBase):
    """逻辑地址对调：只改盘头、等长不相交、未分配一起换、可切回。"""

    def test_swap_basic_mapping_and_content(self):
        disk = self.open()
        size_before = disk.file_size
        # 逐槽记录物理数据，验证对调不搬任何块。
        slots_data_before = [disk._read_slot(p)
                             for p in range(disk.slot_count)]
        res = disk.swap_ranges(0, 11, 3)
        self.assertEqual(res["count"], 3)
        self.assertEqual(res["remapped_slots"], 6)
        st = disk.status()
        # 只改盘头：文件长度不变，每个物理槽的数据逐字节不变。
        self.assertEqual(disk.file_size, size_before)
        for p in range(disk.slot_count):
            self.assertEqual(disk._read_slot(p), slots_data_before[p])
        # 备好的数据切到业务地址，原业务数据留在备用地址。
        self.assertEqual(disk.read_lbn(0), block_content(11))
        self.assertEqual(disk.read_lbn(1), block_content(12))
        self.assertEqual(disk.read_lbn(2), block_content(13))
        self.assertEqual(disk.read_lbn(11), block_content(0))
        self.assertEqual(disk.read_lbn(12), block_content(1))
        self.assertEqual(disk.read_lbn(13), block_content(2))
        # 区间外照旧。
        for lbn in range(3, 11):
            self.assertEqual(disk.read_lbn(lbn), block_content(lbn))
        # 在用块总数不变。
        self.assertEqual(st["used_count"], 14)
        disk.close()

    def test_swap_same_operation_switches_back(self):
        disk = self.open()
        disk.swap_ranges(2, 9, 4)
        disk.swap_ranges(2, 9, 4)  # 同一操作切回
        self.assertAllBlocksReadable(disk)
        disk.close()

    def test_swap_unallocated_positions_move_with_range(self):
        disk = self.open()
        # lbn 18、19 未分配：随区间一起交换。
        disk.swap_ranges(5, 18, 2)
        self.assertEqual(disk.read_lbn(18), block_content(5))
        self.assertEqual(disk.read_lbn(19), block_content(6))
        with self.assertRaises(KeyError):
            disk.read_lbn(5)
        with self.assertRaises(KeyError):
            disk.read_lbn(6)
        st = disk.status()
        self.assertEqual(st["used_count"], 14)
        self.assertEqual(st["used_lbns"],
                         [0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 18, 19])
        disk.close()

    def test_swap_validation(self):
        disk = self.open()
        bad_args = [
            (2, 3, 2),      # 区间重叠
            (3, 2, 2),      # 反向也重叠
            (0, 0, 1),      # 同一起点
            (0, 5, 0),      # 块数为 0
            (0, 5, -1),     # 块数为负
            (-1, 5, 1),     # 起点为负
            (5, -1, 1),
            (0, Disk.MAX_LBN + 1, 1),  # 超出槽表可编码范围
        ]
        for a, b, n in bad_args:
            with self.assertRaises(DiskError, msg=f"{a},{b},{n}"):
                disk.swap_ranges(a, b, n)
        # 全部校验失败不产生任何改动。
        self.assertAllBlocksReadable(disk)
        self.assertEqual(disk.status()["used_lbns"], list(range(14)))
        disk.close()

    def test_swap_persists_across_reopen(self):
        disk = self.open()
        disk.swap_ranges(0, 11, 3)
        disk.close()
        # 已确认成功的对调在重启后仍有效。
        disk = self.open()
        self.assertEqual(disk.read_lbn(0), block_content(11))
        self.assertEqual(disk.read_lbn(11), block_content(0))
        disk.close()

    def test_swap_then_compaction_packs_by_new_order(self):
        disk = self.open()
        disk.swap_ranges(0, 11, 3)
        self.assertEqual(disk.cursor, 0)  # 游标归零，按新映射重扫
        disk.start_compaction()
        while not disk.compact_once():
            pass
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        # 按新的逻辑块号顺序压紧：物理槽 i 依次登记 lbn i。
        self.assertEqual(st["slots"][:14], [i + 1 for i in range(14)])
        self.assertEqual(set(st["slots"][14:]), {0})
        # 后续读块按新映射取数。
        self.assertEqual(disk.read_lbn(0), block_content(11))
        self.assertEqual(disk.read_lbn(11), block_content(0))
        disk.close()

    def test_swap_unallocated_then_compaction(self):
        disk = self.open()
        disk.swap_ranges(5, 18, 2)  # 引入新的逻辑块号 18、19
        disk.start_compaction()
        while not disk.compact_once():
            pass
        st = disk.status()
        expected_used = [0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 18, 19]
        self.assertEqual(st["used_lbns"], expected_used)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        for i, lbn in enumerate(expected_used):
            self.assertEqual(st["slots"][i], lbn + 1)
        self.assertEqual(disk.read_lbn(18), block_content(5))
        self.assertEqual(disk.read_lbn(19), block_content(6))
        disk.close()

    def test_swap_during_running_compaction(self):
        """整理进行中插入对调：整理按新映射压紧截短，读块按新映射取数。"""
        disk = self.open()
        disk.move_delay = 0.02
        disk.start_compaction()
        stop = threading.Event()
        worker = threading.Thread(target=run_compaction_worker,
                                  args=(disk, stop), daemon=True)
        worker.start()
        # 等整理真正开始搬移后再对调，覆盖搬移窗口内外的交错。
        deadline = time.time() + 5
        while disk.moves < 2 and time.time() < deadline:
            time.sleep(0.005)
        disk.swap_ranges(0, 11, 3)
        worker.join(timeout=30)
        stop.set()
        self.assertFalse(worker.is_alive())
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        self.assertEqual(st["slots"][:14], [i + 1 for i in range(14)])
        expected = {lbn: block_content(lbn) for lbn in range(14)}
        for a, b in ((0, 11), (1, 12), (2, 13)):
            expected[a], expected[b] = expected[b], expected[a]
        for lbn, content in expected.items():
            self.assertEqual(disk.read_lbn(lbn), content,
                             f"lbn {lbn} 整理途中对调后内容错误")
        disk.close()

    def test_swap_right_before_truncate_defers_truncate(self):
        """对调恰好落在最后一次搬移后、截短前：本次截短被放弃，
        整理按新映射重新压紧后再截短。"""
        disk = self.open()
        disk.start_compaction()
        # 推进到游标走完所有在用块（下一次调用将截短）。
        while disk.cursor < len(disk.used_lbns()):
            disk.compact_once()
        self.assertEqual(disk.status()["state"], STATE_COMPACTING)
        disk.swap_ranges(4, 13, 2)  # lbn4⇄lbn13，lbn5⇄未分配的 lbn14
        self.assertFalse(disk.compact_once())  # 截短被对调打断
        while not disk.compact_once():
            pass
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        expected_used = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        self.assertEqual(st["used_lbns"], expected_used)
        for i, lbn in enumerate(expected_used):
            self.assertEqual(st["slots"][i], lbn + 1)
        origin_of = {l: l for l in range(14)}
        origin_of[4], origin_of[13] = 13, 4
        origin_of[14] = 5
        del origin_of[5]
        for lbn, origin in origin_of.items():
            self.assertEqual(disk.read_lbn(lbn), block_content(origin))
        disk.close()

    def test_swap_after_completion_then_recompact(self):
        """整理完成后对调：映射立即生效、读块正确；
        值班员再次整理后按新的逻辑块号顺序压紧。"""
        disk = self.open()
        disk.start_compaction()
        while not disk.compact_once():
            pass
        self.assertEqual(disk.status()["state"], STATE_IDLE)
        disk.swap_ranges(4, 13, 2)
        # 映射已生效（物理顺序待下次整理重排，读块始终正确）。
        self.assertEqual(disk.read_lbn(4), block_content(13))
        self.assertEqual(disk.read_lbn(13), block_content(4))
        self.assertEqual(disk.read_lbn(14), block_content(5))
        with self.assertRaises(KeyError):
            disk.read_lbn(5)
        disk.start_compaction()
        while not disk.compact_once():
            pass
        st = disk.status()
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        expected_used = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        for i, lbn in enumerate(expected_used):
            self.assertEqual(st["slots"][i], lbn + 1)
        disk.close()

    def test_swap_while_compaction_paused_in_error_state(self):
        """整理故障停在 error 态时也可对调，续跑按新映射完成。"""
        disk = self.open()
        disk.start_compaction()
        disk.inject_move_error = True
        try:
            while not disk.compact_once():
                pass
        except RuntimeError:
            disk.record_error()
        self.assertEqual(disk.status()["state"], STATE_ERROR)
        disk.swap_ranges(0, 11, 3)
        disk.start_compaction()  # error 态续跑
        while not disk.compact_once():
            pass
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        self.assertEqual(disk.read_lbn(0), block_content(11))
        self.assertEqual(disk.read_lbn(11), block_content(0))
        disk.close()


class TestSwapCrashRecovery(unittest.TestCase):
    """对调的崩溃原子性与持久性（子进程 os._exit 模拟 SIGKILL）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = os.path.join(self.tmp.name, "disk.img")
        build_seed(self.img)

    def tearDown(self):
        self.tmp.cleanup()

    def _helper(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_crash_worker.py")

    def _run_helper(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        return subprocess.run(
            [sys.executable, self._helper(), self.img, *args],
            env=env, capture_output=True, text=True, timeout=30)

    def test_committed_swap_survives_hard_exit(self):
        # 对调提交成功后硬退出：重开只能看到交换后的布局。
        run = self._run_helper("swap-hard", "0", "11", "3")
        self.assertEqual(run.returncode, 42)
        self.assertIn("SWAPPED", run.stdout)
        disk = Disk(self.img)
        self.assertEqual(disk.read_lbn(0), block_content(11))
        self.assertEqual(disk.read_lbn(2), block_content(13))
        self.assertEqual(disk.read_lbn(11), block_content(0))
        for lbn in range(3, 11):
            self.assertEqual(disk.read_lbn(lbn), block_content(lbn))
        disk.close()

    def test_torn_header_during_swap_commit_shows_old_layout(self):
        disk = Disk(self.img)
        disk.swap_ranges(0, 11, 3)
        disk.close()
        # 撕毁活动头副本，模拟对调提交写头途中崩溃：
        # 重开只能看到整批交换前的布局，且数据完好。
        probe = Disk(self.img)
        active = probe._active  # noqa: SLF001 - 测试需要内部指针
        probe.close()
        with open(self.img, "r+b") as fh:
            fh.seek(active * BLOCK_SIZE)
            fh.write(b"\xff" * BLOCK_SIZE)
            fh.flush()
            os.fsync(fh.fileno())
        disk = Disk(self.img)
        for lbn in range(14):
            self.assertEqual(disk.read_lbn(lbn), block_content(lbn))
        disk.close()

    def test_swap_completes_when_compaction_stops_mid_move(self):
        # 盘块已搬到目标槽、尚未登记新位置时整理硬停：
        # 重开后对调仍能完成，续跑按新映射压紧截短。
        run = self._run_helper("move-half-hard")
        self.assertEqual(run.returncode, 42)
        self.assertIn("HALF_MOVE", run.stdout)
        disk = Disk(self.img)
        self.assertEqual(disk.status()["state"], STATE_COMPACTING)
        # 中间态下对调成功：只改盘头，不受未登记的已搬数据影响。
        disk.swap_ranges(1, 15, 2)
        self.assertEqual(disk.read_lbn(15), block_content(1))
        self.assertEqual(disk.read_lbn(16), block_content(2))
        with self.assertRaises(KeyError):
            disk.read_lbn(1)
        disk.close()
        # 续跑整理直到完成。
        out = self._run_helper("resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("state=idle", out.stdout)
        disk = Disk(self.img)
        st = disk.status()
        self.assertEqual(st["state"], STATE_IDLE)
        self.assertEqual(disk.file_size, DATA_OFFSET + 14 * BLOCK_SIZE)
        expected_used = [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16]
        self.assertEqual(st["used_lbns"], expected_used)
        for i, lbn in enumerate(expected_used):
            self.assertEqual(st["slots"][i], lbn + 1)
        for lbn in expected_used:
            origin = {15: 1, 16: 2}.get(lbn, lbn)
            self.assertEqual(disk.read_lbn(lbn), block_content(origin),
                             f"lbn {lbn} 内容错误")
        disk.close()


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
