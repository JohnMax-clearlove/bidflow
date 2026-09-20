#!/usr/bin/env bash
# BidFlow Linux/macOS 安装器（供 curl -fsSL .../install.sh | bash 或 npm 包装器调用）。
#
# 与 Windows 版 install.ps1 同一布局：用 uv 在安装根目录内建立隔离 release，
# 新入口通过 doctor 检查后才切换稳定入口与 state.json；不要求管理员权限，
# 不配置模型 API，不自动签章，不提交投标。完整 Word 分页与链接验收仍需
# Windows 桌面版 Microsoft Word；Linux/macOS 可用于文档生成、解析、检索与审核准备。
#
# 用法：
#   install.sh [--ref main|标签|40位SHA] [--with-ocr|--no-ocr]
#              [--install-root 绝对路径] [--no-path]
#   install.sh --rollback          回滚上一版本（离线）
#   install.sh --uninstall         卸载（离线）
# 环境变量：BIDFLOW_REF、BIDFLOW_INSTALL_ROOT
#
# 安全说明：只从 astral.sh 官方 HTTPS 地址安装固定版本 uv；只从 GitHub 固定
# commit 归档安装 bidflow-local；复用系统已有 uv，不升级、不卸载共享 uv/Python；
# 非空安装目录必须带 BidFlow 产品标记，否则拒绝占用。

set -euo pipefail

PRODUCT="bidflow-local"
REPO="JohnMax-clearlove/bidflow"
UV_VERSION="0.12.15"
UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"
CORE_MODULES="pydantic docx pdfplumber pypdfium2 pypdf openpyxl PIL"

REF="${BIDFLOW_REF:-main}"
ROOT="${BIDFLOW_INSTALL_ROOT:-$HOME/.bidflow}"
WITH_OCR="false"
ROLLBACK="false"
UNINSTALL="false"
NO_PATH="false"

say_step() { printf '[BidFlow] %s\n' "$1"; }
say_note() { printf '          %s\n' "$1"; }
say_warn() { printf '[BidFlow 警告] %s\n' "$1"; }
say_error() { printf '[BidFlow 安装失败] %s\n' "$1" >&2; }

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---------------- 参数解析与互斥校验（任何下载/修改之前完成） ----------------
while [ $# -gt 0 ]; do
  case "$1" in
    --with-ocr) WITH_OCR="true" ;;
    --no-ocr) WITH_OCR="false" ;;
    --ref) [ $# -ge 2 ] || { say_error "--ref 需要参数"; exit 2; }; REF="$2"; shift ;;
    --install-root|-r) [ $# -ge 2 ] || { say_error "--install-root 需要参数"; exit 2; }; ROOT="$2"; shift ;;
    --no-path) NO_PATH="true" ;;
    --rollback) ROLLBACK="true" ;;
    --uninstall) UNINSTALL="true" ;;
    -h|--help) usage; exit 0 ;;
    *) say_error "未知参数：$1"; usage; exit 2 ;;
  esac
  shift
done

if [ "$ROLLBACK" = "true" ] && [ "$UNINSTALL" = "true" ]; then
  say_error "--rollback 与 --uninstall 不能同时使用。"
  exit 2
fi
if { [ "$ROLLBACK" = "true" ] || [ "$UNINSTALL" = "true" ]; } && [ "$REF" != "${BIDFLOW_REF:-main}" ]; then
  say_error "--ref 只能用于安装或更新。"
  exit 2
fi

case "$REF" in
  main|[A-Za-z0-9._-]*) ;;
  *) say_error "Ref 取值不合法：$REF"; exit 2 ;;
esac

