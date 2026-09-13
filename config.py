"""
Configuration for the weekly deals digest.

Edit BRANCH_MATCH to narrow down to your exact branches once you've seen
a first real run's output (see README "First run" section) — the store
files include address text, and chains don't always spell "פלורנטין" the
same way, so this starts as a loose text match and you tighten it after
checking data/stores_seen.csv.
"""

from il_supermarket_scarper import ScraperFactory

# Chains to track. Add/remove ScraperFactory names as needed — see the
# full list with:
#   python3 -c "from il_supermarket_scarper import ScraperFactory as S; print([n for n in dir(S) if not n.startswith('_')])"
CHAINS = [
    ScraperFactory.RAMI_LEVY.name,
    ScraperFactory.SHUFERSAL.name,
    # VICTORY is deprecated in this library version (site moved) — use the
    # replacement source instead. ScraperFactory.get() rejects deprecated
    # names outright, so the plain VICTORY enum member 404s at scrape time.
    ScraperFactory.VICTORY_NEW_SOURCE.name,
]

# Loose, case-insensitive substring matches against the store file's
# address/city fields. A store is "yours" if ANY of these strings appear
# in its address or city. Widen or narrow this list after the first run.
BRANCH_MATCH = [
    "פלורנטין",
    "פלורנטיו",  # OCR/typo variant seen in some chains' data
]

# Fallback: if no branch matches BRANCH_MATCH for a chain (e.g. that
# chain has no Florentin-specific branch and treats all Tel Aviv-Jaffa
# branches the same in its promo file), fall back to any branch in this city.
FALLBACK_CITY_MATCH = ["תל אביב", "תל-אביב", "יפו"]

# Hebrew keyword blocklist: an item is treated as non-vegan if its name
# contains any of these substrings (case-insensitive is meaningless for
# Hebrew, so this is a plain substring check). This is a heuristic, not
# a certified vegan check — it will miss less obvious animal ingredients
# (e.g. gelatin, whey powder, some E-numbers) and can occasionally
# false-positive (e.g. "חלבה" contains "חלב"). Good enough for a first
# pass; refine over time based on what shows up.
NON_VEGAN_KEYWORDS = [
    "בשר", "עוף", "הודו", "כבש", "טלה", "בקר", "חזיר",
    "דג ", "דגים", "סלמון", "טונה", "פילה", "טילפיה",
    "נקניק", "המבורגר", "קבב", "שניצל", "פסטרמה", "סלמי", "נתחי",
    "חלב", "גבינה", "גבינת", "יוגורט", "לבן", "קוטג", "שמנת",
    "חמאה", "ביצים", "ביצת", "מיונז",
    "דבש",
]

# Only keep items whose name contains at least one of these (a coarse
# "this is actually food" filter for chains whose promo files also
# include non-food categories like cleaning/toiletries). Leave empty
# to skip this filter entirely and keep everything that isn't blocked
# by NON_VEGAN_KEYWORDS above.
FOOD_HINT_KEYWORDS: list[str] = []

# Skip promotions whose start/end dates span longer than this many days.
# Chains file standing offers (meal-voucher redemption, "5% off our own
# brand" credit-card perks, ...) as "promotions" with a start/end date
# years apart (e.g. 2022-07-27 to 2031-01-01) — these apply to virtually
# every item in the store and aren't a "this week" deal, but each one
# still explodes into one row per covered item. Filtering by a realistic
# promo length keeps the digest to genuinely time-limited specials.
# Raise this if a real multi-week promo run gets excluded.
MAX_PROMO_SPAN_DAYS = 45
