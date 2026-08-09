# ZmdBot

ZmdBot 是用于查询 ZMDLogs 公开 DPS 榜单的 AstrBot 插件。所有成功的帮助与榜单查询都会生成一张 1280px 宽、内容完整且不分页的 PNG 长图。

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
