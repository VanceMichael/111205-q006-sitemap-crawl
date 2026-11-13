"""RepresentationCache 的端到端与确定性单元测试。

原则：
* 时钟全部通过 FakeClock 显式推进，不使用 sleep 等待业务时序；
* 并发协调只用 threading.Event 门闩，没有轮询/睡眠依赖；
* HTTP 语义（条件请求、Vary、gzip、租户切换）用本机 fixture_server 验证；
* 容量、策略窗口、正文丢失用可脚本化的假传输与可控时钟验证。
"""

import gzip
import threading
import unittest

from fixture_server import site
from representation_cache import FetchError, RepresentationCache


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptedTransport:
    """按调用顺序返回 (status, headers, body) 或抛出给定异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({'url': url, 'headers': dict(headers)})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class GateTransport:
    """首个请求在门闩处阻塞，用于确定性验证并发合并，无需 sleep。"""

    def __init__(self, response):
        self.response = response
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url, headers, timeout):
        with self._lock:
            self.calls.append({'url': url, 'headers': dict(headers)})
        self.entered.set()
        self.release.wait()  # 由测试显式放行，不做轮询/睡眠
        return self.response


def h(response, name):
    return response.headers.get(name.lower())


def sent_header(record, name):
    """从响应器记录里按不区分大小写方式取请求头。"""
    lowered = {k.lower(): v for k, v in record['headers'].items()}
    return lowered.get(name.lower())


class EndToEndCacheTests(unittest.TestCase):
    """针对本机 HTTP 响应器的语义测试。"""

    def setUp(self):
        self._ctx = site(recorder := [])
        self.base = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        self.requests = recorder

    def origin_requests(self, path):
        return [r for r in self.requests if r['path'] == path]

    def test_fresh_hit_within_max_age_uses_clock_not_sleep(self):
        clock = FakeClock()
        cache = RepresentationCache(clock=clock)
        url = self.base + '/localized'
        headers = {'X-Tenant': 'north', 'Accept-Language': 'en'}

        first = cache.fetch(url, headers)
        self.assertEqual(first.body, b'north catalogue')
        self.assertFalse(first.from_cache)
        self.assertEqual(len(self.origin_requests('/localized')), 1)

        clock.advance(4)  # max-age=5 之内
        second = cache.fetch(url, headers)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.body, first.body)
        self.assertEqual(len(self.origin_requests('/localized')), 1)
        kinds = [e.kind for e in second.events]
        self.assertIn('HIT', kinds)

    def test_tenant_and_language_are_isolated_representations(self):
        cache = RepresentationCache()
        url = self.base + '/localized'
        en = cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'en'})
        zh = cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'zh'})
        south = cache.fetch(url, {'X-Tenant': 'south', 'Accept-Language': 'en'})

        self.assertEqual(en.body, b'north catalogue')
        self.assertEqual(zh.body, '北方目录'.encode())
        self.assertEqual(south.body, b'south catalogue')

        entry = cache.debug_entry(url)
        self.assertEqual(len(entry['representations']), 3)
        selectors = {tuple(sorted(r['selecting'].items()))
                     for r in entry['representations']}
        self.assertEqual(len(selectors), 3)

        # 隔离事件必须说明拒绝复用的维度。
        isolated = [e for e in cache.events if e.kind == 'ISOLATED']
        self.assertTrue(isolated)
        reasons = {d['header'] for e in isolated
                   for d in e.detail['dimensions']}
        self.assertIn('accept-language', reasons)

    def test_conditional_validators_never_cross_representations(self):
        cache = RepresentationCache()
        url = self.base + '/localized'
        cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'en'})
        cache.fetch(url, {'X-Tenant': 'south', 'Accept-Language': 'en'})

        # max-age=5 已过由时钟推进模拟；直接用新缓存实例的 0 寿命条目构造。
        clock = FakeClock()
        cache = RepresentationCache(clock=clock)
        cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'en'})
        cache.fetch(url, {'X-Tenant': 'south', 'Accept-Language': 'en'})
        before = len(self.requests)
        clock.advance(6)
        reval = cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'en'})
        conditional = self.requests[before:]
        self.assertEqual(len(conditional), 1)
        self.assertEqual(sent_header(conditional[0], 'If-None-Match'),
                         '"north-en"')
        self.assertEqual(reval.reason, 'revalidated')

        # 南租户的重验证必须带南租户自己的 ETag。
        before = len(self.requests)
        reval_south = cache.fetch(url,
                                  {'X-Tenant': 'south', 'Accept-Language': 'en'})
        south_request = self.requests[before]
        self.assertEqual(sent_header(south_request, 'If-None-Match'),
                         '"south-en"')
        self.assertNotIn('"north-en"', str(south_request['headers']))
        self.assertEqual(reval_south.body, b'south catalogue')

    def test_no_store_and_vary_star_are_never_reused(self):
        cache = RepresentationCache()
        url = self.base + '/no-store'
        response = cache.fetch(url)
        self.assertFalse(response.from_cache)
        self.assertIn('not-cacheable', response.reason)
        self.assertIsNone(cache.debug_entry(url))
        rejected = [e for e in cache.events if e.kind == 'STORE_REJECTED']
        self.assertEqual({e.reason for e in rejected}, {'no-store'})

        # 再次请求仍然回到来源，不读任何旧值。
        cache.fetch(url)
        self.assertEqual(len(self.origin_requests('/no-store')), 2)

    def test_no_cache_forces_revalidation_every_time(self):
        clock = FakeClock()
        cache = RepresentationCache(clock=clock)
        url = self.base + '/revalidate'
        first = cache.fetch(url)
        self.assertEqual(first.body, b'check before reuse')

        clock.advance(100)  # 无论推进多久，no-cache 都必须重验证
        second = cache.fetch(url)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.reason, 'revalidated')
        last_request = self.requests[-1]
        self.assertEqual(last_request['headers'].get('If-None-Match'), '"check"')
        self.assertEqual(second.body, first.body)  # 304 继续引用原正文

    def test_vary_can_change_after_revalidation(self):
        clock = FakeClock()
        cache = RepresentationCache(clock=clock)
        url = self.base + '/vary-shift'
        cache.fetch(url, {'X-Tenant': 'north'})

        clock.advance(1)  # max-age=0，首次即过期
        reval = cache.fetch(url, {'X-Tenant': 'north'})
        self.assertEqual(reval.reason, 'revalidated')
        entry = cache.debug_entry(url)
        vary = set(entry['representations'][0]['vary'])
        self.assertEqual(vary, {'x-tenant', 'accept-language'})

        # 新 Vary 下，同一租户但不同语言是不同表示，必须回源且不得复用。
        before = len(self.requests)
        other_lang = cache.fetch(url,
                                 {'X-Tenant': 'north', 'Accept-Language': 'zh'})
        self.assertFalse(other_lang.from_cache)
        self.assertEqual(len(self.requests), before + 1)
        self.assertIsNotNone(
            [e for e in other_lang.events if e.kind == 'ISOLATED'])

        # 重验证后 max-age=5：完全相同的请求进入新鲜命中。
        clock.advance(2)
        hit = cache.fetch(url, {'X-Tenant': 'north'})
        self.assertEqual(hit.reason, 'fresh')

    def test_body_loss_falls_back_to_full_fetch_never_empty_body(self):
        cache = RepresentationCache()
        url = self.base + '/localized'
        headers = {'X-Tenant': 'north', 'Accept-Language': 'en'}
        first = cache.fetch(url, headers)
        self.assertTrue(cache.debug_drop_body(url, headers))

        before = len(self.requests)
        recovered = cache.fetch(url, headers)
        new_requests = self.requests[before:]
        # 先用该表示自己的 ETag 条件重验证；来源未变回 304 后必须无条件
        # 完整拉取，共两次来源请求，且任何阶段都不返回空正文。
        self.assertEqual(len(new_requests), 2)
        self.assertEqual(sent_header(new_requests[0], 'If-None-Match'),
                         '"north-en"')
        self.assertIsNone(sent_header(new_requests[1], 'If-None-Match'))
        self.assertIsNone(sent_header(new_requests[1], 'If-Modified-Since'))
        self.assertEqual(recovered.body, first.body)
        self.assertNotEqual(recovered.body, b'')
        reasons = [e.reason for e in recovered.events]
        self.assertIn('body-missing', reasons)
        self.assertIn('304-without-body-full-fetch', reasons)

    def test_gzip_negotiation_is_its_own_representation(self):
        cache = RepresentationCache()
        url = self.base + '/localized'
        plain = cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'en'})
        gz = cache.fetch(url, {'X-Tenant': 'north', 'Accept-Language': 'en',
                               'Accept-Encoding': 'gzip'})
        self.assertEqual(h(gz, 'Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(gz.body), plain.body)
        self.assertNotEqual(gz.body, plain.body)
        entry = cache.debug_entry(url)
        self.assertEqual(len(entry['representations']), 2)
        self.assertTrue(all(r['has_body'] for r in
                            entry['representations']))

    def test_tracking_params_share_key_business_params_do_not(self):
        cache = RepresentationCache()
        localized = self.base + '/localized'
        first = cache.fetch(localized + '?id=4&utm_source=mail',
                            {'X-Tenant': 'north', 'Accept-Language': 'en'})
        second = cache.fetch(localized + '?id=4&utm_campaign=sms',
                             {'X-Tenant': 'north', 'Accept-Language': 'en'})
        self.assertEqual(first.canonical_url, second.canonical_url)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(self.origin_requests('/localized')), 1)

        other = cache.fetch(localized + '?id=5',
                            {'X-Tenant': 'north', 'Accept-Language': 'en'})
        self.assertFalse(other.from_cache)
        self.assertEqual(len(self.origin_requests('/localized')), 2)


class PolicyAndConcurrencyTests(unittest.TestCase):
    """可控时钟 + 假传输：策略窗口、并发合并、容量与凭据安全。"""

    @staticmethod
    def _headers(**kw):
        return {k.lower(): v for k, v in kw.items()}

    def test_stale_if_error_window_is_decided_by_policy_not_thread_timing(self):
        clock = FakeClock()
        transport = ScriptedTransport([
            (200, self._headers(**{'Cache-Control':
                                   'max-age=5, stale-if-error=10',
                                   'ETag': '"e1"'}), b'catalog'),
            (503, self._headers(**{'Retry-After': '5'}), b'try later'),
            (503, self._headers(**{'Retry-After': '5'}), b'try later'),
        ])
        cache = RepresentationCache(clock=clock, transport=transport)
        url = 'http://origin.test/catalog'
        first = cache.fetch(url)
        self.assertEqual(first.body, b'catalog')

        clock.advance(8)  # 过期 3 秒，仍在 stale-if-error=10 窗口内
        served = cache.fetch(url)
        self.assertTrue(served.from_cache)
        self.assertEqual(served.reason, 'stale:stale-if-error')
        self.assertEqual(served.body, b'catalog')
        self.assertIn('STALE_SERVED', [e.kind for e in served.events])

        clock.advance(10)  # 累计过期 13 秒，超出窗口
        with self.assertRaises(FetchError):
            cache.fetch(url)
        reject = [e for e in cache.events if e.kind == 'FETCH_ERROR']
        self.assertEqual(reject[-1].reason,
                         'stale-if-error-window-exhausted')

    def test_must_revalidate_and_no_cache_refuse_stale_on_failure(self):
        for directive, expected_reason in [
            ('max-age=5, must-revalidate', 'must-revalidate-forbids-stale'),
            ('no-cache', 'no-cache-requires-revalidation'),
        ]:
            clock = FakeClock()
            transport = ScriptedTransport([
                (200, self._headers(**{'Cache-Control': directive,
                                       'ETag': '"e"'}), b'data'),
                FetchError('connection reset'),
            ])
            cache = RepresentationCache(clock=clock, transport=transport)
            url = 'http://origin.test/strict'
            cache.fetch(url)
            clock.advance(50)
            with self.assertRaises(FetchError):
                cache.fetch(url)
            self.assertEqual(
                [e for e in cache.events if e.kind == 'FETCH_ERROR'][-1].reason,
                expected_reason)

    def test_interrupted_refresh_keeps_old_value_readable_per_policy(self):
        clock = FakeClock()
        transport = ScriptedTransport([
            (200, self._headers(**{'Cache-Control':
                                   'max-age=5, stale-if-error=30',
                                   'ETag': '"e1"'}), b'old catalog'),
            FetchError('refresh interrupted'),
        ])
        cache = RepresentationCache(clock=clock, transport=transport)
        url = 'http://origin.test/catalog'
        stored = cache.fetch(url)
        stored_at = cache.debug_entry(url)['representations'][0]['stored_at']

        clock.advance(9)
        recovered = cache.fetch(url)
        # 中断没有写入任何新元数据：stored_at 不变，旧值按来源策略可读。
        self.assertEqual(
            cache.debug_entry(url)['representations'][0]['stored_at'],
            stored_at)
        self.assertEqual(recovered.body, b'old catalog')
        self.assertEqual(recovered.reason, 'stale:stale-if-error')

        # 而 must-revalidate 的表示在同样中断下必须报错。
        transport2 = ScriptedTransport([
            (200, self._headers(**{'Cache-Control':
                                   'max-age=1, must-revalidate',
                                   'ETag': '"e2"'}), b'locked catalog'),
            FetchError('refresh interrupted'),
        ])
        strict = RepresentationCache(clock=clock, transport=transport2)
        strict.fetch(url)
        clock.advance(5)
        with self.assertRaises(FetchError):
            strict.fetch(url)

    def test_concurrent_refresh_of_same_representation_is_coalesced(self):
        clock = FakeClock()
        transport = GateTransport(
            (200, self._headers(**{'Cache-Control': 'max-age=10',
                                   'Vary': 'X-Tenant',
                                   'ETag': '"shared"'}), b'shared catalog'))
        cache = RepresentationCache(clock=clock, transport=transport)
        url = 'http://origin.test/shared'
        request_headers = {'X-Tenant': 'north'}
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            cache.fetch(url, request_headers))) for _ in range(4)]
        for t in threads:
            t.start()
        self.assertTrue(transport.entered.wait(2))
        # 领头者已在来源处阻塞；此时只可能发生一次来源请求。
        self.assertEqual(len(transport.calls), 1)
        transport.release.set()
        for t in threads:
            t.join(2)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual({r.body for r in results}, {b'shared catalog'})
        coalesced = [e for e in cache.events if e.kind == 'COALESCED']
        self.assertEqual(len(coalesced), 3)

    def test_nonequivalent_representations_use_independent_gates(self):
        clock = FakeClock()
        gates = {}

        def transport(url, headers, timeout):
            gate = GateTransport((200, self._headers(
                **{'Cache-Control': 'max-age=10',
                   'Vary': 'X-Tenant',
                   'ETag': '"' + headers['x-tenant'] + '"'}),
                headers['x-tenant'].encode()))
            gates[headers['x-tenant']] = gate

            def run():
                gate.entered.set()
                gate.release.wait(timeout=5)

            run()
            return gate.response

        cache = RepresentationCache(clock=clock, transport=transport)
        url = 'http://origin.test/multi'
        outcomes = {}

        def worker(tenant):
            outcomes[tenant] = cache.fetch(url, {'X-Tenant': tenant})

        t_north = threading.Thread(target=worker, args=('north',))
        t_south = threading.Thread(target=worker, args=('south',))
        t_north.start()
        t_south.start()
        self.assertTrue(gates['north'].entered.wait(2))
        self.assertTrue(gates['south'].entered.wait(2))
        # 两个表示各自打到来源，互不等待：先放 north，south 仍在自己的门闩上。
        gates['north'].release.set()
        t_north.join(2)
        self.assertFalse(t_north.is_alive())
        self.assertTrue(t_south.is_alive())
        gates['south'].release.set()
        t_south.join(2)
        self.assertFalse(t_south.is_alive())
        self.assertEqual(outcomes['north'].body, b'north')
        self.assertEqual(outcomes['south'].body, b'south')

    def test_capacity_reclamation_removes_metadata_without_dangling_refs(self):
        clock = FakeClock()
        transport = ScriptedTransport([
            (200, self._headers(**{'Cache-Control': 'max-age=100'}), b'aaaa'),
            (200, self._headers(**{'Cache-Control': 'max-age=100'}),
             b'bbbbbbbbbb'),  # 10 字节，容量只够放这一份
        ])
        cache = RepresentationCache(capacity_bytes=10, clock=clock,
                                    transport=transport)
        first_url = 'http://origin.test/a'
        second_url = 'http://origin.test/b'
        cache.fetch(first_url)
        self.assertEqual(cache.debug_body_count(), 1)
        cache.fetch(second_url)

        cache.debug_assert_no_dangling_metadata()
        self.assertIsNone(cache.debug_entry(first_url))  # 元数据随正文一起回收
        self.assertIsNotNone(cache.debug_entry(second_url))
        evicted = [e for e in cache.events if e.kind == 'EVICTED']
        self.assertEqual(evicted[-1].reason, 'capacity')

        # LRU：被命中过的正文更新使用顺序，后写入者反而先被回收。
        transport2 = ScriptedTransport([
            (200, self._headers(**{'Cache-Control': 'max-age=100'}), b'cccc'),
            (200, self._headers(**{'Cache-Control': 'max-age=100'}), b'dddd'),
            (200, self._headers(**{'Cache-Control': 'max-age=100'}), b'eeee'),
        ])
        lru = RepresentationCache(capacity_bytes=8, clock=clock,
                                  transport=transport2)
        lru.fetch('http://origin.test/c')
        lru.fetch('http://origin.test/d')
        lru.fetch('http://origin.test/c')  # 触活 c
        lru.fetch('http://origin.test/e')  # 写入触发回收，d 应先于 c 离场
        lru.debug_assert_no_dangling_metadata()
        self.assertIsNone(lru.debug_entry('http://origin.test/d'))
        self.assertIsNotNone(lru.debug_entry('http://origin.test/c'))

    def test_credentials_are_never_stored_in_plaintext(self):
        clock = FakeClock()
        transport = ScriptedTransport([
            (200, self._headers(**{'Cache-Control': 'max-age=100',
                                   'Vary': 'Authorization'}), b'secret-zone'),
            (200, self._headers(**{'Cache-Control': 'max-age=100',
                                   'Vary': 'Authorization'}),
             b'other-secret-zone'),
        ])
        cache = RepresentationCache(clock=clock, transport=transport)
        url = 'http://origin.test/private'
        token = 'Bearer eyJ-very-secret-token-value'
        cache.fetch(url, {'Authorization': token})
        cache.fetch(url, {'Authorization': 'Bearer another-token'})

        serialized = repr(cache.debug_entry(url)) + repr(list(cache.events))
        self.assertNotIn(token, serialized)
        self.assertNotIn('very-secret-token-value', serialized)
        entry = cache.debug_entry(url)
        self.assertTrue(
            entry['representations'][0]['selecting']['authorization']
            .startswith('sha256:'))
        # 不同凭据仍是两个互不复用的表示。
        self.assertEqual(len(entry['representations']), 2)
        # 出站请求仍使用调用方的原始凭据。
        self.assertEqual(transport.calls[0]['headers']['authorization'], token)

    def test_304_without_local_body_triggers_unconditional_full_fetch(self):
        clock = FakeClock()
        url = 'http://origin.test/orphan'
        transport = ScriptedTransport([
            (304, self._headers(**{'ETag': '"e1"'}), b''),
            (200, self._headers(**{'Cache-Control': 'max-age=5',
                                   'ETag': '"e1"'}), b'real body'),
        ])
        cache = RepresentationCache(clock=clock, transport=transport)
        # 手工构造“元数据存在、正文丢失且已过期”的窄状态：来源先回 304，
        # 缓存本地却没有正文可合并，必须退化为不带条件头的完整拉取。
        from representation_cache import _Entry, _Representation
        rep = _Representation(
            selecting={}, vary_names=tuple(), status=200,
            headers={'etag': '"e1"', 'cache-control': 'max-age=5'},
            body_id=999, stored_at=clock() - 10, lifetime=5,
            no_cache=False, directives={'max-age': '5'})
        with cache._lock:
            cache._entries[url] = _Entry(representations=[rep])
        response = cache.fetch(url)
        self.assertEqual(len(transport.calls), 2)
        # 第一次来源调用是带条件头的 304，第二次必须是无条件完整拉取。
        self.assertIn('if-none-match', transport.calls[0]['headers'])
        self.assertNotIn('if-none-match', transport.calls[1]['headers'])
        self.assertEqual(response.body, b'real body')
        self.assertNotEqual(response.body, b'')
        reasons = [e.reason for e in response.events]
        self.assertIn('304-without-body-full-fetch', reasons)

    def test_origin_5xx_without_any_stored_value_raises_not_empty(self):
        transport = ScriptedTransport([
            (503, self._headers(**{'Retry-After': '5'}), b'try later'),
        ])
        cache = RepresentationCache(transport=transport)
        with self.assertRaises(FetchError):
            cache.fetch('http://origin.test/never-seen')
        # 错误响应不得落盘。
        self.assertIsNone(cache.debug_entry('http://origin.test/never-seen'))


if __name__ == '__main__':
    unittest.main()
