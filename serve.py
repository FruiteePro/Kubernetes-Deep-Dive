#!/usr/bin/env python3
"""
《深入剖析 Kubernetes》本地阅读器

把 HTML/ 目录下的教程聚合成一个网站：左侧目录、右侧正文，支持搜索、翻页、
已读标记、主题切换。

用法:
    python3 serve.py              # 打开浏览器 http://127.0.0.1:8000
    python3 serve.py --port 9000 --no-open

仅依赖 Python 标准库。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML_DIR = os.path.join(ROOT, "HTML")
CACHE_DIR = os.path.join(ROOT, ".reader-cache")

# --------------------------------------------------------------------------- #
# 扫描 HTML 目录
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"^\s*(\d+)\s*[\.\-_、]?\s*(.*)$")


def scan_items() -> list[dict]:
    """返回按序号排好序的教程列表。"""
    items: list[dict] = []
    if not os.path.isdir(HTML_DIR):
        raise SystemExit(f"找不到目录: {HTML_DIR}")
    for name in os.listdir(HTML_DIR):
        if not name.lower().endswith((".html", ".htm")):
            continue
        stem = name.rsplit(".", 1)[0]
        m = _NUM_RE.match(stem)
        num, title = (m.group(1), m.group(2).strip()) if m else ("", stem.strip())
        items.append(
            {
                "id": num or urllib.parse.quote(stem),
                "num": num,
                "title": title or stem,
                "file": name,
                "stem": stem,
            }
        )
    items.sort(key=lambda it: (int(it["num"]) if it["num"] else 10**9, it["stem"]))
    seen: set[str] = set()
    for i, it in enumerate(items, 1):
        base = it["id"]
        while it["id"] in seen:
            it["id"] = f"{base}-{i}"
        seen.add(it["id"])
    return items


ITEMS = scan_items()
BY_ID = {it["id"]: it for it in ITEMS}
ORDER = {it["id"]: i for i, it in enumerate(ITEMS)}


# --------------------------------------------------------------------------- #
# 抽取正文（标准库 HTMLParser，逐标签深度匹配 div#article-content）
# --------------------------------------------------------------------------- #


class ArticleExtractor(HTMLParser):
    """取出 <div id="article-content"> 的完整内部 HTML，丢弃 script/style。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.active = False
        self.depth = 0
        self.skip: str | None = None
        self.parts: list[str] = []

    # -- helpers ---------------------------------------------------------- #
    def _is_target(self, tag: str, attrs: list) -> bool:
        return tag == "div" and dict(attrs).get("id") == "article-content"

    # -- parser hooks ----------------------------------------------------- #
    def handle_starttag(self, tag, attrs):
        if self.skip:
            return
        if not self.active:
            if self._is_target(tag, attrs):
                self.active, self.depth = True, 1  # 外层 div 不入库
            return
        if tag in ("script", "style"):
            self.skip = tag
            return
        if tag == "div":
            self.depth += 1
        self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.skip or not self.active or tag in ("script", "style"):
            return
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip = None
            return
        if not self.active:
            return
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.active = False
                return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip and self.active:
            self.parts.append(data)

    def handle_entityref(self, name):
        if not self.skip and self.active:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip and self.active:
            self.parts.append(f"&#{name};")



