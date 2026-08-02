"""ThetaData Standard ingestion pipeline (TD-STD milestones TD-1..TD-4).

Collector -> normalizer -> aggregator -> feature engine -> snapshot writer,
per BOT_NEXUS_ThetaData_Standard_Implementation_Guide.pdf Section 3.

Isolation rule: nothing in this package writes to live_gex_snapshot.json,
vex_history.json, iv_intraday_state.json, or any other file owned by the
production GEX pipeline. Where those files are useful (spot, gamma_flip,
net_vex, VEX history), this package only ever opens them read-only.
"""

SCHEMA_VERSION = "td-std-1.0"
