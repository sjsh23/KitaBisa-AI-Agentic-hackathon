"""TEST ONLY: writes a FAKE file shaped like a PIHPS Excel export.

The numbers are invented. It exists so the pipeline can be tested without
internet access. Never use it for the real dataset or the demo.
"""
import sys

import numpy as np
import openpyxl
import pandas as pd

out = sys.argv[1] if len(sys.argv) > 1 else "tests/FAKE_pihps_export.xlsx"
rng = np.random.default_rng(0)
days = pd.bdate_range("2024-09-01", "2026-10-02")
t = np.arange(len(days))
lebaran = [pd.Timestamp(x) for x in ("2025-03-31", "2026-03-20")]
bump = sum(np.exp(-(((days - f).days.values + 15) / 12.0) ** 2) * 900 for f in lebaran)
sugar = 16500 + 4 * t + bump + np.cumsum(rng.normal(0, 25, len(days)))
oil = 17500 + 2 * t + np.cumsum(rng.normal(0, 30, len(days)))

wb = openpyxl.Workbook()
ws = wb.active
ws.append(["FAKE TEST DATA - not from PIHPS"])
ws.append(["No.", "Komoditas (Rp)"] + [d.strftime("%d/%m/%Y") for d in days])
fmt = lambda xs: [f"{round(x / 50) * 50:,.0f}" for x in xs]
ws.append(["IX", "Minyak Goreng"] + fmt(oil + 1500))
ws.append(["1", "Minyak Goreng Curah"] + fmt(oil))
ws.append(["X", "Gula Pasir"] + fmt(sugar + 600))
ws.append(["1", "Gula Pasir Kualitas Premium"] + fmt(sugar + 1200))
ws.append(["2", "Gula Pasir Lokal"] + fmt(sugar))
wb.save(out)
print("wrote", out)