class PreFlattener(HTMLParser):
    """把极客时间的代码块规整干净。

    原始结构里，代码块是 `hljs-ln` 行号表格 + 一个嵌在 <pre> 内的
    “复制代码”按钮：
        <pre ...><code><table class="hljs-ln">…</table></code>
          <div class="richcontent-pre-copy">…复制代码</div></pre>
    于是行号、按钮文字都会混进正文（复制出来也带这些垃圾）。
    这里统一压平成 <pre class="code"><code>一行行代码</code></pre>，
    复制按钮改由前端自己加。
    """

    # 代码块内部只可能是这些包装标签，全部丢掉
    WRAPPERS = {"table", "tbody", "thead", "tr", "td", "th", "div", "code", "span", "p"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.in_pre = False
        self.skip_tag: str | None = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if not self.in_pre:
            if tag == "pre":
                self.in_pre = True
                self.out.append('<pre class="code"><code>')
            else:
                self.out.append(self.get_starttag_text())
            return
        if tag == "div" and "richcontent-pre-copy" in (d.get("class") or ""):
            self.skip_tag, self.skip_depth = "div", 1  # 整棵子树丢弃
            return
        if tag not in self.WRAPPERS:
            self.out.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.skip_tag or not self.in_pre or tag in self.WRAPPERS:
            return
        self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if not self.in_pre:
            self.out.append(f"</{tag}>")
            return
        if tag == "pre":
            self.in_pre = False
            self.out.append("</code></pre>")
        elif tag == "tr":
            self.out.append("\n")  # 表格的一行 = 代码的一行
        # 其余包装标签的结束标签直接丢弃

    def handle_data(self, data):
        if not self.skip_tag:  # <pre> 内外都原样透传
            self.out.append(data)

    def handle_entityref(self, name):
        if not self.skip_tag:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip_tag:
            self.out.append(f"&#{name};")


def flatten_pre(fragment: str) -> str:
    parser = PreFlattener()
    parser.feed(fragment)
    parser.close()
    out = "".join(parser.out)
    # 去掉代码块首尾的空行，保留行内缩进
    out = re.sub(r"(<pre class=\"code\"><code>)\n+", r"\1", out)
    out = re.sub(r"\n+\s*</code></pre>", "\n</code></pre>", out)
    return out


_IMG_RE = re.compile(r'(<img\b[^>]*?)\ssrc="([^"]+)"', re.I)
_LAZY_RE = re.compile(r'\sdata-src="[^"]*"', re.I)
_FOOTER_IMG_RE = re.compile(
    r'<p>\s*<img[^>]*47a6f3bf6b92d58512d5a2ed0a556f55[^>]*>\s*</p>', re.I
)

# 原页面里大量 <p></p>、<p><strong></strong></p> 之类的空段落，只贡献空白
_EMPTY_INLINE = (
    r'<(?:strong|b|em|i|span|code|a|u)(?:\s[^>]*)?>\s*</(?:strong|b|em|i|span|code|a|u)>'
)
_EMPTY_P_RE = re.compile(r'<p>(?:\s|' + _EMPTY_INLINE + r')*</p>', re.I)


def _drop_empty(fragment: str) -> str:
    for _ in range(4):  # 逐层剥离嵌套的空标签
        new = _EMPTY_P_RE.sub("", fragment)
        if new == fragment:
            break
        fragment = new
    return fragment


def _clean(fragment: str) -> str:
    """规整代码块、清理空段落与装饰图，并把图片改成本地代理地址。"""
    fragment = flatten_pre(fragment)
    fragment = _FOOTER_IMG_RE.sub("", fragment)  # 结尾固定的“关注/分享”横幅
    fragment = _drop_empty(fragment)
    fragment = _LAZY_RE.sub("", fragment)

    def proxy(m: re.Match) -> str:
        url = html.unescape(m.group(2))
        if url.startswith("//"):
            url = "https:" + url
        if not url.lower().startswith(("http://", "https://")):
            return m.group(0)
        return f'{m.group(1)} src="/img?u={urllib.parse.quote(url, safe="")}"'

    return _IMG_RE.sub(proxy, fragment).strip()


_cache: dict[str, str] = {}
_lock = threading.Lock()


def article_html(item: dict) -> str:
    with _lock:
        if item["id"] in _cache:
            return _cache[item["id"]]
    path = os.path.join(HTML_DIR, item["file"])
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    parser = ArticleExtractor()
    parser.feed(raw)
    parser.close()
    body = "".join(parser.parts)
    if not body.strip():  # 兜底：整页丢进来
        body = raw
    body = _clean(body)
    with _lock:
        _cache[item["id"]] = body
    return body


def article_text(item: dict) -> str:
    txt = re.sub(r"<[^>]+>", "", article_html(item))
    return html.unescape(txt)


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>深入剖析 Kubernetes</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --text:#22262b; --muted:#7b848f; --line:#e6e8ec;
  --accent:#2f6fed; --accent-soft:#eaf0fe; --code-bg:#f4f6f8; --shadow:0 1px 3px rgba(16,24,40,.06);
  --w:760px;
}
html[data-theme="dark"]{
  --bg:#14161a; --panel:#1b1e24; --text:#dcdfe4; --muted:#8b939e; --line:#2a2f36;
  --accent:#7ea6ff; --accent-soft:#22293a; --code-bg:#20242b; --shadow:none;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
#app{display:flex;height:100%;overflow:hidden}

/* ---------------- 侧栏 ---------------- */
#side{width:330px;flex:0 0 330px;background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;height:100%}
#side header{padding:18px 18px 12px;border-bottom:1px solid var(--line)}
#side h1{margin:0 0 6px;font-size:17px;letter-spacing:.2px}
#side .sub{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
#bar{flex:1;height:4px;background:var(--line);border-radius:99px;overflow:hidden}
#bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .25s}
#q{width:100%;margin-top:12px;padding:8px 10px;font-size:13px;color:var(--text);
  background:var(--code-bg);border:1px solid var(--line);border-radius:8px;outline:none}
