"""按实际表示隔离的采集请求缓存层。

范围边界：本模块只处理单个采集请求的缓存、条件重验证与容量回收，
不做全站发现、不遍历 robots.txt 或站点地图。

隔离模型
--------
每个规范化 URL 对应一个缓存条目，条目内按“选择请求”的签名保存多份
互不相同的表示（representation）。选择请求由响应 ``Vary`` 列举的请求头
决定（如 ``X-Tenant``、``Accept-Language``、``Accept-Encoding``）。
因此不同租户、不同语言、不同压缩协商各有独立正文与独立校验器，
ETag / Last-Modified 绝不会跨表示发送条件请求。

凭据处理
--------
``Authorization``、``Proxy-Authorization``、``Cookie`` 等凭据头不会以明文
进入缓存键或元数据，而是替换为带缓存实例专属盐的 SHA-256 摘要；
请求出站时仍使用调用方提供的原始值。

复用策略
--------
* ``Vary: *`` 与 ``Cache-Control: no-store`` 的响应只透传、不落盘，
  并清掉同签名的旧表示。
* ``max-age`` 之内为新鲜命中；``no-cache`` 每次都必须重验证。
* 过期表示携带 ETag / Last-Modified 时发条件请求；304 按 RFC 9111
  合并响应头并继续引用原正文；原正文缺失时退化为无条件完整拉取，
  绝不凭空构造空正文。
* 来源失败（网络错误/5xx）时，``stale-if-error`` 窗口内可服务旧值，
  窗口耗尽或 ``must-revalidate`` / ``no-cache`` 则拒绝复用并抛出错误。

并发与容量
----------
同一表示的并发刷新通过门闩合并为一次来源请求；互不等价的表示各走各的
门闩，互不等待。刷新在响应完整到达前不改动任何既有元数据，因此刷新中断
后旧值的可读性只由来源策略（指令与时钟）决定，与线程调度无关。
正文存放在独立的带 LRU 容量的正文库中，回收正文时同步清除引用它的
全部元数据，保证不存在指向已删除正文的悬空元数据。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from service import canonical_url

# 追踪参数：出缓存键但不影响业务语义。
DEFAULT_TRACKING_PARAMS = ('utm_source', 'utm_campaign')

# 携带明文凭据的请求头：进入缓存键前必须换成盐化摘要。
SENSITIVE_HEADERS = frozenset((
    'authorization',
    'proxy-authorization',
    'cookie',
))

# 逐跳头不参与表示选择。
_HOP_BY_HOP = frozenset((
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'trailers', 'transfer-encoding', 'upgrade', 'host',
    'content-length',
))
# 条件头由缓存依据具体表示自行管理，不允许调用方指定后进入选择签名。
_CONDITIONAL_HEADERS = frozenset((
    'if-match', 'if-none-match', 'if-modified-since',
    'if-unmodified-since', 'if-range',
))

# 304 合并时不得采用的响应头（正文未变，长度声明必须保留旧值）。
_MERGE_EXCLUDED = frozenset(('content-length',))


class FetchError(Exception):
    """来源请求失败且没有可复用的旧表示。"""


@dataclass(frozen=True)
class CacheEvent:
    """一次缓存决策的可观测记录。"""
    kind: str
    reason: str
    url: str
    detail: dict = field(default_factory=dict)
    at: float = 0.0


@dataclass
class CachedResponse:
    """返回给采集任务的响应；正文与头均来自某个确定的表示。"""
    status: int
    headers: dict
    body: bytes
    canonical_url: str
    from_cache: bool
    reason: str
    events: list
    _rep: object = None  # 内部字段：对应的 _Representation（透传响应为 None）

    def read(self) -> bytes:
        return self.body

    def header(self, name: str):
        return self.headers.get(name.lower())


@dataclass
class _Representation:
    """一个具体表示的全部元数据；正文在 _BodyStore 中按 body_id 存放。"""
    selecting: dict                     # Vary 头名 -> 快照值（敏感值为摘要）
    vary_names: tuple
    status: int
    headers: dict                       # 小写响应头，304 后为合并结果
    body_id: int
    stored_at: float
    lifetime: int                       # 新鲜寿命（秒）
    no_cache: bool
    directives: dict

    def is_fresh(self, now: float) -> bool:
        if self.no_cache:
            return False
        return now < self.stored_at + self.lifetime


@dataclass
class _Entry:
    representations: list = field(default_factory=list)


class _BodyStore:
    """正文库：独立于元数据，带字节容量与 LRU 回收。

    所有方法都要求在 RepresentationCache 的锁内调用。
    回收正文时通过 on_evict 回调同步清理引用方元数据。
    """

    def __init__(self, capacity_bytes: int, on_evict):
        self._capacity = capacity_bytes
        self._data = OrderedDict()      # body_id -> bytes
        self._size = 0
        self._seq = 0
        self._on_evict = on_evict

    def put(self, body: bytes) -> int:
        self._seq += 1
        body_id = self._seq
        self._data[body_id] = body
        self._data.move_to_end(body_id)
        self._size += len(body)
        # 容量超限时回收最久未用的其他正文；正在写入的这份不回收自身。
        while self._size > self._capacity and len(self._data) > 1:
            old_id, old_body = self._data.popitem(last=False)
            self._size -= len(old_body)
            self._on_evict(old_id)
        return body_id

    def get(self, body_id: int):
        return self._data.get(body_id)

    def touch(self, body_id: int) -> None:
        if body_id in self._data:
            self._data.move_to_end(body_id)

    def free(self, body_id: int) -> None:
        old = self._data.pop(body_id, None)
        if old is not None:
            self._size -= len(old)

    def delete_for_test(self, body_id: int) -> bool:
        """模拟磁盘/外部正文丢失：只删正文、不动元数据。"""
        old = self._data.pop(body_id, None)
        if old is None:
            return False
        self._size -= len(old)
        return True

    def total_size(self) -> int:
        return self._size

    def __len__(self):
        return len(self._data)


class _Gate:
    """同一表示的并发刷新门闩：领头者请求一次，其余线程等待并共享结果。"""

    def __init__(self):
        self.event = threading.Event()
        # ('rep', _Representation) | ('passthrough',) | ('error', exc)
        self.outcome = None


def _parse_cache_control(headers: dict) -> dict:
    directives = {}
    raw = headers.get('cache-control')
    if not raw:
        return directives
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        if '=' in part:
            key, value = part.split('=', 1)
            directives[key.strip().lower()] = value.strip()
        else:
            directives[part.lower()] = True
    return directives


def _int_directive(directives: dict, name: str):
    value = directives.get(name)
    if value is None or value is True:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _freshness_lifetime(headers: dict, directives: dict) -> int:
    """max-age 优先于 Expires；都没有时保守取 0（立即过期、需重验证）。"""
    max_age = _int_directive(directives, 'max-age')
    if max_age is not None:
        return max_age
    expires = headers.get('expires')
    if expires:
        try:
            expiry = parsedate_to_datetime(expires)
            if expiry is not None:
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                now_wall = datetime.now(timezone.utc)
                return max(0, int((expiry - now_wall).total_seconds()))
        except (TypeError, ValueError):
            pass
    return 0


def _parse_vary(headers: dict):
    raw = headers.get('vary')
    if not raw:
        return tuple(), False
    names = tuple(part.strip().lower() for part in raw.split(',') if part.strip())
    return names, '*' in names


def _urllib_transport(url: str, headers: dict, timeout: float):
    request = Request(url, headers=headers)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        # urllib 把 304/4xx/5xx 都抛成 HTTPError；对缓存而言它们是普通响应。
        body = exc.read()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body
    except URLError as exc:
        raise FetchError('origin request failed: %s' % exc.reason) from exc
    with response:
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.headers.items()}, body


class RepresentationCache:
    """采集请求入口：``fetch()`` 对外提供带表示隔离缓存的 GET。"""

    def __init__(self, capacity_bytes: int = 10 * 1024 * 1024,
                 clock=time.monotonic, transport=None, timeout: float = 5.0,
                 tracking_params=DEFAULT_TRACKING_PARAMS, max_events: int = 500):
        self._lock = threading.Lock()
        self._clock = clock
        self._timeout = timeout
        self._tracking = tuple(tracking_params)
        self._transport = transport or (
            lambda url, headers, timeout: _urllib_transport(url, headers, timeout))
        # 盐只活在缓存实例内，防止摘要被反查/跨实例撞库；盐本身不持久化。
        self._salt = secrets.token_bytes(32)
        self._entries = {}               # canonical url -> _Entry
        self._bodies = _BodyStore(capacity_bytes, self._evict_body)
        self._inflight = {}              # (url, 完整快照签名) -> _Gate
        self.events = deque(maxlen=max_events)

    # ------------------------------------------------------------------ 公开 API

    def fetch(self, url: str, headers: dict | None = None) -> CachedResponse:
        canonical = canonical_url(url, self._tracking)
        raw = self._normalize_headers(headers or {})
        snapshot = self._snapshot(raw)
        signature = tuple(sorted(snapshot.items()))
        local_events = []

        with self._lock:
            now = self._clock()
            entry, rep = self._match_locked(canonical, snapshot, local_events)
            if rep is not None and rep.is_fresh(now) and self._has_body(rep):
                self._bodies.touch(rep.body_id)
                self._record(local_events, 'HIT', 'fresh', canonical,
                             {'vary': list(rep.vary_names)})
                return self._response(rep, canonical, True, 'fresh',
                                      local_events)

            gate = self._inflight.get((canonical, signature))
            if gate is None:
                gate = _Gate()
                self._inflight[(canonical, signature)] = gate
                leader = True
            else:
                leader = False

        if not leader:
            return self._join_gate(gate, canonical, raw, snapshot, local_events)

        try:
            result = self._refresh(canonical, raw, snapshot, rep, local_events)
            gate.outcome = (('rep', result._rep) if result._rep is not None
                            else ('passthrough',))
            return result
        except BaseException as exc:
            gate.outcome = ('error', exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop((canonical, signature), None)
            gate.event.set()

    # ------------------------------------------------------------- 请求侧处理

    @staticmethod
    def _normalize_headers(headers: dict) -> dict:
        return {str(k).lower(): str(v) for k, v in headers.items()}

    def _snapshot(self, raw: dict) -> dict:
        """构造选择请求快照：剔除逐跳/条件头，敏感值替换为盐化摘要。"""
        result = {}
        for name, value in raw.items():
            if name in _HOP_BY_HOP or name in _CONDITIONAL_HEADERS:
                continue
            if name in SENSITIVE_HEADERS:
                result[name] = 'sha256:' + hmac.new(
                    self._salt, value.encode('utf-8'), hashlib.sha256
                ).hexdigest()
            else:
                result[name] = value.strip()
        return result

    def _match_locked(self, canonical: str, snapshot: dict, local_events: list):
        entry = self._entries.get(canonical)
        if entry is None:
            self._record(local_events, 'MISS', 'no-stored-entry', canonical)
            return None, None
        for rep in entry.representations:
            if all(snapshot.get(name) == rep.selecting.get(name)
                   for name in rep.vary_names):
                if not self._has_body(rep):
                    # 元数据还在但正文丢失：仍然返回该表示，使刷新能够携带
                    # 它自己的校验器发条件请求；若来源回 304 再退化为完整拉取，
                    # 任何路径都不会凭空返回空正文。
                    self._record(local_events, 'MISS', 'body-missing', canonical,
                                 {'vary': list(rep.vary_names)})
                return entry, rep
        detail = self._isolation_detail(entry, snapshot)
        self._record(local_events, 'ISOLATED', 'vary-mismatch', canonical, detail)
        self._record(local_events, 'MISS', 'no-matching-representation',
                     canonical)
        return entry, None

    def _isolation_detail(self, entry: _Entry, snapshot: dict) -> dict:
        """说明拒绝复用的维度；敏感值只呈现摘要前缀，不输出任何明文。"""
        names = sorted({n for rep in entry.representations
                        for n in rep.vary_names})
        dimensions = []
        for name in names:
            presented = snapshot.get(name)
            stored = sorted({rep.selecting.get(name) for rep in
                             entry.representations if name in rep.selecting})
            if name in SENSITIVE_HEADERS:
                presented = self._redact(presented)
                stored = [self._redact(v) for v in stored]
            dimensions.append({'header': name, 'presented': presented,
                               'stored_values': stored})
        return {'dimensions': dimensions}

    @staticmethod
    def _redact(value):
        if value is None:
            return None
        return value[:12] + '…' if isinstance(value, str) else 'digest'

    # ------------------------------------------------------------------ 刷新

    def _refresh(self, canonical: str, raw: dict, snapshot: dict,
                 stale_rep, local_events: list) -> CachedResponse:
        request_headers = {k: v for k, v in raw.items()
                           if k not in _HOP_BY_HOP and k not in _CONDITIONAL_HEADERS}
        has_body = stale_rep is not None and self._has_body(stale_rep)
        if stale_rep is not None:
            # 即便正文已丢失也重验证：带上该表示自己的校验器，来源未变会回
            # 304，届时再退化为完整拉取；校验器绝不会跨表示借用。
            reason = 'no-cache' if stale_rep.no_cache else (
                'stale' if has_body else 'body-missing')
            self._record(local_events, 'REVALIDATE', reason, canonical,
                         {'vary': list(stale_rep.vary_names)})
            etag = stale_rep.headers.get('etag')
            last_modified = stale_rep.headers.get('last-modified')
            if etag:
                request_headers['if-none-match'] = etag
            if last_modified:
                request_headers['if-modified-since'] = last_modified
        usable_stale = has_body

        try:
            status, resp_headers, body = self._transport(
                canonical, request_headers, self._timeout)
        except Exception as exc:
            return self._handle_origin_failure(canonical, stale_rep, exc,
                                               local_events, network_error=True)

        if status == 304:
            with self._lock:
                body_lost = not (stale_rep is not None and self._has_body(stale_rep))
            if body_lost:
                # 304 没有正文而本地也没有正文可合并：不能凭空返回空数据，
                # 退化为不带条件头的完整拉取。
                self._record(local_events, 'REVALIDATED',
                             '304-without-body-full-fetch', canonical)
                fallback_headers = {k: v for k, v in request_headers.items()
                                    if k not in _CONDITIONAL_HEADERS}
                status, resp_headers, body = self._transport(
                    canonical, fallback_headers, self._timeout)
            else:
                return self._merge_not_modified(
                    canonical, stale_rep, resp_headers, local_events)

        if 500 <= status < 600:
            return self._handle_origin_failure(
                canonical, stale_rep,
                FetchError('origin responded %s' % status), local_events,
                network_error=False, upstream_status=status)

        directives = _parse_cache_control(resp_headers)
        vary_names, vary_star = _parse_vary(resp_headers)
        if directives.get('no-store') is True or vary_star:
            # 不落盘，并淘汰同签名旧表示，避免下次又把旧私有数据读出来。
            reason = ('no-store' if directives.get('no-store') is True
                      else 'vary-star')
            with self._lock:
                self._discard_matching_locked(canonical, snapshot, vary_names)
            self._record(local_events, 'STORE_REJECTED', reason, canonical)
            return CachedResponse(status, dict(resp_headers), body, canonical,
                                  False, 'not-cacheable:' + reason,
                                  local_events)

        if status >= 400:
            # 确定性的 4xx 直接透传，不覆盖既有缓存。
            self._record(local_events, 'MISS', 'upstream-%s' % status,
                         canonical)
            return CachedResponse(status, dict(resp_headers), body, canonical,
                                  False, 'upstream-%s' % status, local_events)

        return self._store_representation(
            canonical, snapshot, status, resp_headers, body, directives,
            vary_names, local_events)

    def _merge_not_modified(self, canonical: str, rep: _Representation,
                            resp_headers: dict, local_events: list):
        """304：按 RFC 9111 4.3.4 合并头，正文继续引用原 body_id。"""
        now = self._clock()
        with self._lock:
            merged = dict(rep.headers)
            for name, value in resp_headers.items():
                if name in _MERGE_EXCLUDED:
                    continue
                merged[name] = value
            directives = _parse_cache_control(merged)
            vary_names, vary_star = _parse_vary(merged)
            if vary_star:
                # 重验证后来源改判 Vary:*：旧表示不再允许复用。
                self._remove_rep_locked(canonical, rep)
                self._record(local_events, 'STORE_REJECTED', 'vary-star',
                             canonical)
                raise FetchError(
                    'origin switched to Vary: * on revalidation')
            # 新 Vary 若引入旧选择快照没有的头，按“请求未携带”语义取 None。
            new_selecting = {name: rep.selecting.get(name)
                             for name in vary_names}
            entry = self._entries.setdefault(canonical, _Entry())
            # Vary 集合发生变化：按旧 Vary 键控的兄弟表示无法再按新的协商
            # 语义匹配，来源策略已变，保守淘汰，避免错误命中旧协商结果。
            if set(rep.vary_names) != set(vary_names):
                for other in [r for r in entry.representations if r is not rep]:
                    self._record(local_events, 'EVICTED', 'vary-changed',
                                 canonical, {'vary': list(other.vary_names)})
                    self._remove_rep_locked(canonical, other)
            # 替换同选择签名的旧表示；它们的正文才需要释放，当前 rep 的
            # body_id 必须保留——304 没有新正文，只能继续引用原正文。
            for other in [r for r in entry.representations
                          if r.selecting == new_selecting and r is not rep]:
                self._remove_rep_locked(canonical, other)
            entry.representations = [r for r in entry.representations
                                     if r is not rep]
            rep.selecting = new_selecting
            rep.vary_names = vary_names
            rep.headers = merged
            rep.directives = directives
            rep.no_cache = directives.get('no-cache') is True
            rep.lifetime = _freshness_lifetime(merged, directives)
            rep.stored_at = now
            entry.representations.append(rep)
            self._bodies.touch(rep.body_id)
            self._record(local_events, 'REVALIDATED', '304-merged', canonical,
                         {'vary': list(vary_names)})
            return self._response(rep, canonical, True, 'revalidated',
                                  local_events)

    def _store_representation(self, canonical, snapshot, status, headers, body,
                              directives, vary_names, local_events):
        now = self._clock()
        selecting = {name: snapshot.get(name) for name in vary_names}
        with self._lock:
            # 先落正文（可能触发容量回收并清理别处元数据），再换元数据。
            body_id = self._bodies.put(body)
            entry = self._entries.setdefault(canonical, _Entry())
            for old in [r for r in entry.representations
                        if r.selecting == selecting]:
                self._remove_rep_locked(canonical, old)
            rep = _Representation(
                selecting=selecting,
                vary_names=vary_names,
                status=status,
                headers=dict(headers),
                body_id=body_id,
                stored_at=now,
                lifetime=_freshness_lifetime(headers, directives),
                no_cache=directives.get('no-cache') is True,
                directives=dict(directives),
            )
            entry.representations.append(rep)
        self._record(local_events, 'STORED', 'origin-%s' % status, canonical,
                     {'vary': list(vary_names), 'body_bytes': len(body)})
        return self._response(rep, canonical, False, 'stored', local_events)

    # ------------------------------------------------------- 来源失败的旧值决策

    def _handle_origin_failure(self, canonical, stale_rep, exc, local_events,
                               network_error, upstream_status=None):
        now = self._clock()
        with self._lock:
            has_old = stale_rep is not None and self._has_body(stale_rep)
            if not has_old:
                self._record(local_events, 'FETCH_ERROR',
                             'network-error' if network_error else 'origin-error',
                             canonical, {'upstream_status': upstream_status})
            else:
                allowed, reason = self._stale_policy(stale_rep, now)
                if not allowed:
                    self._record(local_events, 'FETCH_ERROR', reason, canonical,
                                 {'upstream_status': upstream_status})
                else:
                    self._record(local_events, 'STALE_SERVED', reason,
                                 canonical,
                                 {'upstream_status': upstream_status,
                                  'stale_seconds': max(0, int(
                                      now - stale_rep.stored_at
                                      - stale_rep.lifetime))})
                    return self._response(stale_rep, canonical, True,
                                          'stale:' + reason, local_events)
        if isinstance(exc, FetchError):
            raise exc
        raise FetchError(str(exc)) from exc

    def _stale_policy(self, rep: _Representation, now: float):
        """返回 (可否服务旧值, 理由)。明确区分窗口、强制重验证与普通旧值。"""
        directives = rep.directives
        if rep.no_cache or directives.get('no-cache') is True:
            return False, 'no-cache-requires-revalidation'
        if (directives.get('must-revalidate') is True
                or directives.get('proxy-revalidate') is True):
            return False, 'must-revalidate-forbids-stale'
        stale_age = now - (rep.stored_at + rep.lifetime)
        window = _int_directive(directives, 'stale-if-error')
        if window is not None:
            if stale_age <= window:
                return True, 'stale-if-error'
            return False, 'stale-if-error-window-exhausted'
        return True, 'stale-on-origin-failure'

    # ------------------------------------------------------------- 并发合并

    def _join_gate(self, gate: _Gate, canonical, raw, snapshot, local_events):
        gate.event.wait()
        outcome = gate.outcome
        if outcome is None or outcome[0] != 'rep':
            # 领头者失败或拿到的是不落盘的透传响应：跟随者独立走完整决策，
            # 由来源策略（旧值/报错/各自透传）决定结果，不共享可疑状态。
            self._record(local_events, 'COALESCED',
                         'leader-failed-independent-retry', canonical)
            return self._fetch_once(canonical, raw, snapshot, local_events)
        rep = outcome[1]
        with self._lock:
            if self._has_body(rep):
                self._bodies.touch(rep.body_id)
                self._record(local_events, 'COALESCED',
                             'joined-in-flight-refresh', canonical,
                             {'vary': list(rep.vary_names)})
                return self._response(rep, canonical, True, 'coalesced',
                                      local_events)
        # 极端情况：正文在唤醒间隙被容量回收，重新走完整流程。
        self._record(local_events, 'MISS', 'body-evicted-during-wait',
                     canonical)
        return self._fetch_once(canonical, raw, snapshot, local_events)

    def _fetch_once(self, canonical, raw, snapshot, local_events):
        """门闩收尾后的独立决策入口（不再注册门闩，避免无限递归）。"""
        with self._lock:
            now = self._clock()
            _, rep = self._match_locked(canonical, snapshot, local_events)
            if rep is not None and rep.is_fresh(now) and self._has_body(rep):
                self._bodies.touch(rep.body_id)
                self._record(local_events, 'HIT', 'fresh', canonical)
                return self._response(rep, canonical, True, 'fresh',
                                      local_events)
        stale = rep if rep is not None else None
        return self._refresh(canonical, raw, snapshot, stale, local_events)

    # ------------------------------------------------------------- 锁内基础操作

    def _has_body(self, rep: _Representation) -> bool:
        return self._bodies.get(rep.body_id) is not None

    def _response(self, rep: _Representation, canonical: str, from_cache: bool,
                  reason: str, local_events: list) -> CachedResponse:
        return CachedResponse(
            status=rep.status,
            headers=dict(rep.headers),
            body=self._bodies.get(rep.body_id) or b'',
            canonical_url=canonical,
            from_cache=from_cache,
            reason=reason,
            events=list(local_events),
            _rep=rep,
        )

    def _remove_rep_locked(self, canonical: str, rep: _Representation) -> None:
        entry = self._entries.get(canonical)
        if entry is None:
            return
        entry.representations = [r for r in entry.representations
                                 if r is not rep]
        self._bodies.free(rep.body_id)
        if not entry.representations:
            self._entries.pop(canonical, None)

    def _discard_matching_locked(self, canonical: str, snapshot: dict,
                                 vary_names: tuple) -> None:
        entry = self._entries.get(canonical)
        if entry is None:
            return
        selecting = {name: snapshot.get(name) for name in vary_names}
        for rep in [r for r in entry.representations if r.selecting == selecting]:
            self._remove_rep_locked(canonical, rep)

    def _evict_body(self, body_id: int) -> None:
        """正文库容量回收回调：同步删除所有引用该正文的元数据。"""
        for canonical in list(self._entries.keys()):
            entry = self._entries[canonical]
            victims = [r for r in entry.representations if r.body_id == body_id]
            for rep in victims:
                entry.representations.remove(rep)
                self._record([], 'EVICTED', 'capacity', canonical,
                             {'vary': list(rep.vary_names)})
            if not entry.representations:
                self._entries.pop(canonical, None)

    def _record(self, local_events: list, kind: str, reason: str,
                url: str, detail: dict | None = None) -> CacheEvent:
        event = CacheEvent(kind=kind, reason=reason, url=url,
                           detail=detail or {}, at=self._clock())
        self.events.append(event)
        local_events.append(event)
        return event

    # ----------------------------------------------------- 测试/自检辅助接口

    def debug_entry(self, url: str):
        """返回某 URL 下全部表示的元数据摘要（敏感值仅摘要）。"""
        canonical = canonical_url(url, self._tracking)
        with self._lock:
            entry = self._entries.get(canonical)
            if entry is None:
                return None
            now = self._clock()
            return {
                'url': canonical,
                'representations': [
                    {
                        'vary': list(r.vary_names),
                        'selecting': dict(r.selecting),
                        'status': r.status,
                        'stored_at': r.stored_at,
                        'lifetime': r.lifetime,
                        'no_cache': r.no_cache,
                        'fresh': r.is_fresh(now),
                        'has_body': self._has_body(r),
                        'body_id': r.body_id,
                    }
                    for r in entry.representations
                ],
            }

    def debug_drop_body(self, url: str, headers: dict | None = None) -> bool:
        """测试辅助：模拟该表示的正文在外存丢失（元数据保留）。"""
        canonical = canonical_url(url, self._tracking)
        snapshot = self._snapshot(self._normalize_headers(headers or {}))
        with self._lock:
            entry = self._entries.get(canonical)
            if entry is None:
                return False
            for rep in entry.representations:
                if all(snapshot.get(name) == rep.selecting.get(name)
                       for name in rep.vary_names):
                    return self._bodies.delete_for_test(rep.body_id)
        return False

    def debug_assert_no_dangling_metadata(self) -> None:
        """自检：不存在指向已删除正文的悬空元数据。"""
        with self._lock:
            for canonical, entry in self._entries.items():
                for rep in entry.representations:
                    if self._bodies.get(rep.body_id) is None:
                        raise AssertionError(
                            'dangling metadata at %s body_id=%s'
                            % (canonical, rep.body_id))

    def debug_body_count(self) -> int:
        with self._lock:
            return len(self._bodies)
