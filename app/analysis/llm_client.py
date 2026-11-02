"""OpenAI 兼容的 LLM 客户端（不依赖 openai SDK）。

密钥只从环境变量读取；不在代码、日志或输出中写入密钥。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:500]


def load_env_file(path: Path) -> None:
    """读取简单的 KEY=VALUE 配置；已存在的环境变量优先。"""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class LLMConfig:
    api_key: str = field(repr=False)
    model: str
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 90
    max_retries: int = 3
    temperature: float = 0.1
    max_tokens: int = 2000
    json_mode: bool = True

    @classmethod
    def from_env(cls, root: Path | None = None) -> "LLMConfig | None":
        from api_settings import SETTINGS

        # 课堂版等无人值守形态要可预期：只看项目 .env，不读本机 GUI 里存的配置。
        # GUI 配置保存在本机用户目录，换一台机器就没了，而课堂版要能照文档复现。
        configured = None if os.getenv("LLM_IGNORE_APP_SETTINGS") == "1" else SETTINGS.llm_kwargs()
        if configured:
            return cls(**configured)
        if os.getenv("REVIEW_APP_MODE") == "1":
            # Desktop app credentials must be explicitly configured by its user.
            # Project .env files and inherited shell keys belong to legacy CLI use.
            return None
        if root is not None:
            load_env_file(root / ".env")
        api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        if not api_key:
            return None
        model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
        base_url = (
            os.getenv("LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        try:
            timeout = int(os.getenv("LLM_TIMEOUT", "90"))
        except ValueError:
            timeout = 90
        try:
            max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2000"))
        except ValueError:
            max_tokens = 2000
        try:
            max_retries = max(1, int(os.getenv("LLM_MAX_RETRIES", "3")))
        except ValueError:
            max_retries = 3
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_tokens=max_tokens,
            max_retries=max_retries,
            json_mode=os.getenv("LLM_JSON_MODE", "1").lower() not in {"0", "false", "no"},
        )

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "json_mode": self.json_mode,
        }


@dataclass
class LLMResponse:
    content: str
    usage: dict[str, Any]
    latency_ms: int
    attempts: int


class ChatClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.session = requests.Session()
        # 保留系统代理设置（访问外部 API 需要）
        self.session.trust_env = True
        # requests.Session 不是线程安全的；并发批次各用各的连接池。
        self._local = threading.local()
        self._json_mode_lock = threading.Lock()

    def _thread_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            direct = bool(getattr(self._local, "direct", False))
            # direct=True 时不读系统/环境代理：本机代理软件留下的失效端口
            # 会让所有请求变成 ProxyError，现象却像 "模型服务连接中断"。
            session.trust_env = not direct
            if direct:
                session.proxies = {}
            self._local.session = session
        return session

    def _drop_session(self) -> None:
        """丢弃本线程的连接池。

        服务端主动关闭 keep-alive 连接后，池里那条连接已经是"半死"状态；
        如果重试还从同一个池里取，就会连续报 RemoteDisconnected——重试等于没重试。
        连接类错误必须重建连接才有意义。
        """
        session = getattr(self._local, "session", None)
        if session is None:
            return
        try:
            session.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响后续新建
            pass
        self._local.session = None

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        url = f"{self.config.base_url}/chat/completions"
        started = time.time()
        try:
            response = self._thread_session().post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.config.timeout,
            )
        except requests.exceptions.ConnectionError:
            # 走代理连不上就改直连再试一次：代理端口失效时，重试同一个代理毫无意义。
            if getattr(self._local, "direct", False):
                raise
            self._local.direct = True
            self._drop_session()
            response = self._thread_session().post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.config.timeout,
            )
        return response, int((time.time() - started) * 1000)

    def chat_json(self, system: str, user: str) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        with self._json_mode_lock:
            if self.config.json_mode:
                payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        attempts = 0
        for attempt in range(1, self.config.max_retries + 1):
            attempts = attempt
            try:
                response, latency = self._post(payload)
                if response.status_code == 400 and "response_format" in payload:
                    # Each in-flight payload must perform its own compatibility
                    # fallback, even if another worker has already disabled JSON
                    # mode for later requests. Preserve capability discovery for
                    # subsequent clients using the same pipeline configuration.
                    with self._json_mode_lock:
                        self.config.json_mode = False
                    payload.pop("response_format", None)
                    # This request can also fail transiently; keep it inside the
                    # same retry boundary as the original request.
                    response, latency = self._post(payload)
            except requests.RequestException as exc:
                last_error = exc
                # 连接被服务端断开（RemoteDisconnected / Connection aborted 等）时，
                # 旧的 keep-alive 连接已经不可用，必须换一条新连接再试，否则重试必然连败。
                self._drop_session()
                if attempt < self.config.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                continue

            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = LLMError(
                    f"HTTP {response.status_code}", response.status_code, response.text
                )
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(
                    2 ** attempt, 20
                )
                if attempt < self.config.max_retries:
                    time.sleep(wait)
                continue

            if response.status_code >= 400:
                raise LLMError(
                    f"LLM 接口返回 HTTP {response.status_code}",
                    response.status_code,
                    response.text,
                )

            try:
                data = response.json()
                choice = data["choices"][0]
                content = choice["message"].get("content") or ""
                finish_reason = choice.get("finish_reason")
            except (ValueError, KeyError, IndexError, AttributeError) as exc:
                last_error = LLMError(f"响应结构异常：{exc}", response.status_code, response.text)
                if attempt < self.config.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                continue

            # 推理型模型可能把 max_tokens 全用在 reasoning 上，content 为空。
            # 这种情况自动扩容重试，而不是当成“没有问题”。
            if not content.strip():
                if finish_reason == "length":
                    last_error = LLMError("输出被 max_tokens 截断（推理占用过多）", response.status_code)
                    current = int(payload.get("max_tokens") or self.config.max_tokens)
                    payload["max_tokens"] = min(current * 2, 8000)
                else:
                    last_error = LLMError("模型返回空内容", response.status_code, response.text)
                if attempt < self.config.max_retries:
                    continue

            usage = data.get("usage") or {}
            return LLMResponse(
                content=content or "",
                usage={
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens"
                    ),
                    "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                },
                latency_ms=latency,
                attempts=attempts,
            )

        raise LLMError(f"LLM 调用失败：{last_error}")


def parse_json_content(content: str) -> dict[str, Any]:
    """从模型输出中提取 JSON；只做去代码围栏，不做内容修补。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
