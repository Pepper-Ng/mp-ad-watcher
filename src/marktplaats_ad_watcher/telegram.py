from __future__ import annotations

from html import escape

import httpx

from marktplaats_ad_watcher.config import Settings
from marktplaats_ad_watcher.models import EvaluatedAd, EvaluationFailure, TelegramSendResult


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, evaluated_ad: EvaluatedAd) -> TelegramSendResult:
        return await self._send_text(
            _format_message(
                evaluated_ad,
                fallback_profile_id=self._settings.active_profile_id,
                fallback_profile_name=self._settings.active_profile_name,
            )
        )

    async def send_test_message(self) -> TelegramSendResult:
        heading = _message_heading(
            "Marktplaats Ad Watcher test",
            profile_id=self._settings.active_profile_id,
            profile_name=self._settings.active_profile_name,
        )
        return await self._send_text(
            f"<b>{escape(heading)}</b>\n"
            "Telegram connectivity is configured and working."
        )

    async def send_ai_failure_alert(
        self,
        failures: list[EvaluationFailure],
    ) -> TelegramSendResult:
        return await self._send_text(
            _format_ai_failure_alert(
                failures,
                profile_id=self._settings.active_profile_id,
                profile_name=self._settings.active_profile_name,
            )
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


def _format_message(
    evaluated_ad: EvaluatedAd,
    *,
    fallback_profile_id: str | None = None,
    fallback_profile_name: str | None = None,
) -> str:
    ad = evaluated_ad.ad
    result = evaluated_ad.result
    raw_heading = (
        "Likely Marktplaats match"
        if result.next_action == "notify"
        else "Check Marktplaats specs"
    )
    profile_id = evaluated_ad.profile_id or fallback_profile_id
    profile_name = evaluated_ad.profile_name or fallback_profile_name
    heading = _message_heading(raw_heading, profile_id=profile_id, profile_name=profile_name)
    lines = [
        f"<b>{escape(heading)}</b>",
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


def _format_ai_failure_alert(
    failures: list[EvaluationFailure],
    *,
    profile_id: str | None,
    profile_name: str | None,
) -> str:
    heading = _message_heading(
        "AI evaluation needs attention",
        profile_id=profile_id,
        profile_name=profile_name,
    )
    lines = [
        f"<b>{escape(heading)}</b>",
        (
            f"A production run could not finish AI evaluation for {len(failures)} search candidate(s). "
            "These are not recommendations. Only real model-call failures are listed here."
        ),
    ]
    for failure in failures[:3]:
        lines.append(
            f'<a href="{escape(failure.url)}">{escape(failure.title)}</a>\n'
            f"Error: {escape(failure.error)}"
        )
    if len(failures) > 3:
        lines.append(f"Plus {len(failures) - 3} additional failed listing(s).")
    return "\n\n".join(lines)


def _message_heading(base_heading: str, *, profile_id: str | None, profile_name: str | None) -> str:
    label = _profile_label(profile_id=profile_id, profile_name=profile_name)
    return f"{label} {base_heading}" if label else base_heading


def _profile_label(*, profile_id: str | None, profile_name: str | None) -> str | None:
    normalized_id = profile_id.strip() if isinstance(profile_id, str) else ""
    normalized_name = profile_name.strip() if isinstance(profile_name, str) else ""
    if normalized_name and normalized_id:
        return f"[{normalized_name} · {normalized_id}]"
    if normalized_name:
        return f"[{normalized_name}]"
    if normalized_id:
        return f"[{normalized_id}]"
    return None
