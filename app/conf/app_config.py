from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
import os
import re

from omegaconf import OmegaConf

# ====== LLM 多模型配置（01 文档 §2.4）======

# 因为"价格是多少"和"什么时候用哪个价格"是两件本质不同的事，它们的变化原因不同、来源不同、复用方式也不同。混在一起会很难维护。

# 装饰器,用于标记一个类为dataclass，并自动生成__init__方法(少写一些代码)
# Tier 英文原意是"层、级、档"
@dataclass
class PriceTier:
    """单个计费档位的单价（元/百万 token）。
    全部可缺省：厂商未公布的价格项保持 None，成本模块降级为仅 token 计数"""
    input: Optional[float] = None    # 输入单价，可为 None
    input_cache_hit: Optional[float] = None  # 缓存命中的输入单价，可为 None
    output: Optional[float] = None   # 输出单价，可为 None

@dataclass
class PricingConfig:
    """计费档位集合。DeepSeek 含 peak/offpeak 两档（tier_rules 驱动时段判定）；
    Qwen/GLM 仅 standard 一档（tier_rules 为 None，恒用唯一档）"""
    # Dict[键类型, 值类型] —— 字典的类型
    # {
    # "peak": PriceTier(input=4.0, output=12.0),
    # "offpeak": PriceTier(input=2.0, output=8.0),
    # }
    # field(...) = 一张"订单备注单"，你上面写：
    # "这份餐的配菜，每次现做一份新的，别用别人吃剩的。"
    # default_factory=dict = 备注单上写的那句"每次现做一份新的"具体对应哪道工序
    # 具体来说，就是"调用 dict() 现做一份"
    # dict = 厨房里真正干活的厨师
    tiers: Dict[str, Any] = field(default_factory=dict)  # 用来给某个字段做"额外配置"的工具
    # tiers:Dict[str,Any] = {}  会报错，因为字典、列表这类可变对象如果直接当默认值，会被所有实例共享：
    tier_rules: Optional[str] = None


@dataclass
class LLMProviderConfig:
    """一个 provider = 一个可调用的模型条目 + 定价策略。
    注意：providers 是按请求实例化的配置模板，不是连接对象"""
    base_url: str = ""
    api_key: str = ""                 # 键名与 yaml 逐字对应（struct 模式下必须一致）
    model: str = ""
    temperature: float = 0.7
    pricing: PricingConfig = field(default_factory=PricingConfig)
    extra_params: Dict[str, Any] = field(default_factory=dict)

@dataclass
class LLMConfig:
    """替换原 LLMConfig（model_name/api_key/base_url 三字段已废弃，旧定义已删除）"""
    default: str = "deepseek"         # 请求未指定/非法 provider 时的兜底
    providers: Dict[str, Any] = field(default_factory=dict)

# ====== Feature Flags（00 文档 §3 定义表）======

@dataclass
class MemoryFlags:
    short_term: bool = True
    long_term: bool = False

@dataclass
class EvaluationFlags:
    retrieval_metrics: bool = True   # 检索指标
    intent_metrics: bool = True      # 意图指标
    sql_metrics: bool = True         # SQL 指标
    cost_metrics: bool = True        # 成本指标
    tool_metrics: bool = True        # 工具指标
    memory_metrics: bool = True      # 记忆指标

# 总电闸箱
@dataclass
class FeatureFlags:
    usage_tracking: bool = True       # 用量统计     开
    capability_routing: bool = True   # 能力路由     开
    rules_fast_path: bool = True      # 规则快速通道 开
    embedding_route: bool = True      # 向量路由     开
    capability_chip: bool = True      # 能力芯片     开
    # [2.0 S3b] 新能力开关：关 = 路由层视该能力不存在（tier1/2/3 全跳过），恢复 dataquery+default 双能力行为
    capability_inventory: bool = True   # 库存问数    开
    capability_replenish: bool = True   # 补货计划    开
    context_management: bool = True   # 上下文管理   开
    memory: MemoryFlags = field(default_factory=MemoryFlags)  # 记忆模块（子开关箱）
    evaluation: EvaluationFlags = field(default_factory=EvaluationFlags)  # 评估模块（子开关箱）

