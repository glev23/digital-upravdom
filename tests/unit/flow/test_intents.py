"""Интенты бытового диалога (FLOW-002)."""

from __future__ import annotations

import pytest

from upravdom.flow.intents import Intent, detect

HELP = [
    "что умеешь",
    "Что ты умеешь?",
    "как пользоваться",
    "как тобой пользоваться",
    "как это работает",
    "а ты кто",
    "ты бот?",
    "инструкция",
    "/help",
    "меню",
]
HOUSE = [
    "какой у меня дом",
    "какой дом привязан",
    "мой адрес",
    "я по какому адресу",
    "это не мой дом",
    "как сменить дом",
    "как отвязать адрес",
]
SMALLTALK = [
    "привет",
    "Здравствуйте!",
    "добрый день",
    "спасибо",
    "спасибо большое",
    "спс",
    "ок",
    "Хорошо.",
    "ясно",
    "да",
    "нет",
    "ага",
    "пока",
    "до свидания",
]
JUNK = [")))", "?", "!!!", "12345", "—"]

# Опасное направление: принять жалобу за болтовню значит потерять проблему
# жителя. Здесь перехвата быть не должно ни при каких условиях.
COMPLAINTS = [
    "привет, течёт кран в подвале",
    "спасибо, но проблема осталась",
    "да, всё ещё течёт",
    "нет воды",
    "нет горячей воды",
    "ок но вода не идет",
    "что делать если нет отопления",
    "лифт застрял",
    "в подъезде темно",
    "какой ужас у нас потоп",
    "мой адрес затопило",
    "дом трясётся",
    "какой дом обслуживает эту котельную, у нас холодно",
]


@pytest.mark.parametrize("phrase", HELP)
def test_help(phrase: str) -> None:
    assert detect(phrase) is Intent.HELP


@pytest.mark.parametrize("phrase", HOUSE)
def test_house_info(phrase: str) -> None:
    assert detect(phrase) is Intent.HOUSE_INFO


@pytest.mark.parametrize("phrase", SMALLTALK)
def test_smalltalk(phrase: str) -> None:
    assert detect(phrase) is Intent.SMALLTALK


@pytest.mark.parametrize("phrase", JUNK)
def test_junk(phrase: str) -> None:
    assert detect(phrase) is Intent.JUNK


@pytest.mark.parametrize("phrase", COMPLAINTS)
def test_complaints_are_never_intercepted(phrase: str) -> None:
    assert detect(phrase) is None, "жалоба ушла бы в болтовню — проблема жителя потеряна"


def test_empty_and_none() -> None:
    assert detect(None) is None
    assert detect("") is None
    assert detect("   ") is None


def test_yo_is_normalized() -> None:
    assert detect("всего доброго") is Intent.SMALLTALK


def test_multiword_gibberish_goes_to_classification() -> None:
    """Мусор из нескольких слов не перехватываем: «нет воды» тоже короткое."""

    assert detect("ляляля тополя") is None
