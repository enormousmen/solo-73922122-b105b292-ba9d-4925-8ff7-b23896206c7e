"""被 tests/test_disk.py 以子进程方式拉起的整理工作进程。

compact-hard : 进入整理态、推进一步后 os._exit(42) 硬退出（模拟 SIGKILL：
               不执行 finally / 不刷新缓冲 / 不关 fd，已 fsync 的盘上状态
               是唯一恢复依据）。
resume       : 自动识别 compacting 态并推进到完成后退出。
swap-hard    : 执行一次逻辑地址对调，提交成功后 os._exit(42) 硬退出
               （验证已确认成功的对调在重启后仍有效）。
move-half-hard: 进入整理态，推进到第一次真实搬移，把数据写进目标槽并
               fsync，但不提交槽表就 os._exit(42)：盘上留下“盘块已搬到
               目标槽、尚未登记新位置”的中间态。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.environ.get(
    "PYTHONPATH", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.cli import cmd_hard_exit, cmd_resume  # noqa: E402
from app.disk import Disk, lbn_to_slot  # noqa: E402


def _swap_hard(image_path: str, a: int, b: int, count: int) -> int:
    disk = Disk(image_path)
    disk.swap_ranges(a, b, count)
    print(f"SWAPPED a={a} b={b} n={count}", flush=True)
    os._exit(42)


def _move_half_hard(image_path: str) -> int:
    disk = Disk(image_path)
    disk.start_compaction()
    while True:
        used = sorted(e - 1 for e in disk.slots if e)
        cursor = disk.cursor
        if cursor >= len(used):
            break
        target_phys = lbn_to_slot(disk.slots)[used[cursor]]
        if target_phys != cursor and disk.slots[cursor] == 0:
            # 与 _move_block 相同的落盘顺序，但停在提交槽表之前。
            disk._write_slot(cursor, disk._read_slot(target_phys))
            print(f"HALF_MOVE slot {target_phys} -> slot {cursor}, "
                  f"not registered", flush=True)
            os._exit(42)
        disk.compact_once()
    os._exit(42)


def main() -> int:
    image_path, mode = sys.argv[1], sys.argv[2]
    if mode == "compact-hard":
        return cmd_hard_exit(image_path)
    if mode == "resume":
        return cmd_resume(image_path)
    if mode == "swap-hard":
        return _swap_hard(image_path, int(sys.argv[3]), int(sys.argv[4]),
                          int(sys.argv[5]))
    if mode == "move-half-hard":
        return _move_half_hard(image_path)
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