#q:focus{border-color:var(--accent)}
#list{flex:1;overflow-y:auto;padding:8px 8px 24px;margin:0;list-style:none}
#list li{margin:1px 0}
#list a{display:flex;gap:9px;padding:7px 10px;border-radius:8px;color:var(--text);
  text-decoration:none;font-size:13.5px;line-height:1.45}
#list a:hover{background:var(--code-bg)}
#list a.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
#list .n{flex:0 0 22px;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px;padding-top:1px}
#list a.on .n{color:var(--accent)}
#list a.done .t::after{content:"✓";margin-left:6px;color:#25a34c;font-weight:700}
#list .t{flex:1}
#list .empty{padding:20px;color:var(--muted);font-size:13px;text-align:center}
#side footer{padding:10px 18px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px}

/* ---------------- 主区 ---------------- */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#topbar{display:flex;align-items:center;gap:10px;padding:10px 22px;
  background:var(--panel);
  border-bottom:1px solid var(--line);z-index:5}
#crumbs{flex:1;min-width:0;font-size:13px;color:var(--muted);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
button{font:inherit;font-size:12.5px;color:var(--text);background:var(--panel);
  border:1px solid var(--line);border-radius:8px;padding:5px 11px;cursor:pointer;white-space:nowrap}
button:hover{border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.solid{background:var(--accent);border-color:var(--accent);color:#fff}
button.solid:hover{color:#fff;opacity:.9}
#menu{display:none}
#scroller{overflow-y:auto;flex:1;scroll-behavior:smooth}

/* ---------------- 正文排版 ---------------- */
article{max-width:var(--w);margin:0 auto;padding:34px 28px 120px}
article.empty{color:var(--muted)}
article .head{margin:0 0 26px;padding-bottom:18px;border-bottom:1px solid var(--line)}
article .head .no{font-size:12px;letter-spacing:1.5px;color:var(--accent);font-weight:700}
article .head h2{margin:6px 0 0;font-size:26px;line-height:1.35;letter-spacing:.2px}
article .head .meta{display:flex;gap:12px;flex-wrap:wrap;align-items:center;
  margin-top:8px;font-size:12px;color:var(--muted)}
article .head .meta .hint{color:var(--muted)}
article .head .meta .hint.done{color:#25a34c;font-weight:600}
.ct{font-size:16.5px}
.ct h1,.ct h2,.ct h3,.ct h4{line-height:1.4;margin:1.7em 0 .7em;font-weight:700}
.ct h1{font-size:23px}.ct h2{font-size:20px}.ct h3{font-size:17.5px}.ct h4{font-size:16.5px}
.ct p{margin:0 0 1.05em}
.ct a{color:var(--accent);text-decoration:none;border-bottom:1px solid var(--accent-soft)}
.ct a:hover{border-bottom-color:var(--accent)}
.ct img{max-width:100%;height:auto;border-radius:8px;display:block;margin:18px auto;
  box-shadow:var(--shadow)}
.ct ul,.ct ol{padding-left:1.5em;margin:0 0 1.05em}
.ct li{margin:.3em 0}
.ct blockquote{margin:1.2em 0;padding:.2em 1em;color:var(--muted);
  border-left:3px solid var(--line);background:var(--code-bg);border-radius:0 8px 8px 0}
.ct blockquote p{margin:.5em 0}
.ct hr{border:0;border-top:1px solid var(--line);margin:2em 0}
.ct code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em;
  background:var(--code-bg);padding:.15em .4em;border-radius:5px}
.ct pre{background:var(--code-bg);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;overflow-x:auto;margin:1.1em 0;line-height:1.6;
  position:relative}
.ct pre code{background:none;padding:0;font-size:13px;white-space:pre}
.ct pre .copy{position:absolute;top:7px;right:7px;font-size:11.5px;padding:2px 9px;
  opacity:0;transition:opacity .15s}
.ct pre:hover .copy,.ct pre .copy.ok{opacity:1}
.ct pre .copy.ok{color:#25a34c;border-color:#25a34c}
.ct table{border-collapse:collapse;width:100%;margin:1.2em 0;font-size:14.5px;display:block;overflow-x:auto}
.ct th,.ct td{border:1px solid var(--line);padding:7px 10px;text-align:left}
.ct th{background:var(--code-bg)}
.ct .hljs-ln{border-collapse:collapse;width:auto}
.ct .hljs-ln td{padding:0;border:0}
.ct .hljs-ln-n:before{content:attr(data-line-number);color:var(--muted);
  padding-right:14px;user-select:none}
.ct .hljs-ln-numbers{vertical-align:top}
.ct figure{margin:1.2em 0}
.ct figcaption{text-align:center;font-size:13px;color:var(--muted)}
.ct>*:first-child{margin-top:0}
#toTop{position:fixed;right:26px;bottom:26px;border-radius:99px;width:40px;height:40px;
  padding:0;font-size:16px;display:none;background:var(--panel);box-shadow:var(--shadow)}
#toTop.show{display:block}

@media (max-width:900px){
  #side{position:fixed;z-index:20;left:0;top:0;transform:translateX(-100%);
    transition:transform .2s ease;box-shadow:0 0 40px rgba(0,0,0,.2)}
  body.nav #side{transform:none}
  #menu{display:inline-block}
  article{padding:22px 18px 100px}
  article .head h2{font-size:22px}
}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <header>
      <h1>深入剖析 Kubernetes</h1>
      <div class="sub"><span id="stat"></span><span id="bar"><i></i></span></div>
      <input id="q" placeholder="搜索标题…  ( / 聚焦)">
    </header>
    <ul id="list"></ul>
    <footer>← → 翻页 · r 已读 · t 主题 · / 搜索</footer>
  </aside>

  <div id="main">
    <div id="topbar">
      <button id="menu">☰</button>
      <div id="crumbs"></div>
      <button id="mark">标记已读</button>
      <button id="prev">← 上一篇</button>
      <button id="next" class="solid">下一篇 →</button>
      <button id="theme" title="切换主题">◐</button>
    </div>
    <div id="scroller">
      <article id="doc" class="empty">从左侧目录选择一篇开始阅读。</article>
    </div>
    <button id="toTop">↑</button>
  </div>
</div>

<script>
const INDEX = __INDEX_JSON__;
const $ = (s) => document.querySelector(s);
const docEl = document.documentElement;
const scroller = $('#scroller');
const doc = $('#doc');

/* ---------- 本地状态 ---------- */
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
};
let read = new Set(store.get('k8s.read', []));
let theme = store.get('k8s.theme', 'light');
docEl.dataset.theme = theme;

/* ---------- 侧栏目录 ---------- */
const byId = {};
INDEX.forEach((it) => (byId[it.id] = it));
const listEl = $('#list');
const nodes = {};

function renderList(filter = '') {
  const f = filter.trim().toLowerCase();
  listEl.innerHTML = '';
  const hit = INDEX.filter((it) =>
    !f || it.title.toLowerCase().includes(f) || it.num.includes(f) || ('第' + it.num).includes(f));
  if (!hit.length) {
    listEl.innerHTML = '<li class="empty">没有匹配的标题</li>';
    return;
  }
  hit.forEach((it) => {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = '#' + it.id;
    a.dataset.id = it.id;
    a.innerHTML = '<span class="n">' + (it.num || '·') + '</span><span class="t"></span>';
    a.querySelector('.t').textContent = it.title;
    a.title = it.title;
    li.appendChild(a);
    listEl.appendChild(li);
    nodes[it.id] = a;
  });
  syncSidebar();
}

function syncSidebar() {
  Object.entries(nodes).forEach(([id, a]) => {
    a.classList.toggle('on', id === current);
    a.classList.toggle('done', read.has(id));
  });
  const pct = INDEX.length ? Math.round((read.size / INDEX.length) * 100) : 0;
  $('#stat').textContent = '已读 ' + read.size + ' / ' + INDEX.length + ' (' + pct + '%)';
  const act = nodes[current];  // 让当前文章在目录里保持可见
  if (act && act.scrollIntoView) act.scrollIntoView({ block: 'nearest' });
  $('#bar > i').style.width = pct + '%';
}

let current = null;
let autoMarked = false;  // 本篇是否已经因为“读到末尾”自动标记过

function neighbors() {
  const i = INDEX.findIndex((it) => it.id === current);
  return [INDEX[i - 1], INDEX[i + 1]];
}

/* ---------- 打开文章 ---------- */
async function open(id, push = true) {
  const it = byId[id];
  if (!it) return;
  current = id;
  if (push && location.hash !== '#' + id) location.hash = id;
  document.title = it.title + ' · 深入剖析 Kubernetes';
  $('#crumbs').textContent = (it.num ? '第 ' + it.num + ' 讲 · ' : '') + it.title;
  doc.className = 'empty';
  doc.textContent = '加载中…';
  setNav();
  try {
    const res = await fetch('/doc/' + encodeURIComponent(id));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    doc.className = '';
    doc.innerHTML =
      '<div class="head"><div class="no">' + (it.num ? '第 ' + it.num + ' 讲' : '') + '</div>' +
      '<h2></h2><div class="meta">约 ' + data.words + ' 字 · ' +
      Math.max(1, Math.round(data.words / 400)) + ' 分钟' +
      '<span class="hint" id="readHint"></span></div></div>' +
      '<div class="ct">' + data.html + '</div>';
    doc.querySelector('h2').textContent = it.title;
    addCopyButtons();
    // 图片加载完会撑高正文，也要重新判断一次“是否已到末尾”
    doc.querySelectorAll('.ct img').forEach((img) => {
      if (!img.complete) img.addEventListener('load', checkBottom, { once: true });
    });
    autoMarked = false;
    scroller.scrollTop = 0;
    updateReadHint();
    const forId = id;
    setTimeout(() => { if (current === forId) checkBottom(); }, 250);
    checkBottom();
  } catch (err) {
    doc.className = 'empty';
    doc.textContent = '加载失败：' + err.message;
  }
  if (window.innerWidth <= 900) document.body.classList.remove('nav');
}

/* 给每个代码块挂一个自己的“复制”按钮（原页面的按钮是混在 <pre> 里的脏数据） */
function addCopyButtons() {
  doc.querySelectorAll('.ct pre').forEach((pre) => {
    const btn = document.createElement('button');
    btn.className = 'copy';
    btn.type = 'button';
    btn.textContent = '复制';
    btn.onclick = async () => {
      const text = (pre.querySelector('code') || pre).innerText.replace(/\n$/, '');
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (e2) {}
        ta.remove();
      }
      btn.textContent = '已复制 ✓';
      btn.classList.add('ok');
      setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('ok'); }, 1200);
    };
    pre.appendChild(btn);
  });
}


