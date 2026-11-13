#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core.py —— 组卷网 / 学科网 试卷「重排 + 打印」引擎（DrissionPage 版）
=====================================================================

设计要点（对应原 Tampermonkey 脚本的四处重构）
------------------------------------------------
1. 不再 `document.body.innerHTML = ''`
   → 只给 <html> 加一个临时 class `zjp-active`，用 CSS 把除打印根以外的
     所有 body 子元素隐藏起来；打印完 `__zjp_restore()` 即可复原页面。

2. 不再 `cloneNode(true)`
   → 把真实节点「搬运」进打印根（appendChild 是移动，不是复制）。
     节点连同它已经完成的图片解码状态一起搬过去，彻底消除打印白屏。

3. 不再 `requestAnimationFrame` 轮询 15 秒
   → 页内装一个事件驱动的「就绪探针」：
       MutationObserver 监听答案区渲染
       img.decode() 等待每张图真正可用（单张有超时兜底）
       document.fonts.ready 等待字体
     Python 侧只做轻量状态轮询，随时可中止，不会卡死。

4. 字体/字号不再靠 `body, * { ... !important }` 全局覆盖
   → 参数化生成一条 scoped CSS，只作用于打印根，并把 KaTeX / MathJax
     排除在外，避免把公式字体一起干掉。

单文件即可跑（CLI 模式，便于排查）：
    python core.py "https://zujuan.xkw.com/...." --size 12 --font SimSun
    python core.py --self-test --pdf          # 用内置模拟试卷跑通全流程
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

def _resolve_app_dir() -> Path:
    """
    程序所在目录。

    PyInstaller/Nuitka 打成单文件后，`__file__` 指向临时解包目录
    （PyInstaller 的 _MEIPASS），PDF、浏览器配置、界面偏好要是写在那里，
    程序一退出就随临时目录被删掉。所以打包运行时必须改用 exe 自身所在目录。
    """
    frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
    if frozen:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()
PROFILE_DIR = APP_DIR / ".chrome_profile"          # 持久化 Cookie，免重复扫码
DEFAULT_OUT_DIR = APP_DIR                          # 默认保存目录 = 程序所在文件夹
DEFAULT_DEBUG_PORT = 9422                          # 固定调试端口：只接管自己启动的浏览器

FONT_OPTIONS = [
    {"key": "SimSun", "label": "宋体", "css": '"SimSun","宋体","Songti SC","Noto Serif SC",serif'},
    {"key": "Microsoft YaHei", "label": "微软雅黑",
     "css": '"Microsoft YaHei","微软雅黑","PingFang SC","Noto Sans SC",sans-serif'},
    {"key": "SimHei", "label": "黑体", "css": '"SimHei","黑体","Heiti SC",sans-serif'},
    {"key": "KaiTi", "label": "楷体", "css": '"KaiTi","楷体","Kaiti SC","STKaiti",serif'},
    {"key": "FangSong", "label": "仿宋", "css": '"FangSong","仿宋","STFangsong",serif'},
    {"key": "Times New Roman", "label": "Times New Roman",
     "css": '"Times New Roman",Times,"Liberation Serif",serif'},
]

SIZE_OPTIONS = [
    {"pt": 10.5, "label": "10.5pt　五号"},
    {"pt": 12, "label": "12pt　　小四"},
    {"pt": 14, "label": "14pt　　四号"},
    {"pt": 15, "label": "15pt　　小三"},
    {"pt": 16, "label": "16pt　　三号"},
    {"pt": 18, "label": "18pt　　小二"},
]

MODE_OPTIONS = {
    "q": "仅试题",
    "qa": "试题 + 答案",
    "qe": "答案移至末尾",
    "a": "仅答案",
}

# 站点 DOM 约定（与原脚本保持一致；JS 里内联了同样的选择器）
#   大题标题 .sec-title                       → 取 span 文本
#   题目     .tk-quest-item.quesroot          → 一题一个
#   题卡     .wrapper.quesdiv                 → 真正搬运的节点
#   题干     .exam-item__cnt                  → 题号 + 干 + 选项
#   答案     .exam-item__opt                  → 答案 + 解析
#   知识点   .knowledge-box                   → 打印前删掉


# --------------------------------------------------------------------------
# 异常 / 配置
# --------------------------------------------------------------------------

class PrintError(RuntimeError):
    """带「下一步建议」的业务异常，UI 可直接展示 hint。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


@dataclass
class PrintConfig:
    url: str
    font_key: str = "SimSun"
    size_pt: float = 12.0
    mode: str = "qa"
    title: str = ""

    headless: bool = False
    headless_fallback: bool = True              # 无头被风控时，自动退回屏幕外的后台窗口
    window_offscreen: bool = False              # 有头但把窗口移到屏幕外（后台下载的回退方案）
    port: Optional[int] = None                 # 有值则接管已开的浏览器
    profile_dir: Path = PROFILE_DIR
    login_timeout: float = 240.0               # 留足扫码时间
    render_timeout: float = 30.0
    ready_timeout: float = 45.0
    quiet_ms: int = 600                        # 页面静止多久算渲染完成
    img_timeout_ms: int = 8000                 # 单张图片兜底超时

    window_print: bool = True                  # True: window.print()；False: 导出 PDF
    pdf_path: Optional[Path] = None
    show_meta: bool = True                     # 卷头姓名/班级/得分
    keep_browser: bool = True
    close_browser_after: bool = False           # 下载模式：存完 PDF 就把浏览器关掉

    def font(self) -> dict:
        for f in FONT_OPTIONS:
            if f["key"] == self.font_key or f["label"] == self.font_key:
                return f
        return FONT_OPTIONS[0]


# --------------------------------------------------------------------------
# CSS：屏幕预览 + 打印视图
# --------------------------------------------------------------------------

def build_css(font_css: str, size_pt: float) -> str:
    """生成打印视图样式。字体、字号是在这里被「注入 CSS」的。"""
    return f"""
/* ================= 屏幕预览 ================= */
html.zjp-active {{ background: #eceff4 !important; }}
html.zjp-active body {{ background: #eceff4 !important; margin: 0 !important; }}
html.zjp-active body > *:not(#zjp-root) {{ display: none !important; }}

#zjp-root {{
  box-sizing: border-box;
  width: 820px;
  max-width: calc(100% - 32px);
  margin: 28px auto 72px;
  padding: 46px 52px 56px;
  background: #fff;
  color: #17181a;
  border-radius: 6px;
  box-shadow: 0 10px 40px rgba(15, 23, 42, .10), 0 1px 3px rgba(15, 23, 42, .06);
  font-family: {font_css};
  font-size: {size_pt}pt;
  line-height: 1.8;
  letter-spacing: .2px;
}}

/* 字体统一（公式渲染器例外，避免破坏公式字体） */
#zjp-root,
#zjp-root *:not(.katex):not(.katex *):not(.MathJax):not(.MathJax *):not(mjx-container):not(mjx-container *) {{
  font-family: {font_css} !important;
}}
#zjp-root .exam-item__cnt,
#zjp-root .exam-item__opt,
#zjp-root .zjp-q {{
  font-size: inherit !important;
  line-height: inherit !important;
}}

