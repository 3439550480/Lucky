"""
记忆管理包（06 文档 M6）

模块结构：
    store.py      SimpleNote/MemoryCard 双结构 + 存储抽象 + JsonFile 实现（§3.1/§3.2）
    extractor.py  记忆提取（正则预筛 + LLM 提取，QueryService finally 阶段调用，§3.3）
    retriever.py  检索注入块渲染（注入提示词动态尾部，KV cache 纪律，§3.4）

Agent 定位：管家角色——记忆的价值不止"答对"，更在于支撑主动性
（主动使用已知偏好与关系）。
核心约束：注入块只出现在提示词动态尾部，固定前缀（05 prefix.py）永不因记忆内容变化。
"""
