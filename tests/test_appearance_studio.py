from shopbot.appearance_studio import (
    _classify_button,
    _classify_message,
    _search_targets,
    _utf16_slice,
)


def test_appearance_search_finds_button_and_message_targets():
    results = _search_targets("سفارش")
    keys = {(item.group, item.key) for item in results}
    assert ("buttons", "orders") in keys
    assert ("messages", "orders") in keys
    assert ("admin_buttons", "operations") in keys


def test_message_classifier_prefers_warning_and_success_states():
    assert _classify_message("ثبت رسید انجام نشد؛ دوباره تلاش کنید") == "warning"
    assert _classify_message("مدرک با موفقیت ثبت شد") == "success"
    assert _classify_message("کارت مقصد و مبلغ پرداخت") == "payment"
    assert _classify_message("به فروشگاه خوش آمدید") == "welcome"


def test_button_classifier_distinguishes_customer_and_admin_targets():
    assert _classify_button("سفارش‌های من", admin=False) == "orders"
    assert _classify_button("فروشگاه و خرید اشتراک", admin=False) == "catalog"
    assert _classify_button("مالی و پرداخت", admin=True) == "finance"
    assert _classify_button("حذف محصول", admin=True) == "delete"


def test_utf16_slice_handles_emoji_offsets_like_telegram_entities():
    text = "A😀B"
    assert _utf16_slice(text, 1, 2) == "😀"