case "$ROOT" in
  /*) ;;
  *) say_error "安装根目录必须是本机绝对路径：$ROOT"; exit 2 ;;
esac
ROOT="${ROOT%/}"
if [ "$ROOT" = "/" ] || [ "$ROOT" = "$HOME" ]; then
  say_error "拒绝把盘根或用户主目录本身作为安装根目录：$ROOT"
  exit 2
fi

is_forty_sha() { printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$'; }

resolve_sha() {
  if is_forty_sha "$REF"; then
    printf '%s' "$REF"
    return 0
  fi
  say_step "把 $REF 经 GitHub API 解析为完整提交 SHA ..."
  curl -fsSL -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${REPO}/commits/${REF}" \
    | sed -n 's/.*"sha"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p' \
    | head -n 1
}

# state.json 由本脚本或 install.ps1 以固定行格式写出；需要字段时用 sed 提取。

# doctor 轻量校验：bash 不做完整 JSON 解析，逐项 grep 并如实提示警告。
check_doctor() {
  local entry="$1" ocr="$2" report
  say_step "运行 bidflow doctor 检查入口 ..."
  if ! report="$("$entry" doctor 2>/dev/null)"; then
    say_error "doctor 命令执行失败。"
    return 1
  fi
  DOCTOR_REPORT="$report"
  printf '%s\n' "$report" | grep -q '"ok"[[:space:]]*:[[:space:]]*true' || { say_error "doctor 未通过（未输出 ok:true）。"; return 1; }
  printf '%s\n' "$report" | grep -Eq '"python"[[:space:]]*:[[:space:]]*"3\.12\.' || { say_error "Python 版本不符合要求（需要 3.12）。"; return 1; }
  local module missing=""
  for module in $CORE_MODULES; do
    printf '%s\n' "$report" | grep -q "\"${module}\"[[:space:]]*:[[:space:]]*true" || missing="${missing}${module}、"
  done
  if [ -n "$missing" ]; then
    say_error "缺少核心依赖：${missing%、}"
    return 1
  fi
  if [ "$ocr" = "true" ]; then
    for module in rapidocr onnxruntime; do
      printf '%s\n' "$report" | grep -q "\"${module}\"[[:space:]]*:[[:space:]]*true" || { say_error "已选择 OCR，但缺少依赖：$module"; return 1; }
    done
  fi
  if ! printf '%s\n' "$report" | grep -q '"word_registered"[[:space:]]*:[[:space:]]*true'; then
    say_warn '未检测到桌面版 Microsoft Word：最终页码与链接验收需在装有 Word 的 Windows 上完成。'
  fi
  if ! printf '%s\n' "$report" | grep -q '"pandoc"[[:space:]]*:[[:space:]]*null'; then
    : # 有 Pandoc
  else
    say_note '未检测到 Pandoc：Word/PDF 结构化转换能力受限，核心流程不受影响。'
  fi
  return 0
}
DOCTOR_REPORT=""

doctor_version() {
  printf '%s\n' "$DOCTOR_REPORT" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}

write_stable_entry() {
  local rel="$1" bin_dir="$2"
  mkdir -p "$bin_dir"
  {
    printf '#!/usr/bin/env bash\n'
    printf '# BidFlow stable entry managed by install.sh - do not edit.\n'
    printf 'SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
    printf 'exec "$SELF_DIR/../%s" "$@"\n' "$rel"
  } > "$bin_dir/bidflow"
  chmod +x "$bin_dir/bidflow"
}

write_state() {
  local state="$1" cur_release="$2" cur_entry="$3" cur_sha="$4" cur_ocr="$5" cur_version="$6" prev_json="$7" bin_dir="$8" path_added="$9"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "product": "%s",\n' "$PRODUCT"
    printf '  "install_root": "%s",\n' "$ROOT"
    printf '  "current": {"release": "%s", "entry": "%s", "sha": "%s", "ocr": %s, "version": "%s", "installed_at": "%s"},\n' \
      "$cur_release" "$cur_entry" "$cur_sha" "$cur_ocr" "$cur_version" "$(now_iso)"
    printf '  "previous": %s,\n' "$prev_json"
    printf '  "path_entry": "%s",\n' "$bin_dir"
    printf '  "path_added": %s\n' "$path_added"
    printf '}\n'
  } > "$state"
}

previous_object_json() {
  local state="$1"
  [ -f "$state" ] || { printf 'null'; return 0; }
  local extracted
  extracted="$(sed -n 's/.*"previous": {\("release"[^{]*\)}.*/{\1}/p' "$state" | head -n 1)"
  if [ -n "$extracted" ]; then
    printf '%s' "$extracted"
  else
    printf 'null'
  fi
}

