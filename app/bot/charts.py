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