/* ---------- 卷头 ---------- */
.zjp-top {{ margin-bottom: 22px; }}
.zjp-title {{
  text-align: center;
  font-size: 1.62em;
  font-weight: 700;
  line-height: 1.4;
  margin: 0 0 12px;
}}
.zjp-head {{
  display: flex;
  justify-content: center;
  gap: 34px;
  flex-wrap: wrap;
  font-size: .92em;
  color: #3f4550;
  margin-bottom: 10px;
}}
.zjp-count {{
  text-align: center;
  font-size: .84em;
  color: #8a919c;
  padding-bottom: 14px;
  border-bottom: 1px solid rgba(15, 23, 42, .10);
}}

/* ---------- 大题 / 题目 ---------- */
.zjp-sec {{
  font-size: 1.06em;
  font-weight: 700;
  margin: 26px 0 12px;
  padding-left: 10px;
  border-left: 3px solid #2f6bff;
  line-height: 1.5;
}}
.zjp-answer-title {{ margin-top: 34px; }}
.zjp-q {{
  margin: 0 0 16px;
  padding: 2px 0;
  break-inside: avoid;
  page-break-inside: avoid;
}}
.zjp-a {{
  margin: 0 0 14px;
  break-inside: avoid;
  page-break-inside: avoid;
}}
.zjp-a-label {{
  font-weight: 700;
  font-size: .95em;
  color: #2f6bff;
  margin-bottom: 2px;
}}

/* 打印时不需要的东西 */
#zjp-root .zjprint-noprint,
#zjp-root .knowledge-box {{ display: none !important; }}

/* ---------- 屏幕提示条 ---------- */
#zjp-hint {{
  position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
  background: rgba(23, 24, 26, .82); color: #fff; font-size: 13px;
  padding: 9px 18px; border-radius: 999px; z-index: 2147483647;
  box-shadow: 0 8px 24px rgba(0, 0, 0, .18); backdrop-filter: blur(6px);
  font-family: {font_css};
}}

/* ================= 打印 ================= */
/* A4 纵向，四边 10mm 页边距。题目本身再由 break-inside 控制不跨页。 */
@page {{ size: A4 portrait; margin: 10mm; }}

@media print {{
  html, body, #zjp-root {{
    background: #fff !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }}
  body > *:not(#zjp-root) {{ display: none !important; }}

  #zjp-root {{
    width: auto !important; max-width: none !important;
    margin: 0 !important; padding: 0 !important;
    box-shadow: none !important; border-radius: 0 !important;
    line-height: 1.72;
    color: #000;
  }}
  #zjp-root .zjp-title {{ font-size: 1.55em; break-after: avoid; page-break-after: avoid; }}
  #zjp-root .zjp-sec {{ break-after: avoid; page-break-after: avoid; }}
  #zjp-root .zjp-a-label {{ break-after: avoid; page-break-after: avoid; }}
  /* 整题不跨页：一道题（含答案解析）尽量落在同一页 */
  #zjp-root .zjp-q,
  #zjp-root .zjp-a {{
    break-inside: avoid;
    page-break-inside: avoid;
  }}
  /* 段落级兜底，避免出现单行孤行 */
  #zjp-root p, #zjp-root li {{ orphans: 3; widows: 3; }}
  #zjp-root h1, #zjp-root h2, #zjp-root h3, #zjp-root h4 {{
    break-after: avoid; page-break-after: avoid;
  }}
  #zjp-root img, #zjp-root svg, #zjp-root table {{ max-width: 100% !important; }}
  #zjp-root img {{ break-inside: avoid; page-break-inside: avoid; }}
  #zjp-root table {{ border-collapse: collapse; }}
  #zjp-root button, #zjp-root .zjprint-noprint, #zjp-hint {{ display: none !important; }}
  #zjp-root a {{ color: inherit !important; text-decoration: none !important; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
