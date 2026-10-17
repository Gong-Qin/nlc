#!/bin/sh
# NLC 免安装启动器 —— 放在哪儿就从哪儿跑，不依赖安装位置、不依赖当前工作目录。
#
# 用法（在本目录下）：
#     ./nlc.sh                 # 打开图形界面
#     ./nlc.sh tr "看磁盘占用"  # 直接翻译
#     ./nlc.sh mcp             # 起 MCP 服务端
#     ./nlc.sh --help
#
# 之所以能这么简单：NLC 的代码是**零第三方依赖**的（只用 Python 标准库），
# 所以不需要虚拟环境、不需要 pip install、不需要联网。
# 整个目录拷到 U 盘，插到任何有 python3 的 Linux 上就能跑。
set -e

# 解析自身真实路径（支持被软链接引用）
SELF=$0
while [ -L "$SELF" ]; do
    LINK=$(readlink "$SELF")
    case "$LINK" in
        /*) SELF=$LINK ;;
        *)  SELF=$(dirname "$SELF")/$LINK ;;
    esac
done
DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd)

# 找 python3
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "NLC 需要 Python 3.8 或更高版本，但没找到 python3。" >&2
    echo "  Debian/Ubuntu: sudo apt install python3" >&2
    echo "  Fedora:        sudo dnf install python3" >&2
    echo "  Arch:          sudo pacman -S python" >&2
    exit 127
fi

# 不带参数时默认开图形界面（没有图形环境会自动给出提示）
if [ $# -eq 0 ]; then
    set -- gui
fi

exec "$PY" -c 'import sys
sys.path.insert(0, sys.argv[1])
del sys.argv[1]
from nlc.cli import main
sys.exit(main())' "$DIR" "$@"
