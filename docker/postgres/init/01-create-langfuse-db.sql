-- Langfuse 自托管（observability profile）使用的独立库。
-- 仅在数据卷为空时由 docker-entrypoint-initdb.d 执行一次；后开 observability 时
-- 也可能被手工重跑，所以这里做成幂等：库已存在时 \gexec 不执行任何语句。
-- 扩展（pg_trgm 等）留到 T3 真正启动 Langfuse 时按需添加，避免 init 失败连累默认三服务。
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')
\gexec