# ====== 上下文管理（05 文档 §2.2）======
@dataclass
class SessionConfig:
    history_max_turns: int = 10          # 历史最多保留多少轮
    history_max_tokens: int = 4000       # 历史最多保留多少 token
    summary_trigger_turns: int = 20      # [预留] 摘要压缩钩子触发阈值
    summary_keep_recent_turns: int = 5   # [预留] 摘要压缩保留最近几轮

# ====== 记忆管理（06 文档 §2.2）======

@dataclass
class MemoryConfig:
    store_backend: str = "json_file"     # 存储后端（json_file/redis/qdrant）
    store_path: str = "data/memory"      # 存储路径（json_file）
    extract_after_run: bool = True       # 运行后提取
    extraction_provider: str = "deepseek"  # 提取提供者（deepseek/qwen/glm）
    retrieval_top_k: int = 5             # 检索 topk
    similarity_threshold: float = 0.60   # 相似度阈值（bge-small-zh 校准：相关非改写对典型 0.6~0.8，0.80 漏召回）

# ====== 公网 demo 防护（v1.1 PRD FR-05 / 十一节）======

@dataclass
class RateLimitConfig:
    """IP 级限流（公网防滥用第一道闸）。
    enabled=false 恒为旧行为（不拦截）；窗口为内存滑动窗口，进程重启即清零"""
    enabled: bool = False      # 默认关闭 = 旧行为（Feature Flags 纪律）
    per_minute: int = 10       # 单 IP 每分钟提问上限（PRD 估算初值，可按费用校准）


@dataclass
class CostGuardConfig:
    """密钥日费用熔断（PRD FR-05 规则 3 / E-04）。
    soft_limit 只告警；hard_limit 触发后当日暂停对外服务（429+公告文案），次日自动恢复。
    计价口径与 03 评估一致：按"缓存未命中价"核算（pricing 配置复用 app_config.llm）"""
    enabled: bool = False      # 默认关闭 = 旧行为
    soft_limit: float = 5.0    # 元/天，软上限：写告警日志
    hard_limit: float = 15.0   # 元/天，硬上限：自动暂停对外服务


@dataclass
class SecurityConfig:
    """公网部署防护配置段（本地开发保持默认全关）。仅作用于 POST /api/query 问数入口"""
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    cost_guard: CostGuardConfig = field(default_factory=CostGuardConfig)


@dataclass
class AuthConfig:
    """登录会话配置（2.0 上下文策略 0.2；用户拍板：8h 班次级 + 2h 闲置）"""
    token_ttl_hours: int = 8     # 绝对有效期（签发后 8h 强制过期）
    idle_timeout_hours: int = 2  # 闲置超时（连续 2h 无请求即失效）


# ====== 所有 dataclass 定义保持不变 ======
@dataclass
class File:
    enable: bool   # 是否开启
    level: str     # 日志级别
    path: str      # 日志路径
    rotation: str  # 日志轮转
    retention: str # 日志保留

@dataclass
class Console:
    enable: bool  # 是否开启
    level: str    # 日志级别

@dataclass
class LoggingConfig:
    file: File      # 文件日志
    console: Console  # 控制台日志

@dataclass
class DBConfig:
    host: str      # 数据库主机地址
    port: int      # 数据库端口
    user: str      # 数据库用户名
    password: str  # 数据库密码
    database: str  # 数据库名称

@dataclass
class QdrantConfig:
    host: str        # 主机地址
    port: int        # 端口
    embedding_size: int  # 向量维度


@dataclass
class EmbeddingConfig:
    host: str   # 主机地址
    port: int   # 端口
    model: str  # 模型名称

@dataclass
class ESConfig:
    host: str       # 主机地址
    port: int       # 端口
    index_name: str # 索引名称

@dataclass
class SQLConfig:
    max_retries: int  # 最大重试次数
    # retry_delay: int # 重试延迟（秒）

