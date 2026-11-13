#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py —— 组卷网试卷打印助手（桌面端 UI）
==========================================

UI 用 pywebview（WebView2 内核）+ 原生 HTML/CSS 实现：
  * 要「内阴影、渐变、悬浮光影过渡、1px 极浅分隔线」这类质感，
    直接用 CSS 控制比用 Flutter 控件拼要精确得多；
  * WebView2 是 Windows 自带的（随 Edge 安装），不需要额外下载运行时。

    pip install drissionpage pywebview

运行：
    python app.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

try:
    import webview
except ImportError:  # pragma: no cover - 首次运行会走到这里
    raise SystemExit(
        "缺少依赖 pywebview。\n"
        "请先执行：pip install drissionpage pywebview\n"
        "（pywebview 使用系统自带的 WebView2 内核，Win10/11 通常无需额外安装）"
    ) from None

from core import (
    APP_DIR,
    DEFAULT_OUT_DIR,
    FONT_OPTIONS,
    MODE_OPTIONS,
    SIZE_OPTIONS,
    PrintConfig,
    PrintError,
    ZujuanPrinter,
)

# 打印内容下拉框的默认选项（完整选项来自 core.MODE_OPTIONS：q / qa / qe / a）
DEFAULT_MODE = "qa"

WINDOW_W, WINDOW_H = 566, 892

