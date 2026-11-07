"""Runtime settings for ISHIBASHI AI HEDGE FUND OS.

MODE は v1.0 では PLAN のみをサポートする。PLAN 以外が指定された場合は起動自体を拒否し、
発注経路が誤って有効化されることを防ぐ (MASTER DIRECTIVE: CURRENT MODE)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Mode(str, Enum):
    PLAN = "PLAN"
    DEMO = "DEMO"
    SEMI_AUTO = "SEMI_AUTO"
    LIVE = "LIVE"


SUPPORTED_MODES = {Mode.PLAN}


class ModeNotSupportedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.PLAN
    database_url: str = f"sqlite:///{BASE_DIR / 'ai_hedge_fund.db'}"
    llm_backend: str = "offline"  # "claude" | "offline"
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    web_search: bool = False
    webhook_secret: str = ""
    account_balance: float = 1_000_000.0
    account_currency: str = "JPY"
    risk_per_trade_pct: float = 0.5   # 推奨
    max_risk_per_trade_pct: float = 1.0  # 上限
    current_drawdown_pct: float = 0.0


def load_settings() -> Settings:
    mode = Mode(os.environ.get("HEDGE_FUND_MODE", "PLAN").upper())
    if mode not in SUPPORTED_MODES:
        raise ModeNotSupportedError(
            f"MODE={mode.value} は v1.0 では未実装です。PLAN モードのみ起動できます。"
        )
    return Settings(
        mode=mode,
        database_url=os.environ.get("DATABASE_URL", Settings.database_url),
        llm_backend=os.environ.get("LLM_BACKEND", "offline"),
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        effort=os.environ.get("CLAUDE_EFFORT", "medium"),
        web_search=os.environ.get("ENABLE_WEB_SEARCH", "false").lower() == "true",
        webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
        account_balance=float(os.environ.get("ACCOUNT_BALANCE", "1000000")),
        account_currency=os.environ.get("ACCOUNT_CURRENCY", "JPY"),
        risk_per_trade_pct=float(os.environ.get("RISK_PER_TRADE_PCT", "0.5")),
        max_risk_per_trade_pct=float(os.environ.get("MAX_RISK_PER_TRADE_PCT", "1.0")),
        current_drawdown_pct=float(os.environ.get("CURRENT_DRAWDOWN_PCT", "0")),
    )
