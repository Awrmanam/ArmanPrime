from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from shopbot.repository import AccessDenied
from shopbot.runtime import answer_keyboard, markup, persistent_router
from shopbot.telegram_adapter import Button


class RedisFake:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, **_):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        return sum(self.values.pop(key, None) is not None for key in keys)

    async def scan_iter(self, match):
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key


class CoordinatorFake:
    def __init__(self):
        self.redis = RedisFake()
        self.state = {}
        self.counter = 0

    async def rate_limit(self, *_):
        return True

    async def issue_callback(self, action, actor, object_id="", version=1, **_):
        self.counter += 1
        token = f"c1.token{self.counter}"
        self.state[token] = {"a": action, "u": actor, "o": object_id, "v": version}
        assert len(token.encode()) <= 64
        return token

    async def resolve_callback(self, token, actor):
        state = self.state[token]
        if state["u"] != actor:
            raise AccessDenied
        return state


class SessionsFake:
    @asynccontextmanager
    async def begin(self):
        yield SimpleNamespace()


class RepoFake:
    def __init__(self):
        self.coordinator = CoordinatorFake()
        self.sessions = SessionsFake()
        self.vault = SimpleNamespace(
            encrypt=lambda value: f"enc:{value}", decrypt=lambda value: value.removeprefix("enc:")
        )
        self.terms = None
        self.accepted = False
        self.owner_id = 1

    def owner(self, actor):
        if actor != self.owner_id:
            raise AccessDenied

    async def user(self, actor, _session):
        return SimpleNamespace(id=uuid4(), telegram_id=actor, kyc_status="VERIFIED")

    async def current_terms(self, _session):
        return self.terms

    async def setup_status(self, _):
        return {
            "terms": bool(self.terms),
            "rate": False,
            "pricing": False,
            "merchant": False,
            "category": False,
            "product": False,
        }

    async def has_current_consent(self, *_):
        return self.accepted

    async def accept_terms(self, *_):
        self.accepted = True

    async def categories(self):
        return [SimpleNamespace(id=uuid4(), title="Category", custom_emoji_id="premium")]

    async def owner_categories(self, _):
        return [
            SimpleNamespace(id=uuid4(), title="Category", active=True, custom_emoji_id="premium")
        ]

    async def owner_products(self, _):
        return [SimpleNamespace(id=uuid4(), title="Product", active=True, pricing_override=None)]

    async def owner_merchant_cards(self, _):
        return [
            SimpleNamespace(
                id=uuid4(),
                bank_name="Bank",
                masked_pan="**** 4444",
                active=True,
                priority=1,
                daily_limit=1000,
            )
        ]

    async def pages(self, _):
        return [SimpleNamespace(id=uuid4(), slug="home")]

    async def page_buttons(self, _, page_id):
        return [
            SimpleNamespace(
                id=uuid4(),
                page_id=page_id,
                text="Catalog",
                active=True,
                custom_emoji_id="premium",
            )
        ]

    async def products(self, category_id):
        return [
            SimpleNamespace(
                id=uuid4(), category_id=category_id, title="Product", custom_emoji_id="premium"
            )
        ]

    async def resolve_emoji_key(self, key):
        return "123456" if key == "premium" else None

    async def emojis(self, _, active_only=False):
        return [SimpleNamespace(id=uuid4(), name="premium", custom_emoji_id="123456", active=True)]

    async def product(self, product_id):
        return SimpleNamespace(
            id=product_id,
            title="Product",
            description="Description",
            duration="30 days",
            plan_type="Plan",
            activation_method="Link",
            warranty_text="Warranty",
            delivery_minutes=60,
        )

    async def verified_cards(self, _):
        return [SimpleNamespace(id=uuid4(), bank_name="Bank", masked_pan="**** 1111")]

    async def customer_orders(self, _):
        return [SimpleNamespace(id=uuid4(), status="PROCESSING", amount_toman=100)]

    async def create_quote(self, *_):
        now = datetime.now(UTC)
        return SimpleNamespace(
            id=uuid4(),
            version=1,
            snapshot={"title": "Product"},
            final_toman=100,
            created_at=now,
            expires_at=now + timedelta(minutes=30),
        )

    async def final_check(self, *_):
        return SimpleNamespace(id=uuid4(), amount_toman=100)

    async def reveal_destination(self, *_):
        return "5555555555554444", "Holder"

    async def requote(self, *_):
        return await self.create_quote()

    async def kyc_queue(self, _):
        return [SimpleNamespace(id=uuid4())]

    async def card_queue(self, _):
        return [SimpleNamespace(id=uuid4(), bank_name="Bank", masked_pan="**** 1111")]

    async def order_queue(self, _):
        return [
            SimpleNamespace(id=uuid4(), status="AWAITING_RECONCILIATION", assigned_admin_id=None)
        ]

    async def payment_for_order(self, *_):
        return SimpleNamespace(receipt_file_id="receipt-file")

    async def audit_events(self, _):
        return [SimpleNamespace(at=datetime.now(UTC), action="test", target="safe")]

    async def submit_kyc(self, *args):
        return SimpleNamespace(id=uuid4())

    async def submit_customer_card(self, _, bank, pan, __):
        assert len(pan) == 16
        return SimpleNamespace(bank_name=bank, masked_pan="**** " + pan[-4:])

    async def submit_receipt(self, *_):
        return SimpleNamespace(status="AWAITING_RECONCILIATION")

    publish_terms = AsyncMock(return_value=SimpleNamespace(version=2))
    set_rate = AsyncMock()
    set_pricing = AsyncMock()
    create_category = AsyncMock()
    create_product = AsyncMock()
    create_merchant_card = AsyncMock()
    upsert_page = AsyncMock()
    update_category = AsyncMock()
    update_product = AsyncMock()
    update_merchant_card = AsyncMock()
    set_product_pricing_override = AsyncMock()
    create_page_button = AsyncMock(return_value=SimpleNamespace(text="Catalog"))
    update_page_button = AsyncMock()
    set_button_emoji = AsyncMock()
    review_kyc = AsyncMock()
    review_card = AsyncMock()
    manual_reconcile = AsyncMock()
    claim = AsyncMock(return_value=True)
    deliver = AsyncMock()
    register_emoji = AsyncMock(return_value=SimpleNamespace(name="premium"))
    set_emoji_active = AsyncMock()
    set_entity_emoji = AsyncMock()


