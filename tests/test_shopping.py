"""Tests for shopping aggregation: two users' plans -> grouped shared list."""
from app.services.mealplan import validate_plan_protein
from app.services.shopping import aggregate_ingredients, format_list_tr


def test_aggregate_sums_same_item():
    lists = [
        [{"name": "Domates", "qty": 2, "unit": "adet", "category": "sebze"}],
        [{"name": "domates", "qty": 3, "unit": "adet", "category": "sebze"}],
    ]
    items = aggregate_ingredients(lists)
    domates = [i for i in items if i.name.lower() == "domates"][0]
    assert "5" in domates.quantity


def test_aggregate_groups_by_category_order():
    lists = [
        [
            {"name": "Tavuk", "qty": 500, "unit": "g", "category": "protein"},
            {"name": "Elma", "qty": 4, "unit": "adet", "category": "meyve"},
            {"name": "Marul", "qty": 1, "unit": "adet", "category": "sebze"},
        ]
    ]
    items = aggregate_ingredients(lists)
    cats = [i.category for i in items]
    # sebze < meyve < protein per SHOPPING_CATEGORIES order
    assert cats == sorted(cats, key=lambda c: ["sebze", "meyve", "protein"].index(c))


def test_aggregate_two_household_plans_merges():
    user1 = [[{"name": "Yumurta", "qty": 6, "unit": "adet", "category": "protein"}]]
    user2 = [[{"name": "Yumurta", "qty": 4, "unit": "adet", "category": "protein"}]]
    items = aggregate_ingredients(user1 + user2)
    egg = [i for i in items if "yumurta" in i.name.lower()][0]
    assert "10" in egg.quantity


def test_unknown_category_falls_to_diger():
    items = aggregate_ingredients([[{"name": "X", "qty": 1, "unit": "adet", "category": "bilinmeyen"}]])
    assert items[0].category == "diger"


def test_format_list_groups_with_headers():
    items = aggregate_ingredients(
        [[{"name": "Marul", "qty": 1, "unit": "adet", "category": "sebze"}]]
    )
    text = format_list_tr(items)
    assert "Sebzeler" in text
    assert "Marul" in text


# --- meal plan protein-floor validation ---


def test_validate_plan_protein_flags_low_days():
    plan = {
        "days": [
            {"day_index": 0, "meals": [{"protein_g": 50}, {"protein_g": 40}]},  # 90 total
            {"day_index": 1, "meals": [{"protein_g": 80}, {"protein_g": 80}]},  # 160 total
        ]
    }
    bad = validate_plan_protein(plan, protein_floor=140)
    assert bad == [0]  # day 0 misses, day 1 (160 >= 140*.95) ok


def test_validate_plan_protein_all_pass():
    plan = {"days": [{"day_index": 0, "meals": [{"protein_g": 150}]}]}
    assert validate_plan_protein(plan, protein_floor=140) == []
