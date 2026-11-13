# 授权采集中的表示缓存与条件重验证

## 现有能力

`service.py` 保持已有 URL 规范化接口。`representation_cache.py` 在请求入口
`RepresentationCache.fetch()` 上提供按实际表示隔离的缓存层，只处理单个采集
请求的缓存与重验证，不做全站发现或站点地图遍历。

缓存层能力：

- **按表示隔离**：每个规范化 URL 下按响应 `Vary` 列举的请求头（租户、
  `Accept-Language`、`Accept-Encoding` 等）分别保存正文与校验器；不同租户、
  语言、压缩协商互不复用，拒绝复用时输出 `ISOLATED` 事件及具体维度。
- **缓存键**：复用 `canonical_url` 保留业务参数、剔除追踪参数；
  `Authorization`、`Proxy-Authorization`、`Cookie` 等凭据头不存明文，
  替换为缓存实例专属盐的 SHA-256 摘要后再参与键控。
- **禁止复用**：`Vary: *` 与 `Cache-Control: no-store` 只透传不落盘，
  并淘汰同签名旧表示，事件理由为 `STORE_REJECTED`。
- **新鲜度与重验证**：`max-age` 内为新鲜命中；`no-cache` 每次重验证；
  ETag / Last-Modified 只属于具体表示，条件请求绝不跨租户或跨语言。
  304 按 RFC 9111 合并响应头并继续引用原正文；原正文缺失时先条件探测，
  若来源仍回 304 则退化为无条件完整拉取，任何路径都不返回凭空构造的空正文。
- **来源失败的旧值策略**：`stale-if-error` 窗口内可服务旧值；
  窗口耗尽、`must-revalidate`、`no-cache` 明确拒绝复用并抛出 `FetchError`，
  理由区分为 `stale-if-error` / `*-window-exhausted` /
  `must-revalidate-forbids-stale` / `no-cache-requires-revalidation`。
- **并发合并**：同一表示的并发刷新合并为一次来源请求（门闩），互不等价的
  表示使用独立门闩互不等待；刷新在响应完整到达前不改写旧元数据，中断后
  旧值是否可读只由来源策略决定。
- **容量回收**：正文独立存放在带字节容量的 LRU 正文库，回收正文时同步删除
  引用它的全部元数据，保证不存在悬空元数据（`debug_assert_no_dangling_metadata`）。
- **可观测**：每次 `fetch` 的 `CachedResponse.events` 与缓存级
  `cache.events` 记录 `HIT` / `MISS` / `ISOLATED` / `REVALIDATE` /
  `REVALIDATED` / `STALE_SERVED` / `STORE_REJECTED` / `COALESCED` /
  `EVICTED` / `FETCH_ERROR` 及理由。

根目录 `test_service.py` 为原有回归测试，`tests/test_baseline.py` 会同时
加载原测试和离线演示验证；`tests/test_representation_cache.py` 覆盖条件
响应、Vary 变化、租户切换、正文丢失、策略窗口、并发合并与容量回收，
时钟由 `FakeClock` 显式推进，测试中不使用 sleep。

## 环境和运行

使用 Python 3.11.10，仅依赖标准库，无需登录账号、安装第三方包或下载数据。`.python-version` 固定解释器版本；`pyproject.toml` 仅登记 unittest 入口，项目不需要打包安装。

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

完整演示命令：

```sh
python3 -m unittest discover -s tests -v && python3 demo.py
```

演示实时读取合成输入并运行当前接口，通常在 90 秒以内结束，不回放历史日志。状态写入临时目录，重复执行不污染仓库；本地 HTTP 示例只访问回环地址，不连接生产系统。

## 合成来源

`fixture_server.site(recorder=None)` 在上下文内监听本机随机端口并自动清理；
传入列表会记录每个请求的路径与请求头（供测试断言条件头与回源次数）。
`fixtures/site.json` 包含条件响应、租户/语言表示、禁止存储、错误响应以及
`/vary-shift`（在 304 重验证后调整 `Vary` 与缓存指令）。服务器是合成来源，
不包含客户端缓存逻辑。

## 能力边界

缓存层只封装单次采集 GET 的表示选择、复用、条件重验证与正文容量管理；
业务编排、持久化的流程决策、全站发现与 robots/站点地图遍历不属于当前
实现范围，演示输出不代表这些扩展能力已经存在。
