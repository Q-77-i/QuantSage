"""M2b 数据接入层：交易日历 + 事件语料归档 → 本地日分区。

模块分工：
  * `archive.py`  —— 调 `xiaoshi-data` CLI 拿归档分片，读回执（不碰密钥 argv、404 = 无分片）
  * `store.py`    —— 分片 → 日分区 Parquet，归一化 `symbols`，写溯源旁注与清单
  * `runner.py`   —— 回填 / 日增量的编排：定日期、串流程、写台账、加锁
  * `scheduler.py`—— 进程内 APScheduler：交易日定时触发 + 启动补缺口

`app/data/calendar.py` 是时间底座（冻结文件，运行期零第三方依赖）。
"""