"""


# --------------------------------------------------------------------------
# 页内 JS（每段都是「函数体」，以 return 结尾，供 tab.run_js 直接执行）
# --------------------------------------------------------------------------

JS_APPLY_FONT = r"""
return (function () {
  var CFG = __CFG__;
  var old = document.getElementById('zjp-style');
  if (old) old.remove();
  var st = document.createElement('style');
  st.id = 'zjp-style';
  st.textContent = CFG.css;
  (document.head || document.documentElement).appendChild(st);
  return { ok: true, cssBytes: CFG.css.length, font: CFG.font, size: CFG.size };
})();
"""

JS_PROBE = r"""
return (function () {
  var q = document.querySelectorAll('.tk-quest-item.quesroot').length;
  var sec = document.querySelectorAll('.sec-title').length;
  var ans = document.querySelectorAll('.exam-item__opt').length;
  var nick = document.querySelector('.user-nickname');
  var body = document.body;
  var txt = (body && body.innerText) || '';
  return {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    questions: q,
    sections: sec,
    answers: ans,
    logged: !!nick,
    user: nick ? nick.innerText.trim() : '',
    bodyLen: txt.length,
    hasLoginForm: !!document.querySelector('input[type="password"]'),
    mentionLogin: txt.indexOf('登录') >= 0,
    mentionPay: (txt.indexOf('开通') >= 0 || txt.indexOf('购买') >= 0),
    mention404: (txt.indexOf('不存在') >= 0 || txt.indexOf('已删除') >= 0)
  };
})();
"""

JS_EXPAND_ANSWERS = r"""
return (function () {
  var R = { clicked: [], already: [], missing: [], counts: {} };

  function fire(el, name, isToggle) {
    if (!el) { R.missing.push(name); return false; }
    // label 本身没有 checked，要把 for 指向的真实控件找出来判断
    var box = el;
    if (el.tagName === 'LABEL') {
      box = (el.htmlFor && document.getElementById(el.htmlFor))
         || el.querySelector('input') || el;
    } else if (el.tagName !== 'INPUT') {
      box = el.querySelector('input') || el;
    }
    if (isToggle && box && box.checked) { R.already.push(name); return false; }
    try { el.click(); R.clicked.push(name); return true; }
    catch (e) { R.missing.push(name + ' (' + e.message + ')'); return false; }
  }

  // 新版：<label for="isshowAnswer">（有 label 就点 label，label 才是可点的那个）
  var labelEl = document.querySelector('label[for="isshowAnswer"]');
  if (labelEl) fire(labelEl, '新版答案开关', true);
  else fire(document.querySelector('#isshowAnswer'), '#isshowAnswer', true);

  // 旧版：.tklabel-checkbox.show-answer
  if (!document.querySelector('#isshowAnswer')) {
    fire(document.querySelector('.tklabel-checkbox.show-answer label'),
         '旧版答案开关', true);
  }

  R.counts = {
    questions: document.querySelectorAll('.tk-quest-item.quesroot').length,
    stems: document.querySelectorAll('.exam-item__cnt').length,
    answers: document.querySelectorAll('.exam-item__opt').length
  };
  return R;
})();
"""

JS_REFLOW = r"""
return (function () {
  var CFG = __CFG__;
  var R = { ok: false, warnings: [], counts: {}, timing: {} };
  var t0 = Date.now();
  var warn = function (m) { R.warnings.push(m); };

  /* ---------- 0. 清理上一轮 ---------- */
  if (typeof window.__zjpRestore === 'function') {
    try { window.__zjpRestore(true); } catch (e) { warn('清理上一轮失败: ' + e.message); }
  }

  /* ---------- 1. 打印根 ---------- */
  var root = document.createElement('div');
  root.id = 'zjp-root';
  root.className = 'zjp-root zjp-mode-' + CFG.mode;

  var undo = [];
  function move(node, parent) {
    if (!node || !parent) return false;
    if (node === parent || node.contains(parent)) return false;
    undo.push({ n: node, p: node.parentNode, x: node.nextSibling });
    parent.appendChild(node);       // 移动而非克隆：保留图片解码状态
    return true;
  }

  /* ---------- 2. 按文档顺序搬运 ---------- */
  var items = Array.prototype.slice.call(
    document.querySelectorAll('.sec-title, .tk-quest-item.quesroot')
  );
  R.counts.items = items.length;

  var cards = [], blocks = [];
  var qNo = 0, secNo = 0, noWrapper = 0, emptyQ = 0;

  for (var i = 0; i < items.length; i++) {
    var node = items[i];
    if (root.contains(node)) continue;      // 已被前一个节点整棵搬走

    /* 2.1 大题标题 */
    if (node.classList.contains('sec-title')) {
      var sp = node.querySelector('span');
      var txt = ((sp ? sp.textContent : node.textContent) || '').replace(/\s+/g, ' ').trim();
      if (!txt) continue;
      var sec = document.createElement('div');
      sec.className = 'zjp-sec';
      sec.textContent = txt;
      root.appendChild(sec);
      secNo++;
      continue;
    }

    /* 2.2 题目 */
    qNo++;
    var card = document.createElement('div');
    card.className = 'zjp-q';
    card.setAttribute('data-no', String(qNo));

    var wrap = node.querySelector('.wrapper.quesdiv');
    if (!wrap) { noWrapper++; wrap = node; }

    // 知识点标签：原地删除（不影响可还原性，它只是装饰）
    var boxes = wrap.querySelectorAll('.knowledge-box');
    for (var b = 0; b < boxes.length; b++) { try { boxes[b].remove(); } catch (e) {} }

    move(wrap, card);

    /* 2.3 答案后置模式：把答案块搬到末尾 */
    if (CFG.mode === 'qe') {
      var opts = wrap.querySelectorAll('.exam-item__opt');
      for (var k = 0; k < opts.length; k++) {
        var box = document.createElement('div');
        box.className = 'zjp-a';
        var lab = document.createElement('div');
        lab.className = 'zjp-a-label';
        lab.textContent = '第 ' + qNo + ' 题';
        box.appendChild(lab);
        move(opts[k], box);
        blocks.push(box);
      }
    }

    /* 2.4 丢弃空卡片 */
    if (!card.querySelector('.exam-item__cnt, .exam-item__opt')) {
      if (!(card.textContent || '').trim()) { emptyQ++; continue; }
    }
    cards.push(card);
    root.appendChild(card);
  }

  if (CFG.mode === 'qe' && blocks.length) {
    var at = document.createElement('div');
    at.className = 'zjp-sec zjp-answer-title';
    at.textContent = '答案与解析';
    root.appendChild(at);
    for (var a = 0; a < blocks.length; a++) root.appendChild(blocks[a]);
  }

  /* ---------- 3. 卷头 ---------- */
  var top = document.createElement('div');
  top.className = 'zjp-top';

  var title = document.createElement('div');
  title.className = 'zjp-title';
  title.textContent = CFG.title || (document.title || '试卷').trim();
  top.appendChild(title);

  if (CFG.showMeta) {
    var head = document.createElement('div');
    head.className = 'zjp-head';
    head.innerHTML = '<span>姓名：___________</span><span>班级：___________</span><span>得分：___________</span>';
    top.appendChild(head);
  }
  var cnt = document.createElement('div');
  cnt.className = 'zjp-count';
  var suffix = { qa: '　（含答案与解析）', qe: '　（答案与解析见卷末）' }[CFG.mode] || '';
  cnt.textContent = CFG.mode === 'a'
    ? '答案与解析　共 ' + cards.length + ' 题'
    : '本卷共 ' + cards.length + ' 题' + suffix;
  top.appendChild(cnt);
  root.insertBefore(top, root.firstChild);

  /* ---------- 4. 挂载 ---------- */
  if (!cards.length) {
    warn('未搬运到任何题目，可能不是试卷详情页');
    R.counts = { items: items.length, sections: secNo, questions: 0 };
    R.timing.ms = Date.now() - t0;
    return R;
  }

  var hint = document.createElement('div');
  hint.id = 'zjp-hint';
  hint.textContent = '打印预览已就绪 · 按 Ctrl+P 可再次唤起打印（Esc 关闭预览）';
  root.appendChild(hint);

  document.body.appendChild(root);
  document.documentElement.classList.add('zjp-active');
  try { window.scrollTo(0, 0); } catch (e) {}

  /* ---------- 5. 可还原 ---------- */
  window.__zjpUndo = function () {
    for (var i = undo.length - 1; i >= 0; i--) {
      var it = undo[i];
      try {
        if (!it.p) continue;
        if (it.x && it.x.parentNode === it.p) it.p.insertBefore(it.n, it.x);
        else it.p.appendChild(it.n);
      } catch (e) {}
    }
    undo.length = 0;
  };
  window.__zjpRestore = function (silent) {
    try { if (window.__zjpUndo) window.__zjpUndo(); } catch (e) {}
    var r = document.getElementById('zjp-root');
    if (r) r.remove();
    document.documentElement.classList.remove('zjp-active');
    if (!silent) {
      var st = document.getElementById('zjp-style');
      if (st) st.remove();
    }
  };

  R.ok = true;

  /* ---------- 6. 体检：答案区是不是"未登录/无权限"占位 ---------- */
  var blocked = 0;
  var opts = root.querySelectorAll('.exam-item__opt');
  for (var o = 0; o < opts.length; o++) {
    var t = opts[o].textContent || '';
    if (t.indexOf('未登录') >= 0 || t.indexOf('不能查看') >= 0
        || t.indexOf('登录后') >= 0 || t.indexOf('开通会员') >= 0) blocked++;
  }

  R.counts = {
    items: items.length, sections: secNo, questions: cards.length,
    answers: blocks.length, noWrapper: noWrapper, emptyQ: emptyQ, blocked: blocked
  };
  R.timing.ms = Date.now() - t0;
  return R;
})();
"""

JS_INSTALL_WAITER = r"""
return (function () {
  var CFG = __CFG__;
  if (window.__zjpWait && window.__zjpWait.stop) { try { window.__zjpWait.stop(); } catch (e) {} }

  var root = document.getElementById('zjp-root') || document.body;
  var S = { t0: Date.now(), last: Date.now(), muts: 0, imgs: new Map(), observer: null };
  var bump = function () { S.last = Date.now(); };

  function track(img) {
    if (!img || S.imgs.has(img)) return;
    var rec = { t: Date.now(), state: 'pending', src: '' };
    S.imgs.set(img, rec);
    try { rec.src = (img.currentSrc || img.src || '').slice(0, 160); } catch (e) {}

    // 懒加载兜底：把 data-src 顶到 src，并关掉 lazy
    try {
      var lazy = img.getAttribute('data-src') || img.getAttribute('data-original')
              || img.getAttribute('data-lazy-src');
      if (lazy && (!img.getAttribute('src') || img.getAttribute('src').indexOf('data:') === 0)) {
        img.src = lazy;
      }
      img.loading = 'eager';
    } catch (e) {}

    var done = false;
    function settle(state) {
      if (done) return;
      done = true;
      rec.state = state;
      rec.dur = Date.now() - rec.t;
      bump();
    }

    // 单张图兜底：死链/永不回调也不能拖死整条链路
    setTimeout(function () { settle(img.naturalWidth > 0 ? 'ok' : 'timeout'); },
               CFG.imgTimeoutMs || 8000);

    if (img.complete && img.naturalWidth > 0) { settle('ok'); return; }
    if (typeof img.decode === 'function') {
      img.decode().then(function () { settle('ok'); },
                        function () { settle(img.naturalWidth > 0 ? 'ok' : 'fail'); });
    } else {
      img.addEventListener('load', function () { settle('ok'); }, { once: true });
      img.addEventListener('error', function () { settle('fail'); }, { once: true });
    }
  }

  function scan() {
    var list = root.querySelectorAll('img');
    for (var i = 0; i < list.length; i++) track(list[i]);
  }

  S.observer = new MutationObserver(function (muts) {
    S.muts += muts.length;
    bump();
    scan();
  });
  S.observer.observe(root, {
    childList: true, subtree: true,
    attributes: true, attributeFilter: ['src', 'data-src']
  });
  scan();

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(bump, function () {});
  }

  window.__zjpWait = {
    stop: function () { try { S.observer.disconnect(); } catch (e) {} },
    status: function () {
      var t = Date.now(), pending = 0, ok = 0, failed = 0, total = 0, bad = [];
      S.imgs.forEach(function (rec) {
        total++;
        if (rec.state === 'pending') pending++;
        else if (rec.state === 'ok') ok++;
        else { failed++; if (bad.length < 5) bad.push(rec.src); }
      });
      var fonts = (!document.fonts) || document.fonts.status === 'loaded';
      var quiet = t - S.last, elapsed = t - S.t0;
      var ready = elapsed >= (CFG.minMs || 800)
               && quiet >= (CFG.quietMs || 600)
               && pending === 0 && fonts;
      return { ready: ready, total: total, ok: ok, pending: pending, failed: failed,
               bad: bad, fonts: fonts, quiet: quiet, elapsed: elapsed, muts: S.muts };
    }
  };
  return { installed: true };
})();
"""

JS_WAIT_STATUS = r"""
var w = window.__zjpWait;
if (!w) return { ready: true, missing: true, total: 0, ok: 0, pending: 0, failed: 0 };
return w.status();
"""

JS_PRINT = r"""
return (function () {
  var CFG = __CFG__;
  if (CFG.title) { try { document.title = CFG.title; } catch (e) {} }
  window.__zjpAfterPrint = 0;
  window.addEventListener('afterprint', function () {
    window.__zjpAfterPrint = Date.now();
  }, { once: true });

  // 用 setTimeout 让本次 run_js 立刻返回，避免 CDP 调用被打印对话框阻塞
  setTimeout(function () {
    try {
      window.focus();
      window.print();
    } catch (e) {
      window.__zjpPrintError = String((e && e.message) || e);
    }
  }, CFG.delay || 400);

  return { scheduled: true, title: document.title };
})();
"""

JS_RESTORE = r"""
return (function () {
  if (typeof window.__zjpRestore === 'function') { window.__zjpRestore(false); }
  else {
    var r = document.getElementById('zjp-root');
    if (r) r.remove();
    document.documentElement.classList.remove('zjp-active');
    var st = document.getElementById('zjp-style');
    if (st) st.remove();
  }
  return { restored: true };
})();
"""


def _js(script: str, cfg: Optional[dict] = None) -> str:
    """把配置 JSON 注入 JS 模板。"""
    if cfg is None:
        cfg = {}
    return script.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
# 站点标题尾巴："-组卷网"、"_学科网"、"| zujuan.xkw.com" 之类
_TITLE_TAIL = re.compile(r"\s*[-_–—|·]\s*(组卷网|学科网|zujuan\.xkw\.com|xkw\.com)\s*$", re.I)


def clean_title(text: str) -> str:
    """去掉标题里的站点后缀，避免打印出来是「…数学试题-组卷网」。"""
    out = (text or "").strip()
    for _ in range(2):                      # 偶尔会连着两个后缀
        new = _TITLE_TAIL.sub("", out).strip()
        if new == out:
            break
        out = new
    return out or "试卷"


def safe_filename(text: str, maxlen: int = 60) -> str:
    text = _BAD_CHARS.sub("_", (text or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text or "试卷")[:maxlen]


def find_browser() -> Optional[str]:
    env = os.environ.get("ZJP_BROWSER_PATH")
    if env and Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("chrome") or shutil.which("msedge")


# 无头 Chrome 的默认 UA 里带 "HeadlessChrome"，多数站点（含组卷网）会直接返回风控页。
UA_TEMPLATE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/{v} Safari/537.36")


def _chrome_version(browser_exe: Optional[str]) -> str:
    """从浏览器安装目录的版本子目录推断版本号，让伪装的 UA 与真实内核一致。"""
    if browser_exe:
        try:
            base = Path(browser_exe).parent
            vers = [p.name for p in base.iterdir()
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p.name)]
            if vers:
                vers.sort(key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True)
                return vers[0]
        except Exception:
            pass
    return "131.0.0.0"


def suggested_pdf_name(title: str, mode: str = "qa") -> str:
    """试卷标题 + 模式标注 + 秒级时间戳，保证同一份卷子多次下载不会互相覆盖。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "" if mode == "qa" else "_" + safe_filename(MODE_OPTIONS.get(mode, mode), 12)
    return f"{safe_filename(title)}{tag}_{stamp}.pdf"


