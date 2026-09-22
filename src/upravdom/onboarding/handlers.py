"""Обработчики онбординга поверх реестра `Dispatcher` (architecture.md §7.1, §8).

- `on_bot_started` — deep-link или приглашение ввести адрес;
- `on_callback` — согласие / поиск / заявка на подключение;
- `gated` — без дома текст = поиск адреса; с домом без согласия — удержание.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from upravdom.bot_gateway import inbox, outbox
from upravdom.bot_gateway.inbox import ClaimedEvent, get_or_create_user
from upravdom.bot_gateway.schemas import ParsedEvent, parse_webhook_payload
from upravdom.config import get_settings
from upravdom.onboarding import service, texts
from upravdom.onboarding.address import (
    MAX_STORED_ADDRESS_CHARS,
    HouseMatch,
    canonical_address,
    normalize_address,
)
from upravdom.onboarding.callbacks import (
    Action,
    OnboardingCallback,
    RefKind,
    decode,
    decode_address,
    encode,
    encode_address,
    inline_keyboard,
)

logger = logging.getLogger(__name__)

Handler = Callable[[AsyncSession, ClaimedEvent], Awaitable[None]]


async def _send(
    session: AsyncSession,
    chat_id: str,
    text: str,
    attachments: list[dict[str, object]] | None = None,
) -> None:
    await outbox.enqueue_message(session, chat_id=chat_id, text_=text, attachments=attachments)


def _fallback_contact() -> str:
    return get_settings().fallback_contact_text or texts.DEFAULT_FALLBACK_CONTACT


async def _send_consent_prompt(
    session: AsyncSession,
    chat_id: str,
    house: service.HouseInfo,
    *,
    ref_kind: RefKind,
    ref_value: str,
    kept_message: bool = False,
) -> None:
    settings = get_settings()
    version = settings.consent_version
    text = texts.HOUSE_RECOGNIZED.format(
        address=house.address,
        consent=texts.consent_text(version, settings.privacy_policy_url),
    )
    if kept_message:
        text += "\n\n" + texts.MESSAGE_KEPT
    keyboard = inline_keyboard(
        [
            [
                (
                    texts.BTN_ACCEPT,
                    encode(OnboardingCallback(Action.ACCEPT, ref_kind, ref_value, version)),
                ),
                (
                    texts.BTN_DECLINE,
                    encode(OnboardingCallback(Action.DECLINE, ref_kind, ref_value, version)),
                ),
            ],
            [
                (
                    texts.BTN_WRONG_HOUSE,
                    encode(OnboardingCallback(Action.WRONG_HOUSE, ref_kind, ref_value)),
                )
            ],
        ]
    )
    await _send(session, chat_id, text, keyboard)


def _not_found_keyboard(address_query: str) -> list[dict[str, object]]:
    encoded = encode_address(address_query)
    return inline_keyboard(
        [
            [
                (
                    texts.BTN_RETRY_ADDRESS,
                    encode(OnboardingCallback(Action.RETRY, RefKind.NONE, "-")),
                )
            ],
            [
                (
                    texts.BTN_LEAVE_ADDRESS,
                    encode(OnboardingCallback(Action.LEAVE, RefKind.ADDR, encoded)),
                )
            ],
        ]
    )


async def _send_not_found(session: AsyncSession, chat_id: str, address_query: str) -> None:
    await _send(
        session,
        chat_id,
        texts.HOUSE_NOT_CONNECTED.format(contact=_fallback_contact()),
        _not_found_keyboard(address_query),
    )


async def _send_address_choices(
    session: AsyncSession, chat_id: str, query: str, matches: list[HouseMatch]
) -> None:
    rows: list[list[tuple[str, str]]] = []
    for match in matches:
        label = match.address_raw
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append(
            [
                (
                    label,
                    encode(OnboardingCallback(Action.PICK, RefKind.SEARCH, str(match.house_id))),
                )
            ]
        )
    rows.append(
        [
            (
                texts.BTN_NOT_IN_LIST,
                encode(OnboardingCallback(Action.NONE_MATCH, RefKind.ADDR, encode_address(query))),
            )
        ]
    )
    await _send(session, chat_id, texts.ADDRESS_CHOICES, inline_keyboard(rows))


async def handle_address_text(session: AsyncSession, chat_id: str, query: str) -> None:
    """Свободный ввод адреса жителем без привязанного дома (ONBOARD-002)."""

    stripped = query.strip()
    parsed = normalize_address(stripped) if stripped else None
    if parsed is None:
        await _send(session, chat_id, texts.ASK_ADDRESS_HINT)
        return

    short = canonical_address(parsed)
    matches = await service.search_houses(session, short)
    if not matches:
        await _send_not_found(session, chat_id, short)
        return
    await _send_address_choices(session, chat_id, short, matches)


async def on_bot_started(session: AsyncSession, event: ClaimedEvent) -> None:
    parsed = parse_webhook_payload(event.payload)
    if not parsed.chat_id or not parsed.max_user_id:
        return
    user = await get_or_create_user(session, parsed.max_user_id)
    state = await service.get_state(session, user.id, get_settings().consent_version)
    onboarded = state.primary_house is not None and state.has_valid_consent

    link = (
        await service.resolve_token(session, parsed.start_payload) if parsed.start_payload else None
    )
    if link is None:
        if onboarded and state.primary_house is not None:
            await _send(
                session,
                parsed.chat_id,
                texts.ALREADY_ONBOARDED.format(address=state.primary_house.address),
            )
        else:
            await _send(
                session,
                parsed.chat_id,
                texts.UNKNOWN_LINK if parsed.start_payload else texts.NO_LINK,
            )
        return

    token = parsed.start_payload or ""
    if onboarded and link.house.id in state.house_ids:
        await _send(
            session, parsed.chat_id, texts.ALREADY_ONBOARDED.format(address=link.house.address)
        )
    elif onboarded and state.primary_house is not None:
        await _send(
            session,
            parsed.chat_id,
            texts.ADD_HOUSE_PROMPT.format(
                address=link.house.address, primary=state.primary_house.address
            ),
            inline_keyboard(
                [
                    [
                        (
                            texts.BTN_ADD_HOUSE,
                            encode(OnboardingCallback(Action.ADD_HOUSE, RefKind.TOKEN, token)),
                        )
                    ]
                ]
            ),
        )
    else:
        await _send_consent_prompt(
            session, parsed.chat_id, link.house, ref_kind=RefKind.TOKEN, ref_value=token
        )


async def _resolve_ref(
    session: AsyncSession, cb: OnboardingCallback
) -> tuple[service.HouseInfo, str] | None:
    """Дом из кнопки + источник согласия. Ссылку перепроверяем при нажатии."""

    if cb.ref_kind is RefKind.TOKEN:
        link = await service.resolve_token(session, cb.ref_value)
        if link is None:
            return None
        return link.house, f"deep_link:{link.label}" if link.label else "deep_link"
    if cb.ref_kind in {RefKind.HOUSE, RefKind.SEARCH}:
        try:
            house_id = uuid.UUID(cb.ref_value)
        except ValueError:
            return None
        house = await service.get_house(session, house_id)
        if house is None:
            return None
        source = "address_search" if cb.ref_kind is RefKind.SEARCH else "reconsent"
        return house, source
    return None


async def on_callback(session: AsyncSession, event: ClaimedEvent) -> None:
    parsed = parse_webhook_payload(event.payload)
    if parsed.callback_id and parsed.max_user_id:
        # Отвечаем на любое нажатие, даже если payload не разобрался: иначе у
        # жителя «висит» кнопка и ничего не происходит.
        await outbox.enqueue_callback_answer(
            session,
            chat_id=parsed.chat_id or parsed.max_user_id,
            callback_id=parsed.callback_id,
            notification=texts.CALLBACK_ACK,
        )
    cb = decode(parsed.text)
    if cb is None or not parsed.max_user_id:
        if parsed.chat_id and parsed.max_user_id:
            logger.warning("unrecognized callback payload, event %s", event.id)
        return
    if not parsed.chat_id:
        logger.warning("onboarding callback without chat_id, event %s", event.id)
        return

    user = await get_or_create_user(session, parsed.max_user_id)
    if cb.action is Action.ACCEPT:
        await _accept(session, parsed, user.id, cb)
    elif cb.action is Action.DECLINE:
        await _decline(session, parsed, cb)
    elif cb.action is Action.WRONG_HOUSE:
        await _send(session, parsed.chat_id, texts.WRONG_HOUSE)
    elif cb.action is Action.ADD_HOUSE:
        await _add_house(session, parsed, user.id, cb)
    elif cb.action is Action.PICK:
        await _pick_house(session, parsed, cb)
    elif cb.action is Action.NONE_MATCH:
        await _none_match(session, parsed, cb)
    elif cb.action is Action.RETRY:
        await _send(session, parsed.chat_id, texts.ASK_ADDRESS)
    elif cb.action is Action.LEAVE:
        await _leave_address(session, parsed, user.id, cb)


async def _pick_house(session: AsyncSession, parsed: ParsedEvent, cb: OnboardingCallback) -> None:
    resolved = await _resolve_ref(session, cb)
    if resolved is None:
        await _send(session, parsed.chat_id, texts.ASK_ADDRESS)
        return
    house, _source = resolved
    await _send_consent_prompt(
        session, parsed.chat_id, house, ref_kind=RefKind.SEARCH, ref_value=str(house.id)
    )


async def _none_match(session: AsyncSession, parsed: ParsedEvent, cb: OnboardingCallback) -> None:
    address = decode_address(cb.ref_value)
    if not address:
        await _send(session, parsed.chat_id, texts.ASK_ADDRESS)
        return
    await _send_not_found(session, parsed.chat_id, address)


async def _leave_address(
    session: AsyncSession, parsed: ParsedEvent, user_id: uuid.UUID, cb: OnboardingCallback
) -> None:
    address = decode_address(cb.ref_value)
    if not address:
        await _send(session, parsed.chat_id, texts.ASK_ADDRESS_HINT)
        return
    # Payload кнопки собирали мы, но пришёл он из сети — длину не доверяем.
    address = address[:MAX_STORED_ADDRESS_CHARS]
    await service.create_house_request(session, user_id, address)
    await _send(session, parsed.chat_id, texts.ADDRESS_LEFT.format(address=address))


async def _accept(
    session: AsyncSession, parsed: ParsedEvent, user_id: uuid.UUID, cb: OnboardingCallback
) -> None:
    resolved = await _resolve_ref(session, cb)
    if resolved is None:
        await _send(session, parsed.chat_id, texts.UNKNOWN_LINK)
        return
    house, source = resolved

    version = get_settings().consent_version
    if cb.consent_version != version:
        await _send_consent_prompt(
            session, parsed.chat_id, house, ref_kind=cb.ref_kind, ref_value=cb.ref_value
        )
        return

    state = await service.get_state(session, user_id, version)
    if state.has_valid_consent and house.id in state.house_ids:
        return

    await service.bind_house(session, user_id, house.id)
    await service.grant_consent(session, user_id, version=version, source=source)
    released = await inbox.release_held(session, parsed.max_user_id)
    await _send(session, parsed.chat_id, texts.ONBOARDED_WITH_HELD if released else texts.ONBOARDED)


async def _decline(session: AsyncSession, parsed: ParsedEvent, cb: OnboardingCallback) -> None:
    await inbox.close_held(session, parsed.max_user_id, reason="consent_declined")
    text = texts.DECLINED
    resolved = await _resolve_ref(session, cb)
    if resolved is not None and resolved[0].ads_phone:
        text += texts.DECLINED_ADS.format(phone=resolved[0].ads_phone)
    await _send(session, parsed.chat_id, text)


async def _add_house(
    session: AsyncSession, parsed: ParsedEvent, user_id: uuid.UUID, cb: OnboardingCallback
) -> None:
    resolved = await _resolve_ref(session, cb)
    if resolved is None:
        await _send(session, parsed.chat_id, texts.UNKNOWN_LINK)
        return
    house, _source = resolved
    state = await service.get_state(session, user_id, get_settings().consent_version)
    if not state.has_valid_consent or state.primary_house is None:
        await _send_consent_prompt(
            session, parsed.chat_id, house, ref_kind=cb.ref_kind, ref_value=cb.ref_value
        )
        return
    await service.bind_house(session, user_id, house.id)
    await _send(
        session,
        parsed.chat_id,
        texts.HOUSE_ADDED.format(address=house.address, primary=state.primary_house.address),
    )


def is_revoke_command(text: str | None) -> bool:
    return text is not None and text.strip().lower() in texts.REVOKE_COMMANDS


def is_start_command(text: str | None) -> bool:
    return text is not None and text.strip().lower().split("@", 1)[0] in texts.START_COMMANDS


def gated(next_handler: Handler) -> Handler:
    """Шлюз перед обработкой содержательного сообщения (architecture.md §7.1)."""

    async def handler(session: AsyncSession, event: ClaimedEvent) -> None:
        parsed = parse_webhook_payload(event.payload)
        if not parsed.chat_id or not parsed.max_user_id:
            return
        user = await get_or_create_user(session, parsed.max_user_id)

        if is_revoke_command(parsed.text):
            revoked = await service.revoke_consent(session, user.id)
            await _send(session, parsed.chat_id, texts.REVOKED if revoked else texts.REVOKE_NOTHING)
            return

        state = await service.get_state(session, user.id, get_settings().consent_version)
        onboarded = state.primary_house is not None and state.has_valid_consent

        if is_start_command(parsed.text):
            if onboarded and state.primary_house is not None:
                await _send(
                    session,
                    parsed.chat_id,
                    texts.ALREADY_ONBOARDED.format(address=state.primary_house.address),
                )
            else:
                await _send(session, parsed.chat_id, texts.NO_LINK)
            return

        if onboarded:
            await next_handler(session, event)
            return

        if state.primary_house is None:
            # Без дома любой текст — поиск адреса (ONBOARD-002), не удержание заявки.
            await handle_address_text(session, parsed.chat_id, parsed.text or "")
            return

        await inbox.hold_for_consent(session, event.id)
        await _send_consent_prompt(
            session,
            parsed.chat_id,
            state.primary_house,
            ref_kind=RefKind.HOUSE,
            ref_value=str(state.primary_house.id),
            kept_message=True,
        )

    return handler
