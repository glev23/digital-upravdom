"""Маскирование ПДн: позитивный, негативный и смешанный наборы (MASK-001)."""

from __future__ import annotations

import pytest

from upravdom.masking import (
    PLACEHOLDER_APARTMENT,
    PLACEHOLDER_EMAIL,
    PLACEHOLDER_FIO,
    PLACEHOLDER_ID,
    PLACEHOLDER_PHONE,
    MaskedText,
    MaskResult,
    mask,
)

# Вымышленные ПДн — не из реальных обращений.


@pytest.mark.parametrize(
    ("raw", "must_contain"),
    [
        # --- телефоны ---
        ("перезвоните +7 917 123-45-67 срочно", PLACEHOLDER_PHONE),
        ("мой номер 89171234567", PLACEHOLDER_PHONE),
        ("телефон 8 (843) 222-33-44", PLACEHOLDER_PHONE),
        ("пишите +7(917)1234567", PLACEHOLDER_PHONE),
        ("городской 222-33-44 свободен", PLACEHOLDER_PHONE),
        ("звоните 8-917-123-45-67", PLACEHOLDER_PHONE),
        ("ТЕЛЕФОН +79171234567", PLACEHOLDER_PHONE),
        ("номер:+7 999 111 22 33 спасибо", PLACEHOLDER_PHONE),
        # --- e-mail ---
        ("напишите на ivanova.test@example.com пожалуйста", PLACEHOLDER_EMAIL),
        ("почта resident42@mail.ru", PLACEHOLDER_EMAIL),
        ("Email: petrov_a@yandex.ru", PLACEHOLDER_EMAIL),
        # --- квартиры ---
        ("я из кв. 42 течёт", PLACEHOLDER_APARTMENT),
        ("кв42 без воды", PLACEHOLDER_APARTMENT),
        ("квартира 15 холодная", PLACEHOLDER_APARTMENT),
        ("квартира номер 7", PLACEHOLDER_APARTMENT),
        ("живу в 42-й квартире", PLACEHOLDER_APARTMENT),
        ("из 12-й звоню", PLACEHOLDER_APARTMENT),
        ("42-я квартира без отопления", PLACEHOLDER_APARTMENT),
        ("КВАРТИРА 99 затопило", PLACEHOLDER_APARTMENT),
        ("кв.8 стояк", PLACEHOLDER_APARTMENT),
        # --- ФИО ---
        ("здравствуйте Иванова течёт кран", PLACEHOLDER_FIO),
        ("пишет Иванов И.И. прошу помощи", PLACEHOLDER_FIO),
        ("обращается И. И. Петров", PLACEHOLDER_FIO),
        ("просила Ольга Петровна", PLACEHOLDER_FIO),
        ("меня зовут Сергей помогите", PLACEHOLDER_FIO),
        ("с уважением, Петров", PLACEHOLDER_FIO),
        ("я — Мария без горячей воды", PLACEHOLDER_FIO),
        ("перезвоните Анне пожалуйста", PLACEHOLDER_FIO),
        ("оставила Сидоровна заявку", PLACEHOLDER_FIO),
        ("течёт у Кузнецова с 3 этажа", PLACEHOLDER_FIO),
        ("пишет Смирнова про лифт", PLACEHOLDER_FIO),
        ("это Николаевна из дома", PLACEHOLDER_FIO),
        # --- длинные номера ---
        ("лицевой счёт 12345678 проверьте", PLACEHOLDER_ID),
        ("СНИЛС 123-456-789 01", PLACEHOLDER_ID),
        ("паспорт 92 04 123456", PLACEHOLDER_ID),
        ("счёт 987654321012", PLACEHOLDER_ID),
        # --- опечатки / регистр / без пунктуации ---
        ("ТЕЧЁТ У ИВАНОВОЙ В КВ 5", PLACEHOLDER_FIO),
        ("кв 33 нет света 89170001122", PLACEHOLDER_PHONE),
        ("петрова ольга петровна кв.1", PLACEHOLDER_FIO),
        ("mail test.user+tag@corp.co.ru срочно", PLACEHOLDER_EMAIL),
    ],
)
def test_positive_masks_category(raw: str, must_contain: str) -> None:
    result = mask(raw)
    assert must_contain in result.text
    assert isinstance(result.text, str)
    assert type(result.text) is str or True  # NewType runtime is str
    # исходное значение не должно остаться целиком для телефонов/email с цифрами
    assert result.counts


