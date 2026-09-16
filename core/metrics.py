"""The ranking metric: DPS by default, rDPS on request.

Upstream ranks every board twice. DPS is the board the site shows first and
the one every record is on; rDPS is the team-contribution reading, and a
record is on that board only when its upload could compute it (as of
2026-09-16 the new parser's uploads, 1.5% of all records). Both boards are
ordered by clear time; what changes between them is which records are
listed and which character counts as the main C.

The option ``--口径`` and the tools' ``metric`` read the same table, so the
two never drift apart; anything else in the plugin speaks of DPS unless it
was asked for rDPS.
"""

METRIC_DPS = "dps"
METRIC_RDPS = "rdps"
METRICS = (METRIC_DPS, METRIC_RDPS)
DEFAULT_METRIC = METRIC_DPS

# What people and models type for either reading; the site's own words for
# them are 直伤口径 and 团队贡献口径.
_METRIC_ALIASES = {
    "dps": METRIC_DPS,
    "直伤": METRIC_DPS,
    "直伤口径": METRIC_DPS,
    "默认": METRIC_DPS,
    "rdps": METRIC_RDPS,
    "r-dps": METRIC_RDPS,
    "团队": METRIC_RDPS,
    "贡献": METRIC_RDPS,
    "团队贡献": METRIC_RDPS,
    "团队贡献口径": METRIC_RDPS,
}


def parse_metric_text(text: str) -> str | None:
    """``dps`` / ``rdps`` for any accepted spelling; None otherwise."""

    value = "".join(text.split()).casefold()
    return _METRIC_ALIASES.get(value)


def metric_label(metric: str) -> str:
    """How the pages and the text spell the metric: DPS or rDPS."""

    return "rDPS" if metric == METRIC_RDPS else "DPS"


def is_rdps(metric: str) -> bool:
    return metric == METRIC_RDPS
