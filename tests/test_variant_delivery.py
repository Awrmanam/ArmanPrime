from shopbot.delivery_router import build_delivery_payload


def context(kind: str) -> dict:
    return {
        "family_title": "ChatGPT",
        "variant_title": "Plus — 1 Month",
        "fulfillment_type": kind,
        "warranty_text": "30 روز",
    }


def test_activation_code_delivery_is_contextual():
    body, link = build_delivery_payload(context("activation_code"), {"code": "ABC-123"})
    assert "ChatGPT — Plus — 1 Month" in body
    assert "کد فعال‌سازی" in body
    assert "ABC-123" in body
    assert "30 روز" in body
    assert "یک رویداد جدید فروشگاه ثبت شد" not in body
    assert link is None


def test_activation_link_delivery_keeps_link_separate():
    body, link = build_delivery_payload(
        context("activation_link"), {"link": "https://example.com/gift"}
    )
    assert "لینک فعال‌سازی" in body
    assert link == "https://example.com/gift"


def test_account_activation_delivery_does_not_ask_for_link_or_code():
    body, link = build_delivery_payload(
        context("account_no_login"), {"note": "اکانت را یک بار باز و بسته کنید."}
    )
    assert "فعال‌سازی روی حساب شما با موفقیت انجام شد" in body
    assert "اکانت را یک بار باز و بسته کنید" in body
    assert link is None


def test_account_credentials_preview_masks_password():
    data = {"identifier": "buyer@example.com", "password": "temporary-secret"}
    preview, _ = build_delivery_payload(context("account_credentials"), data, preview=True)
    delivery, _ = build_delivery_payload(context("account_credentials"), data)
    assert "buyer@example.com" in preview
    assert "••••••••" in preview
    assert "temporary-secret" not in preview
    assert "temporary-secret" in delivery


def test_custom_delivery_can_include_optional_link():
    body, link = build_delivery_payload(
        context("custom"),
        {"content": "تحویل سفارشی انجام شد.", "link": "https://example.com/result"},
    )
    assert "تحویل سفارشی انجام شد" in body
    assert link == "https://example.com/result"