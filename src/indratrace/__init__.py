"""IndraTrace SDK.

OpenTelemetry-native observability SDK for the IndraTrace platform:
one-line instrumentation for web apps and AI agents.

    from indratrace import init_observability, trace_agent, trace_tool

    init_observability(product="compliance")

    @trace_agent("compliance-checker")
    async def run(query): ...

    @trace_tool
    async def risk_score(vendor): ...

    @trace_step
    def parse(raw): ...          # time any non-AI function
"""

from .agent import trace_agent, trace_step, trace_tool
from .context import current_trace_id, record_feedback, session
from .genai import record_llm_usage
from .init import init_observability
from .logs import bridge_loguru
from .version import __version__
from .web import instrument_flask_app

__all__ = [
    "__version__",
    "bridge_loguru",
    "current_trace_id",
    "init_observability",
    "instrument_flask_app",
    "record_feedback",
    "record_llm_usage",
    "session",
    "trace_agent",
    "trace_step",
    "trace_tool",
]
