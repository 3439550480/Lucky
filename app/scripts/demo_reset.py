"""
演示重置脚本（06 §6.4 / Lucky2.0 评测体系升级方案 S3.5）

谁调用：演示/验收前由人手动跑（`uv run python -m app.scripts.demo_reset`）。
链路位置：离线运维脚本，不参与 Agent 运行时链路。
设计 why：长对话演示最怕的不是答错，而是"上一场演示留下的长期记忆 / 会话残留"把这一场带偏
（用户已两次提到"演示前要清记忆"）。把这件事脚本化，换演示前 5 分钟的确定性。

动作（按顺序，每步独立可跳过）：
  1) 清 data/memory/*.json —— 先备份为 *.bak.<时间戳>，再置空为 []（记忆库空）
  2) 探测数仓数据窗口（fact_order 的 min/max date_id）—— 演示口径要跟数据窗口对齐
  3) 打印「演示就绪」检查单（记忆库空 / 数据窗口 / 提交版本 / 可选冒烟结果）

可选：
  --smoke N   跑 N 例常规回归冒烟（默认 0=不跑；>0 会真实调用 LLM，产生费用）
  --no-backup 不备份直接清空（默认备份）
  --dataset   冒烟用的数据集（默认 evaluation/datasets/eval_anta.json）

用法示例：
  uv run python -m app.scripts.demo_reset                 # 只清记忆 + 出检查单
  uv run python -m app.scripts.demo_reset --smoke 3       # 再跑 3 例冒烟（花钱）
"""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.conf.app_config import app_config
from app.evaluation.report import _git_commit

_ROOT = Path(__file__).resolve().parents[2]        # 仓库根（app/scripts/demo_reset.py → 上溯 2 层）


def _count_entries(file: Path) -> int:
    """数一个记忆 JSON 文件里的条目数（文件不存在/空/坏 → 0，不抛）"""
    if not file.is_file():
        return 0
    raw = file.read_text(encoding="utf-8").strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
        return len(data) if isinstance(data, list) else 0
    except json.JSONDecodeError:
        return 0


def memory_counts(store_path: str) -> dict:
    """读取当前记忆库条目数（检查单用，只读不改）

    谁调用：main（清理前后各调一次，给出"清了多少条"的确定证据）
    设计 why：清记忆是破坏性动作，必须能自证"清之前有什么、清之后是空"，
    否则演示现场没人敢按这个按钮。

    示例：memory_counts("data/memory") → {"notes": 3, "cards": 0, "total": 3}
    """
    d = Path(store_path)
    notes = _count_entries(d / "notes.json")
    cards = _count_entries(d / "cards.json")
    return {"notes": notes, "cards": cards, "total": notes + cards}


