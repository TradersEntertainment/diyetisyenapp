"""matplotlib chart rendering for Telegram (PNG in memory)."""
import io
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

COLOR = "#2b7a78"
TARGET_COLOR = "#d95f5f"


def line_chart(
    series: list[tuple[date, float]],
    title: str,
    ylabel: str,
    target: float | None = None,
    target_label: str = "Hedef",
) -> io.BytesIO | None:
    if len(series) < 2:
        return None
    xs = [d for d, _ in series]
    ys = [v for _, v in series]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    ax.plot(xs, ys, marker="o", markersize=4, linewidth=2, color=COLOR)
    if target is not None:
        ax.axhline(target, linestyle="--", linewidth=1.5, color=TARGET_COLOR, label=target_label)
        ax.legend(loc="best")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


def bar_chart(
    series: list[tuple[date, float]],
    title: str,
    ylabel: str,
    target: float | None = None,
) -> io.BytesIO | None:
    if not series:
        return None
    xs = [d for d, _ in series]
    ys = [v for _, v in series]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    ax.bar(xs, ys, color=COLOR, width=0.7)
    if target is not None:
        ax.axhline(target, linestyle="--", linewidth=1.5, color=TARGET_COLOR, label="Hedef")
        ax.legend(loc="best")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y", alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


SLOT_LABELS = [
    ("kahvalti", "Kahvaltı"),
    ("ara_ogun_1", "Ara Öğün 1"),
    ("ogle", "Öğle"),
    ("ara_ogun_2", "Ara Öğün 2"),
    ("aksam", "Akşam"),
    ("gece_atistirmasi", "Gece"),
]
DAY_NAMES = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def plan_image(
    days_data: list[dict],
    title: str,
    target_kcal: int | None = None,
    target_protein: float | None = None,
) -> io.BytesIO | None:
    """Render a weekly meal plan as a PNG grid (7 days x 6 slots + daily totals)."""
    import textwrap

    by_day: dict[int, dict] = {}
    for d in days_data:
        by_day[d["day_index"]] = {m["slot"]: m for m in d["meals"]}
    if not by_day:
        return None

    col_w = [1.1] + [2.5] * len(SLOT_LABELS) + [1.35]
    row_h = 1.5
    width = sum(col_w)
    height = (len(DAY_NAMES) + 1) * row_h

    fig, ax = plt.subplots(figsize=(width * 0.9, height * 0.72 + 0.8), dpi=115)
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.invert_yaxis()
    ax.axis("off")

    subtitle = title
    if target_kcal:
        subtitle += f"  ·  Hedef: {target_kcal} kcal"
        if target_protein:
            subtitle += f" / P {target_protein:g} g"
    ax.set_title(subtitle, fontsize=15, fontweight="bold", color="#17252a", pad=16)

    x_edges = [0.0]
    for w in col_w:
        x_edges.append(x_edges[-1] + w)

    def cell(row, col, text, *, bold=False, bg=None, size=8.2, color="#17252a"):
        x0, x1 = x_edges[col], x_edges[col + 1]
        y0 = row * row_h
        if bg:
            ax.add_patch(plt.Rectangle((x0, y0), x1 - x0, row_h, facecolor=bg, edgecolor="none", zorder=0))
        ax.add_patch(
            plt.Rectangle((x0, y0), x1 - x0, row_h, fill=False, edgecolor="#c9d6d3", linewidth=0.8, zorder=2)
        )
        ax.text(
            (x0 + x1) / 2, y0 + row_h / 2, text,
            ha="center", va="center", fontsize=size, color=color,
            fontweight="bold" if bold else "normal", zorder=3, linespacing=1.25,
        )

    cell(0, 0, "Gün", bold=True, bg=COLOR, color="white", size=9.5)
    for j, (_, label) in enumerate(SLOT_LABELS, start=1):
        cell(0, j, label, bold=True, bg=COLOR, color="white", size=9.5)
    cell(0, len(SLOT_LABELS) + 1, "Toplam", bold=True, bg=COLOR, color="white", size=9.5)

    for i, day_name in enumerate(DAY_NAMES, start=1):
        di = i - 1
        bg = "#f2f7f6" if di % 2 == 0 else "white"
        cell(i, 0, day_name, bold=True, bg=bg, size=9)
        meals = by_day.get(di, {})
        total_kcal = 0
        total_protein = 0.0
        for j, (slot, _) in enumerate(SLOT_LABELS, start=1):
            m = meals.get(slot)
            if m:
                total_kcal += m.get("kcal") or 0
                total_protein += m.get("protein_g") or 0
                name = "\n".join(textwrap.wrap(m.get("name", ""), 24)[:3])
                text = f"{name}\n{m.get('kcal', 0)} kcal · P{m.get('protein_g', 0):g}"
            else:
                text = "—"
            cell(i, j, text, bg=bg)
        on_target = target_kcal and abs(total_kcal - target_kcal) <= target_kcal * 0.10
        cell(
            i, len(SLOT_LABELS) + 1,
            f"{total_kcal} kcal\nP{round(total_protein, 1):g} g",
            bold=True, bg=bg, size=9,
            color=COLOR if (not target_kcal or on_target) else TARGET_COLOR,
        )

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf
