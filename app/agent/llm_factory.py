"""
LLM 多模型工厂（01 文档 §3.1）

链路位置：QueryService.query() 按请求调用 create_llm(provider, usage_tracker)
→ 产出的实例放入 DataAgentContext.llm → 各节点经 runtime.context["llm"] 取用。
设计决策：
  1. 统一走 ChatOpenAI（三家均为 OpenAI 兼容端点），不经 init_chat_model 分发 ——
     少一层间接、报错更直白
  2. tracker 以实例级 callbacks 挂载（构造时传入），节点调用 chain.ainvoke 时
     无需感知 —— 这是"用量采集零侵入"的关键
  3. 每次调用都产生新实例（不缓存单例）：tracker 是 Request 级的，实例必须随请求走
"""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.agent.usage import LLMUsageTracker
from app.conf.app_config import app_config
from app.core.log import logger

# 环境变量缺失时 replace_env_var 写入的标记前缀（见 app_config.py 同名约定）
_MISSING_ENV_PREFIX = "__MISSING_ENV_"


def create_llm(provider: str | None = None,
               usage_tracker: LLMUsageTracker | None = None) -> BaseChatModel:
    """按 provider 配置创建聊天模型实例（每次调用产生新实例，含挂载的 tracker）。

    - provider 为 None 或不在配置中 → 回退 app_config.llm.default 并打 warning（不抛异常，
      前端传错值不能炸请求，02 文档宽松校验策略的后端落点）
    - usage_tracker 非 None 时作为实例级 callback 挂载（features.usage_tracking 开启时由
      QueryService 创建传入）
    - api_key 命中 __MISSING_ENV_ 标记 → 抛 RuntimeError 并指明缺哪个环境变量
      （这是本模块唯一主动抛错的地方：没有密钥的请求注定失败，早失败早提示）
    """
    # step 1: 解析 provider —— 未知值回退默认，warning 带上请求原值便于前端排查
    providers = app_config.llm.providers
    requested = provider
    if provider not in providers:
        provider = app_config.llm.default
        if requested is not None:
            logger.warning(f"未知 provider '{requested}'，回退到默认 '{provider}'")
    cfg = providers[provider]
    # step 2: 环境变量缺失检查 —— 唯一主动抛错的地方（快速失败优于运行时 401）
    api_key = cfg.get("api_key", "")
    if api_key.startswith(_MISSING_ENV_PREFIX):
        missing = api_key.removeprefix(_MISSING_ENV_PREFIX).rstrip("_")
        raise RuntimeError(f"环境变量 {missing} 未设置，无法使用 provider '{provider}'")
    # step 3: 构造模型实例 —— temperature 缺省 0.7（与旧 llm.py 一致）；
    # extra_params 经 model_kwargs 透传厂商专有参数（DeepSeek 思考模式开关等，
    # 01 §2.3 机制），callbacks 在此挂载实现零侵入计量
    callbacks = [usage_tracker] if usage_tracker else None
    return ChatOpenAI(
        model=cfg.get("model", ""),
        base_url=cfg.get("base_url", ""),
        api_key=api_key,
        temperature=cfg.get("temperature", 0.7),
        model_kwargs=cfg.get("extra_params", {}) or {},
        callbacks=callbacks,
    )
