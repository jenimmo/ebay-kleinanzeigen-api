from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from utils.parse_kleinanzeigen_url import (
    InvalidUrlFilterError,
    parse_kleinanzeigen_url,
    map_to_inserate_params,
)

router = APIRouter()


class ConvertUrlRequest(BaseModel):
    url: str


@router.post("/convert-url")
async def convert_url(request: ConvertUrlRequest):
    try:
        parsed = parse_kleinanzeigen_url(request.url)
    except (InvalidUrlFilterError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed URL filter") from exc
    inserate_params, unmapped = map_to_inserate_params(parsed)
    return {"inserate_params": inserate_params, "unmapped": unmapped}
