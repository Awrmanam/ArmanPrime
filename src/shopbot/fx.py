from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

ISO_4217 = frozenset(
    {
        "AED",
        "AUD",
        "CAD",
        "CHF",
        "CNY",
        "EGP",
        "EUR",
        "GBP",
        "HKD",
        "INR",
        "JPY",
        "KRW",
        "KWD",
        "NZD",
        "OMR",
        "QAR",
        "RUB",
        "SAR",
        "SGD",
        "TRY",
        "USD",
    }
)
NAVASAN_SYMBOLS = {
    "AED": "aed_sell",
    "CNY": "cny_sell",
    "EUR": "eur",
    "GBP": "gbp",
    "INR": "inr",
    "RUB": "rub",
    "SGD": "sgd",
    "TRY": "try",
    "USD": "usd_sell",
}


class RateProviderError(RuntimeError):
    """Sanitized provider failure; messages never contain credentials or response bodies."""


@dataclass(frozen=True)
class ProviderRate:
    currency_code: str
    toman_per_unit: Decimal
    provider_name: str
    provider_symbol: str
    provider_timestamp: datetime


class RateProvider(Protocol):
    async def fetch(self, currencies: set[str]) -> list[ProviderRate]: ...


def validate_currency(code: str) -> str:
    normalized = code.strip().upper()
    if normalized not in ISO_4217:
        raise ValueError("UNSUPPORTED_ISO_4217_CURRENCY")
    return normalized


class NavasanRateProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.navasan.tech/latest/",
        *,
        connect_timeout: float = 5,
        read_timeout: float = 10,
        retry_limit: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError("NAVASAN_API_KEY_REQUIRED")
        self._api_key = api_key
        self.base_url = base_url
        self.timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self.retry_limit = retry_limit
        self.transport = transport

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise RateProviderError("MISSING_PROVIDER_TIMESTAMP")
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise RateProviderError("INVALID_PROVIDER_TIMESTAMP") from exc
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @classmethod
    def parse(cls, payload: object, currencies: set[str]) -> list[ProviderRate]:
        if not isinstance(payload, dict):
            raise RateProviderError("INVALID_RESPONSE_SHAPE")
        result = []
        for raw_code in currencies:
            code = validate_currency(raw_code)
            symbol = NAVASAN_SYMBOLS.get(code, code.lower())
            item = payload.get(symbol)
            if not isinstance(item, dict):
                raise RateProviderError("UNKNOWN_PROVIDER_SYMBOL")
            try:
                value = Decimal(str(item["value"]))
            except (KeyError, InvalidOperation, TypeError) as exc:
                raise RateProviderError("INVALID_RATE_VALUE") from exc
            if not value.is_finite() or value <= 0:
                raise RateProviderError("INVALID_RATE_VALUE")
            result.append(
                ProviderRate(code, value, "navasan", symbol, cls._timestamp(item.get("date")))
            )
        return result

    async def fetch(self, currencies: set[str]) -> list[ProviderRate]:
        last_code = "PROVIDER_UNAVAILABLE"
        for attempt in range(self.retry_limit):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self.transport
                ) as client:
                    response = await client.get(self.base_url, params={"api_key": self._api_key})
                    response.raise_for_status()
                    return self.parse(response.json(), currencies)
            except RateProviderError:
                raise
            except (httpx.HTTPError, ValueError):
                last_code = "PROVIDER_REQUEST_FAILED"
                if attempt + 1 < self.retry_limit:
                    await asyncio.sleep(min(2**attempt, 4))
        raise RateProviderError(last_code)
