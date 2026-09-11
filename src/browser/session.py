"""OS file locking prevents two bot processes from sharing a profile."""

import json
import os
import re
from math import isfinite
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

from src.core.exceptions import ConfigurationError, HumanRequired

APPLE_STATE_FILE_ENV = "APPLE_BUY_BOT_STORAGE_STATE_FILE"
APPLE_STATE_MARKER = ".apple-buy-bot-storage-state-imported"
APPLE_STATE_HOSTS = {
    "apple.com",
    "www.apple.com",
    "apple.com.cn",
    "www.apple.com.cn",
    "idmsa.apple.com",
    "appleid.apple.com",
    "account.apple.com",
}


def apple_state_host(host: str) -> bool:
    return (
        host in APPLE_STATE_HOSTS
        or re.fullmatch(r"secure\d*\.www\.apple\.com\.cn", host) is not None
    )


def _state_error(message: str) -> ConfigurationError:
    # Callers must never include JSON values, source paths or parser errors.
    return ConfigurationError("本机 Apple 登录状态文件无效：" + message)


def _state_origin(value: str, *, cookie_url: bool = False) -> str:
    if not isinstance(value, str):
        raise _state_error("来源 URL 必须是字符串") from None
    try:
        parts = urlsplit(value)
        allowed = (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and parts.port in {None, 443}
            and apple_state_host(parts.hostname or "")
            and not parts.query
            and not parts.fragment
            and (cookie_url or parts.path in {"", "/"})
        )
    except (TypeError, ValueError):
        allowed = False
    if not allowed:
        raise _state_error("只允许已知 Apple 登录与商城的 HTTPS 来源") from None
    return "https://" + parts.hostname


def load_local_apple_state(path: Path) -> dict:
    """Validate supported standard storage_state before changing a browser profile."""
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise _state_error("文件超过 5 MiB 限制")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        raise _state_error("文件不存在或无法读取，请检查本机环境变量") from None
    except (ValueError, UnicodeError):
        raise _state_error("需要 UTF-8 JSON 格式") from None
    if isinstance(data, list):
        data = {"cookies": data, "origins": []}
    if not isinstance(data, dict) or set(data) - {"cookies", "origins"}:
        raise _state_error("只支持 cookies/origins，不支持 IndexedDB 或通行密钥")
    cookies, origins = data.get("cookies", []), data.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise _state_error("cookies 和 origins 必须是数组")
    if len(cookies) > 1000 or len(origins) > 100:
        raise _state_error("Cookie 或来源数量超过限制")
    checked_cookies, identities = [], set()
    for cookie in cookies:
        allowed = {
            "name",
            "value",
            "url",
            "domain",
            "path",
            "expires",
            "httpOnly",
            "secure",
            "sameSite",
        }
        if not isinstance(cookie, dict) or set(cookie) - allowed:
            raise _state_error("Cookie 字段不受支持，需标准 Playwright Cookie 格式")
        if (
            not isinstance(cookie.get("name"), str)
            or not cookie["name"]
            or re.search(r"[\s;=]", cookie["name"])
            or not isinstance(cookie.get("value"), str)
            or any(char in cookie["value"] for char in "\r\n\0")
        ):
            raise _state_error("Cookie 名称或值格式不正确")
        value = cookie.copy()
        if "url" in value:
            if "domain" in value or "path" in value:
                raise _state_error("Cookie URL 与 domain/path 不能同时设置")
            source_url = value["url"]
            origin = _state_origin(source_url, cookie_url=True)
            # Normalize supported URL cookies to explicit host/path identities.
            value.pop("url")
            cookie_path = urlsplit(source_url).path
            value["domain"] = urlsplit(origin).hostname
            value["path"] = cookie_path[: cookie_path.rfind("/") + 1] or "/"
            value.setdefault("secure", True)
        else:
            domain, path_value = value.get("domain"), value.get("path")
            if (
                not isinstance(domain, str)
                or not apple_state_host(domain.removeprefix("."))
                or not isinstance(path_value, str)
                or not path_value.startswith("/")
                or any(char in path_value for char in "\r\n\0")
            ):
                raise _state_error("Cookie 域或路径不属于允许的 Apple 范围")
        for flag in ("httpOnly", "secure"):
            if flag in value and not isinstance(value[flag], bool):
                raise _state_error("Cookie 布尔字段格式不正确")
        if "expires" in value and (
            isinstance(value["expires"], bool)
            or not isinstance(value["expires"], (int, float))
            or value["expires"] < -1
            or value["expires"] > 253402300799
            or not isfinite(value["expires"])
        ):
            raise _state_error("Cookie 有效期格式不正确")
        if "sameSite" in value and (
            not isinstance(value["sameSite"], str)
            or value["sameSite"] not in {"Strict", "Lax", "None"}
        ):
            raise _state_error("Cookie sameSite 格式不正确")
        identity = (value["name"], value["domain"], value["path"])
        if identity in identities:
            raise _state_error("Cookie 身份重复")
        identities.add(identity)
        checked_cookies.append(value)
    checked_origins, seen_origins = [], set()
    for origin in origins:
        if not isinstance(origin, dict):
            raise _state_error("来源记录格式不正确")
        if "indexedDB" in origin:
            raise _state_error("不支持 IndexedDB；请重新导出不含 IndexedDB 的登录状态")
        if set(origin) - {"origin", "localStorage"} or not isinstance(origin.get("origin"), str):
            raise _state_error("来源只支持 origin 与 localStorage")
        name = _state_origin(origin["origin"])
        if name in seen_origins:
            raise _state_error("来源重复")
        seen_origins.add(name)
        values = origin.get("localStorage", [])
        if not isinstance(values, list):
            raise _state_error("localStorage 必须是数组")
        keys = set()
        for item in values:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "value"}
                or not isinstance(item["name"], str)
                or not isinstance(item["value"], str)
                or item["name"] in keys
            ):
                raise _state_error("localStorage 项格式不正确或名称重复")
            keys.add(item["name"])
        checked_origins.append({"origin": name, "localStorage": values})
    return {"cookies": checked_cookies, "origins": checked_origins}


class ProfileLock:
    def __init__(self, directory: Path):
        self.path = directory / ".apple-buy-bot.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise HumanRequired(
                "Browser profile is already in use; close its other process"
            ) from None
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            # Closing releases the OS lock even after abnormal application exit.
            self._file.close()
            self._file = None
