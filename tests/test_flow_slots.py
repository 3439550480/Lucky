"""
出入库槽位纯函数 GWT 单测（2.0 上下文策略 3.2/3.5）
运行：PYTHONPATH=. uv run python tests/test_flow_slots.py
"""
import time

from app.agent.nodes.inventory_write.slots import (
    detect_write_intent,
    flow_expired,
    is_cancel,
    is_confirm,
    parse_direction,
    parse_doc_no,
    parse_qty,
    parse_sku,
)


def test_parse_direction():
    assert parse_direction("入库 RK2026101001 ... 20") == "in"
    assert parse_direction("收货 RK1") == "in"
    assert parse_direction("出库 5 件") == "out"
    assert parse_direction("昨天卖了多少") is None


def test_parse_doc_no():
    assert parse_doc_no("入库 rk2026101001 15262011-01-42 20") == "RK2026101001"
    assert parse_doc_no("出库 XS2026100201 20 件") == "XS2026100201"
    assert parse_doc_no("入库 20 件") is None


def test_parse_sku():
    assert parse_sku("入库 RK1 15262011-01-42 20") == "15262011-01-42"
    assert parse_sku("氢跑8 的 42 码") is None          # v1 不做模糊匹配


def test_parse_qty():
    assert parse_qty("入库 RK2026101001 15262011-01-42 20") == 20
    assert parse_qty("入库 RK1 ... 35 件") == 35
    assert parse_qty("入库 20") == 20                    # 剔除单据号后独立整数
    assert parse_qty("入库 RK2026101001") is None        # 无独立数量（不能拿单据号当数量）
    assert parse_qty("入库 RK1 15262011-01-42 0") is None  # 零件数无效


def test_cancel_confirm():
    assert is_cancel("算了不弄了") and is_cancel("取消")
    assert not is_cancel("确认提交")
    assert is_confirm("确认") and is_confirm("OK")
    assert not is_confirm("取消")


def test_write_intent_no_hijack():
    """窄规则：句首方向词才劫持——查询类语句不误入流程"""
    assert detect_write_intent("入库 RK2026101001 15262011-01-42 20")
    assert detect_write_intent("我要入库")
    assert detect_write_intent("登记出库 XS1 5 件")
    # 误劫持防线
    assert not detect_write_intent("昨天入库了多少件")
    assert not detect_write_intent("查一下出库流水")
    assert not detect_write_intent("国庆期间销售出库有多少")


def test_flow_expired():
    pending = {"started_at": time.time() - 700}
    assert flow_expired(pending)                      # >10 分钟
    assert not flow_expired({"started_at": time.time() - 60})
    assert not flow_expired({})                       # 无时间戳不误判


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
