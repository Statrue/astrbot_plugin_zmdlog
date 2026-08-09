# ZmdBot

ZmdBot 是用于查询 ZMDLogs 公开 DPS 榜单的 AstrBot 插件。所有成功的帮助与榜单查询都会生成一张 1280px 宽且不分页的 PNG 长图，具体榜单展示前 30 名。

## 安装

安装插件依赖后，需要在 AstrBot 所使用的同一 Python 环境中安装 Chromium：

```shell
pip install -r requirements.txt
playwright install chromium
```

插件不会在运行时自动下载浏览器。缺少 Chromium、背景资源或模板渲染失败时，只返回统一的图片生成失败提示，不会改发文本榜单数据。

## 指令

- `/zmdlog`、`/zmdlog help`：完整帮助页。
- `/zmdlog 榜单`：全部榜单前三名。
- `/zmdlog 榜单 <关键词>`：查询具体榜单、副本或副本范围。
- `/zmdlog <关键词>`：智能匹配快捷入口。

帮助页会读取当前 AstrBot 会话配置的唤醒前缀，不固定写死为 `/`。

## 配置

主要配置位于 AstrBot 插件配置页，包括 ZMDLogs API 与网页地址、请求与渲染超时、别名文件、缓存 TTL 以及模糊匹配阈值。

## 缓存与容错

- `hot-bosses` 与具体 DPS 榜单分别按查询键缓存，并合并同一键的并发请求。
- `hot-bosses` 刷新失败时，可在一个额外 TTL 周期内使用最近一次成功结果；具体榜单不使用过期数据。
- 网络异常和服务端 `5xx` 最多重试一次，总网络等待不超过 15 秒；客户端 `4xx` 不重试。
- API、协议、渲染及未预期异常均转换为简短的聊天提示，不向聊天消息暴露上游响应内容或堆栈。

## 自动测试

```shell
python -m unittest discover -s tests -v
```

测试覆盖路由与帮助页、别名和模糊匹配、缓存并发与过期策略、API 重试和协议校验，以及模板数据边界与渲染失败。