# --------------------------------------------------------------------------
# 主引擎
# --------------------------------------------------------------------------

class ZujuanPrinter:
    """一次完整的「打开 → 重排 → 打印」流程。每次打印 new 一个实例即可。"""

    STEP_TOTAL = 7

    def __init__(self, cfg: PrintConfig, on_log: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self._on_log = on_log or self._default_log
        self.page = None
        self.tab = None
        self.stats: dict = {}

    # ---------------------------------------------------------------- log
    @staticmethod
    def _default_log(msg: str, level: str) -> None:
        prefix = {
            "info": "  ", "muted": "  ", "step": "", "ok": "  ",
            "warn": "  ", "err": "  ",
        }.get(level, "  ")
        print(f"{time.strftime('%H:%M:%S')} {prefix}{msg}", flush=True)

    def log(self, msg: str, level: str = "info") -> None:
        try:
            self._on_log(msg, level)
        except Exception:
            pass

    def _step(self, no: int, title: str) -> None:
        self.log(f"〔{no}/{self.STEP_TOTAL}〕{title}", "step")

    # ------------------------------------------------------------ helpers
    def _run_js(self, script: str, cfg: Optional[dict] = None, timeout: float = 20.0):
        return self.tab.run_js(_js(script, cfg), timeout=timeout)

    def _probe(self) -> dict:
        try:
            return self.tab.run_js(JS_PROBE) or {}
        except Exception:
            return {}

    # ------------------------------------------------------------ 主流程
    def run(self) -> dict:
        t0 = time.time()
        try:
            self._start_browser()
            self._open()
            self._wait_paper()
            self._inject_style()
            self._reflow()
            self._wait_assets()
            result = self._do_print()
        except PrintError as e:
            self.log(f"✗ {e}", "err")
            if e.hint:
                self.log(f"  建议：{e.hint}", "muted")
            raise
        except Exception as e:                                            # noqa: BLE001
            self.log(f"✗ 未预料的错误：{type(e).__name__}: {e}", "err")
            self.log(traceback.format_exc(limit=3), "muted")
            raise PrintError(f"程序异常：{e}", "请把上面的日志发给开发者") from e

        result["elapsed"] = round(time.time() - t0, 1)
        self.log(f"✓ 全部完成，用时 {result['elapsed']} 秒", "ok")
        return result

    # ------------------------------------------------------- 1 启动浏览器
    def _start_browser(self) -> None:
        self._step(1, "启动 / 接管浏览器")
        try:
            from DrissionPage import ChromiumPage, ChromiumOptions  # noqa: F401
        except ImportError as e:
            raise PrintError("缺少依赖 DrissionPage", "pip install DrissionPage") from e

        if self.cfg.port:
            self.log(f"接管已运行的浏览器 127.0.0.1:{self.cfg.port}", "muted")
            self.page = ChromiumPage(f"127.0.0.1:{self.cfg.port}")
            self.tab = self.page
            self.log("✓ 已接管", "ok")
            return

        self._launch_browser()

    def _launch_browser(self) -> None:
        """按当前 cfg 启动浏览器（无头 / 屏幕外后台窗口）。"""
        from DrissionPage import ChromiumOptions, ChromiumPage

        def make(profile: Optional[Path]):
            co = ChromiumOptions()
            exe = find_browser()
            if exe:
                co.set_browser_path(exe)
                self.log(f"内核：{exe}", "muted")
            if profile is not None:
                co.set_user_data_path(str(profile))
            # 用固定端口，避免误接管桌面上其它调试端口的浏览器实例
            try:
                co.set_local_port(self.cfg.port or DEFAULT_DEBUG_PORT)
            except Exception:
                pass
            co.set_argument("--lang=zh-CN")
            co.set_argument("--no-first-run")
            co.set_argument("--no-default-browser-check")
            co.set_argument("--disable-blink-features=AutomationControlled")
            if self.cfg.headless:
                # 无头模式：不弹窗、不抢焦点，适合「下载」这种纯后台场景
                co.headless(True)
                co.set_argument("--hide-scrollbars")
                co.set_argument("--disable-gpu")
                co.set_argument("--window-size=1440,1000")
                co.set_argument("--force-device-scale-factor=1")
                # 关键：无头版默认 UA 带 HeadlessChrome，组卷网会直接返回「阻断页面」
                ua = UA_TEMPLATE.format(v=_chrome_version(exe))
                co.set_user_agent(ua)
                self.log(f"  已伪装为常规 UA：Chrome/{ua.split('Chrome/')[-1].split(' ')[0]}", "muted")
            else:
                if self.cfg.window_offscreen:
                    # 后台下载的回退方案：有头但把窗口挪到屏幕外，用户看不见
                    co.set_argument("--window-position=-32000,-32000")
                    co.set_argument("--window-size=1400,1000")
                else:
                    # 预览模式：正常最大化，用户要能看到打印对话框
                    co.set_argument("--start-maximized")
            return ChromiumPage(co)

        if self.cfg.headless:
            self.log("无头模式（不弹浏览器窗口）", "muted")

        try:
            self.page = make(self.cfg.profile_dir)
        except Exception as e:                                            # noqa: BLE001
            self.log(f"⚠ 使用持久化配置启动失败（{type(e).__name__}），改用临时配置", "warn")
            self.log("  常见原因：上次的浏览器窗口还开着，占用了同一个用户目录", "muted")
            tmp = APP_DIR / ".chrome_profile_tmp"
            self.page = make(tmp)
        self.tab = self.page
        self.log("✓ 浏览器已就绪", "ok")

    # ----------------------------------------------------------- 2 打开网址
    def _open(self) -> None:
        self._step(2, "打开试卷网址")
        url = (self.cfg.url or "").strip()
        if not re.match(r"^https?://", url):
            raise PrintError(f"网址无效：{url}", "请粘贴完整链接，例如 https://zujuan.xkw.com/…")
        self.log(url, "muted")
        try:
            self.tab.get(url, timeout=self.cfg.render_timeout + 15)
        except Exception as e:                                            # noqa: BLE001
            self.log(f"⚠ 导航告警：{e}", "warn")
        try:
            self.tab.wait.doc_loaded(timeout=self.cfg.render_timeout)
        except Exception:
            pass
        info = self._probe()
        self.log(f"✓ 已打开：{info.get('title', '(无标题)')}", "ok")

    # ------------------------------------------- 3 等登录 + 等试题渲染
    def _wait_paper(self) -> None:
        self._step(3, "等待登录与试题渲染")
        info = self._poll_questions(self.cfg.login_timeout)

        # 无头模式偶尔会被站点风控挡住：自动退回「屏幕外的后台窗口」再试一次
        if not info and self.cfg.headless and self.cfg.headless_fallback:
            self.log("  ⚠ 无头模式没拿到页面，改用后台窗口重试（窗口在屏幕外）", "warn")
            self._restart_headed()
            info = self._poll_questions(self.cfg.login_timeout)

        if not info:
            raise PrintError("等待超时：页面上没有找到试题", self._diagnose_hint(self._probe()))

        user = info.get("user") or "(已登录)"
        self.log(f"✓ 账号：{user}", "ok")
        self.log(f"✓ 检测到 {info.get('questions', 0)} 道题、"
                 f"{info.get('sections', 0)} 个大题分组、"
                 f"{info.get('answers', 0)} 个答案区", "ok")

    def _poll_questions(self, timeout: float) -> dict:
        """轮询直到页面上出现试题；超时返回空 dict。"""
        deadline = time.time() + timeout
        warned_login = False
        last_note = 0.0
        headless = self.cfg.headless
        while True:
            info = self._probe()
            if info.get("questions", 0) > 0:
                return info

            if not info.get("logged") and not warned_login:
                if headless:
                    self.log("⚠ 未检测到登录状态（后台模式无法扫码，可先用「预览打印内容」登录一次）", "warn")
                else:
                    self.log("⚠ 未检测到登录状态，请在浏览器窗口完成扫码登录…", "warn")
                warned_login = True

            now = time.time()
            if now > deadline:
                return {}
            if now - last_note > 10:
                last_note = now
                self.log(f"  仍无试题（{info.get('questions', 0)} 题），继续等待…", "muted")
            time.sleep(0.6)

    def _restart_headed(self) -> None:
        """无头被拦时，换成「移到屏幕外」的有头窗口：用户看不见，但反爬放行。"""
        try:
            if self.page is not None:
                self.page.quit()
        except Exception:
            pass
        self.page = None
        self.cfg.headless = False
        self.cfg.window_offscreen = True
        self._launch_browser()

    @staticmethod
    def _diagnose_hint(info: dict) -> str:
        if info.get("mention404"):
            return "该试卷可能已下架或被删除，请换一份试卷试试"
        if info.get("hasLoginForm") or not info.get("logged"):
            return "请先在浏览器里登录组卷网，再重新点击导出"
        if info.get("mentionPay"):
            return "该试卷可能需要会员权限，请在网页里确认能否正常预览"
        if info.get("bodyLen", 0) < 500:
            return "页面内容为空，可能是网络未加载完或触发了风控，建议在浏览器里刷新一次"
        return "请确认打开的是「试卷详情/预览」页（网址形如 zujuan.xkw.com/…/paper…）"

    # ------------------------------------------------------- 4 注入字体样式
    def _inject_style(self) -> None:
        font = self.cfg.font()
        self._step(4, f"注入字体样式：{font['label']} / {self.cfg.size_pt:g}pt")
        css = build_css(font["css"], self.cfg.size_pt)
        res = self._run_js(JS_APPLY_FONT, {
            "css": css, "font": font["label"], "size": self.cfg.size_pt,
        }) or {}
        if not res.get("ok"):
            raise PrintError("注入样式失败", "请刷新页面后重试")
        self.log(f"✓ 样式已注入（{res.get('cssBytes', 0)} 字节）", "ok")

    # -------------------------------------------------------- 5 展开 + 重排
    def _reflow(self) -> None:
        self._step(5, f"展开答案并重排（{MODE_OPTIONS.get(self.cfg.mode, self.cfg.mode)}）")

        if self.cfg.mode in ("qa", "qe", "a"):
            exp = self._run_js(JS_EXPAND_ANSWERS) or {}
            if exp.get("clicked"):
                self.log(f"  已点击：{'、'.join(exp['clicked'])}", "muted")
            if exp.get("already"):
                self.log("  答案开关本来就已打开", "muted")
            if exp.get("missing"):
                self.log(f"  ⚠ 未找到：{'、'.join(exp['missing'])}", "warn")
            c = exp.get("counts") or {}
            self.log(f"  当前 DOM：{c.get('questions', 0)} 题 / "
                     f"{c.get('stems', 0)} 题干 / {c.get('answers', 0)} 答案区", "muted")
            # 给答案区一点渲染时间（异步挂载），由第 6 步的事件探针兜底

        title = clean_title(self.cfg.title or self._probe().get("title", ""))
        res = self._run_js(JS_REFLOW, {
            "mode": self.cfg.mode,
            "title": title,
            "showMeta": self.cfg.show_meta,
        }) or {}

        for w in res.get("warnings") or []:
            self.log(f"  ⚠ {w}", "warn")

        if not res.get("ok"):
            raise PrintError("重排失败：页面里没有可搬运的题目",
                             "请确认当前标签页是试卷预览页，而不是列表页")

        c = res.get("counts") or {}
        self.log(f"✓ 已搬运 {c.get('questions', 0)} 题、{c.get('sections', 0)} 个分组"
                 f"（{res.get('timing', {}).get('ms', 0)} ms）", "ok")
        if c.get("noWrapper"):
            self.log(f"  ⚠ {c['noWrapper']} 题缺少 .wrapper.quesdiv，已退化为整题搬运", "warn")
        if c.get("blocked"):
            tip = ("请先点「预览打印内容」在浏览器里登录一次，再点「下载」"
                   if self.cfg.headless else "请先在浏览器里登录组卷网再重试")
            self.log(f"  ⚠ {c['blocked']} 个答案区显示「未登录 / 无权限」，"
                     f"导出会缺答案 —— {tip}", "warn")
        if self.cfg.mode == "qe":
            self.log(f"  答案后置：{c.get('answers', 0)} 块已移至卷末", "muted")

    # ------------------------------------------------------- 6 等待资源就绪
    def _wait_assets(self) -> None:
        self._step(6, "等待图片 / 字体就绪（事件驱动）")
        install = self._run_js(JS_INSTALL_WAITER, {
            "quietMs": self.cfg.quiet_ms,
            "imgTimeoutMs": self.cfg.img_timeout_ms,
            "minMs": 800,
        }) or {}
        if not install.get("installed"):
            self.log("  ⚠ 探针安装失败，跳过等待", "warn")
            return

        deadline = time.time() + self.cfg.ready_timeout
        last_report = 0.0
        last_sig = None

        while True:
            st = self.tab.run_js(JS_WAIT_STATUS) or {}
            if st.get("missing"):
                self.log("  ⚠ 探针丢失（页面可能被刷新），跳过等待", "warn")
                return

            sig = (st.get("pending"), st.get("failed"), st.get("total"))
            now = time.time()
            if sig != last_sig or now - last_report > 2:
                last_sig = sig
                last_report = now
                self.log(
                    f"  图片 {st.get('ok', 0)}/{st.get('total', 0)} 就绪"
                    f"｜进行中 {st.get('pending', 0)}｜失败 {st.get('failed', 0)}"
                    f"｜静止 {st.get('quiet', 0)}ms"
                    f"｜DOM 变更 {st.get('muts', 0)} 次", "muted")

            if st.get("ready"):
                self.log(f"✓ 页面已静止且资源就绪（耗时 {st.get('elapsed', 0)} ms）", "ok")
                if st.get("failed"):
                    self.log(f"  ⚠ {st['failed']} 张图片未能加载，打印中可能留白", "warn")
                return

            if now > deadline:
                self.log(f"  ⚠ 等待超过 {self.cfg.ready_timeout:.0f}s，"
                         f"仍有 {st.get('pending', 0)} 张图未完成，先打印", "warn")
                return
            time.sleep(0.2)

    # ---------------------------------------------------------- 7 打印
    def _do_print(self) -> dict:
        title = clean_title(self.cfg.title or self._probe().get("title", "") or "试卷")
        fname = suggested_pdf_name(title, self.cfg.mode)

        if self.cfg.window_print:
            self._step(7, "唤起浏览器原生打印预览")
            res = self._run_js(JS_PRINT, {"title": fname[:-4], "delay": 400}) or {}
            if not res.get("scheduled"):
                raise PrintError("无法唤起打印预览", "可在页面里手动按 Ctrl+P")
            self.log("✓ 打印预览已弹出", "ok")
            self.log(f"  在预览界面选择「另存为 PDF」，文件名已预填：{fname}", "muted")
            self.log(f"  建议保存到程序目录：{DEFAULT_OUT_DIR}", "muted")
            return {
                "mode": "window-print",
                "suggested_name": fname,
                "suggested_dir": str(DEFAULT_OUT_DIR),
            }

        self._step(7, "后台导出 PDF（无弹窗）")
        out = Path(self.cfg.pdf_path) if self.cfg.pdf_path else DEFAULT_OUT_DIR / fname
        self.log(f"  页面尺寸 A4，页边距 10mm，整题不跨页", "muted")
        path = self.export_pdf(out)
        self.log(f"✓ PDF 已保存：{path}（{path.stat().st_size / 1024:.0f} KB）", "ok")

        if self.cfg.close_browser_after:
            self._quit_browser()

        return {"mode": "pdf", "path": str(path), "suggested_name": fname,
                "suggested_dir": str(path.parent), "filename": path.name}

    def _quit_browser(self) -> None:
        """下载模式收尾：关掉自动化浏览器，不留后台窗口。"""
        self.log("  关闭浏览器…", "muted")
        try:
            if self.page is not None:
                self.page.quit()
            self.log("✓ 浏览器已关闭", "ok")
        except Exception as e:                                          # noqa: BLE001
            self.log(f"  ⚠ 关闭浏览器失败（不影响已下载的文件）：{e}", "warn")
        finally:
            self.page = None

    # ------------------------------------------------------------- 导出
    def export_pdf(self, path: Path) -> Path:
        """
        用 CDP 的 Page.printToPDF 生成 PDF。

        尺寸/边距全部交给注入的 @page 规则（preferCSSPageSize=True），
        所以这里 margins 设 0；纸张 A4、10mm 页边距由 CSS 决定。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.tab.run_cdp("Page.enable")
        except Exception:
            pass
        res = self.tab.run_cdp(
            "Page.printToPDF",
            printBackground=True,
            paperWidth=8.27, paperHeight=11.69,      # 兜底：CSS 没生效时按 A4
            marginTop=0, marginBottom=0, marginLeft=0, marginRight=0,
            preferCSSPageSize=True,                  # 采用 @page { size:A4; margin:10mm }
            displayHeaderFooter=False,
            scale=1,
        )
        if not isinstance(res, dict) or "data" not in res:
            raise PrintError("PDF 导出失败（CDP 无返回）", "可改用预览按钮手动另存为 PDF")
        path.write_bytes(base64.b64decode(res["data"]))
        return path

    # ------------------------------------------------------------- 其他
    def restore(self) -> None:
        """把页面恢复成原始形态（打印完想继续用这个标签页时调用）。"""
        try:
            self.tab.run_js(JS_RESTORE)
            self.log("✓ 页面已还原", "ok")
        except Exception:
            pass

    def close(self) -> None:
        """只断开连接，不杀浏览器（否则打印预览会一起消失）。"""
        if self.page is not None and not self.cfg.keep_browser:
            try:
                self.page.quit()
            except Exception:
                pass
        self.page = None


# --------------------------------------------------------------------------
# 内置模拟试卷（--self-test 用，无需登录即可验证全流程）
# --------------------------------------------------------------------------

_MOCK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>2025 届高三数学模拟试卷（本地测试）</title>
<style>
 body{margin:0;font-family:sans-serif;background:#f5f6f8}
 .top-nav{height:56px;background:#1b1f2a;color:#fff;display:flex;align-items:center;padding:0 24px}
 .side-ad{position:fixed;right:0;top:120px;width:160px;height:400px;background:#ffe9a8;z-index:9}
 .layout{display:flex;gap:20px;padding:20px 200px 20px 24px}
 .main{flex:1;background:#fff;padding:24px}
 .foot{height:80px;background:#222;color:#999;margin-top:20px}
 .exam-item__opt{display:none}
 .toolbar{margin:10px 0}
</style></head>
<body>
<div class="top-nav">组卷网 · 模拟页面（用于本地自检）</div>
<div class="side-ad">广告位</div>
<div class="layout"><div class="main">
  <h1>2025 届高三数学模拟试卷</h1>
  <div class="toolbar">
    <input type="checkbox" id="isshowAnswer"><label for="isshowAnswer">显示答案</label>
    <span class="user-nickname" style="display:none">测试用户</span>
  </div>

  <div class="sec-title"><span>一、单选题（本题共 8 小题，每小题 5 分）</span></div>

  <div class="tk-quest-item quesroot"><div class="wrapper quesdiv">
    <div class="exam-item__cnt"><p>1．已知集合 A={x|x²-3x+2=0}，则 A 的子集个数为（　　）</p>
      <p>A．2　　B．3　　C．4　　D．5</p></div>
    <div class="exam-item__opt"><p><b>【答案】C</b></p>
      <p>【解析】A={1,2}，子集个数 2²=4。</p>
      <div class="knowledge-box">知识点：集合</div></div>
  </div></div>

  <div class="tk-quest-item quesroot"><div class="wrapper quesdiv">
    <div class="exam-item__cnt"><p>2．函数 y=ax²+bx+c 的图象如图所示，下列结论正确的是（　　）</p>
      <p>A．a&gt;0　　B．b&gt;0　　C．c&lt;0　　D．b²-4ac&lt;0</p>
      <p><img alt="题图" src="data:image/svg+xml;utf8,&lt;svg xmlns='http://www.w3.org/2000/svg' width='320' height='140'&gt;&lt;rect width='320' height='140' fill='%23eef2ff'/&gt;&lt;path d='M20 120 Q160 -40 300 120' stroke='%232f6bff' stroke-width='3' fill='none'/&gt;&lt;/svg&gt;"></p></div>
    <div class="exam-item__opt"><p><b>【答案】A</b></p>
      <p>【解析】抛物线开口向上，故 a&gt;0。</p></div>
  </div></div>

  <div class="sec-title"><span>二、解答题（本题共 2 小题，共 24 分）</span></div>

  <div class="tk-quest-item quesroot"><div class="wrapper quesdiv">
    <div class="exam-item__cnt"><p>3．在△ABC 中，角 A、B、C 的对边分别为 a、b、c，且 a=3，b=4，C=60°，求 c 及△ABC 的面积。</p>
      <p><img alt="解析图" src="data:image/svg+xml;utf8,&lt;svg xmlns='http://www.w3.org/2000/svg' width='240' height='120'&gt;&lt;rect width='240' height='120' fill='%23fff7ed'/&gt;&lt;circle cx='40' cy='90' r='4' fill='%23ea580c'/&gt;&lt;circle cx='200' cy='90' r='4' fill='%23ea580c'/&gt;&lt;circle cx='60' cy='30' r='4' fill='%23ea580c'/&gt;&lt;/svg&gt;"></p></div>
    <div class="exam-item__opt"><p><b>【答案】c=√13，S=3√3</b></p>
      <p>【解析】由余弦定理 c²=a²+b²-2abcosC=9+16-12=13，故 c=√13；面积 S=½absinC=3√3。</p></div>
  </div></div>

  <div class="tk-quest-item quesroot"><div class="wrapper quesdiv">
    <div class="exam-item__cnt"><p>4．已知数列 {aₙ} 满足 a₁=1，aₙ₊₁=2aₙ+1，求通项公式。</p></div>
    <div class="exam-item__opt"><p><b>【答案】aₙ=2ⁿ-1</b></p>
      <p>【解析】aₙ₊₁+1=2(aₙ+1)，故 {aₙ+1} 是首项为 2、公比为 2 的等比数列。</p>
      <img alt="答案图" src="data:image/svg+xml;utf8,&lt;svg xmlns='http://www.w3.org/2000/svg' width='360' height='60'&gt;&lt;rect width='360' height='60' fill='%23f0fdf4'/&gt;&lt;text x='12' y='38' font-size='18' fill='%2315803d'&gt;a_n = 2^n - 1&lt;/text&gt;&lt;/svg&gt;"></div>
  </div></div>
</div></div>
<div class="foot">页面底部 · 版权信息（打印时应当被隐藏）</div>
<script>
  // 模拟站点行为：勾选后才把答案 display 打开
  document.getElementById('isshowAnswer').addEventListener('change', function (e) {
    document.querySelectorAll('.exam-item__opt').forEach(function (el) {
      el.style.display = e.target.checked ? 'block' : 'none';
    });
  });
</script>
</body></html>
"""


def self_test(pdf: bool = True, headless: bool = False) -> int:
    """用内置模拟页跑一遍完整流程，验证 DOM 逻辑而不需要登录。"""
    mock_dir = APP_DIR / "_selftest"
    mock_dir.mkdir(exist_ok=True)
    html = mock_dir / "mock_paper.html"
    html.write_text(_MOCK_HTML, encoding="utf-8")

    out = mock_dir / "selftest.pdf" if pdf else None
    cfg = PrintConfig(
        url=html.as_uri(),
        font_key="Microsoft YaHei",
        size_pt=12,
        mode="qa",
        headless=headless,
        window_print=not pdf,
        pdf_path=out,
        login_timeout=30,
        ready_timeout=20,
        title="自检 · 2025 届高三数学模拟试卷",
    )
    printer = ZujuanPrinter(cfg)
    try:
        result = printer.run()
    finally:
        printer.close()

    if pdf:
        assert out.exists(), "PDF 未生成"
        size = out.stat().st_size
        assert size > 8 * 1024, f"PDF 太小（{size} B），可能内容为空"
        print(f"\n[自检通过] {out}  ({size/1024:.0f} KB)")
        print(f"打开查看：{out}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    s = input(prompt).strip()
    return s or default


def _tame_stdout() -> None:
    """Windows 控制台可能是 GBK，避免个别符号（✓/✗）触发 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # 保留原编码，只放宽错误处理
        except Exception:
            pass


def main(argv=None) -> int:
    _tame_stdout()
    ap = argparse.ArgumentParser(
        description="组卷网试卷重排打印（无 UI 的 core 版，便于排查问题）",
    )
    ap.add_argument("url", nargs="?", help="试卷网址")
    ap.add_argument("--font", default=None, help="字体 key 或名称，如 SimSun / 宋体")
    ap.add_argument("--size", type=float, default=None, help="字号 pt，如 12")
    ap.add_argument("--mode", default="qa", choices=list(MODE_OPTIONS), help="打印模式")
    ap.add_argument("--pdf", nargs="?", const=str(DEFAULT_OUT_DIR / "试卷.pdf"),
                    default=None, help="导出 PDF 到指定路径（不给路径则用默认名）")
    ap.add_argument("--headless", action="store_true", help="无头模式（调试用）")
    ap.add_argument("--port", type=int, default=None, help="接管 127.0.0.1:PORT 已开的浏览器")
    ap.add_argument("--login-timeout", type=float, default=240, help="等待登录/渲染的秒数")
    ap.add_argument("--self-test", action="store_true", help="用内置模拟试卷自检")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test(pdf=args.pdf is not None, headless=args.headless)

    url = args.url
    if not url:
        print("\n请输入试卷网址（组卷网试卷预览页）：")
        url = _ask("> ")
    if not url:
        print("没有网址，退出。")
        return 2

    font = args.font
    if not font:
        print("\n可选字体：")
        for i, f in enumerate(FONT_OPTIONS, 1):
            print(f"  {i}. {f['label']}")
        font = _ask("选择字体 [1-6，默认 1]：", "1")
        if font.isdigit() and 1 <= int(font) <= len(FONT_OPTIONS):
            font = FONT_OPTIONS[int(font) - 1]["key"]

    size = args.size
    if size is None:
        size = float(_ask("字号 pt [默认 12]：", "12"))

    cfg = PrintConfig(
        url=url,
        font_key=font,
        size_pt=size,
        mode=args.mode,
        headless=args.headless,
        port=args.port,
        login_timeout=args.login_timeout,
        window_print=args.pdf is None,
        pdf_path=Path(args.pdf) if args.pdf else None,
    )

    printer = ZujuanPrinter(cfg)
    try:
        printer.run()
    except PrintError as e:
        print(f"\n失败：{e}")
        if e.hint:
            print(f"建议：{e.hint}")
        return 1
    finally:
        printer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
