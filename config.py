"""All tunable assumptions for the GESIT synthetic data, in one place.

Everything here is an ASSUMPTION except the price anchor, which comes from
PIHPS (Bank Indonesia). Write these down in the assumptions page of the deck.
Supplier and buyer names are fictional.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SEED = 42

# Simulated history window. AS_OF is "today" for the demo: POs whose goods
# receipt would fall after AS_OF are still open.
HISTORY_START = "2024-10-01"
AS_OF = "2026-09-30"

# ---------------------------------------------------------------- price anchor
# Files exported from PIHPS (Excel from the website, or output of fetch_pihps.py).
# Globs are allowed. Use the "Pedagang Besar" (wholesale) market type.
PIHPS_FILES = ["data/pihps/*.xlsx", "data/pihps/pihps_long.csv"]
PIHPS_MARKET = "Pedagang Besar"  # documentation only; set when downloading
# Wholesale price is used as-is. If you only have retail (Pasar Tradisional)
# data, set this to about 0.93 to approximate a bulk B2B price, and say so.
PRICE_SCALE = 1.0

# ---------------------------------------------------------------- company
COMPANY = {
    "BUKRS": "1000",          # company code
    "EKORG": "1000",          # purchasing org
    "EKGRP": "P01",           # purchasing group (raw materials)
    "WERKS": "1100",          # plant
    "WERKS_NAME": "Pabrik Cikarang (fictional F&B producer)",
}
# Cost of a production line stop when a material runs out (assumption, IDR/day).
DOWNTIME_COST_PER_DAY = 150_000_000


@dataclass
class Material:
    matnr: str
    maktx: str
    pihps_series: str          # row name in PIHPS, e.g. "Gula Pasir Lokal"
    meins: str = "KG"
    daily_usage_kg: float = 3_300
    order_qty_kg: tuple = (3_000, 8_000)    # range of one PO
    order_every_days: tuple = (1, 2)        # gap between POs (calendar days)
    safety_stock_days: float = 5
    stock_at_as_of_days: float = 6          # days of cover on hand at AS_OF
    enabled: bool = True


MATERIALS = [
    Material("RM-GULA-001", "Gula Pasir Lokal (GKP), karung 50 kg", "Gula Pasir Lokal"),
    # Optional second material, also anchored on PIHPS. Turn on if you need more rows.
    Material("RM-MGOR-001", "Minyak Goreng Curah", "Minyak Goreng Curah",
             daily_usage_kg=1_200, order_qty_kg=(2_000, 6_000), order_every_days=(3, 7),
             enabled=False),
]


@dataclass
class Supplier:
    lifnr: str
    name: str
    city: str
    province: str
    archetype: str              # plain-language description, for the deck
    premium: float              # price vs PIHPS anchor (+0.05 = 5% above)
    price_sigma: float          # quote noise (log scale)
    lead_days: int              # quoted lead time, calendar days
    on_time_p: float            # probability of on-time delivery, normal conditions
    mean_delay_days: float      # mean delay when late
    capacity_kg_month: float
    rainy_penalty: float = 0.05     # drop in on-time prob in rainy season
    lebaran_penalty: float = 0.10   # drop in on-time prob around Lebaran
    short_ship_p: float = 0.03      # chance of delivering less than ordered
    active_from: str | None = None  # new supplier: no history before this date
    drift: dict | None = None       # {"start": date, "end": date, "on_time_p": target}
    materials: tuple = ("RM-GULA-001", "RM-MGOR-001")


SUPPLIERS = [
    Supplier("100101", "PT Sinar Manis Lampung", "Bandar Lampung", "Lampung",
             "Cheapest but often late, worse in rainy season (sea crossing)",
             premium=-0.04, price_sigma=0.012, lead_days=6, on_time_p=0.66,
             mean_delay_days=4.5, capacity_kg_month=60_000, rainy_penalty=0.18),
    Supplier("100102", "PT Kristal Prima Jaya", "Jakarta Utara", "DKI Jakarta",
             "Very reliable, most expensive",
             premium=0.055, price_sigma=0.008, lead_days=3, on_time_p=0.96,
             mean_delay_days=1.5, capacity_kg_month=50_000, rainy_penalty=0.02,
             lebaran_penalty=0.04, short_ship_p=0.01),
    Supplier("100103", "CV Tebu Makmur Sejahtera", "Surabaya", "Jawa Timur",
             "Balanced incumbent, large capacity",
             premium=0.01, price_sigma=0.010, lead_days=4, on_time_p=0.88,
             mean_delay_days=2.5, capacity_kg_month=90_000),
    Supplier("100104", "PT Cepat Antar Pangan", "Bekasi", "Jawa Barat",
             "Fastest lead time, small capacity (fails when overloaded)",
             premium=0.03, price_sigma=0.010, lead_days=2, on_time_p=0.92,
             mean_delay_days=2.0, capacity_kg_month=18_000),
    Supplier("100105", "CV Gula Rakyat Nusantara", "Kediri", "Jawa Timur",
             "Fine in dry season, poor in rainy season",
             premium=-0.015, price_sigma=0.012, lead_days=5, on_time_p=0.90,
             mean_delay_days=3.5, capacity_kg_month=40_000, rainy_penalty=0.30),
    Supplier("100106", "PT Distribusi Sentosa Abadi", "Semarang", "Jawa Tengah",
             "Good most of the year, struggles around Lebaran",
             premium=0.0, price_sigma=0.010, lead_days=4, on_time_p=0.89,
             mean_delay_days=3.0, capacity_kg_month=45_000, lebaran_penalty=0.40),
    Supplier("100107", "CV Mitra Karya Bersama", "Tangerang", "Banten",
             "Mediocre and pricey, but favoured by one buyer (planted pattern)",
             premium=0.035, price_sigma=0.012, lead_days=4, on_time_p=0.78,
             mean_delay_days=3.0, capacity_kg_month=35_000),
    Supplier("100108", "PT Agro Niaga Utama", "Cirebon", "Jawa Barat",
             "Was reliable, getting worse over the last 6 months",
             premium=0.005, price_sigma=0.010, lead_days=4, on_time_p=0.93,
             mean_delay_days=3.0, capacity_kg_month=45_000,
             drift={"start": "2026-03-01", "end": "2026-09-30", "on_time_p": 0.68}),
    Supplier("100109", "PT Sumber Rasa Baru", "Karawang", "Jawa Barat",
             "New supplier, little history (cold start)",
             premium=0.0, price_sigma=0.010, lead_days=3, on_time_p=0.91,
             mean_delay_days=2.0, capacity_kg_month=30_000, active_from="2026-06-01"),
    Supplier("100110", "UD Berkah Tani", "Cianjur", "Jawa Barat",
             "Low price, tiny capacity, frequent short shipments",
             premium=-0.025, price_sigma=0.015, lead_days=5, on_time_p=0.82,
             mean_delay_days=3.0, capacity_kg_month=12_000, short_ship_p=0.15),
]

# Extra drop in on-time probability per 100% of over-booking in a rolling 30 days.
CAPACITY_PENALTY = 0.35


@dataclass
class Buyer:
    ernam: str
    name: str
    bias: dict = field(default_factory=dict)   # {lifnr: extra preference}


BUYERS = [
    Buyer("BUYER01", "Rina"),
    Buyer("BUYER02", "Dimas"),
    Buyer("BUYER03", "Hendra", bias={"100107": 1.6}),   # planted Integrity Guard pattern
    Buyer("BUYER04", "Sari"),
]

# How buyers chose suppliers before GESIT (habit, not optimisation):
# utility = -PRICE_WEIGHT * price% - LEAD_WEIGHT * lead_days + bias, softmax.
PRICE_WEIGHT = 20.0
LEAD_WEIGHT = 0.15
CHOICE_TEMPERATURE = 1.0

# ---------------------------------------------------------------- calendar
# Rainy season months (BMKG: roughly Nov to Mar in Java and Sumatra).
RAINY_MONTHS = {11, 12, 1, 2, 3}
# Idul Fitri dates (Indonesia). Check against the official SKB calendar.
IDUL_FITRI = ["2024-04-10", "2025-03-31", "2026-03-20", "2027-03-10"]
LEBARAN_WINDOW = (-10, 7)       # days before/after Idul Fitri with logistics disruption

# Model features vs labels split
TEST_MONTHS = 5                 # last N months of closed POs form the test set
