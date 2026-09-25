"""Вырезка пункта нормы для ответа жителю."""

from __future__ import annotations

from upravdom.knowledge.excerpt import match_position, norm_excerpt

# Дословное начало ПП РФ №491 п. 2 (как в data/kb/sources/pp491.md).
PP491_P2 = (
    "2. В состав общего имущества включаются:\n"
    "а) помещения в многоквартирном доме, не являющиеся частями квартир и предназначенные "
    "для обслуживания более одного жилого и (или) нежилого помещения в этом многоквартирном "
    "доме (далее - помещения общего пользования), в том числе межквартирные лестничные "
    "площадки, лестницы, лифты, лифтовые и иные шахты, коридоры, колясочные, чердаки, "
    "технические этажи и технические подвалы, в которых имеются инженерные коммуникации;\n"
    "б) крыши;\n"
    "в) ограждающие несущие конструкции многоквартирного дома (включая фундаменты, несущие "
    "стены, плиты перекрытий, балконные и иные плиты);\n"
    "г) ограждающие ненесущие конструкции многоквартирного дома, обслуживающие более одного "
    "жилого и (или) нежилого помещения (включая окна и двери помещений общего пользования, "
    "перила, парапеты и иные ограждающие ненесущие конструкции);"
)


def test_lift_complaint_shows_lift_clause_with_lead() -> None:
    out = norm_excerpt(PP491_P2, "Лифт", "лифт застрял между этажами, двери не открываются")
    assert "лифты" in out
    assert out.startswith("В состав общего имущества включаются: … ")
    assert not out.startswith("2.")


def test_type_title_outranks_complaint_words() -> None:
    # «двери» из жалобы встречаются в подпункте «г», но тип проблемы — лифт.
    out = norm_excerpt(PP491_P2, "Лифт", "двери лифта не открываются")
    assert "лифты" in out
    assert "окна и двери" not in out


def test_cut_on_word_boundary_and_ellipsis_only_when_cut() -> None:
    out = norm_excerpt(PP491_P2, "Лифт", "лифт", limit=120)
    assert out.endswith("…")
    body = out.removesuffix("…").split(" … ", 1)[-1]
    words = PP491_P2.replace("\n", " ").split()
    assert body.split()[-1].rstrip(",;:") in {w.rstrip(",;:") for w in words}

    short = "5. Коротко о главном."
    assert norm_excerpt(short, "главное") == short


def test_no_match_falls_back_to_start() -> None:
    out = norm_excerpt(PP491_P2, "Газ", "газ не зажигается", limit=80)
    assert out.startswith("2. В состав общего имущества")
    assert out.endswith("…")


def test_match_position_priority_and_yo() -> None:
    assert match_position(PP491_P2, "ёлка") is None
    assert match_position("зелёные ёлки", "ЕЛКИ") is not None
    lift = match_position(PP491_P2, "Лифт")
    doors = match_position(PP491_P2, "двери")
    assert lift is not None and doors is not None and lift < doors
    assert match_position(PP491_P2, "", "крыша") == PP491_P2.replace("\n", " ").find("крыши")


def test_empty_body() -> None:
    assert norm_excerpt("", "лифт") == ""
