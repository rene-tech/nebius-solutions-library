"""App-scoped observability contracts shared by the console and its API.

An app is an independently routed deployment. A source model reference is not
a sufficient selector when two apps deploy the same model.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import Field

from .models import StrictModel

ObservationState = Literal["available", "unavailable", "partial"]


class AppMetricPoint(StrictModel):
    at: datetime
    value: float | None


class AppMetricSeries(StrictModel):
    id: str
    label: str
    points: list[AppMetricPoint] = Field(default_factory=list)


class AppMetricSummary(StrictModel):
    average: float | None = None
    maximum: float | None = None


class AppMetricChart(StrictModel):
    id: str
    title: str
    unit: str
    state: ObservationState = "unavailable"
    reason: str | None = None
    source: str
    aggregation: str
    series: list[AppMetricSeries] = Field(default_factory=list)
    summary: AppMetricSummary = Field(default_factory=AppMetricSummary)


class AppMetrics(StrictModel):
    app_id: str
    from_at: datetime
    to_at: datetime
    step_seconds: int
    charts: list[AppMetricChart]


class AppContainer(StrictModel):
    id: str
    pod_uid: str
    pod_name: str
    namespace: str
    container_name: str
    state: str
    reason: str | None = None
    ready: bool
    node_name: str | None = None
    image: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    restarts: int
    gpu_resources: dict[str, float] = Field(default_factory=dict)
    run_id: str | None = None


class AppContainers(StrictModel):
    app_id: str
    items: list[AppContainer] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    source: str = "kubernetes"
    state: ObservationState = "available"
    reason: str | None = None


class AppLogLine(StrictModel):
    at: datetime
    message: str
    namespace: str
    pod: str
    container: str
    level: str | None = None
    run_id: str | None = None


class AppLogs(StrictModel):
    app_id: str
    items: list[AppLogLine] = Field(default_factory=list)
    next_cursor: str | None = None
    state: ObservationState = "available"
    source: str = "loki"
    reason: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class AppPodIdentity:
    namespace: str
    name: str
    uid: str
    run_id: str | None = None
    gpu_windows: dict[str, list[tuple[datetime, datetime | None]]] = field(default_factory=dict)