class MessageFake:
    def __init__(self, actor=2, text="", photo=None, document=None):
        self.from_user = SimpleNamespace(id=actor)
        self.text, self.photo, self.document = text, photo, document
        self.reply_to_message = None
        self.answers = []
        self.deleted = False

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self

    async def answer_photo(self, file_id, **kwargs):
        self.answers.append((file_id, kwargs))

    async def delete(self):
        self.deleted = True


class QueryFake:
    def __init__(self, token, message, actor=2):
        self.data, self.message = token, message
        self.from_user = SimpleNamespace(id=actor)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def handler(router, observer, name):
    return next(
        item.callback
        for item in getattr(router, observer).handlers
        if item.callback.__name__ == name
    )


@pytest.mark.asyncio
async def test_start_terms_consent_and_home_screens():
    repo = RepoFake()
    router = persistent_router(repo)
    start = handler(router, "message", "start")
    message = MessageFake()
    await start(message)
    assert "راه‌اندازی" in message.answers[-1][0]
    repo.terms = SimpleNamespace(id=uuid4(), version=1, title="Terms", pages=["Body"])
    await start(message)
    keyboard = message.answers[-1][1]["reply_markup"]
    token = keyboard.inline_keyboard[0][0].callback_data
    callback = handler(router, "callback_query", "callback")
    await callback(QueryFake(token, message))
    assert any("صفحه اصلی" in answer[0] for answer in message.answers)
    await start(message)
    assert message.answers[-1][0] == "صفحه اصلی"


@pytest.mark.asyncio
async def test_customer_callback_navigation_and_checkout():
    repo, message = RepoFake(), MessageFake()
    router = persistent_router(repo)
    callback = handler(router, "callback_query", "callback")

    async def dispatch(action, object_id=""):
        token = await repo.coordinator.issue_callback(action, 2, object_id)
        await callback(QueryFake(token, message))
        return message.answers[-1]

    catalog = await dispatch("catalog")
    assert catalog[1]["reply_markup"].inline_keyboard[0][0].icon_custom_emoji_id == "123456"
    category_token = catalog[1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(category_token, message))
    assert (
        message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].icon_custom_emoji_id
        == "123456"
    )
    product_token = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(product_token, message))
    buy_token = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(buy_token, message))
    card_token = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(card_token, message))
    final_token = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(final_token, message))
    assert "کارت مقصد" in message.answers[-1][0]
    await dispatch("account")
    assert "VERIFIED" in message.answers[-1][0]
    await dispatch("my_orders")
    assert "PROCESSING" in message.answers[-1][0]
    await dispatch("begin_kyc")
    assert await repo.coordinator.redis.get("fsm:2") == "kyc.document"
    await dispatch("begin_card")
    assert await repo.coordinator.redis.get("fsm:2") == "card.bank"


