"""Flask 网页接口测试：读盘、整理、故障注入与续作、重建镜像。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.disk import BLOCK_SIZE, DATA_OFFSET  # noqa: E402
from app.seed import block_content, build as build_seed  # noqa: E402
from app.web import create_app  # noqa: E402

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:  # pragma: no cover
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "Flask 未安装")
class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = os.path.join(self.tmp.name, "disk.img")
        build_seed(self.img)
        self.app = create_app(self.img)
        self.client = self.app.test_client()

    def tearDown(self):
        disk = self.app.config.get("DISK")
        if disk is not None:
            stop = self.app.config.get("WORKER_STOP")
            if stop is not None:
                stop.set()
            thread = self.app.config.get("WORKER_THREAD")
            if thread is not None:
                thread.join(timeout=5)
            disk.close()
        self.tmp.cleanup()

    def _wait_state(self, state, timeout=10):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.client.get("/api/status").get_json()
            if last["state"] == state:
                return last
            time.sleep(0.05)
        raise AssertionError(f"状态未到达 {state}，最后状态 {last}")

    def test_index_and_status(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        st = self.client.get("/api/status").get_json()
        self.assertEqual(st["used_count"], 14)
        self.assertEqual(st["file_size"], DATA_OFFSET + 20 * BLOCK_SIZE)

    def test_read_block_api(self):
        resp = self.client.get("/api/blocks/3").get_json()
        self.assertEqual(resp["size"], BLOCK_SIZE)
        # 与种子内容逐字节一致（接口只展示前 64 字节）。
        self.assertEqual(bytes.fromhex(resp["hex"]),
                         block_content(3)[:64])
        self.assertEqual(self.client.get("/api/blocks/77").status_code, 404)

    def test_compaction_via_api(self):
        r = self.client.post("/api/compact/start")
        self.assertEqual(r.status_code, 200)
        st = self._wait_state("idle")
        self.assertEqual(st["file_size"], DATA_OFFSET + 14 * BLOCK_SIZE)
        # 整理后每个逻辑块仍可按原号读取，内容不变。
        for lbn in range(14):
            resp = self.client.get(f"/api/blocks/{lbn}").get_json()
            self.assertEqual(bytes.fromhex(resp["hex"]),
                             block_content(lbn)[:64])

    def test_injected_move_error_then_resume(self):
        self.client.post("/api/fault/move")
        self.client.post("/api/compact/start")
        st = self._wait_state("error")
        # 错误态数据仍可读。
        self.assertEqual(self.client.get("/api/blocks/0").status_code, 200)
        # 续跑至完成。
        self.client.post("/api/compact/start")
        st = self._wait_state("idle")
        self.assertEqual(st["file_size"], DATA_OFFSET + 14 * BLOCK_SIZE)
        self.assertGreaterEqual(st["errors"], 1)

    def test_concurrent_reads_while_compacting(self):
        import threading
        errors = []
        stop = threading.Event()

        def reader():
            n = 0
            while not stop.is_set() and n < 200:
                r = self.client.get(f"/api/blocks/{n % 14}")
                if r.status_code != 200:
                    errors.append(r.status_code)
                n += 1

        threads = [threading.Thread(target=reader) for _ in range(4)]
        self.client.post("/api/compact/start")
        for t in threads:
            t.start()
        self._wait_state("idle")
        stop.set()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])

    def test_reseed(self):
        self.client.post("/api/compact/start")
        self._wait_state("idle")
        r = self.client.post("/api/reseed")
        self.assertEqual(r.status_code, 200)
        st = self.client.get("/api/status").get_json()
        self.assertEqual(st["file_size"], DATA_OFFSET + 20 * BLOCK_SIZE)
        self.assertEqual(st["state"], "idle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
