from __future__ import annotations

from html import escape

import httpx

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.models import EvaluatedAd, TelegramSendResult


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, evaluated_ad: EvaluatedAd) -> TelegramSendResult:
        return await self._send_text(_format_message(evaluated_ad))

    async def send_test_message(self) -> TelegramSendResult:
        return await self._send_text(
            "<b>Marktplaats Ad Watcher test</b>\n"
            "Telegram connectivity is configured and working."
        )

    async def _send_text(self, text: str) -> TelegramSendResult:
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return TelegramSendResult(sent=False, reason="Telegram is not configured.")

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": self._settings.telegram_disable_web_page_preview,
        }

        endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()

        response_payload = response.json()
        message_id = response_payload.get("result", {}).get("message_id")
        return TelegramSendResult(sent=True, message_id=message_id)


def _format_message(evaluated_ad: EvaluatedAd) -> str:
    ad = evaluated_ad.ad
    result = evaluated_ad.result
    heading = (
        "Likely Marktplaats match"
        if result.next_action == "notify"
        else "Check Marktplaats specs"
    )
    lines = [
        f"<b>{heading}</b>",
        f'<a href="{escape(ad.url)}">{escape(ad.title)}</a>',
    ]

    if ad.price:
        lines.append(f"Price: {escape(ad.price)}")
    if ad.location:
        lines.append(f"Location: {escape(ad.location)}")
    if ad.seller:
        lines.append(f"Seller: {escape(ad.seller)}")

    lines.extend(
        [
            f"Confidence: {result.confidence:.2f}",
            f"Reason: {escape(result.reason)}",
        ]
    )

    if result.signals:
        lines.append("Signals: " + escape("; ".join(result.signals[:3])))

    if result.concerns:
        lines.append("Concerns: " + escape("; ".join(result.concerns[:3])))

    return "\n".join(lines)
