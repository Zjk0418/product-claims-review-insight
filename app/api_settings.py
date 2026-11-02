"""Application API settings: memory by default, Windows DPAPI only when remembered.

The public representation never contains the full key. No project .env is read here.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULTS = {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash", "timeout": 90,
            "max_tokens": 6000, "allow_insecure_http": False}


class ApiSettingsError(ValueError):
    pass


def user_config_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "ReviewInsight"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "review-insight"


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise ApiSettingsError("当前系统不支持安全记住密钥，请使用仅本次会话。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(b"ReviewInsight.api-key.v1")
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(entropy_buffer) - 1, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(target))
    else:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), "ReviewInsight API", ctypes.byref(entropy), None, None, 1, ctypes.byref(target))
    if not ok:
        raise ApiSettingsError("Windows 安全存储不可用，请使用仅本次会话。")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _validated(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiSettingsError("配置必须是 JSON 对象。")
    result = dict(current)
    for key in DEFAULTS:
        if key in payload:
            result[key] = payload[key]
    url = str(result["base_url"]).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ApiSettingsError("接口地址须为不含账号、密码、查询参数的 HTTP(S) 地址。")
    allow_insecure_http = bool(result.get("allow_insecure_http"))
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and not allow_insecure_http:
        raise ApiSettingsError(
            "远程接口须使用 HTTPS；如果服务只支持 HTTP，请勾选“允许 http 明文连接（不安全）”。"
        )
    model = str(result["model"]).strip()
    if not model or len(model) > 128 or any(ord(c) < 32 for c in model):
        raise ApiSettingsError("请输入合法的模型名称（最长 128 字符）。")
    try:
        timeout, max_tokens = int(result["timeout"]), int(result["max_tokens"])
    except (TypeError, ValueError):
        raise ApiSettingsError("超时与最大输出长度须为整数。") from None
    if not 5 <= timeout <= 300 or not 128 <= max_tokens <= 32768:
        raise ApiSettingsError("超时范围为 5–300 秒，最大输出长度范围为 128–32768。")
    if "api_key" in payload and payload["api_key"]:
        key = payload["api_key"]
        if not isinstance(key, str) or not key.isascii() or len(key) > 4096 or any(ord(c) < 32 for c in key) or "*" in key:
            raise ApiSettingsError("请输入完整的 API Key，不能使用掩码或包含换行的值。")
        result["api_key"] = key.strip()
    if payload.get("clear_key"):
        result["api_key"] = ""
    result.update(base_url=url, model=model, timeout=timeout, max_tokens=max_tokens,
                  allow_insecure_http=allow_insecure_http)
    return result


class ApiSettingsStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or user_config_directory()
        self.path = self.directory / "api-settings.json"
        self._lock = threading.RLock()
        self._values: dict[str, Any] = {**DEFAULTS, "api_key": ""}
        self._remember = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("key_storage") != "windows-dpapi-v1":
                return
            key = _dpapi(base64.b64decode(saved["protected_key"], validate=True), decrypt=True).decode("utf-8")
            self._values = _validated({**{k: saved.get(k, DEFAULTS[k]) for k in DEFAULTS}, "api_key": key}, self._values)
            self._remember = True
        except (OSError, ValueError, KeyError, UnicodeError, ApiSettingsError):
            # A moved profile or another Windows account cannot decrypt this key.
            self._values = {**DEFAULTS, "api_key": ""}
            self._remember = False

    def public(self) -> dict[str, Any]:
        with self._lock:
            key = self._values["api_key"]
            return {**{k: self._values[k] for k in DEFAULTS}, "configured": bool(key),
                    "key_mask": ("••••" + key[-4:] if len(key) >= 8 else "••••••••") if key else "",
                    "remember": self._remember, "persistence_available": os.name == "nt"}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            values = _validated(payload, self._values)
            remember = bool(payload.get("remember", self._remember))
            if remember and values["api_key"]:
                protected = base64.b64encode(_dpapi(values["api_key"].encode("utf-8"))).decode("ascii")
                saved = {**{k: values[k] for k in DEFAULTS}, "key_storage": "windows-dpapi-v1", "protected_key": protected}
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            else:
                remember = False
                self.path.unlink(missing_ok=True)
            self._values, self._remember = values, remember
            return self.public()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self.path.unlink(missing_ok=True)
            self._values = {**DEFAULTS, "api_key": ""}
            self._remember = False
            return self.public()

    def llm_kwargs(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._values["api_key"]:
                return None
            return {key: self._values[key] for key in
                    ("base_url", "model", "timeout", "max_tokens", "api_key")}

    def test_connection(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Called only by an explicit connection-test action; never persists input."""
        with self._lock:
            values = _validated(payload or {}, self._values)
        if not values["api_key"]:
            raise ApiSettingsError("请先输入 API Key。")
        try:
            response = requests.post(values["base_url"] + "/chat/completions", headers={
                "Authorization": "Bearer " + values["api_key"], "Content-Type": "application/json"
            }, json={"model": values["model"], "messages": [{"role": "user", "content": "Reply OK."}],
                     "max_tokens": 128, "stream": False}, timeout=min(values["timeout"], 30))
            if response.status_code != 200:
                messages = {401: "API 鉴权失败，请检查密钥。", 403: "接口拒绝访问，请检查权限。",
                            402: "接口账户余额不足。", 404: "接口路径或模型不存在。", 429: "接口限流，请稍后重试。"}
                return {"ok": False, "status": response.status_code,
                        "message": messages.get(response.status_code, "接口未接受请求，请检查配置。")}
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                return {"ok": False, "message": "接口未返回兼容的 chat/completions 响应。"}
            return {"ok": True, "message": "连接成功，鉴权与模型请求已通过。", "model": values["model"]}
        except (requests.RequestException, ValueError):
            # Never echo upstream text or exception URLs: services may echo a key.
            return {"ok": False, "message": "连接失败，请检查接口地址、代理或网络。"}


SETTINGS = ApiSettingsStore()


def test_connection(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return SETTINGS.test_connection(payload)
