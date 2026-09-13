"""
Weekly deals digest for specific supermarket branches.

Pulls the current STORE, PRICE_FULL and PROMO_FULL files for the chains in
config.py, finds your branches, joins promotions to item names, filters out
non-vegan items, and writes:
  - data/latest_deals.json   (machine-readable, read by the Claude scheduled task)
  - data/digest.md           (human-readable, same content)
  - data/_debug_columns.json (raw column names seen this run — for tuning config.py)

Designed to run on GitHub Actions, which has normal internet access. It will
NOT work from a network-sandboxed environment (see README) — the chains'
data portals aren't reachable from most sandboxes.
"""

import csv
import json
import os
import shutil
import sys
from datetime import datetime, timezone

import pandas as pd

from il_supermarket_scarper import ScarpingTask, FileTypesFilters
from il_supermarket_parsers import ConvertingTask

import config

DUMPS_DIR = "dumps"
PARSED_DIR = "parsed"
DATA_DIR = "data"

# Some promo files carry a field (e.g. long "remarks"/"additionalrestrictions"
# text) past Python's default 128KB csv field-size limit, which aborts that
# file's parse worker. Raise it up front, before any multiprocessing pool is
# spawned, so forked workers inherit the higher limit too.
_max_field_size = sys.maxsize
while True:
    try:
        csv.field_size_limit(_max_field_size)
        break
    except OverflowError:
        _max_field_size //= 10


def pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Return the first existing column whose name contains any candidate substring."""
    cols = list(df.columns)
    for cand in candidates:
        for col in cols:
            if cand in col:
                return col
    return None


def scrape(file_types: list[str]) -> None:
    # NOTE: does not clear DUMPS_DIR itself — main() calls this twice (once
    # for STORE_FILE, once for PRICE_FULL/PROMO_FULL) and each chain's files
    # land in their own subfolder, so wiping here would delete the previous
    # call's output before it's ever parsed. Clearing happens once, up front,
    # in main().
    task = ScarpingTask(
        enabled_scrapers=config.CHAINS,
        files_types=file_types,
        multiprocessing=4,
        timeout_in_seconds=1800,
    )
    thread = task.start()
    thread.join()


def parse_to_csv() -> None:
    if os.path.isdir(PARSED_DIR):
        shutil.rmtree(PARSED_DIR)
    task = ConvertingTask(
        source_configuration={"folder": DUMPS_DIR},
        output_configuration=[{"output_mode": "csv", "output_folder": PARSED_DIR}],
        status_configuration={"database_type": "json", "base_path": PARSED_DIR},
        enabled_parsers=config.CHAINS,
    )
    thread = task.start()
    thread.join()


def load_csvs(file_type_hint: str) -> pd.DataFrame:
    """Load and concatenate every parsed CSV matching a file-type hint (e.g. 'store', 'promofull', 'pricefull')."""
    frames = []
    if not os.path.isdir(PARSED_DIR):
        return pd.DataFrame()
    for root, _dirs, files in os.walk(PARSED_DIR):
        for fname in files:
            if fname.lower().endswith(".csv") and file_type_hint in fname.lower():
                path = os.path.join(root, fname)
                try:
                    frames.append(pd.read_csv(path, dtype=str))
                except Exception as exc:  # noqa: BLE001
                    print(f"WARN: failed to read {path}: {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def find_my_stores(stores_df: pd.DataFrame) -> pd.DataFrame:
    if stores_df.empty:
        return stores_df
    addr_col = pick_column(stores_df, ["address", "storename", "storeaddress"])
    city_col = pick_column(stores_df, ["city"])
    text_cols = [c for c in [addr_col, city_col] if c]
    if not text_cols:
        print("WARN: could not find address/city columns on store file; keeping all stores")
        return stores_df

    def matches(row) -> bool:
        blob = " ".join(str(row[c]) for c in text_cols)
        return any(term in blob for term in config.BRANCH_MATCH)

    matched = stores_df[stores_df.apply(matches, axis=1)]
    if matched.empty:
        print("No exact branch match found — falling back to city-level match.")

        def city_matches(row) -> bool:
            blob = " ".join(str(row[c]) for c in text_cols)
            return any(term in blob for term in config.FALLBACK_CITY_MATCH)

        matched = stores_df[stores_df.apply(city_matches, axis=1)]
    return matched


def is_food_and_vegan(name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if config.FOOD_HINT_KEYWORDS and not any(k in name for k in config.FOOD_HINT_KEYWORDS):
        return False
    return not any(bad in name for bad in config.NON_VEGAN_KEYWORDS)


def build_digest() -> dict:
    stores_df = load_csvs("store")
    prices_df = load_csvs("pricefull")
    if prices_df.empty:
        prices_df = load_csvs("price")
    promo_df = load_csvs("promofull")
    if promo_df.empty:
        promo_df = load_csvs("promo")

    debug = {
        "stores_columns": list(stores_df.columns),
        "prices_columns": list(prices_df.columns),
        "promo_columns": list(promo_df.columns),
        "stores_row_count": len(stores_df),
        "prices_row_count": len(prices_df),
        "promo_row_count": len(promo_df),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "_debug_columns.json"), "w", encoding="utf-8") as fh:
        json.dump(debug, fh, ensure_ascii=False, indent=2)

    if stores_df.empty:
        stores_df.to_csv(os.path.join(DATA_DIR, "stores_seen.csv"), index=False)
        raise SystemExit("No store data parsed — check data/_debug_columns.json and the dumps/ logs.")

    stores_df.to_csv(os.path.join(DATA_DIR, "stores_seen.csv"), index=False)
    my_stores = find_my_stores(stores_df)

    chain_col = pick_column(stores_df, ["chainid"])
    store_id_col = pick_column(stores_df, ["storeid"])
    item_code_col_prices = pick_column(prices_df, ["itemcode"])
    item_name_col = pick_column(prices_df, ["itemname"])
    item_code_col_promo = pick_column(promo_df, ["itemcode"])
    promo_desc_col = pick_column(promo_df, ["promotiondescription", "promotiontext", "promotiondesc"])
    promo_price_col = pick_column(promo_df, ["discountedprice"])
    promo_start_col = pick_column(promo_df, ["promotionstartdate"])
    promo_end_col = pick_column(promo_df, ["promotionenddate"])
    promo_chain_col = pick_column(promo_df, ["chainid"])
    promo_store_col = pick_column(promo_df, ["storeid"])

    deals = []
    if not (item_code_col_promo and item_code_col_prices and item_name_col):
        print("WARN: missing expected columns for joining promo->item name; see _debug_columns.json")
    else:
        merged = promo_df.merge(
            prices_df[[item_code_col_prices, item_name_col] + ([chain_col] if chain_col and chain_col in prices_df.columns else [])],
            left_on=item_code_col_promo,
            right_on=item_code_col_prices,
            how="left",
        )

        # Restrict to matched stores when we have store id columns on both sides.
        if promo_store_col and store_id_col and not my_stores.empty:
            my_store_ids = set(my_stores[store_id_col].astype(str))
            merged = merged[merged[promo_store_col].astype(str).isin(my_store_ids)]

        for _, row in merged.iterrows():
            name = row.get(item_name_col)
            if not is_food_and_vegan(name):
                continue
            deals.append(
                {
                    "chain": row.get(promo_chain_col) if promo_chain_col else None,
                    "store_id": row.get(promo_store_col) if promo_store_col else None,
                    "item_name": name,
                    "promo_price": row.get(promo_price_col) if promo_price_col else None,
                    "promo_description": row.get(promo_desc_col) if promo_desc_col else None,
                    "start_date": row.get(promo_start_col) if promo_start_col else None,
                    "end_date": row.get(promo_end_col) if promo_end_col else None,
                }
            )

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chains_tracked": config.CHAINS,
        "matched_store_count": 0 if my_stores.empty else len(my_stores),
        "deal_count": len(deals),
        "deals": deals,
    }
    with open(os.path.join(DATA_DIR, "latest_deals.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    lines = [f"# Vegan-relevant deals — {result['generated_at'][:10]}", ""]
    if not deals:
        lines.append("No deals matched this run — check data/_debug_columns.json, this likely needs a config.py tweak.")
    for d in deals:
        price_bit = f" — {d['promo_price']}" if d["promo_price"] else ""
        desc_bit = f" ({d['promo_description']})" if d["promo_description"] else ""
        lines.append(f"- **{d['item_name']}**{price_bit}{desc_bit} [{d['chain']}]")
    with open(os.path.join(DATA_DIR, "digest.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"Wrote {len(deals)} deals from {result['matched_store_count']} matched stores.")
    return result


def main() -> None:
    if os.path.isdir(DUMPS_DIR):
        shutil.rmtree(DUMPS_DIR)
    print("Scraping store lists...")
    scrape([FileTypesFilters.STORE_FILE.name])
    print("Scraping price + promo files...")
    scrape([
        FileTypesFilters.PRICE_FULL_FILE.name,
        FileTypesFilters.PROMO_FULL_FILE.name,
    ])
    print("Parsing downloaded files to CSV...")
    parse_to_csv()
    print("Building digest...")
    build_digest()


if __name__ == "__main__":
    main()
