# -*- coding: utf-8 -*-
"""给 NLC 的图形界面拍真机截图。

为什么需要这个脚本：
    文档里写着"Windows 版还没有真机截图"。而**看图是判断界面做得好不好的
    最直接方式** —— 配色、留白、字号这些问题，读代码是看不出来的。
    人工截图的问题是：不可重复、每次都要人动手、忘了拍就没了。

所以做成脚本：跑一次，固定产出同样的几张图。

怎么用：
    python tools/screenshot.py                    拍默认那几张
    python tools/screenshot.py --out D:\\shots     换输出目录
    python tools/screenshot.py --keep-open        拍完不关窗（自己看）

★ 两个设计决定：
  1. **不联网**。截图要的是稳定的画面，不该因为网不通就拍不出来，
     也不该每次文字都不一样。解释文字是本地写死的（见 _drive）。
     真机联网验证是另一件事（tests/live_demo.py）。
  2. **用独立配置目录**（tools/_shot_cfg），不碰用户真实的配置与密钥。
     用完即删（见《更新规则.md》规则 2）。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 必须在 import nlc 之前设好
_SHOT_CFG = ROOT / "tools" / "_shot_cfg"
os.environ.setdefault("NLC_CONFIG_DIR", str(_SHOT_CFG))


# --------------------------------------------------------------------------- #
# 抓屏：Windows 上用 ctypes 调 GDI，零第三方依赖
# --------------------------------------------------------------------------- #
#: 抓屏方式。**按这个顺序试**，第一个出图的就算数。
#:
#: ★★ 2026-10-03 在这台机器上的实测结论（**这一段改过两次**，都是被自己的
#:    bug 骗了，记下来别再犯）：
#:
#: 1) `GetDIBits` 第 6 个参数类型是 `BITMAPINFO *`（**带 `bmiColors`**），
#:    不是 `BITMAPINFOHEADER *`。只传 header 会**静默返回 0**（不报错）。
#:    我曾据此误判"本机没有可读帧缓冲"，还把错结论写进了项目文档。
#:
#: 2) 抓到像素后，出的图**每个通道变成一条竖带**、看着像"画面重复了三次"。
#:    ★★ 我当时判成"虚拟显示器（向日葵）把桌面截断了"，还让用户去退出向日葵
#:    —— **这是误判，害得用户白折腾。** 真因在 `_write_png`：PNG 要的是
#:    **交错**像素（`R0 G0 B0 A0 R1 G1 …`），我却写成"整行 R / 整行 G / …"
#:    的**平面**布局。通道切片全对、长度全对、函数返回 True，
#:    但解出来每个通道连成一片。修好后 5/5 真机截图一次成功，
#:    **向日葵一直没问题，不需要退出。**
#:
#: 3) 顺带发现 `_looks_repeated` 的阈值 0.75 太松：NLC 自己的界面
#:    （左右两栏布局本来就像）实测各周期命中率 0.65~0.73，
#:    于是正常界面被冤判成畸变。已提到 0.97（真复制接近 1.0）。
#:
#: ⇒ 本机可以正常出真图，不需要改任何系统设置。
#:
#: 一句话教训（两条，第二条更贵）：
#:   **API 返回成功 ≠ API 干成了事；图里有像素 ≠ 图是对的。**
#:   **我的判据太松 ≠ 环境有问题。** 判据误报时，先怀疑判据，
#:   别急着让用户去改他的电脑。
_SCREEN = 0        # 抓整屏再裁剪
_PRINTWINDOW = 1  # 直接抓窗口


def _grab_screen_capture(user32, gdi32, w, h, ctypes_):
    """抓整张屏幕，返回 ``(BGRA 缓冲, 宽, 高)``；失败返回 ``None``。

    ★ 注意 ``GetLastError`` 在这里**完全不能用**判成败 —— BitBlt 之后残留的
      错误码常是 109(ERROR_PRINT_CANCELLED) 之类与成败无关的值。
      只看 BitBlt 的返回值。
    """
    from ctypes import wintypes

    class BITMAPINFOHEADER(ctypes_.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes_.Structure):
        # ★★★ `bmiColors` 这行**不能省**。GetDIBits 的第 6 个参数类型是
        #   `BITMAPINFO *`（**不是** `BITMAPINFOHEADER *`），它按固定步长往里读。
        #   只传 header 的话，按 `BITMAPINFO` 的实际布局解释这个地址就是乱的，
        #   `GetDIBits` 返回 0（失败）—— 而且不报错，纯静默失败。
        #   这就是本脚本 2026-10-03 之前"整屏也抓不到"的**真正原因**：
        #   不是桌面没有帧缓冲，是结构体少了一个字段。
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    hdc = user32.GetDC(0)
    if not hdc or hdc == -1:
        return None
    try:
        mem = gdi32.CreateCompatibleDC(hdc)
        if not mem:
            return None
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        if not bmp:
            gdi32.DeleteDC(mem)
            return None
        old = gdi32.SelectObject(mem, bmp)
        if not gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020):  # SRCCOPY
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            return None
        buf = (ctypes_.c_ubyte * (w * h * 4))()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes_.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h   # 负高度 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes_.byref(bmi), 0)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        return buf if got else None
    finally:
        user32.ReleaseDC(0, hdc)


def _crop(buf, screen_w, left, top, w, h):
    """从整屏缓冲里裁出 ``(left, top, w, h)`` 那一块。"""
    import ctypes as _c
    data = bytes(buf)
    out = _c.create_string_buffer(w * h * 4)
    stride = screen_w * 4
    for y in range(h):
        s = (top + y) * stride + left * 4
        out[y * w * 4:(y + 1) * w * 4] = data[s:s + w * 4]
    return out


def _png_size(png: Path):
    """从 PNG 的 IHDR 里读宽高。读不出返回 (0, 0)。"""
    try:
        data = png.read_bytes()[:33]
    except OSError:
        return (0, 0)
    if len(data) < 33 or data[12:16] != b"IHDR":
        return (0, 0)
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def _read_pixels(png: Path):
    """把 PNG 解回原始像素（RGBA，自上而下，每行前置 filter 字节）。

    只给自己做"内容是否重复"的判定用，所以不追求通用 —— 认自己的 filter
    （写的时候只用 0 = None）即可。

    ★ 交错的：每行是 `R0 G0 B0 A0 R1 G1 B1 A1 …`，一个像素紧挨一个像素。
      别写成"整行 R / 整行 G / …"那种平面布局 —— 那样每个通道会变成
      一条竖带，_looks_repeated 会把它误判成"画面重复畸变"。
    """
    import zlib as _z
    try:
        data = png.read_bytes()
    except OSError:
        return b""
    w, h = _png_size(png)
    if not w or not h:
        return b""
    idat = b""
    i = 8
    while i < len(data) - 8:
        ln = int.from_bytes(data[i:i + 4], "big")
        tag = data[i + 4:i + 8]
        if tag == b"IDAT":
            idat += data[i + 8:i + 8 + ln]
        elif tag == b"IEND":
            break
        i += 12 + ln
    try:
        raw = _z.decompress(idat)
    except Exception:                                    # noqa: BLE001
        return b""
    stride = w * 4
    if len(raw) < h * (stride + 1):
        return b""
    out = bytearray(w * h * 4)
    for y in range(h):
        s = y * (stride + 1) + 1                # 跳过 filter 字节
        out[y * stride:(y + 1) * stride] = raw[s:s + stride]
    return bytes(out)


def _looks_repeated(buf, w: int, h: int) -> bool:
    """检测画面是不是"内容被重复/畸变"（虚拟显示器截断的典型症状）。

    ★ 为什么需要这一步：抓屏 API 在桌面被虚拟显示器截断时，
      **返回成功、像素非零、`_has_ink` 也过**，但画面里同一段内容
      重复出现 2~3 次。只有比对内容才发现得了。

    判据：找一条**竖直分割线**，使它左右两半互为镜像重复。
    具体做法 —— 对若干候选周期 p，抽样比较 ``x`` 与 ``x+p`` 的像素：
    命中率越高说明重复越强。真图（左右内容不同）命中率会明显低。

    ★ 前置门槛：**画面颜色得多**才判。
      大片单色底（比如结构图）本来就"处处周期相同"，会 100% 命中 ——
      那是内容本来就长那样，不是畸变。所以先数一下不同颜色，
      少于 _MIN_COLORS 直接说"不是畸变"。
      （这条是被结构图模式逼出来的：它本来只是画框，却每次都被误判。）
    """
    data = bytes(buf)
    if w < 64 or h < 64:
        return False

    # 颜色数门槛：真机界面有配色、字号、抗锯齿，颜色 plentiful；
    # 纯色/少色图（如结构图）不适用本判据。
    seen = set()
    step = max(1, (w * h) // 20000)
    for i in range(0, w * h * 4, step * 4):
        seen.add(data[i:i + 3])
        if len(seen) >= 64:
            break
    if len(seen) < 24:
        return False

    def hit_rate(period: int) -> float:
        ok = bad = 0
        for y in range(0, h, max(1, h // 30)):
            base = y * w * 4
            for x in range(period, w, 11):
                i = base + x * 4
                if data[i:i + 3] == data[i - period * 4:i - period * 4 + 3]:
                    ok += 1
                else:
                    bad += 1
        return ok / (ok + bad) if (ok + bad) else 0.0

    # 候选周期：画面宽度的常见分数，以及 DPI 缩放后的整数
    global _REPEAT_HIT
    cands = sorted({max(32, w // k) for k in (2, 3, 4, 5, 6, 7, 8, 9)} |
                   {int(w * r) for r in (0.25, 1 / 3, 0.5, 2 / 3, 0.75)})
    best = max((hit_rate(p) for p in cands if p < w), default=0.0)
    # ★★ 阈值 0.97，不是 0.75 —— 这条被实测数据纠正过一次，别再改回去。
    #   2026-10-03 曾在 0.75 上判定"本机画面被虚拟显示器截断、必须退出向日葵"，
    #   那是**误判**：NLC 自己的界面（左右两栏布局本来就长得像）实测各周期
    #   命中率 0.65~0.73，并没有哪个周期特别高。
    #   真正的"同一段被精确复制 N 次"命中率接近 1.0（实测合成三联图 = 1.000）。
    #   所以只有高到 0.97 才算畸变，0.65~0.9 那一段是"布局相似"，属正常。
    #   ★ 教训：把"我的判据太松"当成"环境有问题"，就会去让用户改环境。
    _REPEAT_HIT = best
    return best >= 0.97


#: 最近一次 _looks_repeated 算出的最高命中率。供报错时**报实测数字**用 ——
#: 报错里写死一个百分比等于编数据，不如把真值打出来。
_REPEAT_HIT = 0.0


def _grab_window(hwnd: int, out: Path, mode: int = _PRINTWINDOW) -> bool:
    """把一个窗口画到 PNG。返回是否成功。

    ★ 为什么不用 PIL / pyscreenshot：NLC 的底线是**零第三方依赖**。
      这里直接用 Windows 自带的 GDI（user32 + gdi32）。
    """
    import ctypes
    from ctypes import wintypes

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.SetProcessDPIAware()

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    if mode == _SCREEN:
        # ★ 抓整屏，再按窗口矩形裁剪。窗口可能被别的窗口盖住一小块，
        #   那样那部分会拍到遮挡物 —— 但那本来就是"用户此刻看到的"，
        #   比全黑诚实得多。要避免遮挡就先 `root.lift()`。
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        if rect.left < 0 or rect.top < 0 or rect.left + w > sw or rect.top + h > sh:
            return False                               # 窗口超出屏幕，裁不到
        full = _grab_screen_capture(user32, gdi32, sw, sh, ctypes)
        if full is None:
            return False
        buf = _crop(full, sw, rect.left, rect.top, w, h)
    else:
        src = gdi32.CreateCompatibleDC(0)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h   # 负高度 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        bmp = gdi32.CreateDIBSection(src, ctypes.byref(bmi), 0, None, None, 0)
        if not bmp:
            gdi32.DeleteDC(src)
            return False
        old = gdi32.SelectObject(src, bmp)

        # PW_RENDERFULLCONTENT(2)：少了它，某些窗口会全黑。
        # ★ 本机上它给全 0，所以只是备选（见 _PRINTWINDOW 处注释）。
        if not user32.PrintWindow(hwnd, src, 2):
            user32.PrintWindow(hwnd, src, 0)

        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(src, bmp, 0, h, buf, ctypes.byref(bmi), 0)

        gdi32.SelectObject(src, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(src)
        if not got:
            return False
    return _write_png(out, buf, w, h)


def _write_png(path: Path, bgra, w: int, h: int) -> bool:
    """把 BGRA 像素写成 PNG。标准库手写编码（zlib + CRC32），无第三方依赖。

    PNG 最小结构：签名 + IHDR + IDAT + IEND。
    ★ 每条扫描线前面必须加一个 0（filter type = None）——
      漏掉这一字节的话图片会整体错位，症状是"图裂成两半 / 颜色全乱"。

    ★★★ 最容易错、也真的错过的地方：**PNG 的像素是交错的**，
      即 `R0 G0 B0 A0 R1 G1 B1 A1 …`。
      我曾经写成"先拼完整行的 R，再拼完整行的 G，再 B，再 A"———
      那是**平面存储**，通道切片全对、长度全对、脚本报成功，
      但解出来的图每个通道变成一条竖带，看上去就像"画面水平重复了三次"。
      正确写法是用**跨步切片赋值**把四个通道各归各位：
          out[0::4]=R  out[1::4]=G  out[2::4]=B  out[3::4]=A
      顺带 BGRA → RGBA 也在这一步换掉（PNG 要 R 在前，GDI 给的是 B 在前）。
    """
    stride = w * 4
    data = bytes(bgra)
    raw = bytearray()
    for y in range(h):
        row = data[y * stride:(y + 1) * stride]
        if len(row) < stride:                  # 缓冲被截断：补齐，别写半张图
            row = row + b"\x00" * (stride - len(row))
        out = bytearray(stride)
        out[0::4] = row[2::4]                  # R
        out[1::4] = row[1::4]                  # G
        out[2::4] = row[0::4]                  # B
        out[3::4] = row[3::4]                  # A
        raw.append(0)                          # filter: None
        raw += out

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                     + chunk(b"IEND", b""))
    return True


def _has_ink(png: Path) -> bool:
    """判断一张图是不是"真有内容"。

    ★ 为什么要多这一步：`PrintWindow` / `BitBlt` 在抓不到时会**返回成功**，
      产出一张纯黑图。纯黑图混进 `真机截图/` 里非常危险 ——
      下一个接手的人（或几周后的自己）会以为界面真的长那样。
      所以每张图都要验一下"不是纯黑"。
    """
    try:
        data = png.read_bytes()
    except OSError:
        return False
    if len(data) < 1000:
        return False
    # 只看 IDAT 附近有没有非零的像素数据；PNG 里除了像素还有别的非零字节，
    # 所以用"解压后有没有非零的 RGB"这个判据，而不是在文件字节里瞎找。
    try:
        import zlib as _z

        idat = b""
        i = 8
        while i < len(data) - 8:
            ln = int.from_bytes(data[i:i + 4], "big")
            tag = data[i + 4:i + 8]
            if tag == b"IDAT":
                idat += data[i + 8:i + 8 + ln]
            elif tag == b"IEND":
                break
            i += 12 + ln
        raw = _z.decompress(idat) if idat else b""
    except Exception:                                        # noqa: BLE001
        raw = b""
    if not raw:
        return False
    # 每行第 1 字节是 filter 类型，其余是像素。全 0 像素 = 全黑 = 不可信。
    return any(raw[1:512])


def _toplevel_hwnd(win) -> int:
    """取 Tk toplevel 的 Windows HWND（整数）。

    ★ 为什么要专门取：tkinter 的 `winfo_id()` 返回的是 Tk 自己创建的内部窗口，
      抓它会得到一张空白小图。真正的顶层窗口 HWND 在 `frame()` 里（十六进制串）。
    """
    return int(win.frame(), 16)


class ScreenUnavailable(RuntimeError):
    """抓不到屏幕像素 —— 不是代码的问题，是这个进程所在的桌面没有帧缓冲。

    症状：抓屏 API **返回成功却得到全 0 像素**。
    在真实桌面上不会出现；出现时说明本进程所在的会话桌面不提供屏幕内容
    （无头会话、被远程截断的会话、部分虚拟机显卡驱动）。
    """


def _virtual_displays() -> list:
    """列出虚拟显示器（GameViewer / 向日葵 / ToDesk 之类）。

    ★ 这份清单**只作信息报告，不当病因**。
      2026-10-03 曾把它当成"画面畸变的根因"，据此让用户去退出向日葵 ——
      那是误判：真因是 NLC 自己的 PNG 编码器（平面存储当交错写）。
      修完编码器，这台机器装着 10 个 GameViewer 照样 5/5 出图正常。
      教训：**列出可疑项 ≠ 找到原因**。真要下结论得有对照实验。
    """
    import ctypes
    from ctypes import wintypes

    class DISPLAYDEVICE(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    u = ctypes.windll.user32
    found = []
    i = 0
    while i < 64:
        dd = DISPLAYDEVICE()
        dd.cb = ctypes.sizeof(DISPLAYDEVICE)
        if not u.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        name = dd.DeviceString or ""
        low = name.lower()
        if any(k in low for k in ("virtual", "gameviewer", "sunlogin", "totem",
                                  "向日葵", "virtualdisplay", "dummy")):
            found.append(name)
        i += 1
    return found


def _probe_desktop(hwnd: int = 0) -> str:
    """先探这台机器能不能抓屏，不能就**说清原因与该做什么**。

    ★ 三层检查，缺一不可：
      1. 拿不拿得到屏幕尺寸；
      2. `PrintWindow` 能不能真抓到像素（不信返回值，只信"有内容"）；
      3. 整屏 `BitBlt` 能不能抓，抓到的画面**内容**是不是畸变的。

    ★ 虚拟显示器**只报告、不定罪**。曾在这里断言"它就是畸变根因"并让用户
      去退出向日葵，那是误判（真因是 PNG 编码器）。实测装了不影响抓图。
    """
    import ctypes

    u, gd = ctypes.windll.user32, ctypes.windll.gdi32
    u.SetProcessDPIAware()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    if sw <= 0 or sh <= 0:
        raise ScreenUnavailable("拿不到屏幕尺寸（%dx%d）" % (sw, sh))

    virt = _virtual_displays()
    # ★ 只作 informational。2026-10-03 曾在这里直接断言"虚拟显示器就是畸变根因"，
    #   让用户去退出向日葵 —— 那是**误判**，真因是 NLC 自己的 PNG 编码器
    #   （平面存储当交错写），修了编码器截图就好了，向日葵没问题。
    #   所以这里只报告"装了哪些虚拟显示器"，**不下结论**。
    virt_note = ("；装有虚拟显示器：%s（已确认不影响抓图）"
                 % "、".join(list(dict.fromkeys(virt))[:3]) if virt else "")

    # ① PrintWindow 试抓
    if hwnd:
        from ctypes import wintypes

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BI(ctypes.Structure):
            _fields_ = [("bmiHeader", BIH), ("bmiColors", wintypes.DWORD * 3)]

        rect = wintypes.RECT()
        if u.GetWindowRect(hwnd, ctypes.byref(rect)):
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 0 and h > 0:
                dc = gd.CreateCompatibleDC(0)
                bmi = BI()
                bmi.bmiHeader.biSize = ctypes.sizeof(BIH)
                bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
                bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
                bmp = gd.CreateDIBSection(dc, ctypes.byref(bmi), 0, None, None, 0)
                if bmp:
                    old = gd.SelectObject(dc, bmp)
                    got_pw = (u.PrintWindow(hwnd, dc, 2) or u.PrintWindow(hwnd, dc, 0))
                    buf = (ctypes.c_ubyte * (w * h * 4))()
                    got = gd.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(bmi), 0) if got_pw else 0
                    if got and any(bytes(buf)[:65536]):
                        bad = _looks_repeated(bytes(buf), w, h)
                        hit = _REPEAT_HIT
                        gd.SelectObject(dc, old)
                        gd.DeleteObject(bmp)
                        gd.DeleteDC(dc)
                        if bad:
                            raise ScreenUnavailable(
                                "抓到了像素，但画面内容是**同一段被精确复制了多次**。\n"
                                "        （实测周期命中率 %.3f，接近 1.0，属真畸变。）\n"
                                "        怎么办：重启一下桌面会话，或换一台机器再试。\n"
                                "        本脚本不会拿畸变图充数。" % hit)
                        return "PrintWindow 可用（试抓 %dx%d，内容正常）%s" % (
                            w, h, virt_note)
                    gd.SelectObject(dc, old)
                    gd.DeleteObject(bmp)
                    gd.DeleteDC(dc)

    # ② 退到整屏
    full = _grab_screen_capture(u, gd, sw, sh, ctypes)
    if full is None or not any(bytes(full)[:65536]):
        raise ScreenUnavailable(
            "整屏 BitBlt 也拿不到像素。\n"
            "        常见于：无头会话 / 会话被远程截断 / 显示器驱动不提供屏幕内容。")
    if _looks_repeated(bytes(full), sw, sh):
        raise ScreenUnavailable(
            "整屏能抓到像素，但**同一段内容被精确复制了多次**"
            "（实测周期命中率 %.3f，接近 1.0）。\n"
            "        怎么办：重启桌面会话后再试。本脚本不会拿畸变图充数。"
            % _REPEAT_HIT)
    return "屏幕可读（%dx%d，像素与内容都验过）%s" % (sw, sh, virt_note)


# --------------------------------------------------------------------------- #
# 把界面摆成"值得截图的那个状态"
# --------------------------------------------------------------------------- #
#: 截图用的示例解释。**故意写死**：截图要稳定，联网的结果每次都不一样。
_GLOSS = {
    "netstat": "显示协议统计与连接表", "-ano": "端口号、PID、连接状态三样都显示",
    "|": "管道：把左边的结果交给右边", "findstr": "按文本筛选输出（Windows 版的 grep）",
    ":8080": "要查的就是这个端口",
    "Get": "获取", "-": "连接符", "NetTCPConnection": "TCP 连接对象",
    "LocalPort": "参数名：本地端口", "8080": "端口号", "Select": "挑选",
    "Object": "对象", "ExpandProperty": "把对象的某个属性摊成值",
    "OwningProcess": "属性名：占用该端口的进程号",
}


def _drive(app, page: str, theme: str) -> None:
    """摆好界面：切页、切主题、塞一条本地翻译结果。"""
    app.apply_theme(theme, save=False)
    if page != "cmd":
        app.nav_select(page)
    else:
        app.nav_select("cmd")

    from nlc.risk import assess
    from nlc.tokenizer import tokenize
    from nlc.translator import TokenBlock, Translation

    is_ps = (app.cfg.get("ui") or {}).get("shell") == "powershell"
    cmd = ("Get-NetTCPConnection -LocalPort 8080 | Select-Object -ExpandProperty OwningProcess"
           if is_ps else "netstat -ano | findstr :8080")
    toks = tokenize(cmd)
    blocks = [TokenBlock(t.index, t.text, t.kind, t.gap_before,
                         _GLOSS.get(t.text, "这一块是：%s" % t.text))
              for t in toks]
    tr = Translation(kind="command", command=cmd,
                     summary="查出占用 8080 端口的进程号", blocks=blocks,
                     risk=assess(cmd),
                     notes="若这条在旧版 Windows 上不认，改用 netstat -ano | findstr :8080")
    app.base_tr = tr
    app.render_result(tr)
    app.root.update_idletasks()


#: 要拍哪几张。名字会直接当文件名，所以只用中文与短横线。
SHOTS = [
    ("01-主界面-日间", "cmd", "day"),
    ("02-主界面-夜间", "cmd", "night"),
    ("03-查词页", "lookup", "day"),
    ("04-脚本页", "script", "day"),
    ("05-词库与黑名单", "words", "day"),
]


def shoot(out_dir: Path, keep_open: bool = False, structure_only: bool = False) -> int:
    from nlc import gui as g

    out_dir.mkdir(parents=True, exist_ok=True)
    g._enable_dpi_awareness()

    app = g.NlcApp()
    app.root.geometry("1200x840+40+40")
    app.root.update_idletasks()
    app.root.update()
    hwnd = _toplevel_hwnd(app.root)

    # ★ 先探桌面。抓不到就**立刻停下并说清原因**，绝不产出坏图。
    #   --structure-only 是显式退路：用户知道要的是什么。
    if not structure_only:
        try:
            print("  探测：%s" % _probe_desktop(hwnd))
        except ScreenUnavailable as exc:
            print("\n  ✘ 拍不到像素：%s\n" % exc)
            print("  没有产出任何图片 —— 全黑的图、重复的图，比没有图更容易误导人。")
            print("\n  退路：出「结构图」（画控件位置与文字，不是照片）：")
            print("    python tools\\screenshot.py --structure-only --out %s" % out_dir)
            try:
                app.root.destroy()
            except Exception:                            # noqa: BLE001
                pass
            return 2
    else:
        print("  模式：--structure-only（只画结构图，不抓像素）")

    import ctypes
    ctypes.windll.user32.SetProcessDPIAware()

    made = 0
    try:
        for name, page, theme in SHOTS:
            _drive(app, page, theme)
            app.root.update_idletasks()
            app.root.update()
            # ★ 置顶：整屏抓屏会把遮挡它的东西一起拍进来
            try:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.BringWindowToTop(hwnd)
            except Exception:                            # noqa: BLE001
                pass
            app.root.update()
            time.sleep(0.4)                  # 等重绘落地
            dest = out_dir / ("%s.png" % name)

            if structure_only:
                if _dump_structure(app, out_dir, name, page, theme):
                    made += 1
                    print("  ✔ %-22s 结构图 + 控件清单（%d 项）"
                          % (name, len(_walk_widgets(app.root))))
                else:
                    print("  ✘ %-22s 结构图生成失败" % name)
                continue

            # 依次试各方式；每个都要过"非黑"+"不重复"两关才算数
            got = None
            for mode in (_PRINTWINDOW, _SCREEN):
                if not _grab_window(hwnd, dest, mode):
                    if dest.exists():
                        dest.unlink()
                    continue
                if not _has_ink(dest):
                    dest.unlink()
                    continue
                w, h = _png_size(dest)
                if w > 0 and _looks_repeated(_read_pixels(dest), w, h):
                    print("  ⚠ %-22s 画面内容重复，弃用（宁可不给，也不给坏的）" % dest.name)
                    dest.unlink()
                    continue
                got = mode
                break
            if got is not None:
                how = "PrintWindow" if got == _PRINTWINDOW else "整屏裁剪"
                print("  ✔ %-22s %6.1f KB  （%s）"
                      % (dest.name, dest.stat().st_size / 1024, how))
                made += 1
            else:
                print("  ✘ %-22s 抓图失败（宁可不给，也不给假的）" % dest.name)
    finally:
        if keep_open:
            print("\n  窗口留着，关掉它就结束。")
            app.root.mainloop()
        else:
            app.root.destroy()

    print("\n  共 %d/%d 张，输出在 %s" % (made, len(SHOTS), out_dir))
    return 0 if made == len(SHOTS) else 1


# --------------------------------------------------------------------------- #
# 退路：把界面画成"结构图"（不依赖像素的截图）
# --------------------------------------------------------------------------- #
def _walk_widgets(widget, depth: int = 0, out=None):
    """遍历 tkinter 控件树，产出 [(层级, 控件类, 文字, 左, 上, 宽, 高)]。"""
    if out is None:
        out = []
    try:
        cls = widget.winfo_class()
    except Exception:                                        # noqa: BLE001
        return out
    txt = ""
    for attr in ("text", "label"):
        try:
            v = widget.cget(attr)
            if v:
                txt = str(v)
                break
        except Exception:                                    # noqa: BLE001
            continue
    if not txt:
        # 容器类控件没有 text，试试它内部的第一个 Label（导航按钮常用）
        try:
            for c in widget.winfo_children():
                t = c.cget("text")
                if t:
                    txt = str(t)
                    break
        except Exception:                                    # noqa: BLE001
            pass
    try:
        geom = (widget.winfo_rootx() - _ROOT_POS[0],
                widget.winfo_rooty() - _ROOT_POS[1],
                widget.winfo_width(), widget.winfo_height())
    except Exception:                                        # noqa: BLE001
        geom = (0, 0, 0, 0)
    if txt.strip() or cls in ("Button", "Label", "Entry", "Text"):
        out.append((depth, cls, txt.strip(), geom))
    for c in widget.winfo_children():
        _walk_widgets(c, depth + 1, out)
    return out


#: 根窗口左上角在屏幕上的位置，算相对坐标用
_ROOT_POS = [0, 0]


#: 3x5 点阵数字。结构图上给每个控件标编号，好跟清单里的行对上。
#: 只做 0-9 —— 中文没法用点阵画，文字统一看清单。
_GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


def _draw_structure_png(items, path: Path, w: int, h: int, title: str) -> bool:
    """把控件树画成一张 PNG"界面结构图"。

    ★ 这不是真截图，是**结构图**：像素抓不到时的退路。
      它画的是每个控件的位置、尺寸与编号编号 —— 用来核对布局与文案，
      不能用来判断配色/字号/圆角那些视觉效果。
      所以文件名与说明里都会写明"结构图"，不冒充真截图。
      控件上的文字是编号，去同名的 .txt 清单里查 —— 点阵画不出中文。

    ★ 这里自己攒像素缓冲，喂给 _write_png 之前**必须已经是纯 BGRA、
      不带每行那个 filter 字节** —— _write_png 会按 `stride = w * 4` 切行
      并自己补 filter 0。踩过的坑：我在行尾多塞了一个字节，切行时逐行累积
      错位，出来的图**水平重复 4 次**，但脚本报"成功"。行宽必须跟契约一致。

    ★ 另一个坑：切片赋值 `px[o:o+4] = bytes((r,g,b))` 给的是 3 字节，
      bytearray 会**自动增长**，索引从第一次画线起就全错 —— 表现是
      "越画越慢最后卡死"，而不是报错。宽度必须一次给对。
    """
    w = max(200, int(w))
    h = max(150, int(h))
    rowbytes = w * 4                             # 纯像素行宽，与 _write_png 一致

    # 底色：浅底 + 每 40 像素一条网格线，一眼看出"这是结构图不是照片"
    # ★ 整块底图用"预生成几行 + 按行重复"来铺，别逐像素写 ——
    #   1200x840 = 100 万次 Python 循环要几十秒，外层等不及就 SIGTERM。
    #   这也是当初这函数第一次跑就挂掉的原因（叠加上面那个 3 字节 bug 更糟）。
    cell = bytearray()
    for x in range(w):
        cell += b"\xE4\xE8\xF5\xFF" if (x % 40 == 0) else b"\xFA\xFD\xFF\xFF"
    row_vgrid = bytes(cell)                      # 带竖网格线
    row_main = b"\xFA\xFD\xFF\xFF" * w           # 普通行
    row_hline = b"\xE4\xE8\xF5\xFF" * w          # 横网格线那一行
    px = bytearray()
    for y in range(h):
        px += row_hline if (y % 40 == 0) else row_main
    px[:rowbytes] = row_vgrid                   # 第 0 行是图例条，网格线用不上

    def put(x, y, bgr):
        """画一个像素。**必须写满 4 字节**，见上面那条坑。"""
        if 0 <= x < w and 0 <= y < h:
            o = y * rowbytes + x * 4
            px[o:o + 4] = bgr

    def hline(x0, x1, y, bgr):
        if x1 < x0:
            x0, x1 = x1, x0
        x0 = max(0, x0)
        x1 = min(w - 1, x1)
        if x1 < x0 or not (0 <= y < h):
            return
        o = y * rowbytes + x0 * 4
        n = (x1 - x0 + 1) * 4
        px[o:o + n] = bgr * (x1 - x0 + 1)

    def vline(x, y0, y1, bgr):
        if y1 < y0:
            y0, y1 = y1, y0
        y0 = max(0, y0)
        y1 = min(h - 1, y1)
        for y in range(y0, y1 + 1):
            put(x, y, bgr)

    def rect(x, y, ww, hh, bgr):
        if ww <= 0 or hh <= 0:
            return
        hline(x, x + ww - 1, y, bgr)
        hline(x, x + ww - 1, y + hh - 1, bgr)
        vline(x, y, y + hh - 1, bgr)
        vline(x + ww - 1, y, y + hh - 1, bgr)

    def number(n, x, y, bgr):
        """在 (x, y) 处画 3x5 点阵数字（放大 2 倍，认得出来）。"""
        s = str(n)
        cx = x
        for ch in s:
            g = _GLYPHS.get(ch)
            if g:
                for ry, row in enumerate(g):
                    for rx, bit in enumerate(row):
                        if bit == "1":
                            rect(cx + rx * 2, y + ry * 2, 2, 2, bgr)
            cx += 8

    BLUE = b"\xC0\x60\x40\xFF"                  # BGRA
    GREEN = b"\x60\xA0\x20\xFF"
    AMBER = b"\x10\x80\xB0\xFF"
    DARK = b"\x20\x20\x30\xFF"
    BAR = b"\xD8\xC8\xC0\xFF"

    # 顶部图例条 + 三种控件的颜色样本
    hline(0, w - 1, 0, BAR)
    hline(0, w - 1, 1, BAR)
    hline(0, w - 1, 2, BAR)
    rect(10, 8, 8, 8, BLUE)
    rect(30, 8, 8, 8, GREEN)
    rect(50, 8, 8, 8, AMBER)
    number(1, 66, 8, DARK)

    for idx, (depth, cls, txt, (x, y, ww, hh)) in enumerate(items, 1):
        if ww <= 0 or hh <= 0:
            continue
        if cls == "Button":
            col = BLUE
        elif cls in ("Entry", "Text"):
            col = GREEN
        else:
            col = AMBER
        inset = min(6, depth * 2)
        rect(x + inset, y + inset, ww - inset * 2, hh - inset * 2, col)
        # 编号画在框内左上角；框太小时画不出去就算了，不报错
        if ww - inset * 2 > 26 and hh - inset * 2 > 13:
            number(idx, x + inset + 2, y + inset + 2, DARK)

    return _write_png(path, bytes(px), w, h)


def _dump_structure(app, out_dir: Path, name: str, page: str, theme: str) -> bool:
    """拍一张"结构图 + 控件清单"。像素抓不到时用这个顶上。"""
    _ROOT_POS[:] = [app.root.winfo_rootx(), app.root.winfo_rooty()]
    app.root.update_idletasks()
    app.root.update()
    items = _walk_widgets(app.root)
    w = max(400, app.root.winfo_width())
    h = max(300, app.root.winfo_height())
    safe = "%s-%s-结构图" % (name, theme)
    png = out_dir / ("%s.png" % safe)
    ok = _draw_structure_png(items, png, w, h, safe)
    txt = out_dir / ("%s.txt" % safe)
    lines = [
        "NLC 界面结构图 —— %s / %s / 主题=%s" % (name, page, theme),
        "=" * 78,
        "★ 这不是照片，是**结构图**：画的是控件的位置与尺寸，编号与图上的一一对应。",
        "  看得出：布局、控件数量、每个控件装什么文字、哪里留白不对。",
        "  看不出：配色、字号、圆角这些视觉效果。要那些得看真截图。",
        "",
        "窗口标题：%s" % app.root.title(),
        "窗口尺寸：%d x %d" % (w, h),
        "控件总数：%d" % len(items),
        "图例：蓝=Button  绿=Entry/Text  橙=其它有文字的控件",
        "",
        "%-4s %-5s %-10s %-9s %-6s %s"
        % ("编号", "层级", "控件", "位置", "尺寸", "文字"),
        "-" * 78,
    ]
    for idx, (depth, cls, txt_, (x, y, ww, hh)) in enumerate(items, 1):
        lines.append("%-4d %-5d %-10s (%4d,%4d) %-6s %s"
                     % (idx, depth, cls, x, y, "%dx%d" % (ww, hh), txt_[:36]))
    txt.write_text("\n".join(lines), encoding="utf-8")
    return ok


# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="给 NLC 界面拍真机截图")
    ap.add_argument("--out", default="", help="输出目录（默认 <开发根>/真机截图）")
    ap.add_argument("--keep-open", action="store_true", help="拍完不关窗")
    ap.add_argument("--structure-only", action="store_true",
                    help="只出结构图（跳过像素抓屏）。"
                         "像素抓不到时的退路，画的是控件位置与文字，不是照片")
    _a = ap.parse_args()
    sys.exit(shoot(Path(_a.out) if _a.out else ROOT / "真机截图",
                   _a.keep_open, _a.structure_only))