assert_bidflow_root() {
  if [ -e "$ROOT" ] && [ -n "$(ls -A "$ROOT" 2>/dev/null)" ]; then
    if ! grep -q "\"product\"[[:space:]]*:[[:space:]]*\"${PRODUCT}\"" "$ROOT/.bidflow-install.json" 2>/dev/null; then
      say_error "安装根目录已存在且不是 BidFlow 管理的目录：$ROOT"
      say_note '请改用空目录，或显式选择其他 --install-root。'
      exit 2
    fi
  fi
}

do_uninstall() {
  local state="$1"
  say_step "卸载本安装器管理的内容：$ROOT"
  # release 目录：只删除带产品标记的
  if [ -d "$ROOT/releases" ]; then
    local dir marker
    for dir in "$ROOT"/releases/*; do
      [ -d "$dir" ] || continue
      if grep -q "\"product\"[[:space:]]*:[[:space:]]*\"${PRODUCT}\"" "$dir/.bidflow-release.json" 2>/dev/null; then
        rm -rf "$dir"
      else
        say_warn "保留缺少产品标记的目录：$dir"
      fi
    done
    rmdir "$ROOT/releases" 2>/dev/null || true
  fi
  for sub in runtime cache; do
    if [ -d "$ROOT/$sub" ]; then
      if grep -q "\"product\"[[:space:]]*:[[:space:]]*\"${PRODUCT}\"" "$ROOT/$sub/.bidflow-$sub.json" 2>/dev/null; then
        rm -rf "$ROOT/$sub"
      else
        say_warn "保留缺少产品标记的目录：$ROOT/$sub"
      fi
    fi
  done
  # 稳定入口：只删除带我们注释标记的 bidflow
  if [ -f "$ROOT/bin/bidflow" ]; then
    if head -n 2 "$ROOT/bin/bidflow" 2>/dev/null | grep -q 'BidFlow stable entry managed by install.sh'; then
      rm -f "$ROOT/bin/bidflow"
    else
      say_warn "保留不是本安装器生成的入口文件：$ROOT/bin/bidflow"
    fi
  fi
  rmdir "$ROOT/bin" 2>/dev/null || true
  rm -f "$ROOT/state.json"
  local left
  left="$(ls -A "$ROOT" 2>/dev/null | grep -v '^\.bidflow-install\.json$' || true)"
  if [ -z "$left" ]; then
    rm -f "$ROOT/.bidflow-install.json"
    rmdir "$ROOT" 2>/dev/null || true
    say_step "已卸载并删除安装目录：$ROOT"
  else
    say_step '已卸载本安装器管理的内容。'
    say_warn '以下内容不属于 BidFlow 管理范围，已保留：'
    printf '          %s\n' "$left"
    say_note '如需完全删除，请确认这些文件无用后手动清理；再次安装前请改用空目录。'
  fi
}

do_rollback() {
  local state="$ROOT/state.json"
  if [ ! -f "$state" ]; then
    say_error "未找到安装状态文件：$state"
    exit 1
  fi
  # 先完整读取 current 与 previous，再写任何文件，避免中途改坏状态。
  local cur_json prev_release prev_entry prev_sha prev_ocr prev_version
  cur_json="$(sed -n 's/.*"current": {\("release"[^{]*\)}.*/\1/p' "$state" | head -n 1)"
  prev_release="$(sed -n 's/.*"previous": {"release": "\([^"]*\)".*/\1/p' "$state" | head -n 1)"
  prev_entry="$(sed -n 's/.*"previous": {"release": "[^"]*", "entry": "\([^"]*\)".*/\1/p' "$state" | head -n 1)"
  prev_sha="$(sed -n 's/.*"previous": {"release": "[^"]*", "entry": "[^"]*", "sha": "\([^"]*\)".*/\1/p' "$state" | head -n 1)"
  prev_ocr="$(sed -n 's/.*"previous": {"release": "[^"]*", "entry": "[^"]*", "sha": "[^"]*", "ocr": \(true\|false\).*/\1/p' "$state" | head -n 1)"
  prev_version="$(sed -n 's/.*"previous": {"release": "[^"]*", "entry": "[^"]*", "sha": "[^"]*", "ocr": "\(true\|false\)", "version": "\([^"]*\)".*/\2/p' "$state" | head -n 1)"
  if [ -z "$prev_entry" ]; then
    say_error "没有可回滚的上一版本。"
    exit 1
  fi
  local prev_abs="$ROOT/$prev_entry"
  [ -x "$prev_abs" ] || { say_error "上一版本入口不存在：$prev_abs"; exit 1; }
  check_doctor "$prev_abs" "false" || { say_error "上一版本未通过 doctor 检查，保持当前版本不变。"; exit 1; }
  write_stable_entry "$prev_entry" "$ROOT/bin"
  write_state "$state" "$prev_release" "$prev_entry" "$prev_sha" "$prev_ocr" "$prev_version" "{\"$cur_json\"}" "$ROOT/bin" "true"
  say_step "已回滚到上一版本：$prev_release（$prev_version）。"
}

do_install() {
  assert_bidflow_root
  mkdir -p "$ROOT/bin" "$ROOT/releases" "$ROOT/cache"
  printf '{\n  "schema_version": 1,\n  "product": "%s",\n  "kind": "install-root",\n  "created_at": "%s"\n}\n' \
    "$PRODUCT" "$(now_iso)" > "$ROOT/.bidflow-install.json"
  local state="$ROOT/state.json"

  local sha
  sha="$(resolve_sha)"
  [ -n "$sha" ] || { say_error "无法解析 Ref：$REF（多为网络或 GitHub 访问受限，可改用完整 40 位 SHA）。"; exit 1; }
  say_step "目标提交：$sha"

  local UV=""
  if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
    say_step "复用已有 uv：$UV"
  else
    local runtime="$ROOT/runtime"
    mkdir -p "$runtime"
    say_step "未检测到 uv，从官方地址安装固定版本 uv $UV_VERSION 到 $runtime/uv ..."
    curl -fsSL "$UV_INSTALLER_URL" | env UV_INSTALL_DIR="$runtime/uv" UV_NO_MODIFY_PATH=1 sh || {
      say_error "uv 安装器执行失败。"
      exit 1
    }
    [ -x "$runtime/uv/uv" ] || { say_error "uv 安装完成后未找到可执行文件：$runtime/uv/uv"; exit 1; }
    UV="$runtime/uv/uv"
    printf '{\n  "schema_version": 1,\n  "product": "%s",\n  "kind": "uv-runtime",\n  "uv_version": "%s",\n  "created_at": "%s"\n}\n' \
      "$PRODUCT" "$UV_VERSION" "$(now_iso)" > "$runtime/.bidflow-runtime.json"
    say_step "已安装独立 uv 到 $runtime/uv（不改系统 PATH，不升级或卸载共享 uv）。"
  fi

  local kind="core"
  [ "$WITH_OCR" = "true" ] && kind="ocr"
  local release_name="${sha:0:12}-${kind}-$(now_iso | tr -cd '0-9')-${RANDOM}"
  local release_dir="$ROOT/releases/$release_name"
  mkdir -p "$release_dir"
  printf '{\n  "schema_version": 1,\n  "product": "%s",\n  "sha": "%s",\n  "ocr": %s,\n  "created_at": "%s"\n}\n' \
    "$PRODUCT" "$sha" "$WITH_OCR" "$(now_iso)" > "$release_dir/.bidflow-release.json"

  local extra=""
  [ "$WITH_OCR" = "true" ] && extra="[ocr]"
  local spec="${PRODUCT}${extra} @ https://codeload.github.com/${REPO}/tar.gz/${sha}"
  say_step "安装 ${PRODUCT}${extra}（固定提交 ${sha:0:12}）..."
  say_note "uv tool install --python 3.12 \"$spec\""
  if ! env \
    UV_TOOL_DIR="$release_dir/tool" \
    UV_TOOL_BIN_DIR="$release_dir/bin" \
    UV_PYTHON_INSTALL_DIR="$release_dir/python" \
    UV_CACHE_DIR="$ROOT/cache" \
    UV_NO_MODIFY_PATH=1 \
    "$UV" tool install --python 3.12 "$spec"; then
    say_warn "本次安装未通过，清理未启用的 release 目录：$release_dir"
    rm -rf "$release_dir"
    say_error "uv tool install 失败。"
    exit 1
  fi

  local entry="$release_dir/bin/bidflow"
  if [ ! -e "$entry" ]; then
    say_warn "本次安装未通过，清理未启用的 release 目录：$release_dir"
    rm -rf "$release_dir"
    say_error "uv 安装完成后未找到 bidflow 入口：$release_dir/bin"
    exit 1
  fi

  if ! check_doctor "$entry" "$WITH_OCR"; then
    say_warn "本次安装未通过，清理未启用的 release 目录：$release_dir"
    rm -rf "$release_dir"
    exit 1
  fi
  local version
  version="$(doctor_version)"
  [ -n "$version" ] || version="unknown"

  # 通过 doctor 后才切换稳定入口与状态
  local path_added="false"
  case ":$PATH:" in
    *":$ROOT/bin:"*) path_added="true" ;;
  esac
  if [ "$NO_PATH" != "true" ] && [ "$path_added" != "true" ]; then
    local rc_file marker="# bidflow installer (added by install.sh) - do not edit"
    for rc_file in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [ -f "$rc_file" ] && ! grep -qF "$ROOT/bin" "$rc_file" 2>/dev/null; then
        {
          printf '\n%s\n' "$marker"
          printf 'export PATH="%s:$PATH"\n' "$ROOT/bin"
        } >> "$rc_file"
        path_added="true"
      fi
    done
    if [ "$path_added" = "true" ]; then
      say_step "已把 $ROOT/bin 加入用户 PATH（仅对新终端生效）。"
    fi
  fi

  local prev_json
  prev_json="$(previous_object_json "$state")"
  local rel="releases/$release_name/bin/bidflow"
  write_stable_entry "$rel" "$ROOT/bin"
  write_state "$state" "$release_name" "$rel" "$sha" "$WITH_OCR" "$version" "$prev_json" "$ROOT/bin" "$path_added"

  local kind_text="核心依赖"
  [ "$WITH_OCR" = "true" ] && kind_text="含 OCR"
  say_step "安装完成，已通过新入口的 doctor 检查。"
  say_note "安装目录：$ROOT"
  say_note "当前版本：$version（提交 ${sha:0:12}，$kind_text）"
  say_note '下一步（请新开一个终端，或重新打开 Agent，以继承新的 PATH）：'
  say_note '  bidflow doctor'
  say_note '  bidflow init "项目名" --path "~/投标项目/项目名"'
  say_note '更新：重复运行安装命令；回滚：install.sh --rollback；卸载：install.sh --uninstall。'
}

# ---------------- 入口 ----------------
if [ "$UNINSTALL" = "true" ]; then
  assert_bidflow_root
  do_uninstall
elif [ "$ROLLBACK" = "true" ]; then
  assert_bidflow_root
  do_rollback
else
  do_install
fi
