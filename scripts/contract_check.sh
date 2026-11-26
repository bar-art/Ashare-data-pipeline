#!/bin/sh
# 跨项目格式联合检查：用本地缓存导出两种格式，再分别用两个下游项目自己的环境和导入代码读取。
# 只读下游项目，不修改它们；需要先有缓存数据（quant-agent export 运行过一次即可，之后无需联网）。
# 用法：sh scripts/contract_check.sh [代码...]，默认 600000.SH 600518.SH
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
# 可选的开发检查：下游项目是独立仓库，位置可用环境变量指定；找不到时跳过，不影响本项目。
QRL="${QRL_DIR:-$ROOT/../quant-research-lab}"
ALPHA="${ALPHA_DIR:-$ROOT/../alpha-evidence-lab}"
if [ ! -x "$QRL/.venv/bin/python" ] || [ ! -x "$ALPHA/.venv/bin/python" ]; then
  echo "未找到下游项目环境（可设置 QRL_DIR / ALPHA_DIR）；跳过格式联合检查。"
  exit 0
fi
CODES="${*:-600000.SH 600518.SH}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for target in quant-research-lab alpha-evidence-lab; do
  .venv/bin/quant-agent export --provider baostock --codes $CODES \
    --start 2020-01-02 --end 2024-12-31 --target "$target" --output "$WORK/$target" >/dev/null
done

"$QRL/.venv/bin/python" -I - "$WORK/quant-research-lab" "$QRL/src" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from quant_research_lab.data import MarketDataset
out = Path(sys.argv[1])
data = MarketDataset.read(out / "bars.csv", out / "manifest.json")
print(f"quant-research-lab：读取成功，{len(data.frame)} 行，{len(data.symbols)} 只，{len(data.dates)} 个交易日")
PY

"$ALPHA/.venv/bin/python" -I - "$WORK/alpha-evidence-lab" "$ALPHA/src" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from alpha_evidence_lab.data import load_csv
for path in sorted(Path(sys.argv[1]).glob("*.csv")):
    series = load_csv(path, path.stem, "contract check")
    blocked = sum(1 for bar in series.bars if bar.is_suspended)
    print(f"alpha-evidence-lab：{path.name} 读取成功，{len(series.bars)} 行，停牌 {blocked} 行")
PY
echo "格式联合检查通过"
