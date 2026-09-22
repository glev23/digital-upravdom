"""SPIKE-001: экспорт sergeyzh/BERTA в ONNX и сохранение в репозиторий.

Выполняется один раз локально (с torch — только на этапе экспорта). Результат
коммитится в data/model/berta-onnx/, чтобы Docker-сборка не тянула torch и не
экспортировала модель заново — только `onnxruntime` + копирование файлов.
"""

from __future__ import annotations

from pathlib import Path

from sentence_transformers import SentenceTransformer

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "model" / "berta-onnx"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer("sergeyzh/BERTA", backend="onnx", device="cpu")
    model.save_pretrained(str(OUTPUT_DIR))
    print(f"saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
