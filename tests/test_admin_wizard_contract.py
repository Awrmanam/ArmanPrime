import re
from pathlib import Path

RUNTIME = Path("src/shopbot/runtime.py").read_text()


def test_production_admin_forms_never_parse_compound_pipe_input():
    forbidden = re.compile(
        r"""split\(["']\|["']\)|mode\|markup|بانک\|صاحب|custom_emoji_id یا|target\|row\|position"""
    )
    production = "\n".join(path.read_text() for path in Path("src/shopbot").glob("*.py"))
    assert forbidden.search(production) is None


def test_wizards_expose_persian_steps_and_confirmation():
    for wizard in (
        '"terms"',
        '"rate"',
        '"pricing"',
        '"merchant"',
        '"category"',
        '"product"',
        '"page"',
        '"button"',
        '"delivery"',
        '"appearance"',
    ):
        assert wizard in RUNTIME
    assert "هر بار فقط همین مقدار را ارسال کنید" in RUNTIME
    assert "تأیید و ثبت" in RUNTIME
    assert "این مقدار معتبر نیست" in RUNTIME
    assert "فرم را دوباره آغاز کنید" not in RUNTIME


def test_sensitive_card_draft_is_encrypted_and_preview_redacts_it():
    assert 'data["encrypted_pan"] = repo.vault.encrypt(digits)' in RUNTIME
    assert 'if key not in {"encrypted_pan"}' in RUNTIME
    assert "await message.delete()" in RUNTIME
    assert "create_merchant_card_encrypted" in RUNTIME


def test_custom_pages_generate_keys_and_admin_appearance_is_persistent():
    assert "عنوان نمایشی صفحه" in RUNTIME
    assert "نشانی کامل HTTPS" in RUNTIME
    assert "ظاهر پنل" in RUNTIME
    assert "set_admin_button_preference" in RUNTIME
    assert "رنگ رسمی تلگرام" in RUNTIME
