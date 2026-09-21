# Kubernetes 深度剖析 · 本地阅读器

把《深入剖析 Kubernetes》（张磊 · 极客时间）专栏的 54 篇 HTML 教程，聚合成**一个本地网站**来阅读的小工具。

再也不用一篇文章开一个浏览器标签页：左侧目录、右侧正文，带搜索、翻页、阅读进度和暗色主题。

- 零第三方依赖，只用 Python 标准库
- 单文件实现：[`serve.py`](serve.py)（约 700 行，含前端）

## 特性

| 功能 | 说明 |
| --- | --- |
| 目录聚合 | 启动时扫描 `HTML/`，按讲次排序（`00` → `53`），侧栏可滚动 |
| 搜索 | 按标题 / 讲次过滤，`/` 聚焦，回车打开第一条结果 |
| 翻页 | 顶部「上一篇 / 下一篇」，或键盘 `←` `→`（也支持 `j` `k`） |
| 阅读进度 | 打开即自动标记已读，侧栏显示 `已读 x/54` 进度条与 ✓，`r` 手动切换，存在浏览器 localStorage |
| 主题 | `t` 切换亮色 / 暗色，记住选择 |
| 正文净化 | 精确抽取 `div#article-content`，丢掉脚本、样式、导航、页脚、广告位，补上统一的排版样式 |
| 代码块 | 把原文的行号表格和自带的「复制代码」按钮压平成干净的 `<pre><code>`，鼠标悬停显示自己的「复制」按钮 |
| 图片 | 远程图统一走 `/img` 代理并落地缓存到 `.reader-cache/`，重复阅读不再依赖网络；正文内嵌的 base64 图原样保留 |
| 移动端 | 窄屏下侧栏变为抽屉（左上 ☰ 收起） |

## 快速开始

```bash
# 依赖：Python 3.8+，不需要 pip install 任何东西
python3 serve.py
```

默认监听 `127.0.0.1:8000` 并自动打开浏览器：<http://127.0.0.1:8000/>

常用参数：

```bash
python3 serve.py --port 9000     # 换端口
python3 serve.py --no-open       # 不自动开浏览器
python3 serve.py --host 0.0.0.0  # 局域网内其它设备也能访问
```

停止：终端里 `Ctrl+C`，或 `pkill -f serve.py`。

## 目录结构

```
.
├── serve.py            # 阅读器本体（HTTP 服务 + 前端页面 + 正文抽取）
├── HTML/               # 54 篇教程的原始 HTML（阅读器的数据源）
├── html2md.py          # 可选：HTML → Markdown 转换脚本（用来生成 MD/）
├── MD/                 # 由 html2md.py 生成的 Markdown 版本（本地产物，未纳入仓库）
└── .reader-cache/      # 图片缓存，自动生成，已 gitignore
```

新增教程：把 HTML 丢进 `HTML/`，文件名以两位数字开头（如 `54  xxx.html`）即可，重启服务后自动出现在目录里。

## HTTP 接口

除了网页，还暴露了几个简单接口，方便脚本或二次开发：

| 路径 | 说明 |
| --- | --- |
| `GET /` | 阅读器页面 |
| `GET /doc/<id>` | 单篇正文 JSON：`{id, num, title, html, words}`，`id` 即讲次号（如 `01`） |
| `GET /api/index` | 目录 JSON：`[{id, num, title}]` |
| `GET /img?u=<url>` | 图片代理（带磁盘缓存），用于绕过防盗链并支持离线 |
| `GET /healthz` | 健康检查，返回 `ok` |

示例：

```bash
curl -s localhost:8000/api/index | python3 -m json.tool | head
curl -s localhost:8000/doc/01 | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['title'], d['words'])"
```

## 键盘快捷键

| 按键 | 作用 |
| --- | --- |
| `←` / `j` | 上一篇 |
| `→` / `k` | 下一篇 |
| `/` | 聚焦搜索框（`Enter` 打开第一条结果，`Esc` 清空） |
| `r` | 切换当前文章的已读状态 |
| `t` | 切换亮色 / 暗色主题 |

## 实现要点

1. **正文抽取**：`ArticleExtractor` 基于标准库 `HTMLParser`，按标签深度匹配 `<div id="article-content">`，只保留其内部内容；`<script>` / `<style>` 子树整体丢弃。比正则切分可靠，也省掉了 `bs4` / `html2text` 依赖。
2. **代码块压平**：原始页面每个代码块长这样——

   ```html
   <pre><code><table class="hljs-ln"><tbody><tr>
     <td class="hljs-ln-numbers"><div class="hljs-ln-n" data-line-number="1"></div></td>
     <td class="hljs-ln-code"><div class="hljs-ln-line">$ cf push "我的应用"</div></td>
   </tr></tbody></table></code>
   <div class="richcontent-pre-copy"><span class="iconfont"></span>复制代码</div></pre>
   ```

   其自带的「复制代码」按钮和行号表格混在 `<pre>` 里，直接渲染会多出「复制代码」字样、复制出来是表格。`PreFlattener` 把这 532 个代码块统一重写成 `<pre class="code"><code>一行行代码</code></pre>`，换行取自 `</tr>`，复制按钮改由前端自己加。
3. **空段落清理**：原文含大量 `<p></p>`、`<p><strong></strong></p>`，只产生空白，逐层剥离。
4. **图片代理**：把远程 `<img src>` 改写为 `/img?u=<url>`，服务端带 `Referer` 抓取并缓存到磁盘（文件名由 URL 哈希得到），既绕防盗链又支持离线复读。
5. **前端**：单个 HTML 页面 + 原生 JS，不需要构建。用 `location.hash` 记录当前文章，所以链接可分享、前进后退可用；阅读进度和主题存在 `localStorage`。

## 常见问题

- **端口被占用**：`python3 serve.py --port 9000`。
- **图片转圈 / 打不开**：首次访问某张图需要联网，服务端抓不到会返回 502；网络恢复后重试即可（缓存过的图之后不再联网）。想清空缓存直接删 `.reader-cache/`。
- **阅读进度丢了**：进度存在浏览器 `localStorage`，清站点数据 / 换浏览器会重置。
- **`serve.py` 报语法错误**：确认 Python ≥ 3.8（`python3 -V`），代码只用了标准库。
- **想通读 Markdown**：`python3 html2md.py` 会把 `HTML/` 全量转成 `MD/`（需要 `pip install beautifulsoup4 html2text`），生成后在编辑器里直接读 Markdown 即可。

## 说明

教程原文版权归极客时间及作者所有，本仓库仅用于个人离线阅读与排版整理。
