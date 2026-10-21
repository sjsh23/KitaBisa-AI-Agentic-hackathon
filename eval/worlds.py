"""Extra simulated worlds for the Monte Carlo (FR-EVA-4, D-1, D-3, simulated data).

An extra world is the same generator (data_gen/generate.py) with the same scripted scenarios
S1, S2, S3; only the generator seed differs (config.SEED, the seed of World). It is written
into a temporary folder, read by the agents through their own CsvAdapter on that folder, and
deleted afterwards. The tracked dataset under data_gen/out is never written.

The generator seed changes the history the agents see (POs, goods receipts, quotes, spare
capacity, the SAP-RPT context), not the true supplier behaviour: that comes from
data_gen/config.py, the same in every world.

Only eval/ may run the generator for this or read _truth/ (D-1).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DATA_GEN = Path(__file__).resolve().parent.parent / "data_gen"
TRACKED_OUT = DATA_GEN / "out"
# Pre-declared on 21 Oct 2026, before any multi-world result was seen: the tracked dataset
# (generator seed 42) plus these generator seeds. Do not change after seeing results.
PREDECLARED_SEEDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)

# Runs inside data_gen/ (the PIHPS paths in config.py are relative to it). config.SEED is set
# before `generate` imports `world`, so World's default seed is the requested one.
_GENERATE = """
import sys
import config as C
C.SEED = int(sys.argv[1])
import generate
sys.argv = ["generate.py", "--out", sys.argv[2]]
generate.main()
"""


def tracked_seed() -> int:
    """Generator seed of the tracked dataset (data_gen/out/meta.json)."""
    return int(json.loads((TRACKED_OUT / "meta.json").read_text(encoding="utf-8"))["seed"])


def folder_hashes(folder: Path = TRACKED_OUT) -> dict[str, str]:
    """sha256 of every file under `folder`, by relative path (to prove it is untouched)."""
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def generate_world(seed: int, out_dir: Path) -> dict:
    """Run the generator with generator seed `seed` into `out_dir`; returns its meta.json."""
    out_dir = Path(out_dir).resolve()
    if out_dir == TRACKED_OUT.resolve() or TRACKED_OUT.resolve() in out_dir.parents:
        raise ValueError("an extra world must not be written into data_gen/out")
    done = subprocess.run([sys.executable, "-c", _GENERATE, str(seed), str(out_dir)],
                          cwd=DATA_GEN, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"generator seed {seed} failed: {done.stderr.strip()[-500:]}")
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    if int(meta["seed"]) != seed:
        raise RuntimeError(f"generator wrote seed {meta['seed']}, asked for {seed}")
    return meta


@contextmanager
def temporary_world(seed: int) -> Iterator[Path]:
    """A generated world in a fresh temporary folder, deleted when the block ends."""
    folder = Path(tempfile.mkdtemp(prefix=f"gesit_world_{seed}_"))
    try:
        generate_world(seed, folder)
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)
