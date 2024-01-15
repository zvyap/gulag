""" bmap: static beatmap info (thumbnails, previews, etc.) """
from __future__ import annotations

from fastapi import APIRouter
from fastapi import status
from fastapi.requests import Request
from fastapi.responses import RedirectResponse

import app.settings

# import app.settings

router = APIRouter(tags=["Beatmaps"])

@router.get("/preview/{beatmap_set_id}.mp3")
async def preview(beatmap_set_id: int) -> RedirectResponse:
    return RedirectResponse(f"{app.settings.MIRROR_BEATMAP_PREVIEW}/{beatmap_set_id}", status_code=status.HTTP_301_MOVED_PERMANENTLY)


# forward any unmatched request to osu!
# eventually if we do bmap submission, we'll need this.
@router.get("/{file_path:path}")
async def everything(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=f"https://b.ppy.sh{request['path']}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )
