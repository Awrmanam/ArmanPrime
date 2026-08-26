from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentCapabilities:
    supports_server_verification: bool
    supports_allowed_card: bool
    returns_masked_payer_card: bool
    returns_payer_card_token: bool
    supports_refund: bool
    supports_inquiry: bool


class PaymentProvider(Protocol):
    capabilities: PaymentCapabilities

    async def verify_server_to_server(self, reference: str, amount: int) -> bool: ...


class KYCProvider(Protocol):
    async def verify(self, encrypted_submission_id: str) -> str: ...


class CurrencyProvider(Protocol):
    async def usd_to_toman(self) -> tuple[int, str]: ...
