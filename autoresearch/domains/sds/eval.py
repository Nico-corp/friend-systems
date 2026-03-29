#!/usr/bin/env python3
"""
SDS domain evaluator.
Loads broward-all-filtered.json, samples 10 random parcels, scores each using
weights from target.py, prints average score as float.
"""
import sys
import os
import json
import random

WORKSPACE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DOMAIN_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, DOMAIN_DIR)
import target  # noqa: E402

DATA_PATH = os.path.join(WORKSPACE, "sds", "broward-all-filtered.json")

# Attribute key mappings: target weight -> possible parcel field names
FIELD_MAP = {
    "WEIGHT_ZONING":         ["zoning_score", "zoning", "zoning_value"],
    "WEIGHT_SIZE":           ["size_score", "acreage", "lot_size_acres", "area_acres"],
    "WEIGHT_LOCATION":       ["location_score", "location", "loc_score"],
    "WEIGHT_INFRASTRUCTURE": ["infra_score", "infrastructure_score", "infrastructure"],
    "WEIGHT_MARKET":         ["market_score", "market", "market_value_score"],
    "WEIGHT_FLOOD":          ["flood_score", "flood_zone_score", "flood"],
}

def normalize(val, lo=0, hi=10):
    """Clamp and normalize a raw value to [0,1]."""
    if val is None:
        return 0.5  # neutral if missing
    try:
        val = float(val)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, (val - lo) / (hi - lo)))

def get_field(parcel, candidates):
    for key in candidates:
        if key in parcel:
            return parcel[key]
    return None

def score_parcel(parcel):
    weights = {
        "WEIGHT_ZONING":         target.WEIGHT_ZONING,
        "WEIGHT_SIZE":           target.WEIGHT_SIZE,
        "WEIGHT_LOCATION":       target.WEIGHT_LOCATION,
        "WEIGHT_INFRASTRUCTURE": target.WEIGHT_INFRASTRUCTURE,
        "WEIGHT_MARKET":         target.WEIGHT_MARKET,
        "WEIGHT_FLOOD":          target.WEIGHT_FLOOD,
    }
    total = 0.0
    for weight_key, fields in FIELD_MAP.items():
        raw = get_field(parcel, fields)
        total += weights[weight_key] * normalize(raw)
    # Scale to 0-10
    return total * 10.0

def main():
    if not os.path.exists(DATA_PATH):
        # Fallback: no data, return neutral score
        print("5.0")
        sys.exit(0)

    with open(DATA_PATH) as f:
        data = json.load(f)

    # data may be a list or a dict with a key
    if isinstance(data, dict):
        parcels = data.get("parcels") or data.get("features") or list(data.values())
        if parcels and isinstance(parcels[0], dict) and "properties" in parcels[0]:
            parcels = [p["properties"] for p in parcels]
    else:
        parcels = data

    if not parcels:
        print("5.0")
        sys.exit(0)

    sample_size = min(10, len(parcels))
    sample = random.sample(parcels, sample_size)

    scores = [score_parcel(p) for p in sample]
    avg = sum(scores) / len(scores)
    print(f"{avg:.6f}")

if __name__ == "__main__":
    main()
