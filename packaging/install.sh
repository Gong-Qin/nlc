#!/bin/sh
# NLC 便携安装脚本
#
# 设计目标（「便携」在这里的具体含义）：
#   1. **不污染系统** —— 不写 /usr、不用 root、不装任何 pip 包（代码本来就是零依赖）
#   2. **装在一个目录里** —— 想要就能整体删掉，卸载脚本按清单清理，不留残余
#   3. **离线可装** —— 包里有全部代码，安装过程不联网
#   4. **就地可跑** —— 解压出来不装也能直接跑（见 ../nlc.sh）
#
# 用法：
#   sh install.sh                  # 装到 ~/.local（符合 XDG），并尽量注册桌面图标
#   sh install.sh --prefix /opt/nlc
#   sh install.sh --portable       # 就地安装：不复制代码，只在原地建启动器与清单
#   sh install.sh --dry-run        # 只报告将要做什么，不落盘
#   sh uninstall.sh                # 卸载（保留 ~/.config/nlc 里的个人数据）
#   sh uninstall.sh --purge        # 连个人数据一起删
set -e

VERSION="0.1.0"
PKG_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 自动定位源码根目录 —— 让这个脚本既能从仓库里跑（packaging/install.sh），
# 也能从发布包根目录跑（解压后直接 sh install.sh），两种布局都认得。
if [ -f "$PKG_DIR/../nlc/__init__.py" ]; then
    SRC_DIR=$(CDPATH= cd -- "$PKG_DIR/.." && pwd)      # 仓库布局
elif [ -f "$PKG_DIR/nlc/__init__.py" ]; then
    SRC_DIR="$PKG_DIR"                                  # 发布包布局
else
    echo "找不到 NLC 源码（应在 nlc/ 子目录里）。" >&2
    echo "看起来这个脚本被单独拷贝出来了 —— 请从完整目录里运行。" >&2
    exit 1
fi

# 图标可能在这两个位置之一
ICON_SRC=""
for p in "$PKG_DIR/nlc.svg" "$PKG_DIR/packaging/nlc.svg" "$SRC_DIR/packaging/nlc.svg"; do
    [ -f "$p" ] && ICON_SRC=$p && break
done

# ── 默认值 ──
XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
PREFIX="$XDG_DATA/nlc"
BINDIR="$XDG_BIN"
MODE=copy
DO_BIN=1
DO_DESKTOP=auto
FORCE=0
DRY=0

# ── 参数 ──
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)   PREFIX=$2; shift 2 ;;
        --prefix=*) PREFIX=${1#*=}; shift ;;
        --bin)      BINDIR=$2; shift 2 ;;
        --bin=*)    BINDIR=${1#*=}; shift ;;
        --portable) MODE=portable; shift ;;
        --no-bin)   DO_BIN=0; shift ;;
        --no-desktop) DO_DESKTOP=0; shift ;;
        --desktop)  DO_DESKTOP=1; shift ;;
        --force)    FORCE=1; shift ;;
        --dry-run)  DRY=1; shift ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m  ✔ %s\033[0m\n' "$*"; }

run() {
    if [ "$DRY" = 1 ]; then
        printf '  [预演] %s\n' "$*"
    else
        "$@"
    fi
}

# ──────────────────────────────────────────────────────────
step "1/6  环境自检"
# ──────────────────────────────────────────────────────────
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    warn "没找到 python3。NLC 需要 Python 3.8 或更高版本。"
    exit 1
fi

PYVER=$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')
say "  Python      $PYVER  ($(command -v $PY))"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)'; then
    warn "Python 版本太低（需要 3.8+，当前 $PYVER）。"
    exit 1
fi
ok "版本满足要求（≥3.8）"

# 能不能导入 nlc（这决定安装后是否可用）
if PYTHONPATH="$SRC_DIR" "$PY" -c 'import nlc; print("  NLC 版本    " + nlc.__version__)' 2>/dev/null; then
    ok "代码可正常导入（零第三方依赖）"
else
    warn "代码无法导入，安装包可能不完整。"
    exit 1
fi

# GTK3 只是图形界面要，缺了不影响 CLI 与 MCP
GTK="不可用"
if PYTHONPATH="$SRC_DIR" "$PY" -c '
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk' 2>/dev/null; then
    GTK="可用"
    ok "GTK3 可用 —— 图形界面能跑"
else
    warn "GTK3 不可用 —— 图形界面跑不了，但命令行与 MCP 服务端照常能用"
    warn "  想用图形界面（Debian/Ubuntu）：sudo apt install python3-gi gir1.2-gtk-3.0"
    warn "  想用图形界面（Fedora）：sudo dnf install python3-gobject gtk3"
fi

