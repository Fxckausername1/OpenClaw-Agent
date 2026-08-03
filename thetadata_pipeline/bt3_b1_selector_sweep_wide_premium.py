"""One-off extension of bt3_b1_selector_sweep.py (heff's 2026-07-30 ask, after
watching today's 5 real SMC_TRIANGLE signals all fail contract selection):
how much does widening the PREMIUM BAND specifically, all the way to
$0.20-$1.00, help fill rate versus the live moderate_combo config
(premium $0.15-$0.40)? Isolates the premium lever alone -- keeps every
other moderate_combo threshold (min_abs_delta=0.10, spread/quote-age/
ask-size gates) unchanged, so the comparison against the existing
moderate_combo_report.json is apples-to-apples on the ONE thing that
changed.

Deliberately a standalone script, not an edit to bt3_b1_selector_sweep.py's
own VARIANTS dict -- that file's main() reruns all 4 of its variants
sequentially (~70min total the night of 2026-07-29), and there's no reason
to redo the other 3 to add a 5th. Reuses that module's run_variant() /
load_triangle_events() / generate_b1_signals() unmodified and writes into
the SAME data/thetadata/bt3_b1_selector_sweep/ namespace so the new report
sits alongside the other 4 for direct comparison.
"""
from __future__ import annotations

import logging

from thetadata_pipeline.bt2_selector import SelectorConfig
from thetadata_pipeline.bt3_b1_indicator_only import TRIANGLE_EVENTS_PATH, generate_b1_signals, load_triangle_events
from thetadata_pipeline.bt3_b1_selector_sweep import run_variant

logger = logging.getLogger("thetadata_pkg.bt3_b1_selector_sweep_wide_premium")

VARIANT_NAME = "wide_premium_020_100"
VARIANT_CONFIG = SelectorConfig(min_abs_delta=0.10, premium_low=0.20, premium_high=1.00)


def main():
    logging.basicConfig(level=logging.INFO)
    events = load_triangle_events(TRIANGLE_EVENTS_PATH)
    signals = generate_b1_signals(events)
    logger.info("wide-premium sweep: %d signals loaded, running variant '%s'", len(signals), VARIANT_NAME)
    run_variant(VARIANT_NAME, VARIANT_CONFIG, signals)
    logger.info("wide-premium sweep complete")


if __name__ == "__main__":
    main()
