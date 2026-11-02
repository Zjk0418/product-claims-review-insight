"""BGE 中文语义向量（ONNX Runtime，无需 PyTorch）。

模型默认位于 models/bge-small-zh-v1.5/：
  tokenizer.json + model.onnx（Xenova 导出的 BAAI/bge-small-zh-v1.5）
使用 BGE 官方的 CLS pooling + L2 归一化。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .issue_schema import DIRECTION_LABELS, Issue
from resources import resource_path

MODEL_ID = "BAAI/bge-small-zh-v1.5"
DEFAULT_MODEL_DIR = resource_path("models", "bge-small-zh-v1.5")


class ModelUnavailable(RuntimeError):
    pass


class BGEEmbedder:
    def __init__(
        self,
        model_dir: Path | None = None,
        max_length: int = 256,
        batch_size: int = 16,
        threads: int = 4,
    ):
        self.model_dir = Path(model_dir or DEFAULT_MODEL_DIR)
        model_path = self.model_dir / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not model_path.exists() or not tokenizer_path.exists():
            raise ModelUnavailable(
                f"未找到 BGE 模型文件：{self.model_dir}（需要 model.onnx 与 tokenizer.json）"
            )

        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.batch_size = batch_size
        self.max_length = max_length

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 512), dtype=np.float32)
        vectors: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            encoded = self.tokenizer.encode_batch(chunk)
            input_ids = np.array([item.ids for item in encoded], dtype=np.int64)
            attention_mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)
            feed: dict[str, np.ndarray] = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            outputs = self.session.run(None, feed)
            hidden = outputs[0]
            if hidden.ndim == 3:
                cls = hidden[:, 0, :]  # BGE 使用 CLS pooling
            else:
                cls = hidden
            norm = np.linalg.norm(cls, axis=1, keepdims=True)
            vectors.append((cls / np.maximum(norm, 1e-9)).astype(np.float32))
        return np.vstack(vectors)


def issue_text(issue: Issue) -> str:
    """用于向量化的规范文本：对象 + 方面 + 具体表现 + 方向。"""
    direction = DIRECTION_LABELS.get(issue.direction, issue.direction or "未说明")
    return (
        f"对象：{issue.object}；方面：{issue.aspect}；"
        f"表现：{issue.manifestation}；方向：{direction}"
    )


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


class EmbeddingCache:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[float]] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, text: str) -> np.ndarray | None:
        value = self.data.get(_hash_text(text))
        return np.array(value, dtype=np.float32) if value else None

    def put(self, text: str, vector: np.ndarray) -> None:
        self.data[_hash_text(text)] = [round(float(v), 7) for v in vector]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def embed_issues(
    issues: Iterable[Issue],
    model_dir: Path | None = None,
    cache_path: Path | None = None,
) -> tuple[list[str], np.ndarray, dict[str, Any]]:
    issue_list = list(issues)
    texts = [issue_text(issue) for issue in issue_list]
    cache = EmbeddingCache(cache_path) if cache_path else None
    vectors: list[np.ndarray] = []
    missing: list[str] = []
    missing_index: list[int] = []

    for index, text in enumerate(texts):
        cached = cache.get(text) if cache else None
        if cached is not None and cached.shape[0] == 512:
            vectors.append(cached)
        else:
            vectors.append(np.zeros(512, dtype=np.float32))
            missing.append(text)
            missing_index.append(index)

    if missing:
        embedder = BGEEmbedder(model_dir)
        encoded = embedder.encode(missing)
        for position, index in enumerate(missing_index):
            vectors[index] = encoded[position]
            if cache:
                cache.put(texts[index], encoded[position])
        if cache:
            cache.save()

    matrix = np.vstack(vectors) if vectors else np.zeros((0, 512), dtype=np.float32)
    info = {
        "model": MODEL_ID,
        "dimension": int(matrix.shape[1]) if matrix.size else 512,
        "text_count": len(texts),
        "cache_hits": len(texts) - len(missing),
        "encoded_count": len(missing),
    }
    return texts, matrix, info
