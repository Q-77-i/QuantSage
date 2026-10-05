"""集成测试：小石 MCP 真实握手。

需要：工具包已安装（.env 里的稳定入口有效）+ 有效 Key + 可访问网络。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
REQUIRED_STEPS = {
    "auth_check",
    "initialize",
    "tools_list",
    "get_agent_workflow",
    "bounded_query",
    "adapters_get_tools",
}


def test_mcp_verification_passes(tmp_path: Path) -> None:
    evidence = tmp_path / "mcp-verification.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_xiaoshi_mcp.py",
            "--evidence",
            str(evidence),
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (result.stdout[-1500:] + result.stderr[-1500:])

    payload = json.loads(evidence.read_text())
    assert payload["verdict"] == "pass"
    assert REQUIRED_STEPS <= {step["step"] for step in payload["steps"]}
