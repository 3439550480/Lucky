"""
出入库固定流程（2.0 上下文策略 第三章）

形态：槽位驱动的确定性多轮流程——LLM 不参与流程推进（策略 3.1：
货号/数量/方向任一认错都直接污染库存，写操作要求幂等、二次确认、留痕）。
  flow_guard  —— START 后前置守卫：续流程 / 探测写意图（含权限门禁）/ 放行路由
  flow_step   —— 槽位表驱动推进（一次能填多少填多少）+ 确认门前关卡 + 写库
"""
from app.agent.nodes.inventory_write.flow_guard import flow_guard
from app.agent.nodes.inventory_write.flow_step import flow_step

__all__ = ["flow_guard", "flow_step"]
