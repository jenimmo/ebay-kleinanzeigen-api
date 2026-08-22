"""Typed contracts shared by the Kleinanzeigen MCP support modules.

The models deliberately mirror only the canonical ``/jobs/*`` API.  They are
forward compatible with additive fields returned by the FastAPI service while
keeping the fields required by waiting and merge helpers explicit.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JobType: TypeAlias = Literal["search", "details"]
JobState: TypeAlias = Literal[
    "queued",
    "running",
    "waiting_cooldown",
    "awaiting_continuation",
    "needs_attention",
    "completed",
    "cancelled",
]

TERMINAL_JOB_STATES = frozenset({"completed", "cancelled"})
ACTIONABLE_JOB_STATES = frozenset(
    {
        "waiting_cooldown",
        "awaiting_continuation",
        "needs_attention",
        *TERMINAL_JOB_STATES,
    }
)
MAX_SNAPSHOT_ITEMS = 200


class _OpenAPIModel(BaseModel):
    """Accept additive response fields without weakening required fields."""

    model_config = ConfigDict(extra="allow")


class JobAccepted(_OpenAPIModel):
    """Durable acknowledgement returned by job creation and action routes."""

    job_id: str = Field(min_length=1)
    type: JobType
    state: JobState
    idempotent_replay: bool = False
    status_url: str = Field(min_length=1)
    results_url: str = Field(min_length=1)


class JobStatus(_OpenAPIModel):
    """Current persisted state of a search or detail job."""

    job_id: str = Field(min_length=1)
    type: JobType
    state: JobState
    progress: dict[str, Any] = Field(default_factory=dict)
    results: dict[str, Any] = Field(default_factory=dict)
    continuation: dict[str, Any] = Field(default_factory=dict)
    resume: dict[str, Any] = Field(default_factory=dict)
    queue: dict[str, Any] = Field(default_factory=dict)
    cooldown: dict[str, Any] = Field(default_factory=dict)
    errors: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    @property
    def actionable(self) -> bool:
        return self.state in ACTIONABLE_JOB_STATES


class JobResultItem(_OpenAPIModel):
    """One search result or one current detail/change-feed item."""

    sequence: int = Field(ge=1)
    listing_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class JobResultsPage(_OpenAPIModel):
    """One native cursor page returned by ``GET /jobs/{id}/results``."""

    job_id: str = Field(min_length=1)
    type: JobType
    items: list[JobResultItem] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool


class WaitProgress(BaseModel):
    """Progress notification emitted after each successful status read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    state: JobState
    poll_count: int = Field(ge=1)
    elapsed_seconds: float = Field(ge=0)
    remaining_seconds: float = Field(ge=0)
    status: JobStatus


class WaitResult(BaseModel):
    """Bounded wait outcome; a timeout leaves the durable job running."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: JobStatus
    timed_out: bool
    poll_count: int = Field(ge=1)
    elapsed_seconds: float = Field(ge=0)


class MergeSnapshotCursor(BaseModel):
    """Immutable cursor for stable, deduplicated result snapshots.

    ``api_cursor`` is the last safe opaque API cursor. ``seen_listing_ids`` is
    ordered rather than a set so serialisation is deterministic across clients.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_cursor: str | None = None
    seen_listing_ids: tuple[str, ...] = ()

    @field_validator("seen_listing_ids")
    @classmethod
    def validate_seen_listing_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > MAX_SNAPSHOT_ITEMS:
            raise ValueError(
                f"seen_listing_ids must contain at most {MAX_SNAPSHOT_ITEMS} entries"
            )
        if len(values) != len(set(values)):
            raise ValueError("seen_listing_ids must be unique")
        if any(not value for value in values):
            raise ValueError("seen_listing_ids must not contain empty values")
        return values


class SearchResultSnapshot(BaseModel):
    """Stable merged search-card snapshot, capped at 200 unique listings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    items: tuple[JobResultItem, ...] = ()
    cursor: MergeSnapshotCursor = Field(default_factory=MergeSnapshotCursor)
    has_more: bool = False
    truncated: bool = False
    pages_merged: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> SearchResultSnapshot:
        _validate_snapshot_items(self.items, self.cursor)
        return self


class DetailResultSnapshot(BaseModel):
    """Latest known detail state for each envelope ``listing_id``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    items: tuple[JobResultItem, ...] = ()
    cursor: MergeSnapshotCursor = Field(default_factory=MergeSnapshotCursor)
    has_more: bool = False
    truncated: bool = False
    pages_merged: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> DetailResultSnapshot:
        _validate_snapshot_items(self.items, self.cursor)
        return self


def _validate_snapshot_items(
    items: tuple[JobResultItem, ...], cursor: MergeSnapshotCursor
) -> None:
    if len(items) > MAX_SNAPSHOT_ITEMS:
        raise ValueError(f"snapshot must contain at most {MAX_SNAPSHOT_ITEMS} items")
    listing_ids = tuple(item.listing_id for item in items)
    if len(listing_ids) != len(set(listing_ids)):
        raise ValueError("snapshot items must have unique listing_id values")
    if listing_ids != cursor.seen_listing_ids:
        raise ValueError("snapshot cursor must describe the snapshot item order")


__all__ = [
    "ACTIONABLE_JOB_STATES",
    "DetailResultSnapshot",
    "JobAccepted",
    "JobResultItem",
    "JobResultsPage",
    "JobState",
    "JobStatus",
    "JobType",
    "MAX_SNAPSHOT_ITEMS",
    "MergeSnapshotCursor",
    "SearchResultSnapshot",
    "TERMINAL_JOB_STATES",
    "WaitProgress",
    "WaitResult",
]
