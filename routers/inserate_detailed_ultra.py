"""Removed combined search-and-detail endpoint."""

from fastapi import APIRouter, HTTPException, status


router = APIRouter()


@router.get("/inserate-detailed", deprecated=True)
async def get_inserate_with_details():
    """Direct clients to the explicit search-then-details workflow."""

    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "error": "endpoint_removed",
            "message": (
                "Create a search with POST /jobs/search, then create a detail "
                "job with POST /jobs/details using source_search_job_id"
            ),
            "search_endpoint": "/jobs/search",
            "details_endpoint": "/jobs/details",
        },
    )
