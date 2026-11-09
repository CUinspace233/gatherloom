#!/usr/bin/env python3
"""S0：对比 iOS Vision 与 Tesseract 的截图 OCR（ADR 0003）。

输入是 App 导出的 Documents/S0/ocr 目录：每张图有 <name>.json（Vision 结果）和 <name>.png
（实际送入 Vision 的图片）。可选为某张图添加人工校对文本 <name>.gt.txt，脚本会计算字符错误率（CER）。

用法：
    brew install tesseract tesseract-lang     # macOS；Linux: apt install tesseract-ocr tesseract-ocr-chi-sim
    python3 scripts/s0/ocr_compare.py path/to/S0/ocr > docs/evaluation/s0-ocr-compare.md

只用标准库；Tesseract 通过命令行调用。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def normalize(text: str) -> str:
    """去掉所有空白后比较：OCR 的换行与空格差异不计入错误。"""
    return "".join(text.split())


def cer(reference: str, hypothesis: str) -> float:
    """字符错误率 = 编辑距离 / 参考文本长度。"""
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
        previous = current
    return previous[-1] / len(ref)


def run_tesseract(image: Path, lang: str, psm: int) -> tuple[str, float]:
    started = time.perf_counter()
    result = subprocess.run(
        ["tesseract", str(image), "stdout", "-l", lang, "--psm", str(psm)],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = (time.perf_counter() - started) * 1000
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout, elapsed


def fmt_cer(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ocr_dir", type=Path, help="App 导出的 S0/ocr 目录")
    parser.add_argument("--lang", default="chi_sim+eng")
    parser.add_argument("--psm", type=int, default=3, help="Tesseract 页面分割模式，截图可试 3 / 4 / 6")
    args = parser.parse_args()

    if shutil.which("tesseract") is None:
        print("未找到 tesseract 命令", file=sys.stderr)
        return 2

    rows = []
    for meta in sorted(args.ocr_dir.glob("*.json")):
        image = meta.with_suffix(".png")
        if not image.exists():
            print(f"跳过 {meta.name}：缺少同名 PNG", file=sys.stderr)
            continue
        vision = json.loads(meta.read_text(encoding="utf-8"))
        vision_text = vision.get("fullText", "")
        try:
            tess_text, tess_ms = run_tesseract(image, args.lang, args.psm)
        except RuntimeError as error:
            print(f"{image.name} Tesseract 失败：{error}", file=sys.stderr)
            continue

        gt_path = meta.with_suffix(".gt.txt")
        gt = gt_path.read_text(encoding="utf-8") if gt_path.exists() else None
        rows.append(
            {
                "name": meta.stem,
                "size": f"{vision.get('originalPixelWidth')}×{vision.get('originalPixelHeight')}",
                "vision_chars": len(normalize(vision_text)),
                "vision_ms": vision.get("elapsedMs", 0),
                "tess_chars": len(normalize(tess_text)),
                "tess_ms": tess_ms,
                "vision_cer": cer(gt, vision_text) if gt is not None else None,
                "tess_cer": cer(gt, tess_text) if gt is not None else None,
            }
        )
        tess_out = meta.with_suffix(".tesseract.txt")
        tess_out.write_text(tess_text, encoding="utf-8")

    print(f"# S0 OCR 对比（Vision vs Tesseract {args.lang}, psm {args.psm}）\n")
    print("| 图片 | 尺寸 | Vision 字数 | Vision ms（手机） | Tesseract 字数 | Tesseract ms（本机） | Vision CER | Tesseract CER |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r['name'][:24]} | {r['size']} | {r['vision_chars']} | {r['vision_ms']:.0f} | "
            f"{r['tess_chars']} | {r['tess_ms']:.0f} | {fmt_cer(r['vision_cer'])} | {fmt_cer(r['tess_cer'])} |"
        )

    scored = [r for r in rows if r["vision_cer"] is not None]
    if scored:
        avg_v = sum(r["vision_cer"] for r in scored) / len(scored)
        avg_t = sum(r["tess_cer"] for r in scored) / len(scored)
        print(f"\n有人工校对文本的 {len(scored)} 张平均 CER：Vision {avg_v:.1%}，Tesseract {avg_t:.1%}")
    else:
        print("\n没有 .gt.txt 校对文本，仅列出字数。为 3–5 张图手工校对文本后可得到 CER。")
    print("\nTesseract 识别全文已写入各图同名 .tesseract.txt，可与 Vision 结果对照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
