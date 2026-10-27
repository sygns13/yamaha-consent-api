import logging

import pytest

from app.integrations.whatsapp import FakeWhatsAppSender
from app.models import WhatsappStatus


async def test_fake_sender_only_logs_with_masked_phone_and_no_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.integrations.whatsapp"):
        result = await FakeWhatsAppSender().send_consent_link(
            to="whatsapp:+51987654321", token="token-secreto-abc", transaction_id="TRX-1"
        )

    assert result.status == WhatsappStatus.QUEUED
    assert result.message_sid is None
    assert result.error_code is None
    (record,) = caplog.records
    assert "SIMULADO" in record.getMessage()
    assert record.transaction_id == "TRX-1"
    assert record.to.endswith("4321")
    assert "987654321" not in caplog.text
    assert "token-secreto-abc" not in caplog.text
