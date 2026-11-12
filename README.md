# 站点地图与增量网页发现

## 现有能力

URL 规范化与本地 HTTP 站点。站点提供递归 sitemap、循环索引、robots、重定向、条件响应和暂时失败；尚无爬取队列、目录归档与恢复。

`service.py` 与根目录 `test_service.py` 保留现有接口和回归测试。`tests/test_baseline.py` 同时加载原测试和离线演示测试，不替换或跳过原断言。`demo.py` 只展示当前底座，不代表待开发工作流已经完成。

## 环境和运行

固定 Python 3.11.10，仅使用标准库；无需安装依赖、登录账号或下载数据。`pyproject.toml` 登记 unittest 入口，不是可安装的软件包。在仓库根目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

测试与演示使用相同的本地合成材料，不连接生产系统。文件状态写入临时目录，重复执行不污染仓库。完整展示可执行 `python3 -m unittest discover -s tests -v && python3 demo.py`；无任意等待，预期在 90 秒以内完成。最终工作流应扩展这条展示路径并真实输出其结果，而非回放日志。

## 扩展边界

fixture_server.py 只监听本机随机端口，fixtures/site.json 描述站点响应。采集验收无需访问公网，也没有选中代码。

当前测试只保证既有底座行为。新增流程仍需覆盖任务要求中的中断、重复、并发和人工边界，不应将底座演示作为完成证明。
