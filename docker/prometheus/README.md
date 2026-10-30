# QuantSage 监控（P4-S8）

默认不启动。起停都用 dev.sh（它负责把 token 写成容器可读的文件）：

```bash
bash scripts/dev.sh monitoring start    # Prometheus :9092 + Alertmanager :9093 + alert_hook（按 ALERT_HOOK_ENABLED）
bash scripts/dev.sh monitoring status
bash scripts/dev.sh monitoring stop     # 只停这两个容器，不动 postgres/redis/qdrant
```

前置：`.env` 里 `METRICS_TOKEN` 与 `RAG_SERVICE_TOKEN` 非空（两份 token 不同值）；
容器 scrape 走 Bearer，`host.docker.internal` 已实测可达宿主机回环 8000/8200。

## 看板 = 记录规则 + 查询

缓存命中率的三个口径（`qs:cache_hit_ratio:5m` 是合计，另两个用即时查询）：

```promql
# 合计命中率
qs:cache_hit_ratio:5m

# L1 命中率（分母 = 全部查询：L1 命中 + L2 命中 + 两层都未命中）
sum(rate(quantsage_cache_hits_total{tier="l1"}[5m]))
/
clamp_min(sum(rate(quantsage_cache_hits_total[5m])) + sum(rate(quantsage_cache_misses_total[5m])), 1e-9)

# L2 命中率（分母 = L1 未命中：L2 命中 + 两层都未命中）
sum(rate(quantsage_cache_hits_total{tier="l2"}[5m]))
/
clamp_min(sum(rate(quantsage_cache_hits_total{tier="l2"}[5m])) + sum(rate(quantsage_cache_misses_total[5m])), 1e-9)
```

其余常用查询：

```promql
# 排队 p95（按 kind）
histogram_quantile(0.95, sum by (le, kind) (rate(quantsage_task_queue_seconds_bucket[5m])))

# RAG 服务端降级率（alert 用同一口径；停 RAG 时看 up，不产生比率）
sum(rate(rag_rerank_degraded_total{reason=~"queue_full|timeout"}[5m]))
/
clamp_min(sum(rate(rag_rerank_requests_total[5m])), 1e-9)

# 是否在采集
up{job=~"quantsage-.*"}
```

## 改配置之后

改 `prometheus.yml` 或规则后**重启容器**（`docker compose restart prometheus`）。
实测 v3.15.1：`SIGHUP` 热加载后 `/api/v1/targets` 会变成空列表（scrape 池没恢复），
重启容器才恢复；规则文件本身由 `rule_files` 每次评估读取，但也建议一并重启。

## 规则自测

```bash
docker run --rm -v "$PWD/docker/prometheus/rules:/p:ro" \
  --entrypoint /bin/promtool prom/prometheus:v3.15.1 check rules /p/quantsage.yml
docker run --rm -v "$PWD/docker/prometheus/rules:/p:ro" \
  --entrypoint /bin/promtool prom/prometheus:v3.15.1 test rules /p/quantsage_test.yml
```

## 告警落点

Alertmanager → `http://host.docker.internal:9099/alerts` → `scripts/alert_hook.py`
写 `logs/alerts.log`（JSONL）并发 macOS 通知。hook 只绑回环、不做鉴权，这是本机演练姿态。
