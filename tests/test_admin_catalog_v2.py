from types import SimpleNamespace

from shopbot import app_v2
from shopbot.admin_catalog_v2 import _decimal_label, _duration_label, _error_message


def test_catalog_v2_normalizes_common_duration_and_decimal_labels():
    assert _duration_label("1month") == "1 ماه"
    assert _duration_label("12 Months") == "12 ماه"
    assert _duration_label("45 روز") == "45 روز"
    assert _decimal_label("1000.00000000") == "1000"
    assert _decimal_label("4.50000000") == "4.5"


def test_catalog_v2_has_specific_validation_messages():
    assert "https://" in _error_message("INVALID_SUPPLIER_URL")
    assert "عدد" in _error_message("INVALID_FIXED_PRICE")
    assert "سابقه" in _error_message("PLAN_HAS_HISTORY")


def test_app_v2_puts_catalog_router_before_existing_routers(monkeypatch):
    catalog_router = object()

    class FakeDispatcher:
        def __init__(self):
            self.sub_routers = ["delivery", "admin", "variant", "legacy"]

        def include_router(self, router):
            self.sub_routers.append(router)

    dispatcher = FakeDispatcher()
    repo = SimpleNamespace(variant_store=object())
    runtime = SimpleNamespace(repo=repo, dispatcher=dispatcher)
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))

    monkeypatch.setattr(app_v2, "create_base_app", lambda _settings: app)
    monkeypatch.setattr(app_v2, "build_admin_catalog_v2_router", lambda _repo, _store: catalog_router)

    result = app_v2.create_app(object())

    assert result is app
    assert dispatcher.sub_routers[0] is catalog_router
    assert dispatcher.sub_routers[1:] == ["delivery", "admin", "variant", "legacy"]
