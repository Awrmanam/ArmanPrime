from decimal import Decimal

import pytest

from shopbot.domain import PricingRule, calculate_price, decimal_value


def test_fractional_markup_rounds_half_up():
    rule = PricingRule(markup_percent=Decimal("10.125"))
    assert calculate_price("1.005", 100, rule) == 111


def test_fees_reserve_and_fixed_cost_are_decimal_safe():
    rule = PricingRule(
        platform_fee_percent=Decimal("1.5"),
        payment_fee_percent=Decimal("2.25"),
        warranty_reserve_percent=Decimal("0.75"),
        fixed_cost_toman=100,
        markup_percent=Decimal("10"),
    )
    assert calculate_price(Decimal("10"), 1_000, rule) == 11_605


def test_target_margin_mode():
    rule = PricingRule(target_margin_percent=Decimal("20"))
    assert calculate_price("10", 1_000, rule) == 12_500


def test_fixed_toman_override():
    assert calculate_price("999", 999, PricingRule(fixed_price_toman=123_457)) == 123_457


def test_owner_rounding_increment_applies_exactly_once():
    rule = PricingRule(markup_percent=Decimal("10"), rounding_increment_toman=5_000)
    assert calculate_price("20", "1250", rule, "3") == 30_000


@pytest.mark.parametrize(
    "rule",
    [
        PricingRule(target_margin_percent=Decimal("100")),
        PricingRule(target_margin_percent=Decimal("-1")),
        PricingRule(markup_percent=Decimal("-0.1")),
        PricingRule(platform_fee_percent=Decimal("100")),
        PricingRule(warranty_reserve_percent=Decimal("-1")),
    ],
)
def test_invalid_percentages_are_rejected(rule):
    with pytest.raises(ValueError):
        calculate_price("10", 100, rule)


def test_float_is_never_silently_converted():
    with pytest.raises(TypeError):
        decimal_value(0.1)
