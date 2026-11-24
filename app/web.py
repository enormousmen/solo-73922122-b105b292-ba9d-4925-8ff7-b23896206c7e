"""Flask 单卷整理页：读逻辑块、启动整理、查看实际文件长度与状态。"""

from __future__ import annotations

import os
import threading

from flask import Flask, jsonify, render_template, request

from .disk import (DATA_OFFSET, STATE_COMPACTING, STATE_ERROR, STATE_IDLE,
                   Disk, DiskError, run_compaction_worker)
from .seed import build as build_seed_image


def create_app(image_path: str | None = None) -> Flask:
    image_path = image_path or os.environ.get(
        "DISK_IMAGE",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "disk.img"))
    move_delay = float(os.environ.get("MOVE_DELAY", "0"))

    app = Flask(__name__)
    app.config["DISK"] = None
    app.config["WORKER_THREAD"] = None
    app.config["WORKER_STOP"] = None

    def get_disk() -> Disk:
        disk: Disk | None = app.config["DISK"]
        if disk is None:
            if not os.path.exists(image_path):
                os.makedirs(os.path.dirname(image_path) or ".", exist_ok=True)
                build_seed_image(image_path)
            disk = Disk(image_path)
            disk.move_delay = move_delay
            app.config["DISK"] = disk
            # 上次进程在整理中退出：重启后台线程从持久化游标续作。
            if disk.state == STATE_COMPACTING:
                _spawn_worker(disk)
        return disk

    def _spawn_worker(disk: Disk) -> None:
        stop = threading.Event()
        app.config["WORKER_STOP"] = stop
        thread = threading.Thread(
            target=run_compaction_worker, args=(disk, stop),
            name="compact-worker", daemon=True)
        app.config["WORKER_THREAD"] = thread
        thread.start()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def api_status():
        return jsonify(get_disk().status())

    @app.get("/api/blocks/<int:lbn>")
    def api_read(lbn: int):
        disk = get_disk()
        try:
            data = disk.read_lbn(lbn)
        except KeyError:
            return jsonify({"error": f"逻辑块 {lbn} 不存在（空槽或已回收）"}), 404
        except DiskError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "lbn": lbn,
            "size": len(data),
            "hex": data[:64].hex(),
            "preview": _safe_preview(data[:64]),
        })

    @app.post("/api/compact/start")
    def api_compact_start():
        disk = get_disk()
        if disk.state == STATE_COMPACTING:
            return jsonify({"ok": False, "message": "整理已在进行中"}), 409
        try:
            disk.start_compaction()
        except DiskError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        _spawn_worker(disk)
        return jsonify({"ok": True, "message": "整理已启动/续跑"})

    @app.post("/api/fault/move")
    def api_fault_move():
        disk = get_disk()
        disk.inject_move_error = True
        return jsonify({"ok": True, "message": "下一次搬移将报错（一次性）"})

    @app.post("/api/swap")
    def api_swap():
        """对调两段逻辑地址区间：业务区 ⇄ 备用区，只改盘头槽表。

        整理进行中也可调用；对调后整理按新的逻辑块号顺序继续压紧。
        """
        disk = get_disk()
        body = request.get_json(silent=True) or {}
        try:
            start_a = int(body["a"])
            start_b = int(body["b"])
            count = int(body["count"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "需要整数参数 a、b、count"}), 400
        try:
            result = disk.swap_ranges(start_a, start_b, count)
        except DiskError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({
            "ok": True,
            "message": (f"已对调 [{start_a},{start_a + count}) ⇄ "
                        f"[{start_b},{start_b + count})，"
                        f"重登记 {result['remapped_slots']} 个槽"),
            **result,
        })

    @app.post("/api/fault/truncate")
    def api_fault_truncate():
        disk = get_disk()
        disk.inject_truncate_error = True
        return jsonify({"ok": True, "message": "下一次截短将报错（一次性）"})

    @app.post("/api/reseed")
    def api_reseed():
        """恢复出厂镜像，便于反复演示（仅允许在 idle 态操作）。"""
        disk = get_disk()
        if disk.state == STATE_COMPACTING:
            return jsonify({"ok": False,
                            "error": "整理进行中，不能重建镜像"}), 409
        disk.close()
        app.config["DISK"] = None
        if app.config["WORKER_STOP"] is not None:
            app.config["WORKER_STOP"].set()
            app.config["WORKER_STOP"] = None
        build_seed_image(image_path)
        return jsonify({"ok": True, "message": "演示镜像已重建"})

    return app


def _safe_preview(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def main() -> None:
    app = create_app()
    port = int(os.environ.get("PORT", "8000"))
    # threaded=True：读请求与整理线程并发，验证读不等待整理。
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