# 说明：这里不用 pywebview 的 transparent=True。
# WinForms 顶层窗体本身不支持真透明，开启后四角会露出窗体的默认底色（发白），
# 深色主题下尤其明显。改成不透明窗口 + 整窗铺满渐变，四角自然不会再有白块；
# Win11 的 DWM 仍会自己给窗口加圆角，观感不受影响。
WINDOW_TRANSPARENT = False


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>组卷网试卷打印助手</title>
<style>
  :root{
    --brand:#2F6BFF;
    --brand-2:#3B82F6;
    --brand-3:#1D4ED8;
    --ink:#101828;
    --ink-2:#475467;
    --ink-3:#98A2B3;
    --line:rgba(16,24,40,.06);
    --card:#FFFFFF;
    --card-shadow:rgba(16,24,40,.16);
    --hover:rgba(16,24,40,.06);
    --ease:cubic-bezier(.2,.8,.2,1);

    /* 背景 / 控件 / 文本，暗色主题只改这几个变量 */
    --bg-a:#F4F7FC; --bg-b:#EEF2F9; --bg-c:#FFFFFF;
    --frame-ring:rgba(255,255,255,.85);
    --frame-ring-2:rgba(16,24,40,.03);
    --hero-a:#1B2436; --hero-b:#3B4A63;
    --field:#F5F7FB;
    --field-hover:#F2F5FA;
    --field-focus:#FFFFFF;
    --field-line:rgba(16,24,40,.05);
    --field-inner:rgba(16,24,40,.045);
    --placeholder:#AEB7C6;
    --panel:rgba(255,255,255,.72);
    --panel-shadow:rgba(16,24,40,.5);
    --log:#475467;
    --ts:#B4BCC9;
    --lv-ok:#067647; --lv-err:#B42318; --lv-warn:#B54708;
    --lv-muted:#98A2B3; --lv-step:#175CD3;
    --banner-ok-bg:#ECFDF3; --banner-ok-fg:#067647; --banner-ok-ring:rgba(18,183,106,.16);
    --banner-err-bg:#FEF3F2; --banner-err-fg:#B42318; --banner-err-ring:rgba(240,68,56,.16);
    --chevron:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'><path d='M1 1.5 6 6.5 11 1.5' fill='none' stroke='%2398A2B3' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>");

    /* 窗口底色 / 下拉菜单弹层底色（必须不透明） */
    --page-bg:#F4F7FC;
    --menu:#FFFFFF;
    --menu-hover:#F2F5FA;
    --menu-line:rgba(16,24,40,.10);
  }
  *{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html,body{ height:100%; margin:0; }
  html{ color-scheme:light; }
  body{
    font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,-apple-system,sans-serif;
    color:var(--ink);
    background:var(--page-bg);          /* 整窗铺底，避免四角露出窗体白底 */
    overflow:hidden;
    user-select:none;
    -webkit-font-smoothing:antialiased;
    transition:background .42s var(--ease);
  }

  /* ---------- 外框 ---------- */
  .app{
    position:relative;
    width:100%; height:100%;
    /* 无边框窗口不再做圆角：Win11 由 DWM 负责，Win10 则整窗铺满，两者都不会留白角 */
    border-radius:0;
    background:
      radial-gradient(120% 80% at 8% 0%, #FFFFFF 0%, rgba(255,255,255,0) 60%),
      linear-gradient(158deg,var(--bg-a) 0%,var(--bg-b) 42%,var(--bg-c) 100%);
    box-shadow:inset 0 0 0 1px var(--frame-ring), inset 0 0 0 2px var(--frame-ring-2);
    display:flex; flex-direction:column;
    overflow:hidden;
    transition:box-shadow .4s var(--ease);
  }
  /* 顶部一层极淡的高光，避免大面积纯色发闷 */
  .app::before{
    content:""; position:absolute; inset:0 0 auto 0; height:180px;
    background:radial-gradient(60% 100% at 50% 0%, rgba(47,107,255,.10), transparent 70%);
    pointer-events:none;
  }
  /* 暗色主题：一层会淡入的深色渐变，保证切换是平滑过渡而不是硬跳 */
  .app::after{
    content:""; position:absolute; inset:0; z-index:0; pointer-events:none;
    background:
      radial-gradient(70% 100% at 50% 0%, rgba(64,120,255,.22), transparent 72%),
      linear-gradient(158deg,#151D30 0%,#0E1524 46%,#080C15 100%);
    opacity:0;
    transition:opacity .42s var(--ease);
  }
  body.dark .app::after{ opacity:1; }

  /* ---------- 标题栏 ---------- */
  .titlebar{
    position:relative; z-index:3;
    height:46px; flex:0 0 46px;
    display:flex; align-items:center; gap:10px;
    padding:0 10px 0 20px;
  }
  .brand{ display:flex; align-items:center; gap:9px; }
  .brand .dot{
    width:9px; height:9px; border-radius:50%;
    background:linear-gradient(135deg,#5B9BFF,#2F6BFF);
    box-shadow:0 0 0 4px rgba(47,107,255,.12);
  }
  .brand .name{ font-size:13px; font-weight:600; color:var(--ink-2); letter-spacing:.3px; }
  .drag-fill{ flex:1 1 auto; align-self:stretch; cursor:default; }
  .win-ctrl{ display:flex; gap:4px; align-items:center; }
  .win-ctrl button{
    width:30px; height:30px; border:0; border-radius:9px;
    background:transparent; color:#7A8699; cursor:pointer;
    display:grid; place-items:center;
    transition:background .18s var(--ease), color .18s var(--ease), transform .18s var(--ease);
  }
  .win-ctrl button:hover{ background:var(--hover); color:var(--ink); }
  .win-ctrl button:active{ transform:scale(.94); }
  .win-ctrl button.close:hover{ background:#FEE4E2; color:#D92D20; }
  .win-ctrl svg{ width:14px; height:14px; }

  /* ---------- 主题开关（圆圈 · 太阳 / 月亮） ---------- */
  .theme{
    position:relative;
    width:30px; height:30px; padding:0;
    border:0; border-radius:50%;           /* 正圆 */
    background:transparent; color:#7A8699;
    cursor:pointer; display:grid; place-items:center;
    transition:background .22s var(--ease), color .22s var(--ease),
               transform .25s var(--ease), box-shadow .22s var(--ease);
  }
  .theme:hover{
    color:var(--ink);
    background:var(--hover);
    box-shadow:inset 0 0 0 1px var(--line);
  }
  .theme:active{ transform:scale(.9); }
  .theme:focus-visible{ outline:0; box-shadow:0 0 0 3px rgba(47,107,255,.18); }
  .theme svg{
    position:absolute; width:15px; height:15px;
    transition:opacity .28s var(--ease), transform .46s var(--ease);
  }
  .theme .moon{ opacity:0; transform:rotate(-70deg) scale(.55); }
  body.dark .theme .sun{ opacity:0; transform:rotate(70deg) scale(.55); }
  body.dark .theme .moon{ opacity:1; transform:rotate(0) scale(1); }
  body.dark .theme{ color:#C7D2E5; }

  /* ---------- 主体 ---------- */
  .body{
    position:relative; z-index:2;
    flex:1 1 auto; min-height:0;
    padding:6px 24px 0;
    display:flex; flex-direction:column;
  }

  .hero{ padding:12px 4px 20px; }
  .hero h1{
    margin:0 0 8px; font-size:23px; line-height:1.34; letter-spacing:-.2px;
    font-weight:700;
    background:linear-gradient(180deg,var(--hero-a) 0%,var(--hero-b) 100%);
    -webkit-background-clip:text; background-clip:text; color:transparent;
    transition:background .4s var(--ease);
  }
  .hero p{ margin:0; font-size:13px; color:var(--ink-3); line-height:1.7; }

  /* ---------- 表单卡片 ---------- */
  .card{
    background:var(--card);
    border-radius:18px;
    padding:20px 20px 22px;
    box-shadow:
      0 1px 2px rgba(16,24,40,.04),
      0 14px 40px -18px var(--card-shadow),
      inset 0 0 0 1px var(--line);
    transition:background .42s var(--ease), box-shadow .42s var(--ease);
  }
  .field + .field{ margin-top:16px; }
  .field > label{
    display:block; font-size:11.5px; font-weight:600; letter-spacing:.6px;
    color:var(--ink-3); margin:0 0 8px 2px;
  }
  .row2{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }

  /* 字号 / 字体 实时预览 */
  .preview{
    margin-top:16px;
    padding:12px 16px;
    min-height:64px;
    display:flex; align-items:center; gap:6px;
    border-radius:14px;
    background:var(--field);
    color:var(--ink);
    box-shadow:
      inset 0 0 0 1px var(--field-line),
      inset 0 2px 4px var(--field-inner);
    overflow:hidden;
    transition:background .3s var(--ease), color .3s var(--ease), box-shadow .3s var(--ease);
  }
  .preview-lbl{
    flex:0 0 auto;
    font-size:11.5px; font-weight:600; letter-spacing:.6px;
    color:var(--ink-3);
  }
  #preview{
    flex:1 1 auto; min-width:0;
    color:var(--ink);
    line-height:1.35;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    transition:color .3s var(--ease);
  }

  /* 输入框：无边框 + 柔和内阴影 */
  .input-wrap{ position:relative; }
  input[type=text], select{
    width:100%; height:48px;
    font:inherit; font-size:14px; color:var(--ink);
    border:0; outline:0; border-radius:14px;
    padding:0 16px;
    background:var(--field);
    box-shadow:
      inset 0 0 0 1px var(--field-line),
      inset 0 2px 4px var(--field-inner);
    transition:box-shadow .22s var(--ease), background .3s var(--ease), color .3s var(--ease);
    appearance:none;
  }
  input::placeholder{ color:var(--placeholder); transition:color .3s var(--ease); }
  input[type=text]:hover, select:hover{ background:var(--field-hover); }
  input[type=text]:focus, select:focus{
    background:var(--field-focus);
    box-shadow:
      inset 0 0 0 1px rgba(47,107,255,.42),
      inset 0 2px 4px rgba(16,24,40,.03),
      0 0 0 4px rgba(47,107,255,.10);
  }
  select{
    cursor:pointer;
    padding-right:40px;
    background-image:var(--chevron);
    background-repeat:no-repeat;
    background-position:right 16px center;
    /* 关键：让原生下拉弹层跟随主题（Chrome/WebView2 会按 color-scheme 渲染弹层底色） */
    color-scheme:light;
  }
  body.dark select{ color-scheme:dark; }
  /* 支持自定义 select 弹层的内核（Chrome 135+）直接给选项上色，双保险 */
  option{
    background-color:var(--menu);
    color:var(--ink);
    font-size:14px;
    padding:8px 10px;
  }
  option:checked,
  option:hover{ background-color:var(--menu-hover); color:var(--ink); }
  optgroup{ background-color:var(--menu); color:var(--ink-3); }

  /* ---------- 按钮组：左「预览打印内容」右「下载」 ---------- */
  .btn-row{
    margin-top:20px;
    display:grid;
    grid-template-columns:1.34fr 1fr;
    gap:10px;
  }
  .btn{
    height:54px;
    border:0; border-radius:16px; cursor:pointer;
    font:inherit; font-size:14.5px; font-weight:600; letter-spacing:.3px;
    display:flex; align-items:center; justify-content:center; gap:8px;
    position:relative; overflow:hidden;
    transition:transform .22s var(--ease), box-shadow .22s var(--ease),
               filter .22s var(--ease), background .22s var(--ease), color .22s var(--ease);
  }
  .btn svg{ width:16px; height:16px; flex:0 0 auto; }

  /* 左：次要按钮（浅底 + 品牌色文字） */
  .btn-ghost{
    color:var(--brand);
    background:rgba(47,107,255,.09);
    box-shadow:inset 0 0 0 1px rgba(47,107,255,.18);
  }
  .btn-ghost:hover{
    background:rgba(47,107,255,.15);
    box-shadow:inset 0 0 0 1px rgba(47,107,255,.3), 0 8px 20px -14px rgba(47,107,255,.8);
    transform:translateY(-1px);
  }
  .btn-ghost:active{ transform:translateY(0) scale(.994); }
  body.dark .btn-ghost{ color:#9CBBFF; background:rgba(47,107,255,.16);
    box-shadow:inset 0 0 0 1px rgba(120,160,255,.24); }
  body.dark .btn-ghost:hover{ background:rgba(47,107,255,.24); }

  /* 右：主按钮（品牌渐变） */
  .btn-primary{
    color:#fff;
    background:linear-gradient(135deg,#5B9BFF 0%,#2F6BFF 46%,#1D4ED8 100%);
    box-shadow:
      0 1px 1px rgba(255,255,255,.35) inset,
      0 12px 26px -10px rgba(47,107,255,.70),
      0 2px 6px rgba(47,107,255,.18);
  }
  .btn-primary::after{ /* 掠过式高光 */
    content:""; position:absolute; top:0; left:-60%; width:40%; height:100%;
    background:linear-gradient(100deg,transparent,rgba(255,255,255,.35),transparent);
    transform:skewX(-18deg); transition:left .55s var(--ease);
  }
  .btn-primary:hover{ filter:brightness(1.06); transform:translateY(-1.5px);
    box-shadow:0 1px 1px rgba(255,255,255,.4) inset,
               0 18px 34px -12px rgba(47,107,255,.78),
               0 3px 8px rgba(47,107,255,.22); }
  .btn-primary:hover::after{ left:120%; }
  .btn-primary:active{ transform:translateY(0) scale(.994); filter:brightness(.98); }

  .btn:disabled{ cursor:default; transform:none !important; }
  .btn-ghost:disabled{ color:var(--ink-3); background:var(--field);
    box-shadow:inset 0 0 0 1px var(--field-line); }
  .btn-primary:disabled{ filter:grayscale(.25) brightness(.97);
    box-shadow:0 8px 18px -12px rgba(47,107,255,.4); }
  .btn:disabled::after{ display:none; }

  .spinner{
    width:15px; height:15px; border-radius:50%;
    border:2px solid currentColor; border-top-color:transparent; opacity:.75;
    animation:spin .8s linear infinite; display:none;
  }
  .btn.loading .spinner{ display:block; }
  @keyframes spin{ to{ transform:rotate(360deg); } }

  /* ---------- 状态区 ---------- */
  .panel{
    margin-top:16px; flex:1 1 auto; min-height:0;
    display:flex; flex-direction:column;
    background:var(--panel);
    border-radius:16px;
    box-shadow:inset 0 0 0 1px var(--line), 0 10px 30px -22px var(--panel-shadow);
    overflow:hidden;
    transition:background .42s var(--ease);
  }
  .panel-empty{
    flex:1 1 auto; display:flex; flex-direction:column;
    align-items:center; justify-content:center; gap:6px;
    padding:20px; text-align:center;
    font-size:12px; line-height:1.7; color:var(--ink-3);
    transition:color .35s var(--ease);
  }
  .panel-empty .kbd{
    font-size:11px; color:var(--ink-3); opacity:.75;
  }
  body.busy .panel-empty, body.done .panel-empty, body.error .panel-empty{ display:none; }

  .panel-head{
    flex:0 0 auto;
    display:flex; align-items:center; gap:9px;
    padding:12px 16px;
    border-bottom:1px solid var(--line);   /* 1px 极浅分隔线 */
    font-size:12px; font-weight:600; color:var(--ink-2);
  }
  .status-dot{
    width:7px; height:7px; border-radius:50%; background:#12B76A;
    box-shadow:0 0 0 3px rgba(18,183,106,.14);
    transition:background .2s;
  }
  body.error .status-dot{ background:#F04438; box-shadow:0 0 0 3px rgba(240,68,56,.14); }
  body.busy .status-dot{ background:#2F6BFF; box-shadow:0 0 0 3px rgba(47,107,255,.16);
    animation:pulse 1.2s ease-in-out infinite; }
  @keyframes pulse{ 50%{ opacity:.35; } }

  .progress{
    flex:0 0 auto; height:2px; background:rgba(47,107,255,.10); overflow:hidden;
  }
  .progress i{
    display:block; height:100%; width:36%;
    background:linear-gradient(90deg,transparent,#2F6BFF,transparent);
    animation:slide 1.15s linear infinite; display:none;
  }
  body.busy .progress i{ display:block; }
  @keyframes slide{ from{ transform:translateX(-100%);} to{ transform:translateX(320%);} }

  .log{
    flex:1 1 auto; min-height:0; overflow:auto;
    padding:10px 14px 14px;
    font-family:"Cascadia Mono",Consolas,"SF Mono",monospace;
    font-size:11.5px; line-height:1.85; color:var(--log);
    user-select:text;
    transition:color .35s var(--ease);
  }
  .log::-webkit-scrollbar{ width:8px; }
  .log::-webkit-scrollbar-thumb{ background:var(--hover); border-radius:8px; }
  .log .ln{ display:flex; gap:8px; white-space:pre-wrap; word-break:break-all; }
  .log .ts{ color:var(--ts); flex:0 0 auto; }
  .log .lv-ok{ color:var(--lv-ok); }
  .log .lv-err{ color:var(--lv-err); }
  .log .lv-warn{ color:var(--lv-warn); }
  .log .lv-muted{ color:var(--lv-muted); }
  .log .lv-step{ color:var(--lv-step); font-weight:600; }

  /* ---------- 底部 ---------- */
  .foot{
    flex:0 0 auto;
    margin-top:auto;                       /* 空闲时把页脚压到底部 */
    padding:12px 4px 16px;
    display:flex; align-items:center; gap:8px;
    font-size:11.5px; color:var(--ink-3);
  }
  .foot b{ color:var(--ink-2); font-weight:600; }
  .link{
    color:var(--brand); cursor:pointer; text-decoration:none; font-weight:600;
    border-radius:6px; padding:2px 4px; transition:background .18s var(--ease);
  }
  .link:hover{ background:rgba(47,107,255,.08); }
  .path{
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    max-width:250px; direction:rtl; text-align:left;
  }

  /* 结果提示条 */
  .banner{
    display:none; margin-top:12px;
    padding:11px 14px; border-radius:12px;
    font-size:12.5px; line-height:1.6;
    background:var(--banner-ok-bg); color:var(--banner-ok-fg);
    box-shadow:inset 0 0 0 1px var(--banner-ok-ring);
    transition:background .35s var(--ease), color .35s var(--ease);
  }
  body.done .banner{ display:block; }
  body.error .banner{
    display:block; background:var(--banner-err-bg); color:var(--banner-err-fg);
    box-shadow:inset 0 0 0 1px var(--banner-err-ring);
  }
  .banner b{ font-weight:700; }

  /* ---------- 暗色主题 ---------- */
  body.dark{ color-scheme:dark; }
  body.dark{
    --ink:#E9EEF7;
    --ink-2:#B7C2D4;
    --ink-3:#74829A;
    --line:rgba(255,255,255,.09);
    --card:rgba(255,255,255,.055);
    --card-shadow:rgba(0,0,0,.55);
    --hover:rgba(255,255,255,.08);
    --frame-ring:rgba(255,255,255,.10);
    --frame-ring-2:rgba(255,255,255,.03);
    --hero-a:#F2F6FF; --hero-b:#A9B8D4;
    --field:rgba(255,255,255,.06);
    --field-hover:rgba(255,255,255,.085);
    --field-focus:rgba(255,255,255,.10);
    --field-line:rgba(255,255,255,.10);
    --field-inner:rgba(0,0,0,.35);
    --placeholder:#66748C;
    --panel:rgba(255,255,255,.045);
    --panel-shadow:rgba(0,0,0,.75);
    --log:#AEBACE;
    --ts:#5B6880;
    --lv-ok:#5BD9A2; --lv-err:#FF9C93; --lv-warn:#FFC16B;
    --lv-muted:#68758C; --lv-step:#7FB2FF;
    --banner-ok-bg:rgba(18,183,106,.14); --banner-ok-fg:#6FE0AE;
    --banner-ok-ring:rgba(18,183,106,.26);
    --banner-err-bg:rgba(240,68,56,.14); --banner-err-fg:#FFA79F;
    --banner-err-ring:rgba(240,68,56,.26);
    --page-bg:#080C15;
    --menu:#1A2337;
    --menu-hover:#232E48;
    --menu-line:rgba(255,255,255,.12);
    --chevron:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'><path d='M1 1.5 6 6.5 11 1.5' fill='none' stroke='%23A9B6CC' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  }
  body.dark .app{ box-shadow:inset 0 0 0 1px var(--frame-ring), inset 0 0 0 2px var(--frame-ring-2); }
  body.dark .btn-primary{ filter:saturate(1.04) brightness(1.02); }
  body.dark .win-ctrl button.close:hover{ background:rgba(217,45,32,.22); color:#FF8F86; }

  /* 文字颜色跟随主题平滑过渡 */
  .titlebar, .brand .name, .hero p, .field > label, .panel-head, .foot{
    transition:color .35s var(--ease), background .35s var(--ease);
  }
</style>
</head>
<body>
<div class="app">

  <div class="titlebar">
    <div class="brand"><span class="dot"></span><span class="name">试卷打印助手</span></div>
    <!-- 只有这条空白区域可以拖动窗口，避免拖动时误触输入框 -->
    <div class="drag-fill pywebview-drag-region"></div>
    <div class="win-ctrl">
      <button id="theme" class="theme" title="切换深色 / 浅色" aria-label="切换主题">
        <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.8" stroke-linecap="round">
          <circle cx="12" cy="12" r="4.1"/>
          <path d="M12 2.7v2.2M12 19.1v2.2M2.7 12h2.2M19.1 12h2.2
                   M5.5 5.5l1.55 1.55M16.95 16.95l1.55 1.55
                   M18.5 5.5l-1.55 1.55M7.05 16.95L5.5 18.5"/>
        </svg>
        <svg class="moon" viewBox="0 0 24 24" fill="currentColor">
          <path d="M20.4 14.9a8.9 8.9 0 1 1-11.3-11.3 7.2 7.2 0 0 0 11.3 11.3z"/>
        </svg>
      </button>
      <button id="btn-min" title="最小化">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 8h10"/></svg>
      </button>
      <button id="btn-close" class="close" title="关闭">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>
      </button>
    </div>
  </div>

  <div class="body">
    <div class="hero">
      <h1>把在线试卷，<br>变成一张能直接打印的卷子</h1>
      <p>自动展开答案 · 去掉导航与广告 · 图片全部就绪后才唤起打印</p>
    </div>

    <div class="card">
      <div class="field">
        <label for="url">试卷网址</label>
        <div class="input-wrap">
          <input id="url" type="text" spellcheck="false" autocomplete="off"
                 placeholder="https://zujuan.xkw.com/… 试卷预览页链接">
        </div>
      </div>

      <div class="field">
        <label for="mode">打印内容</label>
        <select id="mode"><!--MODES--></select>
      </div>

      <div class="field row2">
        <div>
          <label for="size">字号</label>
          <select id="size"><!--SIZES--></select>
        </div>
        <div>
          <label for="font">字体</label>
          <select id="font"><!--FONTS--></select>
        </div>
      </div>

      <div class="preview">
        <span class="preview-lbl">字号预览：</span>
        <span id="preview">函数 y = ax² + bx + c</span>
      </div>

      <div class="btn-row">
        <button class="btn btn-ghost" id="btn-preview" title="打开浏览器原生打印预览">
          <span class="spinner"></span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M6 9V3h12v6"/>
            <path d="M6 18H4.6A1.6 1.6 0 0 1 3 16.4v-5.8A1.6 1.6 0 0 1 4.6 9h14.8A1.6 1.6 0 0 1 21 10.6v5.8A1.6 1.6 0 0 1 19.4 18H18"/>
            <rect x="6" y="14" width="12" height="7" rx="1.4"/>
          </svg>
          <span class="lbl">预览打印内容</span>
        </button>

        <button class="btn btn-primary" id="btn-download" title="后台静默生成 PDF 并保存到程序目录">
          <span class="spinner"></span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 3.5v11"/>
            <path d="M7.5 10.5 12 15l4.5-4.5"/>
            <path d="M4.5 17.5v1.6A1.4 1.4 0 0 0 5.9 20.5h12.2a1.4 1.4 0 0 0 1.4-1.4v-1.6"/>
          </svg>
          <span class="lbl">下载</span>
        </button>
      </div>
    </div>

    <div class="banner" id="banner"></div>

    <div class="panel">
      <div class="panel-head">
        <span class="status-dot"></span>
        <span id="panel-title">准备就绪</span>
      </div>
      <div class="progress"><i></i></div>
      <div class="panel-empty" id="panel-empty">
        <span>处理进度会实时显示在这里</span>
        <span class="kbd">首次运行会打开 Chrome 让你扫码登录，之后不用再登</span>
      </div>
      <div class="log" id="log"></div>
    </div>

    <div class="foot">
      <span>文件保存到</span>
      <span class="path" id="outdir" title=""></span>
      <a class="link" id="open-folder">打开文件夹</a>
    </div>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const outDir = __OUTDIR__;
const FONTS = __FONTS__;
const THEME_KEY = 'zjp-theme';

let seq = 0, timer = null, startedAt = 0;
let py = null;

function applyTheme(name) {
  document.body.classList.toggle('dark', name === 'dark');
}

function currentTheme() {
  return document.body.classList.contains('dark') ? 'dark' : 'light';
}

function api() { return (window.pywebview && window.pywebview.api) || null; }

function setState(cls) {
  document.body.classList.remove('busy', 'done', 'error');
  if (cls) document.body.classList.add(cls);
}

function addLog(item) {
  const log = $('#log');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const div = document.createElement('div');
  div.className = 'ln';
  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = item.t;
  const tx = document.createElement('span');
  tx.className = 'lv-' + (item.lv || 'info');
  tx.textContent = item.m;
  div.appendChild(ts); div.appendChild(tx);
  log.appendChild(div);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function banner(html, isError) {
  $('#banner').innerHTML = html;
  if (isError) document.body.classList.add('error');
}

const BTNS = { preview: '#btn-preview', download: '#btn-download' };
const DONE_LABEL = { preview: '预览打印内容', download: '下载' };

function setBusy(on, label, action) {
  for (const sel of Object.values(BTNS)) $(sel).disabled = on;
  // 只让被点击的那个按钮显示 spinner
  for (const [act, sel] of Object.entries(BTNS)) {
    const btn = $(sel);
    btn.classList.toggle('loading', on && act === action);
    btn.querySelector('.lbl').textContent = (on && act === action && label)
      ? label : DONE_LABEL[act];
  }
  if (on) { setState('busy'); $('#panel-title').textContent = '正在处理…'; }
}

function validate(url) {
  if (!url) return { block: true, msg: '请先粘贴试卷网址' };
  if (!/^https?:\/\//i.test(url))
    return { block: true, msg: '网址需要以 http:// 或 https:// 开头' };
  return { block: false, msg: '' };
}

async function poll() {
  const a = api(); if (!a) return;
  let r;
  try { r = await a.poll(seq); } catch (e) { return; }
  if (!r) return;
  // 后端已按 since 过滤；这里再去重一次，防止同一条被追加两遍
  (r.logs || []).forEach((it) => {
    if (it.i <= seq) return;
    addLog(it);
    seq = it.i;
  });
  if (r.state && r.state.step) $('#panel-title').textContent = r.state.step;

  if (r.state && r.state.done) {
    clearInterval(timer); timer = null;
    setBusy(false);
    if (r.state.ok) {
      setState('done');
      $('#panel-title').textContent = r.state.title || '已完成';
      banner('<b>' + (r.state.msg || '完成') + '</b><br>' + (r.state.hint || ''), false);
    } else {
      setState('error');
      $('#panel-title').textContent = '处理失败';
      banner('<b>' + (r.state.msg || '失败') + '</b><br>' + (r.state.hint || ''), true);
    }
  }
}

async function start(action) {
  const a = api();
  if (!a) { banner('界面还没准备好，请稍等一秒再试', true); return; }

  const url = $('#url').value.trim();
  const v = validate(url);
  if (v.block) { banner(v.msg, true); return; }

  seq = 0; $('#log').innerHTML = ''; $('#banner').innerHTML = '';
  document.body.classList.remove('error');
  setState('busy');
  setBusy(true, action === 'download' ? '正在下载…' : '正在准备…', action);
  startedAt = Date.now();

  const payload = {
    url: url,
    font: $('#font').value,
    size: parseFloat($('#size').value),
    mode: $('#mode').value,
    action: action
  };
  let resp;
  try { resp = await a.start(payload); } catch (e) { resp = { ok: false, msg: String(e) }; }
  if (!resp || !resp.ok) {
    setBusy(false); setState('error');
    banner((resp && resp.msg) || '启动失败', true);
    return;
  }
  if (!timer) timer = setInterval(poll, 350);
  poll();
}

// 字号 / 字体 实时预览
function updatePreview() {
  const el = $('#preview');
  if (!el) return;
  const size = parseFloat($('#size').value) || 12;
  const key = $('#font').value;
  const f = FONTS.find((x) => x.key === key);
  el.style.fontSize = size + 'pt';
  el.style.fontFamily = f ? f.css : '';
}

function init() {
  $('#outdir').textContent = outDir;
  $('#outdir').title = outDir;

  // 主题：优先用上次选择，其次跟随系统
  let savedTheme = null;
  try { savedTheme = localStorage.getItem(THEME_KEY); } catch (e) {}
  if (!savedTheme) {
    try {
      savedTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch (e) { savedTheme = 'light'; }
  }
  applyTheme(savedTheme);

  $('#theme').addEventListener('click', () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  });

  updatePreview();
  $('#size').addEventListener('change', updatePreview);
  $('#font').addEventListener('change', updatePreview);

  $('#btn-preview').addEventListener('click', () => start('preview'));
  $('#btn-download').addEventListener('click', () => start('download'));
  // 输入框回车 = 下载（更常用的动作）
  $('#url').addEventListener('keydown', (e) => { if (e.key === 'Enter') start('download'); });
  $('#open-folder').addEventListener('click', () => api() && api().open_folder());
  $('#btn-min').addEventListener('click', () => api() && api().minimize());
  $('#btn-close').addEventListener('click', () => api() && api().close());

  // Enter 在输入框回车即开始，符合直觉
  window.addEventListener('pywebviewready', () => { $('#url').focus(); });
  setTimeout(() => { try { $('#url').focus(); } catch (e) {} }, 300);
}

document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>
"""


def render_html() -> str:
    fonts = "".join(
        f'<option value="{f["key"]}">{f["label"]}</option>' for f in FONT_OPTIONS
    )
    sizes = "".join(
        f'<option value="{s["pt"]}"{" selected" if s["pt"] == 12 else ""}>{s["label"]}</option>'
        for s in SIZE_OPTIONS
    )
    modes = "".join(
        f'<option value="{k}"{" selected" if k == DEFAULT_MODE else ""}>{label}</option>'
        for k, label in MODE_OPTIONS.items()
    )
    return (
        HTML.replace("<!--FONTS-->", fonts)
        .replace("<!--SIZES-->", sizes)
        .replace("<!--MODES-->", modes)
        # 路径里可能有反斜杠，必须用 JSON 编码后再塞进 JS 字符串
        .replace("__OUTDIR__", json.dumps(str(DEFAULT_OUT_DIR), ensure_ascii=False))
        .replace("__FONTS__", json.dumps(FONT_OPTIONS, ensure_ascii=False))
    )


# --------------------------------------------------------------------------
# 后端 API（JS 侧通过 pywebview.api.* 调用）
# --------------------------------------------------------------------------

class Api:
    def __init__(self) -> None:
        # 注意：窗口引用必须以下划线开头。pywebview 会递归遍历 js_api 的所有
        # 公开属性来生成 JS 可调用的方法表，若叫 self.window，就会顺着
        # Window → native(WinForms) → COM 一路递归，直到 maximum recursion
        # depth exceeded 并刷出成百上千行日志。下划线开头的属性会被直接跳过。
        self._window = None
        self._lock = threading.Lock()
        self._logs: list[dict] = []
        self._seq = 0
        self._state = {
            "busy": False, "done": False, "ok": None,
            "msg": "", "hint": "", "step": "", "title": "",
        }

    # ---------- 工作线程 → UI 的日志 ----------
    def _on_log(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self._seq += 1
            self._logs.append({
                "i": self._seq,
                "t": time.strftime("%H:%M:%S"),
                "m": msg,
                "lv": level,
            })
            if level == "step":
                # 去掉〔n/7〕前缀，标题更干净
                self._state["step"] = msg.split("〕", 1)[-1]
            if len(self._logs) > 800:
                del self._logs[:200]

    # ---------- 供 JS 调用 ----------
    def start(self, payload: dict | None = None) -> dict:
        with self._lock:
            if self._state["busy"]:
                return {"ok": False, "msg": "正在处理中，请稍候"}
            self._state.update({"busy": True, "done": False, "ok": None,
                                "msg": "", "hint": "", "step": "准备中…", "title": ""})

        payload = payload if isinstance(payload, dict) else {}
        url = (payload.get("url") or "").strip()
        font = payload.get("font") or "SimSun"
        size = float(payload.get("size") or 12)
        mode = payload.get("mode") or DEFAULT_MODE
        if mode not in MODE_OPTIONS:          # 兜底：非法值退回默认
            mode = DEFAULT_MODE
        action = payload.get("action") or "preview"
        if action not in ("preview", "download"):
            action = "preview"

        if not url.startswith(("http://", "https://")):
            with self._lock:
                self._state.update({"busy": False, "done": True, "ok": False,
                                    "msg": "网址无效", "hint": "请粘贴以 http(s):// 开头的链接"})
            return {"ok": False, "msg": "网址无效"}

        cfg = PrintConfig(
            url=url,
            font_key=font,
            size_pt=size,
            mode=mode,
            # 预览：有头浏览器 + window.print()，留着让用户自己另存为
            # 下载：无头浏览器静默跑完，CDP 直接出 PDF，存完关掉浏览器
            headless=(action == "download"),
            window_print=(action == "preview"),
            pdf_path=None,
            keep_browser=True,
            close_browser_after=(action == "download"),
        )
        threading.Thread(target=self._worker, args=(cfg, action), daemon=True).start()
        return {"ok": True}

    def _worker(self, cfg: PrintConfig, action: str = "preview") -> None:
        printer = ZujuanPrinter(cfg, on_log=self._on_log)
        try:
            res = printer.run()
            if action == "download":
                name = res.get("filename") or res.get("suggested_name") or "试卷.pdf"
                folder = res.get("suggested_dir") or str(DEFAULT_OUT_DIR)
                head = "PDF 已下载（后台生成，未弹窗）"
                hint = f"<b>{name}</b><br>保存在 {folder}"
            else:
                name = res.get("suggested_name") or "试卷.pdf"
                head = "浏览器打印预览已弹出"
                hint = (f"在打印窗口中把「目标打印机」选为 <b>另存为 PDF</b>，"
                        f"文件名已预填为 <b>{name}</b>，保存到程序目录即可。")
            with self._lock:
                self._state.update({
                    "busy": False, "done": True, "ok": True,
                    "msg": head, "hint": hint,
                    "title": "下载完成" if action == "download" else "打印预览已就绪",
                })
        except PrintError as e:
            self._on_log(f"✗ {e}", "err")
            with self._lock:
                self._state.update({"busy": False, "done": True, "ok": False,
                                    "msg": str(e), "hint": e.hint or "",
                                    "title": "处理失败"})
        except Exception as e:  # noqa: BLE001
            self._on_log(f"✗ 未预料的错误：{type(e).__name__}: {e}", "err")
            with self._lock:
                self._state.update({"busy": False, "done": True, "ok": False,
                                    "msg": f"程序异常：{e}", "hint": "请查看日志区",
                                    "title": "处理失败"})
        finally:
            printer.close()

    def poll(self, since: int = 0) -> dict:
        with self._lock:
            logs = [l for l in self._logs if l["i"] > since]
            return {"logs": logs, "state": dict(self._state), "seq": self._seq}

    def open_folder(self) -> None:
        try:
            os.startfile(str(DEFAULT_OUT_DIR))          # noqa: S606
        except Exception:
            pass

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def close(self) -> None:
        if self._window:
            self._window.destroy()


def _install_crash_log() -> None:
    """
    打包成 --windowed 后没有控制台，启动异常会静默退出。
    这里把未捕获异常写到程序目录的 crash.log，方便排查。
    """
    def hook(exc_type, exc, tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            (APP_DIR / "crash.log").write_text(text, encoding="utf-8")
        except Exception:
            pass

    sys.excepthook = hook
    # 只有打包运行时才接管 stderr；源码运行仍照常输出到控制台
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        try:
            sys.stderr = open(APP_DIR / "stderr.log", "a", encoding="utf-8", buffering=1)
        except Exception:
            pass


def main() -> None:
    _install_crash_log()
    api = Api()
    window = webview.create_window(
        "组卷网试卷打印助手",
        html=render_html(),
        width=WINDOW_W,
        height=WINDOW_H,
        frameless=True,
        easy_drag=True,          # 无边框窗口靠它拖动
        transparent=WINDOW_TRANSPARENT,
        resizable=False,
        background_color="#F4F7FC",
        js_api=api,
    )
    api._window = window
    # private_mode=False + storage_path：让 localStorage（主题选择）和窗口状态能留到下次
    webview.start(debug=False, private_mode=False,
                  storage_path=str(APP_DIR / ".webview"))


if __name__ == "__main__":
    main()
