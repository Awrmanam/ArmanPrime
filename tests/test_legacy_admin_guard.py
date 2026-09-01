from types import SimpleNamespace

from shopbot.enhanced import _drop_legacy_admin_handlers


def _handler(name: str):
    async def callback():
        return None

    callback.__name__ = name
    return SimpleNamespace(callback=callback)


def test_legacy_admin_and_setup_handlers_are_removed():
    handlers = [_handler("start"), _handler("admin"), _handler("setup"), _handler("kyc")]
    router = SimpleNamespace(message=SimpleNamespace(handlers=handlers))

    result = _drop_legacy_admin_handlers(router)

    assert result is router
    assert [handler.callback.__name__ for handler in router.message.handlers] == ["start", "kyc"]
