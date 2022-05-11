from __future__ import annotations

from typing import MutableMapping
from typing import Optional

import app.state.services
from app.objects.channel import Channel

cache: MutableMapping[str, Channel] = {}

# create


async def create(
    name: str,
    topic: str,
    read_priv: int,
    write_priv: int,
    auto_join: bool,
    instance: bool,
) -> Channel:
    """Create a channel in cache and the database."""
    # created_at = datetime.now() # TODO: add audit details to db schema

    if not instance:
        # instanced channels only exist in the cache, not database
        # TODO: should channel id be saved in channels objects?
        channel_id = await app.state.services.database.execute(
            "INSERT INTO channels (name, topic, read_priv, write_priv, auto_join) "
            "VALUES (:name, :topic, :read_priv, :write_priv, :auto_join)",
            {
                "name": name,
                "topic": topic,
                "read_priv": read_priv,
                "write_priv": write_priv,
                "auto_join": auto_join,  # TODO: need int()?
            },
        )

    channel = Channel(
        name=name,
        topic=topic,
        read_priv=read_priv,
        write_priv=write_priv,
        auto_join=auto_join,
        instance=instance,
    )

    cache[channel.name] = channel

    # NOTE: if you'd like this new channel to be broadcasted to all the players
    # online, you'll need to send them a packet so the clients are aware of it.

    return channel


# read


def _fetch_by_name_cache(name: str) -> Optional[Channel]:
    """Fetch a channel from the cache by name."""
    return cache.get(name)


async def _fetch_by_name_database(name: str) -> Optional[Channel]:
    """Fetch a channel from the cache by name."""
    row = await app.state.services.database.fetch_one(
        "SELECT * FROM channels WHERE name = :name",
        {"name": name},
    )
    if row is None:
        return None

    return Channel(
        name=row["name"],
        topic=row["topic"],
        read_priv=row["read_priv"],
        write_priv=row["write_priv"],
        auto_join=row["auto_join"] == 1,
    )


async def fetch(name: str) -> Optional[Channel]:
    """Fetch a channel from the cache, or database by name."""
    if channel := _fetch_by_name_cache(name):
        return channel

    if channel := await _fetch_by_name_database(name):
        return channel

    return None


async def fetch_all() -> set[Channel]:
    """Fetch all channels from the cache, or database."""
    if cache:
        return set(cache.values())
    else:
        channel_names = {
            row["name"]
            for row in await app.state.services.database.fetch_all(
                "SELECT name FROM channels",
            )
        }

        channels = set()
        for name in channel_names:
            if channel := await fetch(name):  # should never be false
                channels.add(channel)

        return channels


async def _populate_caches() -> None:
    """Populate the cache with all values from the database."""
    all_resources = await fetch_all()

    for resource in all_resources:
        cache[resource.name] = resource

    return None


# update

# delete


def delete_instance(name: str) -> None:
    """Delete an instanced channel from the cache."""
    if channel := _fetch_by_name_cache(name):
        assert channel.instance, "Channel is not an instance."

        del cache[channel.name]
    else:
        raise ValueError(f"Channel {name} not found in cache.")

    return None


async def delete(name: str) -> None:
    """Delete a channel from the cache and the database."""

    if channel := _fetch_by_name_cache(name):
        del cache[channel.name]
    else:
        raise ValueError(f"Channel {name} not found in cache.")

    if not channel.instance:
        await app.state.services.database.execute(
            "DELETE FROM channels WHERE name = :name",
            {"name": name},
        )