function setNav() {
  const [prev, next] = neighbors();
  $('#prev').disabled = !prev;
  $('#next').disabled = !next;
  $('#prev').title = prev ? prev.title : '';
  $('#next').title = next ? next.title : '';
  $('#mark').textContent = read.has(current) ? '✓ 已读（点击取消）' : '标记已读';
}

function markRead(id, force) {
  const want = force === undefined ? !read.has(id) : force;
  if (want) read.add(id); else read.delete(id);
  store.set('k8s.read', [...read]);
  syncSidebar();
  setNav();
  updateReadHint();
}

/* 正文已经滚到末尾（留 80px 余量） */
function atBottom() {
  return scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 80;
}

/* 只有真的看到末尾才自动标记已读；一篇只自动标记一次 */
function checkBottom() {
  if (!current || autoMarked || !atBottom()) return;
  autoMarked = true;
  if (!read.has(current)) markRead(current, true);
}

/* 标题下方那行提示：未读 → 提示“读到末尾自动标记”，已读 → ✓ */
function updateReadHint() {
  const el = document.getElementById('readHint');
  if (!el) return;
  const done = read.has(current);
  el.textContent = done ? '✓ 已读' : '· 读到末尾自动标记为已读';
  el.classList.toggle('done', done);
}

/* ---------- 事件 ---------- */
$('#list').addEventListener('click', (e) => {
  const a = e.target.closest('a');
  if (!a || !a.dataset.id) return;
  e.preventDefault();  // 统一交给 open()，避免 hashchange 再加载一次
  open(a.dataset.id);
});
window.addEventListener('hashchange', () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (id && id !== current) open(id, false);
});
$('#prev').onclick = () => { const [p] = neighbors(); if (p) open(p.id); };
$('#next').onclick = () => { const [, n] = neighbors(); if (n) open(n.id); };
$('#mark').onclick = () => current && markRead(current);
$('#menu').onclick = () => document.body.classList.toggle('nav');
$('#theme').onclick = () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  docEl.dataset.theme = theme;
  store.set('k8s.theme', theme);
};
$('#toTop').onclick = () => (scroller.scrollTop = 0);
scroller.addEventListener('scroll', () => {
  $('#toTop').classList.toggle('show', scroller.scrollTop > 400);
  checkBottom();
});
window.addEventListener('resize', checkBottom);

