"""Execution Agent / MT5 Bridge.

PLAN モードでは無効。発注・決済・口座操作の要求はすべて ExecutionDisabledError で拒否する。
v1.0 には MT5 への接続コードそのものを含めない（DEMO / LIVE は将来バージョンで実装し、
その際も Risk Manager の APPROVED と人間の承認を前提とする）。
"""
from __future__ import annotations

from .config import Mode
from .models import TradingSignal


class ExecutionDisabledError(PermissionError):
    pass


class MT5Bridge:
    def __init__(self, mode: Mode):
        self.mode = mode

    def _guard(self, action: str) -> None:
        if self.mode is Mode.PLAN:
            raise ExecutionDisabledError(f"PLAN モードでは {action} は禁止されています（提案のみ）。")
        raise ExecutionDisabledError(f"MODE={self.mode.value} の {action} は v1.0 では未実装です。")

    def send_order(self, signal: TradingSignal, lot: float) -> None:
        self._guard("注文送信")

    def close_position(self, ticket: int) -> None:
        self._guard("決済")

    def modify_position(self, ticket: int, sl: float | None = None, tp: float | None = None) -> None:
        self._guard("ポジション変更")
