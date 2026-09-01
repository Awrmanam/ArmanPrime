from shopbot.admin_menu import (
    ADMIN_HOME_TEXT,
    ADMIN_SECTIONS,
    LEGACY_ADMIN_PREFIX,
)


def test_admin_menu_is_grouped_and_keeps_management_actions():
    assert tuple(ADMIN_SECTIONS) == (
        "catalog",
        "operations",
        "finance",
        "content",
        "system",
    )

    actions = {
        action
        for _title, _description, items in ADMIN_SECTIONS.values()
        for _label, action, _style in items
    }
    assert actions == {
        "admin.category",
        "admin.product",
        "admin.orders",
        "admin.kyc",
        "admin.cards",
        "admin.rate",
        "admin.pricing",
        "admin.merchant",
        "admin.terms",
        "admin.page",
        "admin.kyc_page",
        "admin.emoji",
        "admin.appearance",
        "admin.management",
        "admin.audit",
    }


def test_admin_menu_main_screen_stays_compact():
    assert len(ADMIN_SECTIONS) == 5
    assert all(len(items) <= 5 for _title, _description, items in ADMIN_SECTIONS.values())


def test_legacy_admin_bridge_catches_old_home_without_rewriting_new_home():
    old_home = (
        "پنل مدیریت\n\n"
        "وضعیت آمادگی فروشگاه: آماده فروش\n"
        "نرخ ارز: حالت دستی اضطراری"
    )

    assert old_home.startswith(LEGACY_ADMIN_PREFIX)
    assert not ADMIN_HOME_TEXT.startswith(LEGACY_ADMIN_PREFIX)
