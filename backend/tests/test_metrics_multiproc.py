"""S5：Prometheus 多进程渲染与死 pid 文件名解析。

多进程路径必须在**子进程**里测：`MULTIPROC` 在模块 import 时读取，pytest 主进程里
改环境变量已经晚了。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.core.metrics import _pid_of

BACKEND = Path(__file__).resolve().parents[1]


def test_pid_parsing_supports_counter_and_gauge_files() -> None:
    assert _pid_of("counter_1234.db") == 1234
    assert _pid_of("gauge_all_42.db") == 42
    assert _pid_of("counter_not-a-pid.db") is None
    assert _pid_of("README") is None


def test_render_aggregates_under_multiproc_env(tmp_path: Path) -> None:
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "from app.core import metrics\n"
        "dead = Path(os.environ['PROMETHEUS_MULTIPROC_DIR']) / 'counter_999999.db'\n"
        "dead.write_text('x')\n"
        "metrics.cleanup_dead_processes()\n"
        "print('dead_exists', dead.exists())\n"
        "metrics.HTTP_REQUESTS.labels('GET', '/x', '200').inc()\n"
        "metrics.set_task_counts({'queued': 3})\n"
        "metrics.set_snapshot({'pg_connections': 7, 'backup_age_seconds': 1000})\n"
        "print(metrics.render().decode())\n"
    )
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "PYTHONPATH": str(BACKEND),
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "dead_exists False" in result.stdout
    assert "quantsage_http_requests_total" in result.stdout
    assert 'quantsage_tasks{status="queued"} 3.0' in result.stdout
    # 单次 scrape 只出一份，不是「每个进程各一份」再求和
    assert result.stdout.count('quantsage_tasks{status="queued"}') == 1
    assert result.stdout.count("quantsage_http_requests_total{") == 1
    # S8：scrape 快照 Gauge 在多进程下也只出一份（只数样本行，跳过 HELP/TYPE）
    samples = [
        line
        for line in result.stdout.splitlines()
        if line.startswith(("quantsage_pg_connections ", "quantsage_backup_age_seconds "))
    ]
    assert sorted(samples) == [
        "quantsage_backup_age_seconds 1000.0",
        "quantsage_pg_connections 7.0",
    ]
