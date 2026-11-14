from prometheus_client import Counter, Histogram

TURNS = Counter("bashculture_agent_turns_total", "Completed agent turns", ["mode"])
TOOLS = Counter(
    "bashculture_agent_tools_total", "Attempted tools, including invalid calls", ["tool", "status"]
)
FALLBACKS = Counter("bashculture_agent_fallback_total", "Ordinary-search fallbacks", ["reason"])
LATENCY = Histogram("bashculture_agent_seconds", "Agent turn latency", ["mode"])
COST = Counter(
    "bashculture_agent_rub_total", "Measured or conservatively accounted model cost", ["estimated"]
)


def record(
    mode: str,
    reason: str | None,
    calls: list[dict[str, object]],
    seconds: float,
    cost: float,
    estimated: bool,
) -> None:
    from app.agent.tools import MODELS

    TURNS.labels(mode).inc()
    LATENCY.labels(mode).observe(seconds)
    COST.labels(str(estimated).lower()).inc(cost)
    if mode == "fallback":
        FALLBACKS.labels(reason or "unknown").inc()
    for call in calls:
        name = str(call.get("tool", "unknown"))
        TOOLS.labels(name if name in MODELS else "unknown", str(call.get("status", "error"))).inc()
