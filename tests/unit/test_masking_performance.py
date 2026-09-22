"""Бюджет маскирования < 1 мс на сообщение до 1000 символов (MASK-001)."""

from __future__ import annotations

import time

from upravdom.masking import mask


def test_mask_under_one_ms_for_thousand_chars() -> None:
    # Прогрев (компиляция regex / загрузка словаря имён)
    sample = (
        "Здравствуйте, меня зовут Сергей Иванов, кв. 42, телефон +7 917 123-45-67, "
        "mail test@example.com, на 5 этаже течёт стояк третий день, ПП 354."
    )
    mask(sample)

    text = (sample + " ") * 20
    text = text[:1000]
    assert len(text) == 1000

    times: list[float] = []
    for _ in range(200):
        t0 = time.perf_counter()
        mask(text)
        times.append(time.perf_counter() - t0)

    # Медиана устойчивее к редким пикам планировщика ОС.
    times.sort()
    median_ms = times[len(times) // 2] * 1000
    assert median_ms < 1.0, f"median={median_ms:.3f} ms"
