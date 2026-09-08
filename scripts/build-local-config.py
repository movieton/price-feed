#!/usr/bin/env python3
"""Build a local, ignored config from a Shopify bulk JSONL snapshot."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "runtime/private/live-catalog.jsonl"
TARGET = ROOT / "config.local.json"


def category_id(name):
    # Stable numeric technical ID derived from the exact Shopify product type.
    return str(int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:15], 16))


def main():
    product_types = set()
    with SOURCE.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                row = json.loads(line)
                name = (row["product"].get("productType") or "").strip()
                if name:
                    product_types.add(name)

    mapping = {name: category_id(name) for name in sorted(product_types)}
    if len(set(mapping.values())) != len(mapping):
        raise RuntimeError("category_id_collision")

    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config.update({
        "live_confirmed": True,
        "shop_domain": "ra4abi-aj.myshopify.com",
        "market": "France",
        "country": "FR",
        "currency": "EUR",
        "language": "fr",
        "language_mode": "primary",
        "price_mode": "base",
        "catalog_query": "product_status:active",
        "availability": "tracked_aggregate_positive",
        "availability_confirmed": True,
        "missing_policy": "skip",
        "min_offers": 1,
        "max_skip_fraction": 1,
        "max_drop_fraction": 1,
        "shop": {
            "name": "Routes&Roads",
            "company": "Routes&Roads",
            "url": "https://www.routesandroads.fr",
        },
        "categories": [{"id": ident, "name": name} for name, ident in mapping.items()],
        "category_map": mapping,
        "option_params": {},
        "extra_data": "",
        "output": "runtime/public/price-feed.xml",
        "state_dir": "runtime/private",
    })
    TARGET.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    TARGET.chmod(0o600)
    print(json.dumps({"config": TARGET.name, "categories": len(mapping)}))


if __name__ == "__main__":
    main()
