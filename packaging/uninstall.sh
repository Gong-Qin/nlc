#!/bin/sh
# NLC 卸载脚本
#
# 安全原则：**只删安装清单里记着的、我们自己创建的东西。**
# 不猜、不扫、不碰用户目录里的其它文件。
#
# 用法：
#   sh uninstall.sh                 # 卸载程序，保留个人数据（密钥、词库、黑名单、配置）
#   sh uninstall.sh --purge         # 连个人数据一起删（会二次确认）
#   sh uninstall.sh --prefix DIR    # 指定当初装到哪
#   sh uninstall.sh --dry-run       # 只列出将要删除什么，不真删
set -e

PKG_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PURGE=0
DRY=0
PREFIX=""

while [ $# -gt 0 ]; do
    case "$1" in
        --purge)    PURGE=1; shift ;;
        --dry-run)  DRY=1; shift ;;
        --prefix)   PREFIX=$2; shift 2 ;;
        --prefix=*) PREFIX=${1#*=}; shift ;;
        -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m  ✔ %s\033[0m\n' "$*"; }

# ── 定位安装位置 ──
MANIFEST=""

if [ -n "$PREFIX" ]; then
    # ★ 显式传了 --prefix，就以它为准：找不到清单就**报错退出**，
    #   绝不偷偷回落到默认位置。
    #   早期版本会回落 —— 结果是 `--prefix /` 这种危险输入不但没被拒绝，
    #   反而把默认位置的安装给卸了，还报"卸载完毕"。参数被静默忽略是很危险的。
    case "$PREFIX" in
        /|""|"$HOME"|"$HOME/")
            warn "拒绝：--prefix 指向危险路径（$PREFIX）"
            say "  出于安全，本脚本不会对根目录或家目录本身做任何操作。"
            exit 1 ;;
    esac
    if [ ! -f "$PREFIX/.nlc-install-manifest" ]; then
        warn "拒绝：$PREFIX 里没有 NLC 安装清单（.nlc-install-manifest）"
        say "  · 请确认路径是否写对"
        say "  · 本脚本只卸载「带清单」的 NLC 安装，不猜着删"
        exit 1
    fi
    MANIFEST="$PREFIX/.nlc-install-manifest"
else
    # 没指定就按 包目录 → 包目录上一层（就地模式） → 默认位置 依次找
    for cand in "$PKG_DIR" "$PKG_DIR/.." "${XDG_DATA_HOME:-$HOME/.local/share}/nlc"; do
        if [ -f "$cand/.nlc-install-manifest" ]; then
            MANIFEST="$cand/.nlc-install-manifest"
            break
        fi
    done
fi

if [ -z "$MANIFEST" ]; then
    warn "没找到安装清单（.nlc-install-manifest）。"
    warn "  · 如果当初装到别处，用 --prefix /那个/路径"
    warn "  · 或者直接手动删掉安装目录"
    say ""
    say "  出于安全，本脚本**不会**在没有清单的情况下猜着删任何东西。"
    exit 1
fi

# 读清单
. "$MANIFEST" 2>/dev/null || true
PREFIX=${prefix:-$(dirname "$MANIFEST")}
BINPATH=${bin:-}
DESKTOPFILE=${desktop:-}
ICONFILE=${icon:-}
MODE=${mode:-copy}

step "将要卸载"
say "  代码目录    $PREFIX   （模式：$MODE）"
[ -n "$BINPATH" ]    && say "  启动器      $BINPATH"
[ -n "$DESKTOPFILE" ] && say "  桌面图标    $DESKTOPFILE"
[ -n "$ICONFILE" ]   && say "  图标文件    $ICONFILE"
say "  安装清单    $MANIFEST"

# ── ★ 安全阀：绝不允许删掉危险路径 ──
step "安全检查"
case "$PREFIX" in
    /|"" )           warn "拒绝：安装目录是根目录"; exit 1 ;;
    "$HOME")         warn "拒绝：安装目录是家目录本身"; exit 1 ;;
    "$HOME/")        warn "拒绝：安装目录是家目录本身"; exit 1 ;;
esac
# 路径至少要两层，比如 /opt/nlc、~/.local/share/nlc
_depth=$(printf '%s' "$PREFIX" | tr -cd '/' | wc -c)
if [ "$_depth" -lt 2 ]; then
    warn "拒绝：安装目录太浅（$PREFIX），不像是一个专门的安装位置"
    exit 1
fi
# 必须确实是 NLC 的安装目录
if [ "$MODE" != "portable" ] && [ ! -f "$PREFIX/nlc/__init__.py" ]; then
    warn "拒绝：$PREFIX 里没有 NLC 代码，不像是本程序装的地方"
    exit 1
fi
ok "通过（不是危险路径，且确认是 NLC 的安装目录）"

# ── 删 ──
step "开始卸载"
rm_one() {
    # $1 路径  $2 说明
    [ -z "$1" ] && return 0
    if [ ! -e "$1" ]; then
        say "  跳过（本来就没有）  $2"
        return 0
    fi
    if [ "$DRY" = 1 ]; then
        printf '  [预演] 删除 %s  （%s）\n' "$1" "$2"
    else
        rm -f "$1" 2>/dev/null || rm -rf "$1"
        printf '  已删除 %s  （%s）\n' "$1" "$2"
    fi
}

rm_one "$BINPATH" "启动器"
rm_one "$DESKTOPFILE" "桌面图标"
# 桌面上的那份入口
for d in "$HOME/桌面" "$HOME/Desktop"; do
    [ -f "$d/nlc.desktop" ] && rm_one "$d/nlc.desktop" "桌面入口"
done
[ -n "$ICONFILE" ] && rm_one "$ICONFILE" "图标文件"

if [ "$MODE" = portable ]; then
    say "  就地模式：代码目录（$PREFIX）是你自己的目录，不删"
    [ "$DRY" != 1 ] && rm -f "$MANIFEST" 2>/dev/null || true
else
    rm_one "$PREFIX" "代码目录（含清单，整目录删除）"
fi

# 刷新桌面数据库
if [ "$DRY" != 1 ] && command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${XDG_DATA_HOME:-$HOME/.local/share}/applications" >/dev/null 2>&1 || true
fi

# ── 个人数据 ──
CFG="${NLC_CONFIG_DIR:-$HOME/.config/nlc}"
step "个人数据"
if [ -d "$CFG" ]; then
    say "  位置        $CFG"
    if [ "$PURGE" = 1 ]; then
        if [ "$DRY" = 1 ]; then
            printf '  [预演] 删除 %s（含 API 密钥、词库、黑名单、配置）\n' "$CFG"
        else
            printf '\033[31m  这会删掉你的 API 密钥、个人词库、黑名单和全部设置。\033[0m\n'
            printf '  确认请输入 yes：'
            read -r ANS
            if [ "$ANS" = "yes" ]; then
                rm -rf "$CFG"
                ok "个人数据已删除"
            else
                say "  已取消，个人数据保留在 $CFG"
            fi
        fi
    else
        say "  已保留（密钥、词库、黑名单、配置都在）"
        say "  连它一起删：sh $0 --purge"
    fi
else
    say "  没有个人数据目录"
fi

step "完成"
if [ "$DRY" = 1 ]; then
    say "  以上是预演，什么都没动。去掉 --dry-run 才会真的删。"
else
    say "  卸载完毕。"
fi
