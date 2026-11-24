# 单卷虚拟磁盘整理台（Flask）

虚拟磁盘回收逻辑块后，尾部的**空槽仍撑着镜像文件**。本项目提供一个 Flask
单卷整理页和一份带空槽的本地镜像，值班员可以：

- 按**逻辑块号**读取 4 KiB 数据；
- 一键启动**整理（compact）**，把在用块向镜像头部压紧并截短文件；
- 实时查看**实际文件长度**、槽位映射、整理游标与搬移次数；
- 注入搬移/截短故障，观察“报错但数据仍可读、重试可续作”。

## 目录结构

```
app/
  disk.py      镜像格式、双份崩溃安全头、原地整理算法、读写锁
  seed.py      内置演示镜像（20 槽，14 个在用块 + 6 个空槽）
  web.py       Flask 应用与 HTTP API
  cli.py       命令行工具（seed/status/verify/compact-once/hard-exit/resume）
  templates/index.html  单页界面
tests/         19 项单元/集成测试（含并发读、硬崩溃续作、撕裂头恢复）
scripts/       可复现演示脚本（Docker 版 / 本地版 / 故障注入版）
Dockerfile, docker-compose.yml, requirements.txt
```

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

镜像文件放在命名卷 `disk-data` 的 `/data/disk.img`，容器重建不丢状态。
环境变量 `MOVE_DELAY`（秒）放慢每次搬移，便于观察整理过程。

### 本地直接运行

```bash
pip install -r requirements.txt
python -m app.seed data/disk.img     # 首次可省略，网页启动时会自动建
python -m app.web                    # 默认 0.0.0.0:8000，DISK_IMAGE/PORT 可覆盖
```

## 镜像格式

| 区域 | 偏移 | 内容 |
|---|---|---|
| 头副本 A | 0 | 4 KiB，世代号 + 状态机 + 槽表 + 整头 CRC32 |
| 头副本 B | 4096 | 同上，两份轮流写，永远至少一份完整有效 |
| 数据槽 0..N-1 | 8192 + i·4096 | 每个 4 KiB；槽表项 0=空槽，否则为“逻辑块号+1” |

内置镜像物理布局（`空`=空槽，数字=逻辑块号）：

```
[0, 空, 1, 2, 空, 3, 4, 5, 空, 6, 7, 8, 空, 9, 10, 11, 空, 12, 13, 空]
```

整理前文件长度 `8192 + 20·4096 = 90112`；整理后恰好
`8192 + 14·4096 = 65536`，逻辑块 0..13 顺序压紧到物理槽 0..13，**块号与
逐字节内容均保持原样**。

## 整理算法如何满足各项约束

1. **只用镜像内空槽，镜像外临时数据 ≤ 4 KiB**
   游标 `i` 要求第 `i` 个在用块进入物理槽 `i`：
   - 槽 `i` 为空：直接把目标块搬入；
   - 槽 `i` 被别的在用块占用：先把该块挪到一个 `≥ i` 的空枢轴槽
     （在用块数 U < 总槽数 S，区间内恒有空槽），再搬目标块。
   每次搬移只经过一个 4 KiB 暂存缓冲（`Disk.max_temp_seen` 可观测），
   不存在第二份临时块。

2. **读请求不等整理，搬移前发出的读也返回完整原数据**
   每次搬移顺序固定：读源槽 → 暂存 → **写目标槽并 fsync** → 提交新槽表。
   槽表提交前读路径始终命中源槽旧数据；提交后目标槽已有完整数据。
   `pread` 在读写锁之外执行，整理的磁盘 I/O 不阻塞读；测试用 6 线程并发读
   及 2400 次真实 HTTP 并发读验证零失败。

3. **搬移/截短报错数据仍可读**
   故障在槽表提交前抛出，盘上状态停留在上一个已提交世代；工作线程把状态置
   为 `error`（持久化），界面可再点“启动整理”从原游标续跑。

4. **进程中途退出后重启可读盘并继续整理**
   状态机（`idle/compacting/error`）、游标、搬移计数全部在双份头里持久化，
   每次提交 `pwrite + fsync` 后才切换活动副本。重启时选取世代号最大的 CRC
   有效副本；若处于 `compacting` 态，网页自动重新拉起工作线程续作。

5. **截短安全**
   仅当全部在用块已压紧到前 U 个槽，并再次确认尾部槽表全为 0 后才
   `ftruncate`，不可能截到在用数据。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 整理台页面 |
| GET | `/api/status` | 状态、实际文件长度、槽位图、游标、搬移/故障计数、临时峰值 |
| GET | `/api/blocks/<lbn>` | 按逻辑块号读 4 KiB（返回前 64 字节 hex 与可打印预览） |
| POST | `/api/compact/start` | 启动整理；error 态调用即续作 |
| POST | `/api/fault/move` | 一次性注入：下一次搬移抛错 |
| POST | `/api/fault/truncate` | 一次性注入：下一次截短抛错 |
| POST | `/api/reseed` | 重建出厂演示镜像（仅非整理中） |

## 可复现的中断续作示例

```bash
# Docker：真实 SIGKILL 强杀容器，重启后自动续作并校验长度与块内容
bash scripts/demo_interrupt_resume.sh

# 无 Docker：子进程 os._exit(42) 硬退出模拟 SIGKILL（不执行任何清理），
# 两次中断后续跑，最后 verify 全部块
bash scripts/demo_local.sh

# 故障注入（需要先启动网页）
bash scripts/demo_fault_injection.sh
```

> 说明：本项目开发所在的沙箱禁止向进程发送信号，自动化测试里用 `os._exit`
> 模拟硬崩溃（语义等价：无 finally、无缓冲刷新、无 fd 清理，恢复只依赖已
> fsync 的盘上状态）；Docker 演示脚本使用真实的 `docker kill --signal=KILL`。

## 测试

```bash
python -m unittest discover -s tests -v
# test_disk: 13 项（格式/整理/故障/并发读/硬崩溃续作/撕裂头/CRC）
# test_web : 6 项（Flask 接口全链路）
```

## 已知边界

- 槽表为定长（单卷上限约 1000 个槽，见 `disk.py` 的 `SLOTS_MAX`）；
- 单工作线程模型：同一时刻只跑一个整理任务，重复启动返回 409；
- 暂存缓冲在内存中（`io.BytesIO`），容量恒 ≤ 4 KiB；若要求暂存也落盘，
  可替换为同目录下的单块临时文件，算法不变。