$('#q').addEventListener('input', (e) => renderList(e.target.value));
$('#q').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    const first = listEl.querySelector('a');
    if (first) open(first.dataset.id);
  } else if (e.key === 'Escape') {
    $('#q').value = ''; renderList(''); $('#q').blur();
  }
});

document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
  if (typing) return;
  if (e.key === '/') { e.preventDefault(); $('#q').focus(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'ArrowLeft' || e.key === 'j') $('#prev').click();
  else if (e.key === 'ArrowRight' || e.key === 'k') $('#next').click();
  else if (e.key === 'r' && current) markRead(current);
  else if (e.key === 't') $('#theme').click();
});

/* ---------- 启动 ---------- */
renderList();
const start = decodeURIComponent(location.hash.slice(1));
if (start && byId[start]) open(start, false);
else {
  const firstUnread = INDEX.find((it) => !read.has(it.id)) || INDEX[0];
  if (firstUnread) open(firstUnread.id);
}
</script>
</body>
</html>
"""

INDEX_JSON = json.dumps(
    [{"id": it["id"], "num": it["num"], "title": it["title"]} for it in ITEMS],
    ensure_ascii=False,
)


# --------------------------------------------------------------------------- #
# 图片代理（带磁盘缓存，离线也能看已读过的图）
# --------------------------------------------------------------------------- #

_img_lock = threading.Lock()


def fetch_image(url: str) -> tuple[bytes, str]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]+", "_", url)[-120:]
    path = os.path.join(CACHE_DIR, key)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read(), "image/*"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://time.geekbang.org/",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "image/png")
    with _img_lock:
        with open(path, "wb") as fh:
            fh.write(data)
    return data, ctype


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "K8sReader/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 安静模式

    # -- 输出辅助 ---------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control", "public, max-age=86400" if cache else "no-store"
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._send(code, msg.encode("utf-8"), "text/plain; charset=utf-8")

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if route in ("/", "/index.html"):
            self._send(200, PAGE.replace("__INDEX_JSON__", INDEX_JSON).encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        if route == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        if route.startswith("/doc/"):
            item_id = urllib.parse.unquote(route[len("/doc/"):])
            item = BY_ID.get(item_id)
            if not item:
                self._error(404, f"no such article: {item_id}")
                return
            body = article_html(item)
            self._json(
                {
                    "id": item["id"],
                    "num": item["num"],
                    "title": item["title"],
                    "html": body,
                    "words": len(re.sub(r"\s+", "", article_text(item))),
                }
            )
            return

        if route == "/img":
            qs = urllib.parse.parse_qs(parsed.query)
            url = (qs.get("u") or [""])[0]
            if not url.lower().startswith(("http://", "https://")):
                self._error(400, "bad url")
                return
            try:
                data, ctype = fetch_image(url)
            except Exception as exc:  # noqa: BLE001
                self._error(502, f"fetch failed: {exc}")
                return
            self._send(200, data, ctype, cache=True)
            return

        if route == "/api/index":
            self._json([{"id": it["id"], "num": it["num"], "title": it["title"]} for it in ITEMS])
            return

        self._error(404, "not found")

    do_HEAD = do_GET


def main() -> None:
    ap = argparse.ArgumentParser(description="Kubernetes 专栏本地阅读器")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"共 {len(ITEMS)} 篇教程 · 阅读地址: {url}  (Ctrl+C 退出)")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
