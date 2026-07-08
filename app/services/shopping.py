"""Shared household shopping list, aggregated from both users' weekly meal plans."""
from collections import defaultdict
from dataclasses import dataclass

from app.models.shopping import SHOPPING_CATEGORIES

CATEGORY_LABELS_TR = {
    "sebze": "🥦 Sebzeler",
    "meyve": "🍎 Meyveler",
    "protein": "🍗 Protein",
    "sut_urunleri": "🥛 Süt Ürünleri",
    "donuk": "🧊 Donuk",
    "tahil": "🌾 Tahıllar",
    "baharat": "🧂 Baharatlar",
    "icecek": "🥤 İçecekler",
    "diger": "🛒 Diğer",
}


@dataclass
class AggregatedItem:
    name: str
    quantity: str
    category: str


def _normalize_category(cat: str | None) -> str:
    cat = (cat or "diger").strip().lower()
    return cat if cat in SHOPPING_CATEGORIES else "diger"


def aggregate_ingredients(ingredient_lists: list[list[dict]]) -> list[AggregatedItem]:
    """Merge ingredient dicts from many planned meals (possibly two users' plans).

    Ingredient dict shape: {"name": str, "qty": number, "unit": str, "category": str}.
    Quantities with the same (name, unit) are summed; different units are listed together.
    """
    totals: dict[tuple[str, str], float] = defaultdict(float)
    categories: dict[str, str] = {}

    for ingredients in ingredient_lists:
        for ing in ingredients or []:
            name = str(ing.get("name", "")).strip()
            if not name:
                continue
            key_name = name.lower()
            unit = str(ing.get("unit", "") or "").strip().lower()
            try:
                qty = float(ing.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            totals[(key_name, unit)] += qty
            categories.setdefault(key_name, _normalize_category(ing.get("category")))

    # Merge units of the same item into one display line
    by_name: dict[str, list[str]] = defaultdict(list)
    for (key_name, unit), qty in sorted(totals.items()):
        if qty > 0:
            qty_str = f"{qty:g} {unit}".strip()
        else:
            qty_str = ""
        if qty_str:
            by_name[key_name].append(qty_str)
        else:
            by_name.setdefault(key_name, [])

    items = [
        AggregatedItem(
            name=key_name.capitalize(),
            quantity=" + ".join(parts),
            category=categories.get(key_name, "diger"),
        )
        for key_name, parts in by_name.items()
    ]
    items.sort(key=lambda i: (SHOPPING_CATEGORIES.index(i.category), i.name))
    return items


async def build_weekly_shopping_list(session, week_start):
    """Create (or rebuild) the shared household list from all active plans of the week.

    Both users' planned quantities are per-person, so simply summing them yields
    the household need — shared dinners included.
    """
    from sqlalchemy import select

    from app.models import MealPlan, PlannedMeal, ShoppingItem, ShoppingList

    res = await session.execute(
        select(MealPlan).where(MealPlan.week_start == week_start, MealPlan.status == "active")
    )
    plans = list(res.scalars())
    ingredient_lists: list[list[dict]] = []
    for plan in plans:
        meals_res = await session.execute(select(PlannedMeal).where(PlannedMeal.plan_id == plan.id))
        for meal in meals_res.scalars():
            if meal.ingredients:
                ingredient_lists.append(meal.ingredients)

    items = aggregate_ingredients(ingredient_lists)

    res = await session.execute(select(ShoppingList).where(ShoppingList.week_start == week_start))
    slist = res.scalar_one_or_none()
    if slist:
        await session.refresh(slist, ["items"])
        slist.items.clear()
    else:
        slist = ShoppingList(week_start=week_start)
        session.add(slist)
        await session.flush()
    for item in items:
        session.add(
            ShoppingItem(
                list_id=slist.id, name=item.name, quantity=item.quantity, category=item.category
            )
        )
    await session.flush()
    return slist


def format_list_tr(items: list[AggregatedItem], checked_ids: set[int] | None = None) -> str:
    """Plain-text Turkish rendering grouped by category (for Telegram)."""
    lines: list[str] = []
    current = None
    for item in items:
        if item.category != current:
            current = item.category
            lines.append(f"\n{CATEGORY_LABELS_TR.get(current, current)}")
        qty = f" — {item.quantity}" if item.quantity else ""
        lines.append(f"  • {item.name}{qty}")
    return "\n".join(lines).strip()
