"""app/api/middleware 中间件包（v1.1 新增）。

挂载在 main.py，作用于 HTTP 层；当前只有公网防护类中间件（rate_limit）。
中间件一律"默认关闭 = 旧行为"（Feature Flags 纪律）。
"""
