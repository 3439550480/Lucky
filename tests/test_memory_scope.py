"""
双层记忆作用域 GWT 单测（2.0 上下文策略 2.1/2.3/2.7）
运行：PYTHONPATH=. uv run python tests/test_memory_scope.py
"""
import json
import tempfile
from pathlib import Path

from app.agent.auth.staff_identity import StaffIdentity
from app.agent.memory.extractor import _downgrade_scope
from app.agent.memory.retriever import _visible
from app.agent.memory.store import JsonFileMemoryStore, MemoryCard, SimpleNote, new_id

MGR = StaffIdentity("M001", "王志远", ["MANAGER"], frozenset({"inventory.read", "inventory.write", "dataquery.query", "replenish.store", "replenish.warehouse"}))
TEMP = StaffIdentity("T001", "周小雨", ["TEMP"], frozenset({"inventory.read", "dataquery.query"}))


def _store(tmp: Path) -> JsonFileMemoryStore:
    return JsonFileMemoryStore(str(tmp))


def test_scope_roundtrip(tmp_path: Path):
    """Given 双层条目 When 存取 Then scope/owner_id 完整往返"""
    store = _store(tmp_path)
    store.save_note(SimpleNote(id=new_id(), content="本人负责鞋类区域", ts=1.0,
                               source_thread_id="t", scope="personal", owner_id="M001"))
    store.save_card(MemoryCard(id=new_id(), subject="门店", relation_to_user="规则 → 补货按此口径",
                               facts=["本店补货覆盖天数：鞋 7"], narrative="店长确认",
                               ts=1.0, source_thread_id="t", scope="store", owner_id="ST001"))
    note = store.all_notes()[0]
    card = store.all_cards()[0]
    assert note.scope == "personal" and note.owner_id == "M001"
    assert card.scope == "store" and card.owner_id == "ST001"


def test_legacy_entries_compat(tmp_path: Path):
    """Given 存量 JSON（无 scope/owner_id 字段）When 加载 Then 默认 personal+空 owner（全员可见）"""
    store = _store(tmp_path)
    legacy = [{"id": "x", "content": "旧全局记忆", "ts": 1.0, "source_thread_id": "t", "vector": None}]
    store.notes_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    note = store.all_notes()[0]
    assert note.scope == "personal" and note.owner_id == ""
    assert _visible(note, TEMP)            # 存量条目不因升级消失


def test_visibility_personal_isolated(tmp_path: Path):
    """Given M001 的个人层记忆 When T001 检索 Then 不可见；M001 可见"""
    store = _store(tmp_path)
    store.save_note(SimpleNote(id=new_id(), content="本人负责鞋类区域", ts=1.0,
                               source_thread_id="t", scope="personal", owner_id="M001"))
    items = store.all_notes()
    assert _visible(items[0], MGR)
    assert not _visible(items[0], TEMP)


def test_visibility_store_shared(tmp_path: Path):
    """Given 门店层记忆 When 任意员工检索 Then 可见"""
    store = _store(tmp_path)
    store.save_card(MemoryCard(id=new_id(), subject="门店", relation_to_user="规则 → 口径",
                               facts=["补货口径"], narrative="", ts=1.0,
                               source_thread_id="t", scope="store", owner_id="ST001"))
    assert _visible(store.all_cards()[0], TEMP)


def test_gate_downgrade(tmp_path: Path):
    """Given 门店层写入 When 店长/库管 Then 通过；临时工/无身份 Then 降级 personal（不丢弃）"""
    assert _downgrade_scope("store", MGR) == "store"
    keeper = StaffIdentity("K001", "李慧", ["STAFF", "KEEPER"],
                           frozenset({"inventory.read", "inventory.write", "dataquery.query", "replenish.store"}))
    assert _downgrade_scope("store", keeper) == "store"
    assert _downgrade_scope("store", TEMP) == "personal"
    assert _downgrade_scope("store", None) == "personal"
    assert _downgrade_scope("personal", MGR) == "personal"


if __name__ == "__main__":
    import inspect
    import tempfile as _tf

    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        if "tmp_path" in inspect.signature(fn).parameters:
            with _tf.TemporaryDirectory() as td:
                fn(Path(td))
        else:
            fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
