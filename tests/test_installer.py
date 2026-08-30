from pathlib import Path


def test_interactive_installer_asks_only_required_identity_values():
    script = Path("install.sh").read_text()
    prompts = [line for line in script.splitlines() if line.lstrip().startswith("read -r")]
    assert len(prompts) == 3
    assert "Bot token" in prompts[0]
    assert "Admin Telegram user ID" in prompts[1]
    assert "Order notification chat ID" in prompts[2]
    forbidden = (
        "Support username",
        "Timezone",
        "Money unit",
        "Currency provider",
        "KYC mode",
        "Strong match",
        "Payment provider",
        "Brand name",
    )
    assert not any(value in script for value in forbidden)
    assert "ORDER_CHAT=${ORDER_CHAT:-$ADMIN_ID}" in script
    assert "Send /admin to the bot" in script
    assert "Send /setup" not in script