DESKTOP_ENV="无"
if [ -n "$XDG_CURRENT_DESKTOP" ] || [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    DESKTOP_ENV="${XDG_CURRENT_DESKTOP:-图形环境}"
fi
say "  桌面环境    $DESKTOP_ENV"

# ──────────────────────────────────────────────────────────
step "2/6  目标位置"
# ──────────────────────────────────────────────────────────
if [ "$MODE" = portable ]; then
    PREFIX=$SRC_DIR
    say "  模式        就地（不复制代码）"
else
    say "  模式        复制安装"
fi
say "  代码目录    $PREFIX"
[ "$DO_BIN" = 1 ] && say "  启动器      $BINDIR/nlc"

if [ "$MODE" = copy ] && [ -e "$PREFIX" ] && [ "$FORCE" != 1 ]; then
    warn "目标目录已存在：$PREFIX"
    warn "  · 想覆盖升级：加 --force"
    warn "  · 想装到别处：--prefix /别的/路径"
    warn "  · 想先清干净：sh uninstall.sh"
    exit 1
fi

# ──────────────────────────────────────────────────────────
step "3/6  安装代码"
# ──────────────────────────────────────────────────────────
clean_caches() {
    # 清掉 pycache，别把编译缓存也装进去
    find "$1" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$1" -name '*.pyc' -delete 2>/dev/null || true
}

if [ "$MODE" = copy ]; then
    run mkdir -p "$PREFIX"
    # 用 tar 管道复制，顺便排除缓存与个人配置
    if [ "$DRY" = 1 ]; then
        printf '  [预演] 复制 %s/nlc → %s/nlc\n' "$SRC_DIR" "$PREFIX"
    else
        ( cd "$SRC_DIR" && tar cf - \
            --exclude='__pycache__' --exclude='*.pyc' --exclude='.tmpcfg' \
            --exclude='_proto_*.json' \
            nlc tests packaging nlc.sh README.md .gitignore 2>/dev/null ) \
          | ( cd "$PREFIX" && tar xf - )
        clean_caches "$PREFIX"
    fi
    ok "代码已复制"
else
    ok "就地使用，未复制文件"
fi

# ──────────────────────────────────────────────────────────
step "4/6  生成启动器"
# ──────────────────────────────────────────────────────────
LAUNCH_DIR="$PREFIX"
if [ "$MODE" = portable ]; then
    LAUNCH_DIR="$SRC_DIR"
fi

if [ "$DO_BIN" = 1 ]; then
    run mkdir -p "$BINDIR"
    if [ "$DRY" = 1 ]; then
        printf '  [预演] 写入启动器 %s/nlc\n' "$BINDIR"
    else
        cat > "$BINDIR/nlc" <<LAUNCHER
#!/bin/sh
# NLC 启动器（由 install.sh 生成）
set -e
exec "$PY" -c 'import sys
sys.path.insert(0, sys.argv[1])
del sys.argv[1]
from nlc.cli import main
sys.exit(main())' "$LAUNCH_DIR" "\$@"
LAUNCHER
        chmod 755 "$BINDIR/nlc"
    fi
    ok "启动器已写入 $BINDIR/nlc"
fi

# 顺带在安装目录也放一份，方便直接调用
if [ "$MODE" = copy ] && [ "$DRY" != 1 ]; then
    for src in "$SRC_DIR/nlc.sh" "$PKG_DIR/nlc.sh"; do
        [ -f "$src" ] && cp "$src" "$PREFIX/nlc.sh" 2>/dev/null && break
    done
    chmod 755 "$PREFIX/nlc.sh" 2>/dev/null || true
fi

# ★ 把安装/卸载脚本也留一份在安装目录里。
#   不这么做的话，用户装完之后手里就没有卸载工具了 —— 想卸载还得翻出当初那个安装包，
#   这跟「装在一个目录里、想删就整体删掉」的便携承诺是矛盾的。
if [ "$MODE" = copy ] && [ "$DRY" != 1 ]; then
    for f in install.sh uninstall.sh; do
        for cand in "$PKG_DIR/$f" "$SRC_DIR/packaging/$f"; do
            if [ -f "$cand" ]; then
                cp "$cand" "$PREFIX/$f" 2>/dev/null && chmod 755 "$PREFIX/$f" 2>/dev/null
                break
            fi
        done
    done
fi

# ──────────────────────────────────────────────────────────
step "5/6  桌面图标"
# ──────────────────────────────────────────────────────────
DESKTOP_DONE=0
want_desktop() {
    if [ "$DO_DESKTOP" = 1 ]; then return 0; fi
    if [ "$DO_DESKTOP" = 0 ]; then return 1; fi
    # auto：装了 GTK3 且有图形环境才注册
    [ "$GTK" = "可用" ] && [ "$DESKTOP_ENV" != "无" ]
}

if [ "$DO_BIN" = 1 ] && want_desktop; then
    APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
    run mkdir -p "$APPS" "$ICONS"
    if [ "$DRY" != 1 ]; then
        if [ -n "$ICON_SRC" ]; then cp "$ICON_SRC" "$ICONS/nlc.svg" 2>/dev/null || true; fi
        cat > "$APPS/nlc.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Version=1.0
Name=NLC
Name[zh_CN]=自然语言命令行
GenericName=Natural Language Command-line
Comment=在人与命令行之间加一层 AI 翻译；AI 只翻译，不执行
Comment[zh_CN]=在人与命令行之间加一层 AI 翻译；AI 只翻译，不执行
Exec=$BINDIR/nlc gui
Icon=nlc
Terminal=false
Categories=Utility;Development;Console;
Keywords=command;shell;terminal;ai;自然语言;命令行;
StartupNotify=true
ENTRY
        chmod 644 "$APPS/nlc.desktop"
        command -v update-desktop-database >/dev/null 2>&1 && \
            update-desktop-database "$APPS" >/dev/null 2>&1 || true
        command -v gtk-update-icon-cache >/dev/null 2>&1 && \
            gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" \
            >/dev/null 2>&1 || true
    fi
    DLOCAL=""
    [ -d "${HOME}/桌面" ] && DLOCAL="${HOME}/桌面"
    [ -d "${HOME}/Desktop" ] && DLOCAL="${HOME}/Desktop"
    if [ -n "$DLOCAL" ]; then
        run cp "$APPS/nlc.desktop" "$DLOCAL/nlc.desktop" 2>/dev/null || true
        [ "$DRY" != 1 ] && chmod 755 "$DLOCAL/nlc.desktop" 2>/dev/null || true
        say "  桌面上也放了一个入口：$DLOCAL/nlc.desktop"
        DESKTOP_DONE=1
    fi
    ok "桌面图标已注册"
else
    if [ "$DO_DESKTOP" = 0 ]; then
        say "  已按参数跳过"
    else
        say "  跳过（没有图形环境或缺 GTK3）"
    fi
fi

# ──────────────────────────────────────────────────────────
step "6/6  写安装清单（卸载靠它）"
# ──────────────────────────────────────────────────────────
MANIFEST="$PREFIX/.nlc-install-manifest"
if [ "$DRY" = 1 ]; then
    printf '  [预演] 写入清单 %s\n' "$MANIFEST"
else
    {
        echo "# NLC 安装清单 —— uninstall.sh 按这个清理，不要手改"
        echo "version=$VERSION"
        echo "prefix=$PREFIX"
        echo "mode=$MODE"
        echo "python=$PY"
        echo "bin=$([ "$DO_BIN" = 1 ] && echo "$BINDIR/nlc" || echo "")"
        echo "desktop=$([ -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/nlc.desktop" ] && echo "${XDG_DATA_HOME:-$HOME/.local/share}/applications/nlc.desktop" || echo "")"
        echo "icon=$([ -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/nlc.svg" ] && echo "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/nlc.svg" || echo "")"
    } > "$MANIFEST"
