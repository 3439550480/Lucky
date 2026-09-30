"""
KV cache 固定前缀构建器（05 文档 §3.4）

前缀 = system prompt（含能力清单与工具定义），进程内逐字节恒定。
⚠️ 前缀变更纪律（§3.4）：任何字节变更使全部缓存失效——修改视为协议级变更，
必须递增 PREFIX_VERSION 并登记修订记录（能力注册表 description 的修改同样受此约束）。
"""
from app.agent.capabilities.registry import registry
from app.prompt.prompt_loader import load_prompt

PREFIX_VERSION = "v1"          # 前缀协议版本——system_prompt.prompt 内容变更时必须递增


def build_system_prefix() -> str:
    """渲染固定前缀（system_prompt.prompt + registry 能力清单）。
    每次调用重新渲染但结果恒定（registry 进程内不变）——不做模块级缓存，
    与其它 prompt 的加载方式保持一致，避免测试时的状态残留。
    占位符用 __CAPABILITIES__ + replace 而非 str.format：前缀内含工具定义 JSON 花括号，
    format 会把 JSON 的花括号误当占位符解析"""
    capabilities = registry.build_llm_context()   # 04 已有的能力清单渲染（复用，不另造格式）
    return load_prompt("system_prompt").replace("__CAPABILITIES__", capabilities)
