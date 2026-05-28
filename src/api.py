"""Hyperliquid info 端点的薄客户端。统一处理 SSL 兼容 + 简单重试。"""
from __future__ import annotations
import json
import os
import time
from typing import Any

import requests
import urllib3

from . import config


class HLClient:
    def __init__(self, base_url: str = config.HL_INFO_URL) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        # 在企业网络下 verify=True 经常因为 MITM 代理失败；保留回退机制。
        self._verify = True
        if os.environ.get("HL_STRICT_SSL") == "1":
            self._verify = True
        elif config.SSL_FALLBACK:
            # 第一次请求时再决定是否要回退。
            pass

    def _do_request(self, body: dict) -> Any:
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                r = self.session.post(
                    self.base_url,
                    data=json.dumps(body),
                    timeout=30,
                    verify=self._verify,
                )
                if r.status_code == 429:
                    # 速率限制 - 退避一下
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.SSLError as e:
                last_err = e
                if self._verify and config.SSL_FALLBACK:
                    # 一次性切换到不验证证书，并在 stderr 提示。
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    self._verify = False
                    print("[api] SSL 证书校验失败，回退到 verify=False（仅本地研究用途）")
                    continue
                raise
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(1 + attempt)
        raise RuntimeError(f"Hyperliquid info 请求失败: {last_err}")

    def call(self, body: dict) -> Any:
        time.sleep(config.REQUEST_SLEEP_S)
        return self._do_request(body)

    # ------------- 各端点的便捷封装 -------------

    def clearinghouse_state(self, user: str) -> dict:
        return self.call({"type": "clearinghouseState", "user": user})

    def spot_clearinghouse_state(self, user: str) -> dict:
        return self.call({"type": "spotClearinghouseState", "user": user})

    def open_orders(self, user: str) -> list:
        return self.call({"type": "openOrders", "user": user})

    def frontend_open_orders(self, user: str) -> list:
        return self.call({"type": "frontendOpenOrders", "user": user})

    def historical_orders(self, user: str) -> list:
        return self.call({"type": "historicalOrders", "user": user})

    def user_fills(self, user: str) -> list:
        return self.call({"type": "userFills", "user": user})

    def user_fills_by_time(self, user: str, start_ms: int, end_ms: int) -> list:
        return self.call({
            "type": "userFillsByTime",
            "user": user,
            "startTime": start_ms,
            "endTime": end_ms,
        })

    def portfolio(self, user: str) -> list:
        return self.call({"type": "portfolio", "user": user})

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list:
        return self.call({
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        })