@dataclass
class AppConfig:
    logging: LoggingConfig   # 日志配置
    db_meta: DBConfig        # 元数据库配置
    db_dw: DBConfig          # 数据仓库配置
    qdrant: QdrantConfig     # 向量搜索引擎配置
    embedding: EmbeddingConfig  # 向量生成器配置
    es: ESConfig             # 文档搜索引擎配置
    llm: LLMConfig           # LLM 配置（多厂商 providers）
    sql: SQLConfig           # SQL 配置
    features: FeatureFlags = field(default_factory=FeatureFlags)  # [NEW] 模块开关
    session: SessionConfig = field(default_factory=SessionConfig) # [NEW] 上下文管理
    memory: MemoryConfig = field(default_factory=MemoryConfig)    # [NEW] 记忆管理
    security: SecurityConfig = field(default_factory=SecurityConfig)  # [NEW v1.1] 公网防护
    auth: AuthConfig = field(default_factory=AuthConfig)          # [NEW 2.0] 登录会话

# ====================== 配置加载（修改部分） ======================
# 修改：配置文件在项目根目录的 conf 文件夹下
# 从当前文件位置（app/conf/app_config.py）向上两级到项目根目录，然后进入 conf
config_file = Path(__file__).resolve().parents[2] / "conf" / "app_config.yaml"

# ====== .env 加载（唯一的环境变量来源）======
# 约定：配置中的 ${VAR} 统一只从项目根目录 .env 文件取值，
# Windows 系统/用户环境变量不再参与（避免"key 到底存在哪"的分裂状态）。
# .env 已被 .gitignore 排除，真实密钥不会进仓库；仓库内提供占位符版供参照。

def _load_dotenv(path: Path) -> dict[str, str]:
    """解析 .env 文件 → dict。容错：文件不存在返回空 dict（启动不阻断，
    缺失的 key 由下方 replace_env_var 转成 __MISSING_ENV_ 标记，create_llm 给出明确报错）；
    跳过空行/注释行/无 = 的行；KEY=VALUE 两侧空白剔除；值不支持引号包裹（保持简单）"""
    vars_: dict[str, str] = {}
    if not path.is_file():
        return vars_
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        vars_[key.strip()] = value.strip()
    return vars_

dotenv_vars: dict[str, str] = _load_dotenv(config_file.parent.parent / ".env")

# 先读取 YAML 文件原始内容
with open(config_file, "r", encoding="utf-8") as f:
    raw_yaml = f.read()

# 手动替换 ${VAR_NAME} 为 .env 中的值（唯一来源）
def replace_env_var(match):
    var_name = match.group(1)
    value = dotenv_vars.get(var_name)
    if value is None:
        # .env 缺失该 key 时【不能】保留 ${VAR} 原样：OmegaConf 会把 ${...} 当作内部插值语法
        # 去配置树解析，找不到直接抛 InterpolationKeyError。
        # 改用无害标记占位，create_llm 检测到标记时给出"请在 .env 中设置 X"的明确报错
        return f"__MISSING_ENV_{var_name}__"
    return value

raw_yaml = re.sub(r'\$\{(\w+)}', replace_env_var, raw_yaml)

# 再用 OmegaConf 加载替换后的内容
context = OmegaConf.create(raw_yaml)


# app_config.py 是"表格模板"（定义结构），app_config.yaml 是"填好的表格"（存储数据）。两者合并后，才能得到可用的配置对象 -> 合并后的对象就是 app_config
# 生成 schema 并合并
schema = OmegaConf.structured(AppConfig)
app_config: AppConfig = OmegaConf.to_object(OmegaConf.merge(schema, context))  # 内容+结构


# ====================== 测试 ======================
if __name__ == '__main__':
    print("ES 地址 =", app_config.es.host)
    print("默认 LLM =", app_config.llm.default)
    print("可用 providers =", list(app_config.llm.providers.keys()))
    print("DeepSeek 模型 =", app_config.llm.providers["deepseek"].model)
    print("usage_tracking 开关 =", app_config.features.usage_tracking)
    print("配置加载成功！")
