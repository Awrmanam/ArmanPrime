from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from shopbot.domain import PricingRule, calculate_price
from shopbot.fx import NavasanRateProvider, RateProviderError


def test_multi_currency_decimal_pricing_rounds_once():
    rule = PricingRule(markup_percent=Decimal("10"))
    assert calculate_price("100", "1250", rule, "2") == 140250
    assert calculate_price("3.333", "65432.125", PricingRule(), "0") == 218085
    assert calculate_price("1", "1", PricingRule(fixed_price_toman=123456), "99") == 123456


@pytest.mark.asyncio
async def test_navasan_parses_rub_eur_and_rejects_invalid_payloads():
    payload = {
        "rub": {"value": "1250.25", "date": "2026-08-30T10:00:00Z"},
        "eur": {"value": "76543.125", "date": "2026-08-30T10:00:00+00:00"},
    }

    async def respond(request):
        assert request.url.params["api_key"] == "test-key"
        return httpx.Response(200, json=payload)

    provider = NavasanRateProvider(
        "test-key", retry_limit=1, transport=httpx.MockTransport(respond)
    )
    rates = await provider.fetch({"RUB", "EUR"})
    assert {item.currency_code: item.toman_per_unit for item in rates} == {
        "RUB": Decimal("1250.25"),
        "EUR": Decimal("76543.125"),
    }
    assert all(item.provider_timestamp == datetime(2026, 8, 30, 10, tzinfo=UTC) for item in rates)
    with pytest.raises(RateProviderError, match="INVALID_RATE_VALUE"):
        provider.parse({"rub": {"value": "0", "date": "2026-08-30T10:00:00Z"}}, {"RUB"})
    with pytest.raises(RateProviderError, match="MISSING_PROVIDER_TIMESTAMP"):
        provider.parse({"rub": {"value": "1"}}, {"RUB"})


@pytest.mark.asyncio
async def test_navasan_timeout_is_bounded_and_sanitized():
    attempts = 0

    async def timeout(_request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("secret-key must never propagate")

    provider = NavasanRateProvider(
        "secret-key", retry_limit=2, transport=httpx.MockTransport(timeout)
    )
    with pytest.raises(RateProviderError, match="PROVIDER_REQUEST_FAILED") as error:
        await provider.fetch({"RUB"})
    assert attempts == 2
    assert "secret-key" not in str(error.value)
