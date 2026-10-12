"""
额外上下文补全节点

负责在 SQL 生成前补齐不来自召回结果、但模型必须知道的运行环境信息
当前包括日期信息和数据库方言/版本，后续可继续扩展权限、租户、时区等上下文
"""
import asyncio
from datetime import date

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState, DateInfoState, DBInfoState
from app.core.log import logger


def _fmt_date_id(value) -> str:
    """date_id（20261001）→ 可读日期（2026-10-01）；异常值原样返回字符串"""
    try:
        s = str(int(value))
    except (TypeError, ValueError):
        return str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


async def add_extra_context(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """补齐 SQL 生成所需的日期和数据库环境信息"""

    writer = runtime.stream_writer
    step = "增加额外上下文"
    writer({"type": "progress", "step": step, "status": "running"})
    await asyncio.sleep(0.5)
    try:
        dw_mysql_repository = runtime.context["dw_mysql_repository"]

        # 当前日期信息会帮助模型处理“今天 本月 本季度 最近 N 天”等相对时间表达
        today = date.today()
        date_str = today.strftime("%Y-%m-%d")
        weekday = today.strftime("%A")
        quarter = f"Q{(today.month - 1) // 3 + 1}"

        # [S0 持续运营] 数据边界动态化：daily_append 每天把库追加到「昨天」，数据窗口
        # 会随运营时间增长——提示词里写死日期段必然与库脱节（问"昨天卖了多少钱"会被
        # 按过期边界拒答）。改为每次实时查事实表跨度注入 date_info.data_range。
        # 降级纪律：查询失败只留空串 + warning，不阻断主链路（边界缺失时模型退化为
        # 只看【今天】日期，不会比现状更差）。
        data_range = ""
        try:
            range_rows = await dw_mysql_repository.run(
                "SELECT MIN(date_id) AS mn, MAX(date_id) AS mx FROM fact_sales",
                timeout_ms=5000, max_rows=1,
            )
            if range_rows and range_rows[0].get("mn") is not None:
                data_range = (f"{_fmt_date_id(range_rows[0]['mn'])}"
                              f" ~ {_fmt_date_id(range_rows[0]['mx'])}")
        except Exception as e:
            logger.warning(f"数据边界查询失败（降级为空串，不阻断主链路）: {e}")

        date_info = DateInfoState(date=date_str, weekday=weekday, quarter=quarter,
                                  data_range=data_range)

        # 数据库方言和版本会影响函数名 日期运算 limit 语法等 SQL 细节
        db = await dw_mysql_repository.get_db_info()
        db_info = DBInfoState(**db)
        logger.info(f"数据库信息：{db_info}")
        logger.info(f"日期信息：{date_info}")

        writer({"type": "progress", "step": step, "status": "success"})
        return {"date_info": date_info, "db_info": db_info}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise