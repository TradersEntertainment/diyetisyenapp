"""OpenFoodFacts barcode lookup (free, no API key)."""
import logging

import httpx

log = logging.getLogger(__name__)

_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"


async def lookup(barcode: str) -> dict | None:
    """Per-100g nutrition for a barcode, or None if not found."""
    code = "".join(ch for ch in str(barcode) if ch.isdigit())
    if not code:
        return None
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "DiyetisyenBot/1.0"}) as client:
            resp = await client.get(_URL.format(code=code))
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("status") != 1:
                return None
            p = data["product"]
            n = p.get("nutriments", {})

            def num(key):
                v = n.get(key)
                try:
                    return round(float(v), 1) if v is not None else None
                except (TypeError, ValueError):
                    return None

            return {
                "name": (p.get("product_name_tr") or p.get("product_name") or "").strip() or None,
                "brand": (p.get("brands") or "").split(",")[0].strip() or None,
                "kcal": num("energy-kcal_100g"),
                "protein_g": num("proteins_100g"),
                "carb_g": num("carbohydrates_100g"),
                "fat_g": num("fat_100g"),
                "fiber_g": num("fiber_100g"),
            }
    except Exception:
        log.exception("openfoodfacts lookup failed for %s", barcode)
        return None
