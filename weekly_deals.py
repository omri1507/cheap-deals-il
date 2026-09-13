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
                    df = pd.read_csv(path, dtype=str)
                    # il_supermarket_parsers only writes file-level fields
                    # (chainid, chainname, found_folder, ...) on each source
                    # file's first output row, leaving them NaN on every
                    # subsequent row from that same file — the XML they come
                    # from states them once per file, not once per store.
                    # These columns don't otherwise change within one parsed
                    # CSV (which is one file per chain), so forward-filling
                    # is a safe, minimal fix rather than a real remapping.
                    df = df.ffill()
                    frames.append(df)
                except Exception as exc:  # noqa: BLE001
                    print(f"WARN: failed to read {path}: {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def find_my_stores(stores_df: pd.DataFrame) -> pd.DataFrame:
    if stores_df.empty:
        return stores_df
    # Search every text-bearing column, not just whichever pick_column finds
    # first: a place name like "תל אביב" or a neighborhood name just as
    # often shows up in storename ("בן יהודה תל אביב") as in address, and
    # "city" is sometimes a numeric city code rather than a name, so no
    # single column is a reliable enough signal on its own.
    candidate_names = ["address", "storename", "storeaddress", "city"]
    text_cols = [c for c in stores_df.columns if any(cand in c for cand in candidate_names)]
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


def extract_item_codes(value) -> list[str]:
    """Pull every itemcode out of a promo row's nested item-list JSON.

    No chain puts a flat itemcode column on the promotion row: one
    promotion can cover several items, so the government XML schema nests
    them (e.g. groups -> group -> promotionitems -> promotionitem -> [...]),
    and the exact nesting/column ('groups' vs a top-level 'promotionitems')
    varies by chain. A single child collapses to a dict instead of a
    one-item list in this schema's XML->JSON flattening, so a plain
    ['promotionitem'] lookup would miss that case — recursing through
    everything and collecting any 'itemcode' key sidesteps needing to know
    the exact shape per chain.
    """
    if not isinstance(value, str) or not value.strip().startswith("{"):
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return []

    codes: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key.lower() == "itemcode" and val not in (None, "", "NO_BODY"):
                    codes.append(str(val))
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(parsed)
    # de-dup, keep order
    seen: set[str] = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def promo_is_time_limited(start_val, end_val, max_days: int = 45) -> bool:
    """True if a promo's start/end span looks like a real time-boxed
    special rather than a standing store-wide offer (see MAX_PROMO_SPAN_DAYS
    in config.py). Dates that can't be parsed are kept rather than dropped."""
    try:
        start = datetime.fromisoformat(str(start_val)[:19])
        end = datetime.fromisoformat(str(end_val)[:19])
    except (ValueError, TypeError):
        return True
    return (end - start).days <= max_days


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
    nested_item_cols = [c for c in ("groups", "promotionitems") if c in promo_df.columns]
    promo_desc_col = pick_column(promo_df, ["promotiondescription", "promotiontext", "promotiondesc"])
    promo_price_col = pick_column(promo_df, ["discountedprice"])
    promo_start_col = pick_column(promo_df, ["promotionstartdate"])
    promo_end_col = pick_column(promo_df, ["promotionenddate"])
    promo_chain_col = pick_column(promo_df, ["chainid"])
    promo_store_col = pick_column(promo_df, ["storeid"])

    deals = []
    if not (item_name_col and item_code_col_prices):
        print("WARN: missing expected columns on price file for item-name lookup; see _debug_columns.json")
    elif not (item_code_col_promo or nested_item_cols):
        print("WARN: no itemcode column or nested item list found on promo rows; see _debug_columns.json")
    else:
        # Restrict to matched stores FIRST, scoped by chain when possible:
        # store ids are only unique within a chain, so matching storeid
        # alone could pull in a different chain's promo for a coincidentally
        # identical numeric store id. This also keeps the per-row JSON
        # parsing below cheap — it only has to run over one store's promos,
        # not the whole chain's.
        my_promo = promo_df
        if promo_store_col and store_id_col and not my_stores.empty:
            if promo_chain_col and chain_col and chain_col in my_stores.columns:
                my_store_keys = set(
                    my_stores[chain_col].astype(str) + "::" + my_stores[store_id_col].astype(str)
                )
                promo_keys = promo_df[promo_chain_col].astype(str) + "::" + promo_df[promo_store_col].astype(str)
                my_promo = promo_df[promo_keys.isin(my_store_keys)]
            else:
                my_store_ids = set(my_stores[store_id_col].astype(str))
                my_promo = promo_df[promo_df[promo_store_col].astype(str).isin(my_store_ids)]

        # Drop standing store-wide offers (meal-voucher redemption, credit-
        # card perks, ...) filed as "promotions" spanning years rather than
        # a real weekly special — see MAX_PROMO_SPAN_DAYS in config.py.
        # Each one would otherwise explode into one row per covered item.
        if promo_start_col and promo_end_col:
            time_limited = my_promo.apply(
                lambda row: promo_is_time_limited(
                    row.get(promo_start_col), row.get(promo_end_col), config.MAX_PROMO_SPAN_DAYS
                ),
                axis=1,
            )
            my_promo = my_promo[time_limited]

        # No chain puts a flat itemcode column on the promotion row itself
        # (a promotion can cover several items) — item codes live nested
        # several levels deep inside a 'groups' or 'promotionitems' JSON
        # blob instead, and which column/depth varies by chain. Collect
        # every itemcode this row's JSON contains (falling back to a flat
        # itemcode column too, in case some chain does expose one directly)
        # and explode to one row per (promo, item).
        def row_item_codes(row) -> list[str]:
            codes = []
            if item_code_col_promo:
                v = row.get(item_code_col_promo)
                if isinstance(v, str) and v.strip():
                    codes.append(v.strip())
            for col in nested_item_cols:
                codes.extend(extract_item_codes(row.get(col)))
            return codes or [None]  # keep the promo row even with no item found

        my_promo = my_promo.copy()
        my_promo["_item_codes"] = my_promo.apply(row_item_codes, axis=1)
        exploded = my_promo.explode("_item_codes")

        # Look up item name by itemcode only (drop_duplicates first) instead
        # of merging the full prices table: itemcode repeats once per store
        # in prices_df, so a plain merge on itemcode alone would fan out
        # into one row per (promo, store-carrying-that-item) pair.
        item_names = prices_df[[item_code_col_prices, item_name_col]].drop_duplicates(
            subset=[item_code_col_prices]
        )
        merged = exploded.merge(
            item_names,
            left_on="_item_codes",
            right_on=item_code_col_prices,
            how="left",
        )

        for _, row in merged.iterrows():
            name = row.get(item_name_col)
            used_description_fallback = False
            if not isinstance(name, str) or not name.strip():
                # The itemcode extracted above may not exist in the price
                # file (e.g. discontinued item), or no itemcode could be
                # found at all. Fall back to the promo's own description,
                # which usually names the item directly (e.g. "קופון דבש
                # לחיץ 500 גרם"), rather than dropping the promo outright.
                name = row.get(promo_desc_col) if promo_desc_col else None
                used_description_fallback = True
            if not is_food_and_vegan(name):
                continue
            deals.append(
                {
                    "chain": row.get(promo_chain_col) if promo_chain_col else None,
                    "store_id": row.get(promo_store_col) if promo_store_col else None,
                    "item_name": name,
                    "item_name_is_promo_description": used_description_fallback,
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
