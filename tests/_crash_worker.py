"""被 tests/test_disk.py 以子进程方式拉起的整理工作进程。

compact-hard: 进入整理态、推进一步后 os._exit(42) 硬退出（模拟 SIGKILL：
              不执行 finally / 不刷新缓冲 / 不关 fd，已 fsync 的盘上状态
              是唯一恢复依据）。
resume      : 自动识别 compacting 态并推进到完成后退出。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.environ.get(
    "PYTHONPATH", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.cli import cmd_hard_exit, cmd_resume  # noqa: E402


def main() -> int:
    image_path, mode = sys.argv[1], sys.argv[2]
    if mode == "compact-hard":
        return cmd_hard_exit(image_path)
    if mode == "resume":
        return cmd_resume(image_path)
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
