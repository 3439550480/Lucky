"""
会话与上下文管理包（05 文档 M5）

模块结构：
    context_store.py     轨迹记账入口（无状态策略层，介质仍是 state["messages"]，§3.1/§3.2）
    history_provider.py  历史供给统一入口（generate_sql/default_answer/路由共用，§3.3）
    session_store.py     checkpointer 存储抽象（InMemory 实现 + Redis/Sqlite 接口预留，§3.5）
    prefix.py            KV cache 固定前缀构建器（§3.4，⑤-2 落地）

核心不变量：能力中间态（retrieved_*/table_infos 等）永不进入轨迹——
轨迹只含 {"role","content"} (+可选 capability/ts 元数据) 的 user/assistant 条目
"""
