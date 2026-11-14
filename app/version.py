"""アプリのバージョン（日付 YYYYMMDD）。

exe は作った日（tools/build_exe.py が app/_build_version.py に書いて exe に入れる。公開用 zip の名前と同じ日付）。
ソースから動かすときは、最後のコミットの日付に -dev を付ける（git が無ければ dev）。
"""
from __future__ import annotations

import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_date() -> str | None:
    """最後のコミットの日付（YYYYMMDD）。git が無い・リポジトリでなければ None。"""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cd", "--date=format:%Y%m%d"], cwd=ROOT,
                             capture_output=True, text=True, timeout=3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    date = out.stdout.strip()
    return date if out.returncode == 0 and len(date) == 8 and date.isdigit() else None


def get_version() -> str:
    try:
        from ._build_version import VERSION as built     # exe を作ったときだけある（Git の管理外）
        return built
    except ImportError:
        pass
    date = _git_date()
    return f"{date}-dev" if date else "dev"


VERSION = get_version()