def clear_memory(store_path: str, backup: bool = True) -> dict:
    """清空长期记忆库（notes.json + cards.json 置为 []），返回前后计数

    谁调用：main（--no-backup 可关备份，但默认备份是故意的：清错了好回滚）
    设计 why：演示可重复的前提是"每场从同一状态起跑"。长期记忆是唯一会跨场累积的状态，
    所以它必须能被一键归零；备份文件带时间戳，不会互相覆盖。

    step 1 备份：把现有文件复制成 <name>.<YYYYmmdd-HHMMSS>.bak（同目录）
    step 2 置空：原文件写 "[]"（记忆 store 的空态），保持文件存在以免 store 误判
    step 3 复核：再用 memory_counts 读一次，确认 total == 0（自证成功）

    示例：clear_memory("data/memory")
         → {"before": {...}, "after": {...}, "backups": ["notes.json.20261012-220000.bak"]}
    """
    d = Path(store_path)
    d.mkdir(parents=True, exist_ok=True)
    before = memory_counts(store_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups: list[str] = []
    for name in ("notes.json", "cards.json"):
        f = d / name
        # step 1: 备份（有内容才备份，空文件不留垃圾）
        if backup and f.is_file() and _count_entries(f) > 0:
            bak = d / f"{name}.{stamp}.bak"
            shutil.copy2(f, bak)
            backups.append(bak.name)
        # step 2: 置空
        f.write_text("[]", encoding="utf-8")
    # step 3: 复核
    after = memory_counts(store_path)
    return {"before": before, "after": after, "backups": backups}


async def data_window() -> dict | None:
    """探测数仓数据窗口（fact_order 的 min/max date_id）

    谁调用：main（检查单里"演示口径对齐"一行）
    设计 why：库中数据会被 daily_append 持续追加（见 docs/演示题库.md 维护约定 4），
    演示时口径若与真实窗口不一致，"问今天的数"会答错。先把窗口打出来，人一眼就能对齐。

    示例：await data_window() → {"min_date": 20250101, "max_date": 20261009, "rows": 12345}
    """
    from app.clients.mysql_client_manager import dw_mysql_client_manager
    try:
        dw_mysql_client_manager.init()
        async with dw_mysql_client_manager.session_factory() as session:
            res = await session.execute(text(
                "SELECT MIN(date_id) AS min_date, MAX(date_id) AS max_date, "
                "COUNT(*) AS rows FROM fact_order"))
            row = res.mappings().fetchone()
        return dict(row) if row else None
    except Exception as e:                      # 探不到不算失败：演示可能离线/库未起
        return {"error": str(e)}
    finally:
        if dw_mysql_client_manager.engine is not None:
            await dw_mysql_client_manager.close()


def run_smoke(n: int, dataset: str) -> dict | None:
    """跑 N 例常规回归冒烟（真实调用 LLM，产生费用）

    谁调用：main（仅当 --smoke N > 0）
    设计 why：清完记忆后直接上演示台，等于把"链路是否还通"赌在运气上。
    跑 3 例的成本约几分钱，换"演示链路确实通"的确定性。

    示例：run_smoke(3, "evaluation/datasets/eval_anta.json") → {"returncode": 0}
    """
    if n <= 0:
        return None
    cmd = [sys.executable, "-m", "app.scripts.run_evaluation",
           "-d", dataset, "-e", "demo_smoke", "--limit", str(n)]
    proc = subprocess.run(cmd, cwd=str(_ROOT))
    return {"returncode": proc.returncode}


async def main() -> int:
    """演示重置主流程（检查单是本脚本唯一的输出契约）"""
    # step 0: 参数解析
    ap = argparse.ArgumentParser(description="演示重置：清记忆 + 出「演示就绪」检查单")
    ap.add_argument("--smoke", type=int, default=0, help="跑 N 例常规回归冒烟（>0 真实调 LLM，花钱）")
    ap.add_argument("--dataset", default="evaluation/datasets/eval_anta.json", help="冒烟数据集")
    ap.add_argument("--no-backup", action="store_true", help="不备份直接清空记忆")
    args = ap.parse_args()

    print("===== 演示重置 demo_reset =====")

    # step 1: 清长期记忆（唯一会跨场累积的状态）
    mem = clear_memory(app_config.memory.store_path, backup=not args.no_backup)
    print(f"[1/3] 记忆库：清空 {mem['before']['total']} 条"
          f"（notes {mem['before']['notes']} / cards {mem['before']['cards']}）"
          f" → 现存 {mem['after']['total']} 条")
    for b in mem["backups"]:
        print(f"      备份：{app_config.memory.store_path}/{b}")

    # step 2: 数据窗口（口径对齐）
    win = await data_window()
    print(f"[2/3] 数据窗口：{win}")

    # step 3: 可选冒烟
    smoke = run_smoke(args.smoke, args.dataset)

    # step 4: 「演示就绪」检查单
    print("\n----- 演示就绪检查单 -----")
    print(f"  记忆库为空      : {'是' if mem['after']['total'] == 0 else '否（请检查！）'}"
          f"（{mem['after']['total']} 条）")
    print(f"  数据窗口已探测  : {'是' if win and 'error' not in win else '否（库未起？）'}")
    print(f"  提交版本        : {_git_commit() or '—'}")
    if smoke is None:
        print("  冒烟            : 跳过（--smoke 0；演示前建议 ≥3 例）")
    else:
        print(f"  冒烟            : {'通过' if smoke['returncode'] == 0 else '失败'}"
              f"（退出码 {smoke['returncode']}）")
    ok = mem["after"]["total"] == 0 and (smoke is None or smoke["returncode"] == 0)
    print(f"  结论            : {'演示就绪' if ok else '未就绪（见上）'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")     # Windows 控制台默认 GBK，中文输出会乱码
    sys.exit(asyncio.run(main()))
