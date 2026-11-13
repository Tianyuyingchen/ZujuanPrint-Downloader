# 组卷网试卷打印助手

把组卷网 / 学科网的在线试卷，一键变成能直接打印的卷子：
展开答案 → 去掉导航与广告 → 等图片全部就绪 → 唤起浏览器原生打印预览。

## 安装

```bat
pip install drissionpage pywebview
```

> pywebview 在 Windows 上用系统自带的 **WebView2**（随 Edge 安装），不需要额外下载运行时。
> 如果提示找不到 WebView2，装一下微软的 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) 即可。

## 运行

```bat
python app.py
```

> 不想看到后面的黑色控制台窗口，就用 `pythonw app.py` 启动（没有输出，纯窗口）。

1. 粘贴试卷网址（形如 `https://zujuan.xkw.com/xxxxx.html`）
2. 选「打印内容」：仅试题 / 试题 + 答案 / 答案移至末尾 / 仅答案
3. 选字号、字体（下方有实时预览：函数 y = ax² + bx + c）
4. 点右边按钮，两种用法：
   - **预览打印内容**：打开浏览器原生打印预览，自己选「另存为 PDF」（文件名已预填）
   - **下载**：无头浏览器后台静默跑完，直接生成 PDF 存到程序目录，存完自动关闭，
     **全程不弹窗口、不抢焦点**；导出用的是 CDP `Page.printToPDF`，A4、10mm 页边距、整题不跨页
5. 首次使用需要登录——先点「预览打印内容」扫一次码，
   登录态保存在程序目录的 `.chrome_profile`，之后「下载」的静默流程也能直接用

右上角的 **☀ / 🌙 圆形按钮** 切换浅色 / 深色主题，选择会记住。

## 命令行（排查问题用）

界面出问题时，用 core.py 直接跑，日志更完整：

```bat
python core.py "https://zujuan.xkw.com/xxxxx.html" --size 12 --font SimSun
python core.py --self-test --pdf        :: 用内置模拟试卷自检，不需要登录
```

常用参数：`--mode q|qa|qe|a`（仅试题 / 题+答案 / 答案后置 / 仅答案）、
`--port 9222`（接管已开的浏览器）、`--pdf 输出路径`（不上打印预览，直接存 PDF）。

程序固定用 **9422** 调试端口启动自己的浏览器，不会误接管桌面上其它调试实例。

## 「下载」的静默链路

1. 无头 Chrome 启动（`--headless=new` + `--disable-gpu`），不弹窗、不抢焦点
2. 无头的默认 UA 带 `HeadlessChrome`，组卷网会直接返回「阻断页面」——
   所以会按本机 Chrome 的实际版本伪装成常规 UA（从安装目录版本号推断）
3. 万一仍被拦：自动退回「窗口移到屏幕外 `-32000,-32000`」的有头模式再试一次
4. 重排 + 图片全部 `decode()` 完成后，用 CDP `Page.printToPDF` 导出：
   `@page { size: A4; margin: 10mm; }`、`print-color-adjust: exact`、整题 `break-inside: avoid`
5. 存到程序目录并关闭浏览器

## 实现要点

| 原脚本的做法 | 现在的做法 | 为什么 |
|---|---|---|
| `document.body.innerHTML = ''` | 给 `<html>` 加 `zjp-active` 类，用 CSS 隐藏其它元素 | 打印完可还原，不丢页面状态 |
| `cloneNode(true)` | `appendChild` **移动**真实节点 | 图片解码状态跟着走，不会白屏 |
| `requestAnimationFrame` 轮 15 秒 | MutationObserver + `img.decode()` + `fonts.ready` | 页面一静止就立刻打印，坏图有单张超时兜底 |
| `body, * { font-family: !important }` | 参数化生成 scoped CSS，排除 KaTeX/MathJax | 换字体不破坏公式 |

## 目录

```
app.py            UI（pywebview + HTML/CSS，含暗色主题）
core.py           网页操作、注入、重排、打印
requirements.txt  依赖
.chrome_profile/  首次运行后生成，存登录态
.webview/         首次运行后生成，存界面偏好（主题）
```

## 打包成单文件 exe

```bat
:: PyInstaller（推荐：pythonnet / pywebview 兼容最好，27.6 MB）
pyinstaller --onefile --windowed --name ZujuanPrint-PyInstaller ^
  --icon assets\app.ico --hidden-import webview.platforms.winforms ^
  --exclude-module kivy --exclude-module kivymd --exclude-module pygame ^
  --exclude-module cv2 --exclude-module numpy --exclude-module matplotlib ^
  --exclude-module tkinter --exclude-module PIL app.py
```

```bat
:: Nuitka（55.3 MB；必须用「纯英文路径」的 venv，否则 Depends.exe 解析中文用户名会崩）
python -m venv C:\zjp-venv
C:\zjp-venv\Scripts\pip install drissionpage pywebview nuitka
C:\zjp-venv\Scripts\python -m nuitka --onefile --onefile-no-compression ^
  --windows-console-mode=disable --windows-icon-from-ico=assets\app.ico ^
  --output-filename=ZujuanPrint-Nuitka.exe --output-dir=release ^
  --include-package=DrissionPage --assume-yes-for-downloads ^
  --nofollow-import-to=kivy --nofollow-import-to=pygame --nofollow-import-to=cv2 ^
  --nofollow-import-to=numpy --nofollow-import-to=PIL app.py
```

> ⚠️ Nuitka 版目前**启动即崩**：`Failed to resolve Python.Runtime.Loader.Initialize`
> （pythonnet 在 Nuitka 编译产物里加载 .NET 程序集失败，属已知兼容问题）。
> 用 PyInstaller 版即可；两者都带 `assets/app.ico` 图标。

## 常见问题

- **点了没反应 / 一直提示等试题**：确认已经登录过，且打开的是试卷*预览*页而不是列表页。
- **答案位置写着「抱歉！您未登录」**：答案需要登录才可见，日志会提示 `⚠ N 个答案区显示「未登录 / 无权限」`；先点「预览打印内容」登录一次，再点「下载」。
- **打印出的图片是空白**：说明图片本身没加载出来，日志里会有 `⚠ N 张图片未能加载`。
- **想改默认打印模式**：`app.py` 顶部的 `DEFAULT_MODE`（`q` / `qa` / `qe` / `a`）。
- **上次的浏览器还开着导致启动失败**：程序会自动退回临时配置；也可以直接 `--port` 接管。
