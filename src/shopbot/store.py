"""Application state and use-cases used by Telegram handlers.

The in-memory implementation is intentionally deterministic for bot and integration tests. The
same aggregate boundaries are persisted by the SQL migration; production repositories can replace
this store without moving security checks into Telegram handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from .domain import (
    CheckoutService,
    CustomerCard,
    Payment,
    PaymentService,
    PriceQuote,
    PricingRule,
    Product,
    TermsVersion,
    User,
)
from .enums import CardStatus, KYCStatus, OrderStatus, PaymentStatus


class NotFoundError(Exception):
    pass


class PermissionDenied(Exception):
    pass


@dataclass
class Category:
    id: UUID
    title: str
    active: bool = True
    position: int = 0


@dataclass
class MerchantCard:
    id: UUID
    bank_name: str
    holder_name: str
    encrypted_pan: str
    masked_pan: str
    priority: int = 0
    daily_limit: int = 0
    active: bool = True


@dataclass
class PageButton:
    id: UUID
    text: str
    action: str
    row: int
    position: int
    style: str = "default"
    custom_emoji_id: str | None = None
    active: bool = True


@dataclass
class Page:
    id: UUID
    slug: str
    text: str
    buttons: list[PageButton] = field(default_factory=list)


@dataclass
class PremiumEmoji:
    id: UUID
    name: str
    custom_emoji_id: str
    valid: bool = True


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor_id: int
    action: str
    target: str
    detail: str


@dataclass
class Delivery:
    order_id: UUID
    text: str
    activation_link: str | None
    delivered_at: datetime


class MemoryNotifications:
    def __init__(self) -> None:
        self.payment_reviews: list[UUID] = []
        self.fulfillment_orders: list[UUID] = []

    async def payment_review(self, order, payment) -> None:
        self.payment_reviews.append(order.id)

    async def fulfillment_ready(self, order) -> None:
        self.fulfillment_orders.append(order.id)


class ApplicationStore:
    def __init__(self, owner_id: int, vault=None) -> None:
        self.owner_id = owner_id
        self.vault = vault
        self.users: dict[int, User] = {}
        self.terms: list[TermsVersion] = []
        self.categories: dict[UUID, Category] = {}
        self.products: dict[UUID, Product] = {}
        self.product_categories: dict[UUID, UUID] = {}
        self.customer_cards: dict[UUID, CustomerCard] = {}
        self.merchant_cards: dict[UUID, MerchantCard] = {}
        self.pages: dict[str, Page] = {"home": Page(uuid4(), "home", "")}
        self.emojis: dict[str, PremiumEmoji] = {}
        self.usd_rate: int | None = None
        self.pricing_rule = PricingRule()
        self.quotes: dict[UUID, PriceQuote] = {}
        self.orders = {}
        self.payments: dict[UUID, Payment] = {}
        self.deliveries: dict[UUID, Delivery] = {}
        self.audit: list[AuditEntry] = []
        self.notifications = MemoryNotifications()
        self.checkout = CheckoutService(30)
        self.payment_service = PaymentService(self.notifications)

    def require_owner(self, actor_id: int) -> None:
        if actor_id != self.owner_id:
            raise PermissionDenied("OWNER_REQUIRED")

    def record(self, actor_id: int, action: str, target: str, detail: str = "") -> None:
        self.audit.append(AuditEntry(datetime.now(UTC), actor_id, action, target, detail))

    def user(self, telegram_id: int) -> User:
        if telegram_id not in self.users:
            self.users[telegram_id] = User(telegram_id=telegram_id)
        return self.users[telegram_id]

    @property
    def current_terms(self) -> TermsVersion:
        published = [item for item in self.terms if item.published]
        if not published:
            raise NotFoundError("TERMS_NOT_PUBLISHED")
        return max(published, key=lambda item: item.version)

    def publish_terms(self, actor_id: int, title: str, body: str) -> TermsVersion:
        self.require_owner(actor_id)
        terms = TermsVersion.publish(
            len(self.terms) + 1, title, tuple(body.split("\f")), datetime.now(UTC)
        )
        self.terms.append(terms)
        self.record(actor_id, "terms.publish", str(terms.id), f"version={terms.version}")
        return terms

    def accept_terms(self, telegram_id: int) -> None:
        user = self.user(telegram_id)
        user.accepted_terms_id = self.current_terms.id
        self.record(telegram_id, "terms.accept", str(self.current_terms.id))

    def set_kyc(self, actor_id: int, telegram_id: int, status: KYCStatus, reason: str = "") -> None:
        self.require_owner(actor_id)
        self.user(telegram_id).kyc_status = status
        self.record(actor_id, f"kyc.{status.value.lower()}", str(telegram_id), reason)

    def add_category(self, actor_id: int, title: str) -> Category:
        self.require_owner(actor_id)
        category = Category(uuid4(), title, position=len(self.categories))
        self.categories[category.id] = category
        self.record(actor_id, "category.create", str(category.id))
        return category

    def add_product(
        self, actor_id: int, category_id: UUID, title: str, usd: Decimal, stock: int
    ) -> Product:
        self.require_owner(actor_id)
        if category_id not in self.categories:
            raise NotFoundError("CATEGORY_NOT_FOUND")
        product = Product(uuid4(), title, usd, stock)
        self.products[product.id] = product
        self.product_categories[product.id] = category_id
        self.record(actor_id, "product.create", str(product.id))
        return product

    def set_rate(self, actor_id: int, rate: int) -> None:
        self.require_owner(actor_id)
        if rate <= 0:
            raise ValueError("INVALID_RATE")
        self.usd_rate = rate
        self.record(actor_id, "currency.manual_rate", "USD_TOMAN", str(rate))

    def set_pricing(self, actor_id: int, markup: Decimal | None, margin: Decimal | None) -> None:
        self.require_owner(actor_id)
        self.pricing_rule = PricingRule(markup_percent=markup, target_margin_percent=margin)
        self.record(actor_id, "pricing.rule", "global")

    def add_page(self, actor_id: int, slug: str, text: str) -> Page:
        self.require_owner(actor_id)
        page = Page(uuid4(), slug, text)
        self.pages[slug] = page
        self.record(actor_id, "page.upsert", slug)
        return page

    def add_button(
        self,
        actor_id: int,
        slug: str,
        text: str,
        action: str,
        row: int,
        position: int,
        style: str,
        emoji_name: str | None,
    ) -> PageButton:
        self.require_owner(actor_id)
        if style not in {"default", "primary", "success", "danger"}:
            raise ValueError("INVALID_STYLE")
        emoji_id = self.emojis[emoji_name].custom_emoji_id if emoji_name else None
        button = PageButton(uuid4(), text, action, row, position, style, emoji_id)
        self.pages[slug].buttons.append(button)
        self.record(actor_id, "button.create", str(button.id))
        return button

    def register_emoji(self, actor_id: int, name: str, custom_emoji_id: str) -> PremiumEmoji:
        self.require_owner(actor_id)
        if not custom_emoji_id.isdigit():
            raise ValueError("INVALID_CUSTOM_EMOJI")
        emoji = PremiumEmoji(uuid4(), name, custom_emoji_id)
        self.emojis[name] = emoji
        self.record(actor_id, "emoji.register", name)
        return emoji

    def verified_cards(self, telegram_id: int) -> list[CustomerCard]:
        user = self.user(telegram_id)
        return [
            card
            for card in self.customer_cards.values()
            if card.user_id == user.id and card.status == CardStatus.VERIFIED
        ]

    def add_customer_card(self, telegram_id: int, bank: str, last4: str) -> CustomerCard:
        user = self.user(telegram_id)
        card = CustomerCard(
            uuid4(),
            user.id,
            bank,
            f"**** {last4}",
            last4,
            uuid4().hex,
            CardStatus.PENDING_VERIFICATION,
        )
        self.customer_cards[card.id] = card
        self.record(telegram_id, "customer_card.submit", str(card.id))
        return card

    def review_customer_card(self, actor_id: int, card_id: UUID, status: CardStatus) -> None:
        self.require_owner(actor_id)
        card = self.customer_cards.get(card_id)
        if not card:
            raise NotFoundError("CARD_NOT_FOUND")
        self.customer_cards[card_id] = CustomerCard(
            card.id,
            card.user_id,
            card.bank_name,
            card.masked_pan,
            card.last4,
            card.fingerprint,
            status,
        )
        self.record(actor_id, f"customer_card.{status.value.lower()}", str(card_id))

    def add_merchant_card(
        self, actor_id: int, bank: str, holder: str, pan: str, priority: int, limit: int
    ) -> MerchantCard:
        self.require_owner(actor_id)
        if not self.vault:
            raise RuntimeError("VAULT_REQUIRED")
        digits = "".join(c for c in pan if c.isdigit())
        if len(digits) != 16:
            raise ValueError("INVALID_PAN")
        card = MerchantCard(
            uuid4(),
            bank,
            holder,
            self.vault.encrypt(digits),
            f"**** {digits[-4:]}",
            priority,
            limit,
        )
        self.merchant_cards[card.id] = card
        self.record(actor_id, "merchant_card.create", str(card.id), card.masked_pan)
        return card

    def select_merchant_card(self) -> MerchantCard:
        active = [card for card in self.merchant_cards.values() if card.active]
        if not active:
            raise NotFoundError("MERCHANT_CARD_NOT_FOUND")
        return min(active, key=lambda card: card.priority)

    def create_quote(
        self, telegram_id: int, product_id: UUID, card_id: UUID, now: datetime, version: int = 1
    ) -> PriceQuote:
        if self.usd_rate is None:
            raise NotFoundError("RATE_NOT_CONFIGURED")
        user, product = self.user(telegram_id), self.products[product_id]
        card = self.customer_cards[card_id]
        quote = self.checkout.create_quote(
            user, self.current_terms, card, product, self.usd_rate, self.pricing_rule, now, version
        )
        self.quotes[quote.id] = quote
        self.record(telegram_id, "quote.create", str(quote.id), f"amount={quote.final_toman}")
        return quote

    def requote(self, old: PriceQuote, now: datetime) -> PriceQuote:
        product = self.products[old.product_id]
        order = self.checkout.orders_by_quote.get(old.id)
        self.checkout.expire(old, order, product, now)
        return self.create_quote(
            self.users_by_id(old.user_id).telegram_id,
            old.product_id,
            old.selected_card_id,
            now,
            old.version + 1,
        )

    def users_by_id(self, user_id: UUID) -> User:
        return next(user for user in self.users.values() if user.id == user_id)

    def confirm(self, telegram_id: int, quote_id: UUID, now: datetime):
        quote = self.quotes[quote_id]
        if quote.user_id != self.user(telegram_id).id:
            raise PermissionDenied("QUOTE_OWNER_REQUIRED")
        order = self.checkout.confirm_final_check(quote, now)
        self.orders[order.id] = order
        self.payments.setdefault(
            order.id, Payment(uuid4(), order.id, PaymentStatus.AWAITING_PAYMENT)
        )
        return order

    def receiving_card(
        self, telegram_id: int, quote_id: UUID, now: datetime
    ) -> tuple[MerchantCard, str]:
        quote = self.quotes[quote_id]
        card = self.customer_cards[quote.selected_card_id]
        merchant = self.select_merchant_card()
        pan = self.checkout.reveal_receiving_card(
            self.user(telegram_id),
            self.current_terms,
            card,
            quote,
            now,
            merchant.encrypted_pan,
            self.vault.decrypt,
        )
        return merchant, pan

    def deliver(
        self, actor_id: int, order_id: UUID, text: str, activation_link: str | None = None
    ) -> Delivery:
        self.require_owner(actor_id)
        order = self.orders[order_id]
        if order.status != OrderStatus.PROCESSING or order.assigned_admin_id != actor_id:
            raise PermissionDenied("CLAIMED_PROCESSING_ORDER_REQUIRED")
        delivery = Delivery(order_id, text, activation_link, datetime.now(UTC))
        self.deliveries[order_id] = delivery
        order.status = OrderStatus.DELIVERED
        self.record(actor_id, "order.deliver", str(order_id))
        return delivery
