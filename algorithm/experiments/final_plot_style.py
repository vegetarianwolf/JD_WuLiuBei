"""Reusable Matplotlib styling for the final experiment figures.

The visual language follows the result figures in the supplied reference PDF:
serif typography, restrained low-saturation colours, thin axes, compact
legends, and publication-ready vector or high-resolution raster output.

Importing this module does not mutate Matplotlib's global configuration.  Use
``reference_plot_style`` as a context manager around figure construction.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Mapping

import matplotlib as mpl
from cycler import cycler
from matplotlib.figure import Figure


FULL_WIDTH_INCHES = 7.1
TWO_PANEL_FIGSIZE = (FULL_WIDTH_INCHES, 3.1)
FOUR_PANEL_FIGSIZE = (FULL_WIDTH_INCHES, 6.0)

# Fig. 4-style operator palette: teal through cool grey to salmon.
OPERATOR_PALETTE = (
    "#029EAC",
    "#94C3CB",
    "#D3D8E2",
    "#EE9F99",
    "#E9807B",
)

# Figs. 8--14-style sensitivity curves.
METHOD_LINE_PALETTE = ("#1F77B4", "#FF7F0E", "#2CA02C")
METHOD_MARKERS = ("o", "s", "^")
METHOD_LINE_STYLES = tuple(
    {"color": color, "marker": marker, "linestyle": "-"}
    for color, marker in zip(METHOD_LINE_PALETTE, METHOD_MARKERS, strict=True)
)

# Figs. 7, 9, 11, and 13-style cost-component bars.
COMPONENT_COLORS = {
    "total_cost": "#5D77B0",
    "truck_cost": "#F4B674",
    "paired_drone_cost": "#EC7168",
    "rp_drone_cost": "#6FC285",
    "rp_activation_cost": "#FF8970",
}
TRAVEL_TIME_BAR_STYLE = {
    "facecolor": "white",
    "edgecolor": "#00E5DF",
    "hatch": "////",
    "linewidth": 0.6,
}

# Model fills used by the grouped bars and boxplots.
MODEL_FILL_PALETTE = ("#F2DED9", "#F8E8D0", "#D7DCE3", "#DBE1CC")
COVERAGE_PALETTE = {
    "served": "#60B8B5",
    "unserved": "#ED8585",
}
HEATMAP_CMAP = "coolwarm"

BOXPLOT_STYLE = {
    "patch_artist": True,
    "showmeans": True,
    "meanprops": {
        "marker": "*",
        "markersize": 3.2,
        "markeredgewidth": 0.45,
    },
    "flierprops": {
        "marker": "d",
        "markersize": 2.2,
        "markerfacecolor": "#666666",
        "markeredgecolor": "#666666",
        "markeredgewidth": 0.35,
    },
}

REFERENCE_RC = {
    "font.family": "serif",
    "font.serif": [
        "Times New Roman",
        "STIX Two Text",
        "Songti SC",
        "SimSun",
        "DejaVu Serif",
    ],
    "mathtext.fontset": "stix",
    "axes.unicode_minus": False,
    "axes.labelsize": 8.5,
    "axes.titlesize": 9.0,
    "axes.linewidth": 0.6,
    "axes.axisbelow": True,
    "axes.prop_cycle": cycler(color=METHOD_LINE_PALETTE),
    "axes.grid": False,
    "xtick.labelsize": 7.5,
    "ytick.labelsize": 7.5,
    "xtick.direction": "out",
    "ytick.direction": "out",
    "xtick.major.size": 2.8,
    "ytick.major.size": 2.8,
    "xtick.major.width": 0.55,
    "ytick.major.width": 0.55,
    "legend.fontsize": 7.5,
    "legend.frameon": False,
    "legend.handlelength": 1.8,
    "legend.handletextpad": 0.5,
    "legend.columnspacing": 1.1,
    "lines.linewidth": 1.0,
    "lines.markersize": 3.8,
    "lines.markeredgewidth": 0.5,
    "patch.edgecolor": "#6A6A6A",
    "patch.linewidth": 0.45,
    "hatch.linewidth": 0.6,
    "grid.color": "#B0B0B0",
    "grid.linewidth": 0.55,
    "grid.alpha": 0.5,
    "figure.dpi": 150,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.dpi": 600,
    "savefig.facecolor": "white",
    "savefig.transparent": False,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.03,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}


def reference_plot_style(
    overrides: Mapping[str, Any] | None = None,
) -> AbstractContextManager[None]:
    """Return an isolated rc context for reference-style experiment plots.

    ``overrides`` is applied last, allowing an individual figure to opt into
    settings such as ``{"axes.grid": True}`` without changing global state.
    """

    settings = dict(REFERENCE_RC)
    if overrides:
        settings.update(overrides)
    return mpl.rc_context(rc=settings)


def save_publication_figure(
    figure: Figure,
    output_path: str | Path,
    *,
    dpi: int = 600,
    close: bool = False,
    **savefig_kwargs: Any,
) -> Path:
    """Save a figure using stable publication defaults and return its path.

    PDF and SVG preserve vector text and lines; PNG uses the supplied ``dpi``.
    Parent directories are created when necessary.  Additional keyword
    arguments are forwarded to ``Figure.savefig`` and take precedence over
    the defaults below.
    """

    output = Path(output_path)
    if output.suffix.lower() not in {".pdf", ".png", ".svg"}:
        raise ValueError("output_path must end in .pdf, .png, or .svg")

    output.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "dpi": dpi,
        "bbox_inches": "tight",
        "pad_inches": 0.03,
        "facecolor": "white",
        "transparent": False,
    }
    options.update(savefig_kwargs)
    figure.savefig(output, **options)
    if close:
        import matplotlib.pyplot as plt

        plt.close(figure)
    return output


__all__ = [
    "BOXPLOT_STYLE",
    "COMPONENT_COLORS",
    "COVERAGE_PALETTE",
    "FOUR_PANEL_FIGSIZE",
    "FULL_WIDTH_INCHES",
    "HEATMAP_CMAP",
    "METHOD_LINE_PALETTE",
    "METHOD_LINE_STYLES",
    "METHOD_MARKERS",
    "MODEL_FILL_PALETTE",
    "OPERATOR_PALETTE",
    "REFERENCE_RC",
    "TRAVEL_TIME_BAR_STYLE",
    "TWO_PANEL_FIGSIZE",
    "reference_plot_style",
    "save_publication_figure",
]