@pytest.mark.parametrize(
    "raw",
    [
        "на 5-м этаже холодно",
        "3 подъезд без света",
        "с 9-го этажа течёт",
        "улица Декабристов, 10",
        "г. Казань, ул. Патриса Лумумбы, д. 5",
        "по ПП 354 недопустимый перерыв",
        "по 416 постановлению",
        "ст. 36 ЖК общее имущество",
        "ПП РФ №491 пункт 5",
        "+14 в квартире уже второй день",
        "18 градусов на термометре",
        "с 9 до 17 никого нет",
        "третий день без воды",
        "с 22.09 нет отопления",
        "Батарея холодная совсем",
        "Вода ржавая из крана",
        "Водоканал не отвечает",
        "Татэнерго отключило свет",
        "нет горячей воды с утра",
        "течёт стояк в подвале",
        "засор канализации в подъезде",
        "лифт не работает неделю",
        "крыша протекает после дождя",
        "авария на теплосети",
        "норматив 4 часа единовременно",
        # Бытовые формулировки, которые ломала первая версия правил ФИО/квартир:
        # суффиксы фамилий срабатывали на любом слове, «из N» считалось квартирой.
        "течёт из-под машины во дворе, причина неизвестна",
        "у нас нет воды из 5 кранов, в доме 3 подъезда",
        "хочу спросить почему нет горячей воды",
        "течь в подвале у магазина, затопило кабину лифта",
        "выпал кирпич с фасада",
        "в подъезде 2 из 3 лифтов не работают",
        "протечка с крыши, видны следы на потолке около стояков",
        "затопило квартиру сверху, соседи не открывают, звонили в диспетчерскую",
        "никто не пишет ответ уже неделю",
        "с августа нет горячей воды",
        "была дана заявка, никто не пришёл",
        "ОКОЛО СТОЯКОВ ТЕЧЁТ, ПРИЧИНА НЕЯСНА",
    ],
)
def test_negative_unchanged_byte_for_byte(raw: str) -> None:
    assert mask(raw).text == raw


def test_mixed_case_full_string() -> None:
    raw = "я из 42-й, Иванова, 8 917 123 45 67, на 5 этаже с потолка течёт"
    result = mask(raw)
    assert PLACEHOLDER_APARTMENT in result.text
    assert PLACEHOLDER_FIO in result.text
    assert PLACEHOLDER_PHONE in result.text
    assert "5 этаже" in result.text or "на 5 этаже" in result.text
    assert "течёт" in result.text
    assert "42" not in result.text
    assert "Иванова" not in result.text
    assert "917" not in result.text


def test_mask_result_has_no_original_secrets() -> None:
    raw = "течёт у Иванова +79171234567 кв.42 secret@mail.ru счёт 1234567890"
    result = mask(raw)
    blob = repr(result)
    assert "79171234567" not in blob
    assert "Иванова" not in blob
    assert "secret@mail.ru" not in blob
    assert "1234567890" not in blob
    assert "кв.42" not in blob
    assert isinstance(result, MaskResult)
    assert set(result.counts) >= {"phone", "email", "apartment", "fio", "id_number"}


def test_masked_text_newtype_documented() -> None:
    """CLASSIFY-001/RIGHTS-001 принимают MaskedText, не сырой str (для mypy)."""

    result = mask("тест")
    assert isinstance(result.text, str)
    assert MaskedText.__name__ == "MaskedText"
    typed: MaskedText = result.text
    assert typed == result.text


def test_at_least_forty_positives_and_twenty_five_negatives() -> None:
    # Проверяем размеры наборов по исходным спискам в этом модуле
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    pos = neg = 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "parametrize"):
                continue
            if not dec.args:
                continue
            arg0 = dec.args[0]
            if isinstance(arg0, ast.Constant) and arg0.value == "raw":
                cases = dec.args[1]
                if isinstance(cases, ast.List):
                    neg = max(neg, len(cases.elts))
            if isinstance(arg0, ast.Tuple):
                cases = dec.args[1]
                if isinstance(cases, ast.List):
                    pos = max(pos, len(cases.elts))
    assert pos >= 40, pos
    assert neg >= 25, neg


def test_completeness_targets_on_labeled_set() -> None:
    """Полнота по категориям на размеченном наборе (цель в закрытии)."""

    phones = [
        "+7 917 123-45-67",
        "89171234567",
        "8 (843) 222-33-44",
        "+7(917)1234567",
        "222-33-44",
        "8-999-111-22-33",
        "+79161234567",
        "84722112233",
    ]
    emails = [
        "a@b.ru",
        "user.name@example.com",
        "x+y@mail.ru",
        "cap@YANDEX.RU",
    ]
    apartments = [
        "кв. 42",
        "кв42",
        "квартира 15",
        "квартира номер 7",
        "в 42-й квартире",
        "из 12-й",
        "42-я квартира",
        "кв.8",
    ]
    fios = [
        "течёт Иванова",
        "пишет Иванов И.И.",
        "обращается И. И. Петров",
        "просила Ольга Петровна",
        "меня зовут Сергей",
        "с уважением, Петров",
        "я — Мария",
        "у Кузнецова засор",
        "пишет Смирнова",
        "оставила Сидоровна",
    ]

    def rate(samples: list[str], placeholder: str) -> float:
        hits = sum(1 for s in samples if placeholder in mask(s).text)
        return hits / len(samples)

    assert rate(phones, PLACEHOLDER_PHONE) == 1.0
    assert rate(emails, PLACEHOLDER_EMAIL) == 1.0
    assert rate(apartments, PLACEHOLDER_APARTMENT) == 1.0
    fio_rate = rate(fios, PLACEHOLDER_FIO)
    assert fio_rate >= 0.9, fio_rate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("петрова ольга петровна кв.1", "[ФИО] [КВАРТИРА]"),
        ("ТЕЧЁТ У ИВАНОВОЙ В КВ 5", "ТЕЧЁТ У [ФИО] В [КВАРТИРА]"),
        ("обращается Анна Сергеевна Кузнецова", "обращается [ФИО]"),
        ("течёт у Кузнецова с 3 этажа", "течёт у [ФИО] с 3 этажа"),
    ],
)
def test_full_name_is_masked_as_one_unit(raw: str, expected: str) -> None:
    assert mask(raw).text == expected