fi
ok "清单已写入"

# ──────────────────────────────────────────────────────────
step "安装完成"
# ──────────────────────────────────────────────────────────
say "  版本        $VERSION"
say "  代码        $PREFIX"
[ "$DO_BIN" = 1 ] && say "  启动器      $BINDIR/nlc"
say "  图形界面    $GTK"
say ""
say "  下一步："
if [ "$DO_BIN" = 1 ] && ! command -v nlc >/dev/null 2>&1; then
    say "   ① 把 $BINDIR 加进 PATH（当前 shell 里先执行一次）："
    say "        export PATH=\"$BINDIR:\$PATH\""
    say "      想永久生效，把这行加到 ~/.bashrc 或 ~/.profile"
fi
say "   ② 填 AI 通道的密钥（只翻译和解释需要它）："
say "        nlc set-key deepseek sk-你的key"
say "        nlc providers        # 看有哪些通道"
say "        nlc test deepseek    # 体检"
say ""
say "   ③ 试试不需要密钥的功能："
say "        nlc risk \"rm -rf /usr\"     # 判风险"
say "        nlc tokenize 'du -sh *'    # 切命令词块"
say "        nlc mcp                    # 起 MCP 服务端"
say ""
if [ "$MODE" = copy ]; then
    say "  卸载：sh $PREFIX/uninstall.sh        （保留个人数据）"
    say "        sh $PREFIX/uninstall.sh --purge（连个人数据一起删）"
    say "        （安装/卸载脚本都留了一份在安装目录里，不用再翻出原始安装包）"
else
    say "  卸载：sh $PKG_DIR/uninstall.sh"
fi
say ""
if [ "$GTK" = "不可用" ]; then
    warn "提醒：这台机器没有 GTK3，图形界面用不了；CLI 与 MCP 不受影响。"
    [ "$DESKTOP_DONE" = 1 ] && warn "桌面图标虽然注册了，但点开跑不了 —— 装好 GTK3 就能用。"
fi
