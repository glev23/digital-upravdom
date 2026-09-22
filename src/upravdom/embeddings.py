"""Эмбеддинги через ONNX Runtime — без torch в рантайме (SPIKE-001).

Модель `sergeyzh/BERTA` экспортируется в ONNX на стейдже `exporter`
(`docker/app.Dockerfile` / `scripts/export_onnx.py`) и попадает в образ как
`data/model/berta-onnx/`. Локально для разработки тот же каталог создаёт
`export_onnx.py`. `torch`/`sentence-transformers` в рантайм продукта не входят.

Пулинг и нормализация повторяют sentence-transformers: mean pooling по
attention_mask + L2-нормализация (совпадение векторов с эталоном проверено
в SPIKE-001 — cosine similarity ~1.0 на 50 фразах).

Промпты — из `config_sentence_transformers.json` модели (E5-стиль):
асимметричный поиск использует `search_query:` / `search_document:`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from tokenizers import Tokenizer

MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "model" / "berta-onnx"

# Точные строки из data/model/berta-onnx/config_sentence_transformers.json
PROMPT_SEARCH_QUERY = "search_query: "
PROMPT_SEARCH_DOCUMENT = "search_document: "
PROMPT_CLASSIFICATION = "categorize_entailment: "

MAX_SEQUENCE_LENGTH = 512


@lru_cache
def _session() -> ort.InferenceSession:
    return ort.InferenceSession(
        str(MODEL_DIR / "onnx" / "model.onnx"), providers=["CPUExecutionProvider"]
    )


@lru_cache
def _embed_tokenizer() -> Tokenizer:
    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    tokenizer.enable_padding()
    tokenizer.enable_truncation(max_length=MAX_SEQUENCE_LENGTH)
    return tokenizer


@lru_cache
def _count_tokenizer() -> Tokenizer:
    """Без truncation/padding — для проверки лимита чанка."""

    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def token_length(text: str, *, prompt: str) -> int:
    """Число токенов промпт+текст без обрезки — для контроля лимита чанка."""

    return len(_count_tokenizer().encode(prompt + text).ids)


def warmup() -> None:
    """Прогрев ONNX-сессии и токенизатора при старте приложения (CLASSIFY-001)."""

    embed(["warmup"], prompt=PROMPT_SEARCH_QUERY)


def embed(texts: list[str], *, prompt: str = PROMPT_CLASSIFICATION) -> NDArray[np.float32]:
    """L2-нормализованные эмбеддинги, dim 768 (architecture.md §2).

    `prompt` обязателен по смыслу модели; дефолт — классификационный префикс
    SPIKE-001, чтобы старые вызовы без явного промпта не ломались молча.
    Для KB/retrieval передавайте `PROMPT_SEARCH_QUERY` / `PROMPT_SEARCH_DOCUMENT`.
    """

    if not texts:
        return np.zeros((0, 768), dtype=np.float32)

    prefixed = [prompt + t for t in texts]
    encodings = _embed_tokenizer().encode_batch(prefixed)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)

    (last_hidden_state,) = _session().run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )

    mask = attention_mask[:, :, None].astype(np.float32)
    summed = (last_hidden_state * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
    mean_pooled = summed / counts

    norms = np.linalg.norm(mean_pooled, axis=1, keepdims=True)
    norms = np.clip(norms, a_min=1e-9, a_max=None)
    result: NDArray[np.float32] = (mean_pooled / norms).astype(np.float32)
    return result
