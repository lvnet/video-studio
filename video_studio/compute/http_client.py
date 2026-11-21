"""算力层 HTTP 客户端底座。

三条硬约束（对齐米老板的项目红线）：
1. 零版权风险：本模块只发任务协议请求，不下载/打包任何模型或权重。
2. 默认不外连：默认 ``StubHttpClient`` 只做内存模拟，不发真实网络请求，
   因此默认零成本、零副作用。要真连必须在配置里显式开启并注入真实客户端。
3. 可测试：真实协议逻辑（构造请求 / 解析响应 / 错误映射）与传输层解耦，
   测试注入假客户端即可断言协议正确性，不依赖任何外部服务。

对外契约：
- ``HttpClient.request(method, url, ...) -> Response``
- ``Response`` 携带 status 与 data（JSON 解析为 dict/list，二进制为 bytes）
- 传输失败抛 ``TransportError``；HTTP 非 2xx 抛 ``HttpError``（带 status），
  后端据此归一化到统一 ``FailureCode``。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Response:
    status: int
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpError(Exception):
    """HTTP 非 2xx。status 供后端归一化失败码。"""

    def __init__(self, status: int, message: str = "", body: Any = None):
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.body = body


class TransportError(Exception):
    """连接失败 / 超时 / DNS 错误等，通常可重试。"""

    def __init__(self, message: str, cause: BaseException | None = None):
        super().__init__(message)
        self.cause = cause


# 假客户端的响应函数签名：(method, path, params, json_body) -> Response | dict
Responder = Callable[[str, str, dict, dict | None], Any]


class HttpClient:
    """传输层接口。所有后端只依赖本类，便于注入真实实现或假实现。"""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Response:
        raise NotImplementedError

    # 便捷方法
    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Response:
        return self.request("POST", path, **kw)


class UrllibHttpClient(HttpClient):
    """标准库真实 HTTP 客户端。仅在配置显式开启时使用。"""

    def __init__(self, base_url: str = "", default_headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.default_headers = dict(default_headers or {})

    def _url(self, path: str, params: dict | None) -> str:
        url = path if path.startswith("http://") or path.startswith("https://") \
            else self.base_url + "/" + path.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Response:
        url = self._url(path, params)
        hdr = dict(self.default_headers)
        hdr.update(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdr.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdr, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                parsed = _parse_body(raw, content_type)
                return Response(resp.status, parsed, dict(resp.headers.items()))
        except urllib.error.HTTPError as e:
            raw = e.read()
            raise HttpError(e.code, str(e.reason), _parse_body(raw, "")) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TransportError(f"连接失败：{e}", e) from e


def _parse_body(raw: bytes, content_type: str) -> Any:
    if not raw:
        return None
    if "json" in content_type.lower():
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return raw
    # 先尝试当 JSON（很多本地服务不严格设 Content-Type）
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw


class StubHttpClient(HttpClient):
    """默认客户端：不发任何真实请求，交给注入的 responder 在内存里模拟。"""

    def __init__(self, responder: Responder | None = None):
        self.responder = responder
        self.calls: list[dict] = []  # 记录所有调用，便于测试断言

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Response:
        self.calls.append({
            "method": method.upper(), "path": path,
            "params": dict(params or {}), "json_body": json_body,
        })
        if self.responder is None:
            return Response(200, {"ok": True, "stub": True})
        out = self.responder(method.upper(), path, dict(params or {}), json_body)
        return out if isinstance(out, Response) else Response(200, out)


class FakeHttpClient(HttpClient):
    """测试用：按 (method, path) 预置响应序列，可断言调用记录。"""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None):
        self.routes = routes or {}
        self.calls: list[dict] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Response:
        key = (method.upper(), path)
        self.calls.append({"method": key[0], "path": path,
                           "params": dict(params or {}), "json_body": json_body})
        if key not in self.routes:
            raise HttpError(404, f"FakeHttpClient 未注册路由：{key}")
        out = self.routes[key]
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, Response) else Response(200, out)
