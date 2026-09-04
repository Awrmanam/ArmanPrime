from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from shopbot.repository import AccessDenied
from shopbot.runtime import persistent_router


class RedisFake:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, **_kwargs):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        return sum(self.values.pop(key, None) is not None for key in keys)


class CoordinatorFake:
    def __init__(self):
        self.redis = RedisFake()
        self.callbacks = {}
        self.counter = 0

    async def rate_limit(self, *_args):
        return True

    async def issue_callback(self, action, actor, object_id="", version=1, **_kwargs):
        self.counter += 1
        token = f"c1.dispatch{self.counter}"
        self.callbacks[token] = {"a": action, "u": actor, "o": object_id, "v": version}
        return token

    async def resolve_callback(self, token, actor):
        state = self.callbacks[token]
        if state["u"] != actor:
            raise AccessDenied("CALLBACK_ACTOR_MISMATCH")
        return state


class SessionsFake:
    @asynccontextmanager
    async def begin(self):
        yield SimpleNamespace()


class RepoFake:
    def __init__(self):
        self.coordinator = CoordinatorFake()
        self.sessions = SessionsFake()
        self.terms = None
        self.accepted = False

    def owner(self, _actor):
        raise AccessDenied("OWNER_REQUIRED")

    async def user(self, actor, _session, *, username=None, display_name=None):
        return SimpleNamespace(
            telegram_id=actor,
            username=username,
            display_name=display_name,
        )

    async def current_terms(self, _session):
        return self.terms

    async def has_current_consent(self, *_args):
        return self.accepted

    async def accept_terms(self, *_args):
        self.accepted = True


class RecordingSession(BaseSession):
    """Telegram network boundary used while exercising the real Dispatcher."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.message_id = 100

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            self.message_id += 1
            return Message(
                message_id=self.message_id,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
            ).as_(bot)
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"unexpected Telegram method: {type(method).__name__}")

    async def stream_content(self, *_args, **_kwargs):
        if False:  # pragma: no cover - required async-generator shape
            yield b""


def incoming_message(bot: Bot, actor: int, text: str, message_id: int = 1) -> Message:
    entities = [MessageEntity(type="bot_command", offset=0, length=len(text))]
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=actor, type="private"),
        from_user=User(id=actor, is_bot=False, first_name="Customer"),
        text=text,
        entities=entities,
    ).as_(bot)


@pytest.mark.asyncio
async def test_real_dispatcher_processes_start_consent_and_home():
    repo = RepoFake()
    repo.terms = type(
        "Terms", (), {"id": uuid4(), "version": 1, "title": "Terms", "pages": ["Body"]}
    )()
    session = RecordingSession()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(persistent_router(repo))

    await dispatcher.feed_update(
        bot, Update(update_id=1, message=incoming_message(bot, 2, "/start"))
    )
    terms_message = next(call for call in session.calls if isinstance(call, SendMessage))
    consent_token = terms_message.reply_markup.inline_keyboard[0][0].callback_data
    assert len(consent_token.encode()) <= 64
    assert terms_message.reply_markup.inline_keyboard[0][0].style == "success"

    callback_message = incoming_message(bot, 2, "Terms", message_id=2)
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="callback-1",
                from_user=User(id=2, is_bot=False, first_name="Customer"),
                chat_instance="private-2",
                data=consent_token,
                message=callback_message,
            ),
        ),
    )

    sent_texts = [call.text for call in session.calls if isinstance(call, SendMessage)]
    assert sent_texts == [
        "Terms\n\nBody",
        "به فروشگاه خوش آمدید\n\nمحصول موردنظر خود را از فروشگاه انتخاب کنید.",
    ]
    assert repo.accepted is True
    home_call = [call for call in session.calls if isinstance(call, SendMessage)][-1]
    assert all(
        len(button.callback_data.encode()) <= 64
        for row in home_call.reply_markup.inline_keyboard
        for button in row
    )

    foreign_token = home_call.reply_markup.inline_keyboard[0][0].callback_data
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=3,
            callback_query=CallbackQuery(
                id="callback-cross-user",
                from_user=User(id=3, is_bot=False, first_name="Other"),
                chat_instance="private-3",
                data=foreign_token,
                message=incoming_message(bot, 3, "Home", message_id=3),
            ),
        ),
    )
    denial = [call for call in session.calls if isinstance(call, AnswerCallbackQuery)][-1]
    assert denial.show_alert is True
    assert denial.text == "این درخواست منقضی شده است؛ دوباره تلاش کنید."
    await bot.session.close()
