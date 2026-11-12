# Sitemap Crawl

这是一个用于sitemap crawl流程的 Python 基础项目，当前提供单条业务记录的校验与规范化函数。测试输入均为合成数据。

## 运行与验证

在项目目录执行 `python3 -m unittest discover -s . -p 'test_*.py' -v` 运行测试。服务入口位于 `service.py`，仅使用 Python 标准库，不需要联网或安装第三方依赖。

## 当前范围

本项目暂未包含批次编排、持久化、故障恢复和运行报告，可在现有领域函数基础上扩展完整工作流。
