#!/usr/bin/env bash
# 拼接 Safari 分享预处理脚本：Readability（Apache-2.0）+ Gatherloom 包装。
# 输出：ios/ShareExtension/Resources/GatherloomPreprocess.js（入库，Mac 上无需 Node 即可构建 App）
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
src="$root/ios/Preprocess"
out="$root/ios/ShareExtension/Resources/GatherloomPreprocess.js"
expected_sha="34dcab3d0832d0019f02990eed6b6124e029e8c32b9f0c6f2550544ff8dff174"

if command -v sha256sum >/dev/null; then
  actual_sha="$(sha256sum "$src/vendor/Readability.js" | cut -d' ' -f1)"
else
  actual_sha="$(shasum -a 256 "$src/vendor/Readability.js" | cut -d' ' -f1)"
fi
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "vendor/Readability.js 校验值不符（期望 @mozilla/readability 0.6.0）" >&2
  exit 1
fi

{
  echo "/* 本文件由 scripts/s0/build_preprocess_js.sh 生成，请勿直接编辑。 */"
  echo "/* 包含 @mozilla/readability 0.6.0（Apache License 2.0，见 ios/Preprocess/vendor/LICENSE.md）。 */"
  echo "(function () {"
  echo "var module = undefined;"
  cat "$src/vendor/Readability.js"
  echo
  echo "window.Readability = Readability;"
  echo "})();"
  echo "var Readability = window.Readability;"
  cat "$src/src/preprocess.js"
} > "$out"
echo "已生成 $out"
