from shopbot.file_storage import StoredEvidence
from shopbot.providers import PaymentCapabilities


def test_interface_value_objects_are_concrete_and_truthful():
    evidence = StoredEvidence(storage_key="telegram:file-id", encrypted=False, size=0)
    assert evidence.storage_key == "telegram:file-id"
    assert not evidence.encrypted
    capabilities = PaymentCapabilities(
        supports_server_verification=False,
        supports_allowed_card=False,
        returns_masked_payer_card=False,
        returns_payer_card_token=False,
        supports_refund=False,
        supports_inquiry=False,
    )
    assert not capabilities.supports_server_verification
    assert not capabilities.supports_allowed_card
