-- Lucky demo 数据库只读账号（v1.1 PRD 十一节："公网 Text2SQL 第一道硬防线"）
--
-- why：Text2SQL 生成的 SQL 理论上可能包含写操作/删表；应用层校验之上，
-- 从 MySQL 账号权限层面（仅 SELECT）物理杜绝写风险，优先级高于一切应用层防护。
--
-- 用法（服务器上执行）：
--   mysql -h 127.0.0.1 -P 3307 -u root -p < deploy/mysql_readonly.sql
--   （密码请勿写进任何文件；执行后把 conf/app_config.yaml db_dw.user/password
--    改为 lucky_ro/<新密码> 并同步到服务器 .env 同等管控）
--
-- 注意：lzs 账号 + Lzs666 密码已入 git 历史（PRD 外发现），公网部署必须：
--   1) 改用本只读账号连 dw 库；2) 修改 lzs 密码或仅绑定 localhost；
--   3) meta 库仍需写权限（建库脚本写元数据），仅限本地/内网使用，不暴露公网。

-- 1. 创建只读账号（密码替换为强密码；仅允许本机连入，公网走后端进程转发）
CREATE USER IF NOT EXISTS 'lucky_ro'@'localhost' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';

-- 2. 只授 dw 库 SELECT（问数链路唯一需要查询的库）
GRANT SELECT ON dw.* TO 'lucky_ro'@'localhost';

-- 3. 立即生效并验证：用 lucky_ro 登录后执行任何写 SQL 应被拒绝
FLUSH PRIVILEGES;
