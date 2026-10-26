"""run_mcp.sh: offline režim jen s celými vahami modelu v cache."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "run_mcp.sh"


def offline_flag(tmp_path: Path, weights_bytes) -> str:
    snap = tmp_path / "hub" / "models--BAAI--bge-m3" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    if weights_bytes is not None:
        blob = tmp_path / "blob"
        with open(blob, "wb") as f:
            f.truncate(weights_bytes)                     # řídký soubor, nezabírá disk
        (snap / "pytorch_model.bin").symlink_to(blob)     # jako v HF cache: symlink do blobs/
    # kopie skriptu, která místo spuštění serveru jen vypíše proměnnou
    text = SCRIPT.read_text().replace("exec .venv/bin/fastmcp run main.py:mcp", 'echo "OFFLINE=${HF_HUB_OFFLINE:-}"')
    script = tmp_path / "run.sh"
    script.write_text(text)
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
    env.update(HF_HOME=str(tmp_path), ES_REMOTE_URL="http://x:9200")
    out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=True).stdout
    return out.strip().splitlines()[-1]


def test_offline_with_complete_weights(tmp_path):
    assert offline_flag(tmp_path, 2_300_000_000) == "OFFLINE=1"


def test_online_when_weights_empty(tmp_path):          # stav po promazání cache
    assert offline_flag(tmp_path, 0) == "OFFLINE="


def test_online_without_weights(tmp_path):
    assert offline_flag(tmp_path, None) == "OFFLINE="
