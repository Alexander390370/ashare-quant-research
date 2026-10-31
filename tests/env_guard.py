"""保护真实 .env 不被测试覆盖。

背景
----
`cli/prompts.py` 的 `ensure_api_key()` 用下面这行决定往哪个 .env 写：

    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")

`find_dotenv(usecwd=True)` 会**从当前目录向上逐级查找**，直到找到 .env 或到达
根目录。所以 `tests/test_api_key_env.py` 里虽然 `monkeypatch.chdir(tmp_path)`，
但临时目录里没有 .env，查找会一路上溯到仓库根的 .env —— 于是测试用的假 key
`sk-deepseek-test` 被写进了**用户真实的凭据文件**。

实测后果：跑一次全量测试，真实 DEEPSEEK_API_KEY 就被覆盖成 `sk-deepseek-test`，
下一次真实分析直接 401。

防护策略
--------
不在 conftest 里改上游行为（那会让测试与上游分叉），而是：

1. 提供一个显式的备份/恢复工具（本模块的 `backup_env` / `restore_env`）；
2. 在测试会话开始前自动备份 .env，结束后自动恢复（见 conftest 的 hook）。

这样既能跑全量测试，又不会丢凭据。
"""

from __future__ import annotations

import shutil
from pathlib import Path

_ENV_NAME = ".env"
_BACKUP_NAME = ".env.test-backup"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def env_path() -> Path:
    return _REPO_ROOT / _ENV_NAME


def backup_path() -> Path:
    return _REPO_ROOT / _BACKUP_NAME


def backup_env() -> bool:
    """把真实 .env 备份起来。返回 True 表示做了备份。"""
    source = env_path()
    if not source.exists():
        return False
    shutil.copy2(source, backup_path())
    return True


def restore_env() -> bool:
    """用备份覆盖 .env，并删掉备份。返回 True 表示做了恢复。"""
    saved = backup_path()
    if not saved.exists():
        return False
    shutil.copy2(saved, env_path())
    saved.unlink()
    return True


def env_key_looks_like_test() -> bool:
    """检查当前 .env 里的 key 是否被测试污染（形如 sk-xxx-test）。"""
    target = env_path()
    if not target.exists():
        return False
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.rstrip().endswith("-test")
    return False
