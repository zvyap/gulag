from __future__ import annotations

import app.state.services
from app.objects.achievement import Achievement

# TODO: is this weird? all the others are mappings
cache = set()

## create

## read


async def fetch_all() -> set[Achievement]:
    if cache:
        return cache
    else:
        achievements = set()
        for row in await app.state.services.database.fetch_all(
            "SELECT * FROM achievements",
        ):
            row = dict(row)  # make mutable copy

            # NOTE: achievement conditions are stored as stringified python
            # expressions in the database to allow for extensive customizability.
            condition = eval(f"lambda score, mode_vn: {row.pop('cond')}")

            achievement = Achievement(**row, condition=condition)
            achievements.add(achievement)

        return achievements


async def _populate_caches() -> None:
    all_resources = await fetch_all()

    for resource in all_resources:
        cache.add(resource)

    return None


## update

## delete