@pytest.mark.asyncio
async def test_buy_gate_provides_actionable_kyc_and_card_buttons():
    repo, message = RepoFake(), MessageFake()
    repo.verified_cards = AsyncMock(return_value=[])
    router = persistent_router(repo)
    callback = handler(router, "callback_query", "callback")
    token = await repo.coordinator.issue_callback("buy", 2, str(uuid4()))
    await callback(QueryFake(token, message))
    text_value, kwargs = message.answers[-1]
    assert "KYC" in text_value
    actions = {
        repo.coordinator.state[button.callback_data]["a"]
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert actions == {"begin_kyc", "begin_card"}


@pytest.mark.asyncio
async def test_missing_or_inactive_registry_emoji_renders_clean_text():
    repo, message = RepoFake(), MessageFake()
    repo.resolve_emoji_key = AsyncMock(return_value=None)
    router = persistent_router(repo)
    callback = handler(router, "callback_query", "callback")
    token = await repo.coordinator.issue_callback("catalog", 2)
    await callback(QueryFake(token, message))
    button = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Category"
    assert getattr(button, "icon_custom_emoji_id", None) is None

    owner = MessageFake(actor=1)
    token = await repo.coordinator.issue_callback("admin.page.buttons", 1, str(uuid4()))
    await callback(QueryFake(token, owner, actor=1))
    page_button = owner.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert page_button.text == "Catalog"
    assert getattr(page_button, "icon_custom_emoji_id", None) is None


@pytest.mark.asyncio
async def test_admin_rbac_menu_queues_and_decision_forms():
    repo = RepoFake()
    router = persistent_router(repo)
    admin = handler(router, "message", "admin")
    denied = MessageFake(actor=2)
    await admin(denied)
    assert "مجاز نیست" in denied.answers[-1][0]
    owner = MessageFake(actor=1)
    await admin(owner)
    assert owner.answers[-1][1]["reply_markup"].inline_keyboard
    dashboard_labels = [
        button.text
        for row in owner.answers[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any("قوانین فروشگاه" in label and "نیازمند تنظیم" in label for label in dashboard_labels)
    assert "وضعیت آمادگی فروشگاه" in owner.answers[-1][0]
    callback = handler(router, "callback_query", "callback")
    for action in (
        "admin.kyc",
        "admin.cards",
        "admin.orders",
        "admin.audit",
        "admin.terms",
        "admin.rate",
        "admin.pricing",
        "admin.category",
        "admin.product",
        "admin.merchant",
        "admin.page",
        "admin.emoji",
    ):
        token = await repo.coordinator.issue_callback(action, 1)
        await callback(QueryFake(token, owner, actor=1))
    assert any("صف بررسی" in item[0] for item in owner.answers)
    token = await repo.coordinator.issue_callback("admin.payment.approve", 1, str(uuid4()))
    await callback(QueryFake(token, owner, actor=1))
    assert "دلیل" in owner.answers[-1][0]

    for action in ("admin.category.toggle", "admin.product.toggle"):
        token = await repo.coordinator.issue_callback(action, 1, f"{uuid4()}:0")
        await callback(QueryFake(token, owner, actor=1))
        assert "تغییر" in owner.answers[-1][0]
    merchant_id = uuid4()
    repo.owner_merchant_cards = AsyncMock(
        return_value=[
            SimpleNamespace(
                id=merchant_id,
                active=True,
                priority=1,
                daily_limit=1000,
            )
        ]
    )
    token = await repo.coordinator.issue_callback("admin.merchant.toggle", 1, f"{merchant_id}:0")
    await callback(QueryFake(token, owner, actor=1))
    assert "تغییر" in owner.answers[-1][0]

    page_id = uuid4()
    token = await repo.coordinator.issue_callback("admin.page.buttons", 1, str(page_id))
    await callback(QueryFake(token, owner, actor=1))
    assert "دکمه‌های صفحه" in owner.answers[-1][0]
    page_button = owner.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert page_button.icon_custom_emoji_id == "123456"
    token = await repo.coordinator.issue_callback("admin.button.create", 1, str(page_id))
    await callback(QueryFake(token, owner, actor=1))
    assert await repo.coordinator.redis.get("fsm:1") == f"admin.button:{page_id}"

    category_id = uuid4()
    token = await repo.coordinator.issue_callback(
        "admin.entity.emoji", 1, f"category:{category_id}"
    )
    await callback(QueryFake(token, owner, actor=1))
    select_token = owner.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(select_token, owner, actor=1))
    repo.set_entity_emoji.assert_awaited_with(1, "category", category_id, "premium")

    button_id = uuid4()
    token = await repo.coordinator.issue_callback("admin.button.emoji", 1, str(button_id))
    await callback(QueryFake(token, owner, actor=1))
    select_token = owner.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(select_token, owner, actor=1))
    repo.set_button_emoji.assert_awaited_with(1, button_id, "premium")


@pytest.mark.asyncio
async def test_admin_close_cancel_and_commands_clear_actor_state():
    repo = RepoFake()
    repo.terms = SimpleNamespace(id=uuid4(), version=1, title="Terms", pages=["Body"])
    router = persistent_router(repo)
    callback = handler(router, "callback_query", "callback")
    owner = MessageFake(actor=1)
    for key in ("fsm:1", "terms-title:1", "delivery-draft:1:order"):
        await repo.coordinator.redis.set(key, "temporary")
    token = await repo.coordinator.issue_callback("admin.close", 1)
    await callback(QueryFake(token, owner, actor=1))
    assert not any(
        key.endswith(":1") or key.startswith("delivery-draft:1:")
        for key in repo.coordinator.redis.values
    )
    assert owner.answers[-1][0].startswith("پنل مدیریت")

    await repo.coordinator.redis.set("fsm:1", "admin.emoji")
    admin = handler(router, "message", "admin")
    await admin(owner)
    assert await repo.coordinator.redis.get("fsm:1") is None
    await repo.coordinator.redis.set("fsm:1", "admin.emoji")
    cancel = handler(router, "message", "cancel")
    await cancel(owner)
    assert await repo.coordinator.redis.get("fsm:1") is None

    customer = MessageFake(actor=2)
    await repo.coordinator.redis.set("fsm:2", "admin.emoji")
    start = handler(router, "message", "start")
    await start(customer)
    assert await repo.coordinator.redis.get("fsm:2") is None
    assert "Terms" in customer.answers[-1][0]


class RejectingKeyboardMessage(MessageFake):
    def __init__(self, failures, error_text="style is not supported"):
        super().__init__()
        self.failures = failures
        self.error_text = error_text
        self.attempts = 0

    async def answer(self, text, **kwargs):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=2, text=text), message=self.error_text
            )
        return await super().answer(text, **kwargs)


@pytest.mark.asyncio
async def test_keyboard_feature_rejection_retries_once_without_unicode_fallback():
    rich = markup([[Button("Plain title", "c1.token", "primary", "123456")]])
    rich_serialized = rich.inline_keyboard[0][0].model_dump(exclude_none=True)
    assert rich_serialized["style"] == "primary"
    assert rich_serialized["icon_custom_emoji_id"] == "123456"
    message = RejectingKeyboardMessage(1)
    await answer_keyboard(
        message, "Screen", [[Button("Plain title", "c1.token", "primary", "123456")]]
    )
    assert message.attempts == 2
    button = message.answers[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Plain title"
    serialized = button.model_dump(exclude_none=True)
    assert "style" not in serialized and "icon_custom_emoji_id" not in serialized

    twice = RejectingKeyboardMessage(2, "icon_custom_emoji_id is not supported")
    with pytest.raises(TelegramBadRequest):
        await answer_keyboard(twice, "Screen", [[Button("Plain title", "c1.token", "success")]])
    assert twice.attempts == 2

    unrelated = RejectingKeyboardMessage(1, "chat not found")
    with pytest.raises(TelegramBadRequest):
        await answer_keyboard(unrelated, "Screen", [[Button("Plain title", "c1.token")]])
    assert unrelated.attempts == 1


@pytest.mark.asyncio
async def test_user_and_admin_text_fsm_and_uploads():
    repo = RepoFake()
    router = persistent_router(repo)
    form = handler(router, "message", "form_text")
    upload = handler(router, "message", "uploaded_file")
    message = MessageFake(text="Bank")
    await repo.coordinator.redis.set("fsm:2", "card.bank")
    await form(message)
    message.text = "4111111111111111"
    await form(message)
    assert message.deleted
    photo = SimpleNamespace(file_id="photo", file_unique_id="photo-unique")
    message.photo, message.text = [photo], ""
    await upload(message)
    assert "**** 1111" in message.answers[-1][0]
    await repo.coordinator.redis.set("fsm:2", "kyc.document")
    message.photo = [SimpleNamespace(file_id="kyc", file_unique_id="kyc-unique")]
    await upload(message)
    await repo.coordinator.redis.set("receipt-order:2", str(uuid4()))
    message.photo = [SimpleNamespace(file_id="receipt", file_unique_id="receipt-unique")]
    await upload(message)
    assert "اثبات پرداخت نیست" in message.answers[-1][0]

    unsupported = handler(router, "message", "unsupported_message")
    await repo.coordinator.redis.set("fsm:2", "kyc.document")
    await unsupported(message)
    assert "فقط تصویر" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_admin_text_forms_claim_delivery_and_emoji():
    repo, owner = RepoFake(), MessageFake(actor=1)
    router = persistent_router(repo)
    form = handler(router, "message", "form_text")
    callback = handler(router, "callback_query", "callback")
    token = await repo.coordinator.issue_callback("admin.rate", 1)
    await callback(QueryFake(token, owner, actor=1))
    owner.text = "not-a-number"
    await form(owner)
    assert "معتبر نیست" in owner.answers[-1][0]
    assert await repo.coordinator.redis.get("fsm:1") == "admin.wizard"
    owner.text = "۵۰٬۰۰۰".replace("٬", "")
    await form(owner)
    assert "پیش‌نمایش نرخ" in owner.answers[-1][0]
    confirm = owner.answers[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data
    await callback(QueryFake(confirm, owner, actor=1))
    assert repo.set_rate.await_count == 1

    await repo.coordinator.redis.set("fsm:1", "admin.emoji")
    owner.text = "inline-premium"
    owner.reply_to_message = SimpleNamespace(
        entities=[SimpleNamespace(type="custom_emoji", custom_emoji_id="123456")]
    )
    await form(owner)
    assert "Premium Emoji" in owner.answers[-1][0]

    emoji_handler = handler(router, "message", "admin_emoji")
    owner.text = "/admin_emoji premium"
    owner.reply_to_message = SimpleNamespace(
        entities=[SimpleNamespace(type="custom_emoji", custom_emoji_id="123456")]
    )
    await emoji_handler(owner)
    assert "Premium Emoji" in owner.answers[-1][0]
