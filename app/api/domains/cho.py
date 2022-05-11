""" cho: handle cho packets from the osu! client """
from __future__ import annotations

import asyncio
import re
import struct
import time
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Callable
from typing import Literal
from typing import Optional
from typing import Sequence
from typing import TypedDict
from typing import Union

import databases.core
from fastapi import APIRouter
from fastapi import Response
from fastapi.param_functions import Header
from fastapi.requests import Request
from fastapi.responses import HTMLResponse

import app.packets
import app.settings
import app.state
import app.utils
from app import commands
from app import menus
from app import repositories
from app import usecases
from app._typing import IPAddress
from app.constants import regexes
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.constants.mods import SPEED_CHANGING_MODS
from app.constants.privileges import ClientPrivileges
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log
from app.logging import magnitude_fmt_time
from app.objects.channel import Channel
from app.objects.match import Match
from app.objects.match import MatchTeams
from app.objects.match import MatchTeamTypes
from app.objects.match import MatchWinConditions
from app.objects.match import Slot
from app.objects.match import SlotStatus
from app.objects.menu import Menu
from app.objects.menu import MenuCommands
from app.objects.menu import MenuFunction
from app.objects.player import Action
from app.objects.player import ClientDetails
from app.objects.player import LastNp
from app.objects.player import OsuVersion
from app.objects.player import Player
from app.objects.player import PresenceFilter
from app.packets import BanchoPacketReader
from app.packets import BasePacket
from app.packets import ClientPackets
from app.usecases.performance import ScoreDifficultyParams

BEATMAPS_PATH = Path.cwd() / ".data/osu"
FIRST_BANCHOPY_USER_ID = 3

BASE_DOMAIN = app.settings.DOMAIN

# TODO: dear god
NOW_PLAYING_RGX = re.compile(
    r"^\x01ACTION is (?:playing|editing|watching|listening to) "
    rf"\[https://osu\.(?:{re.escape(BASE_DOMAIN)}|ppy\.sh)/beatmapsets/(?P<sid>\d{{1,10}})#/?(?:osu|taiko|fruits|mania)?/(?P<bid>\d{{1,10}})/? .+\]"
    r"(?: <(?P<mode_vn>Taiko|CatchTheBeat|osu!mania)>)?"
    r"(?P<mods>(?: (?:-|\+|~|\|)\w+(?:~|\|)?)+)?\x01$",
)

router = APIRouter(tags=["Bancho API"])


@router.get("/")
async def bancho_http_handler():
    """Handle a request from a web browser."""
    packets = app.state.packets["all"]

    return HTMLResponse(
        b"<!DOCTYPE html>"
        + "<br>".join(
            (
                f"Running bancho.py v{app.settings.VERSION}",
                f"Players online: {len(app.state.sessions.players) - 1}",
                '<a href="https://github.com/osuAkatsuki/bancho.py">Source code</a>',
                "",
                f"<b>packets handled ({len(packets)})</b>",
                "<br>".join([f"{p.name} ({p.value})" for p in packets]),
            ),
        ).encode(),
    )


@router.post("/")
async def bancho_handler(
    request: Request,
    osu_token: Optional[str] = Header(None),
    user_agent: Literal["osu!"] = Header(...),
):
    ip = app.state.services.ip_resolver.get_ip(request.headers)
    if osu_token is None:
        # the client is performing a login
        async with app.state.services.database.connection() as db_conn:
            login_data = await login(await request.body(), ip, db_conn)

        return Response(
            content=login_data["response_body"],
            headers={"cho-token": login_data["osu_token"]},
        )

    # get the player from the specified osu token.
    player = app.state.sessions.players.get(token=osu_token)

    if not player:
        # chances are, we just restarted the server
        # tell their client to reconnect immediately.
        return Response(
            content=(
                app.packets.notification("Server has restarted.")
                + app.packets.restart_server(0)  # ms until reconnection
            ),
        )

    if player.restricted:
        # restricted users may only use certain packet handlers.
        packet_map = app.state.packets["restricted"]
    else:
        packet_map = app.state.packets["all"]

    # bancho connections can be comprised of multiple packets;
    # our reader is designed to iterate through them individually,
    # allowing logic to be implemented around the actual handler.
    # NOTE: any unhandled packets will be ignored internally.

    with memoryview(await request.body()) as body_view:
        for packet in BanchoPacketReader(body_view, packet_map):
            await packet.handle(player)

    player.last_recv_time = time.time()

    response_data = player.dequeue()
    return Response(content=response_data)


""" Packet logic """


def register(
    packet: ClientPackets,
    restricted: bool = False,
) -> Callable[[type[BasePacket]], type[BasePacket]]:
    """Register a handler in `app.state.packets`."""

    def wrapper(cls: type[BasePacket]) -> type[BasePacket]:
        app.state.packets["all"][packet] = cls

        if restricted:
            app.state.packets["restricted"][packet] = cls

        return cls

    return wrapper


@register(ClientPackets.PING, restricted=True)
class Ping(BasePacket):
    async def handle(self, player: Player) -> None:
        pass  # ping be like


@register(ClientPackets.CHANGE_ACTION, restricted=True)
class ChangeAction(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.action = reader.read_u8()
        self.info_text = reader.read_string()
        self.map_md5 = reader.read_string()

        self.mods = reader.read_u32()
        self.mode = reader.read_u8()
        if self.mods & Mods.RELAX:
            if self.mode == 3:  # rx!mania doesn't exist
                self.mods &= ~Mods.RELAX
            else:
                self.mode += 4
        elif self.mods & Mods.AUTOPILOT:
            if self.mode in (1, 2, 3):  # ap!catch, taiko and mania don't exist
                self.mods &= ~Mods.AUTOPILOT
            else:
                self.mode += 8

        self.map_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        # update the user's status.
        player.status.action = Action(self.action)
        player.status.info_text = self.info_text
        player.status.map_md5 = self.map_md5
        player.status.mods = Mods(self.mods)
        player.status.mode = GameMode(self.mode)
        player.status.map_id = self.map_id

        # broadcast it to all online players.
        if not player.restricted:
            app.state.sessions.players.enqueue(app.packets.user_stats(player))


IGNORED_CHANNELS = ["#highlight", "#userlog"]


async def contextually_fetch_channel(
    player: Player,
    channel_name: str,
) -> Optional[Channel]:
    """Resolve the channel from the player object and channel name."""
    if channel_name == "#spectator":
        if player.spectating:  # we're spectating someone
            spec_id = player.spectating.id
        elif player.spectators:  # someone's spectating us
            spec_id = player.id
        else:
            return None

        return await repositories.channels.fetch(f"#spec_{spec_id}")
    elif channel_name == "#multiplayer":
        if not player.match:
            # they're not in a match?
            return

        return player.match.chat
    else:
        return await repositories.channels.fetch(channel_name)


@register(ClientPackets.SEND_PUBLIC_MESSAGE)
class SendMessage(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.msg = reader.read_message()

    async def handle(self, player: Player) -> None:
        if player.silenced:
            log(f"{player} sent a message while silenced.", Ansi.LYELLOW)
            return

        msg = self.msg.text.strip()  # remove leading & trailing whitespace

        if not msg:
            return

        recipient = self.msg.recipient
        if recipient in IGNORED_CHANNELS:
            return

        channel = await contextually_fetch_channel(player, recipient)
        if channel is None:
            log(f"{player} wrote to non-existent {recipient}.", Ansi.LYELLOW)
            return

        if player not in channel:
            log(f"{player} wrote to {recipient} without being in it.")
            return

        sufficient_privileges = usecases.channels.can_write(channel, player.priv)
        if not sufficient_privileges:
            log(f"{player} wrote to {recipient} with insufficient privileges.")
            return

        # limit message length to 2k chars
        # perhaps this could be dangerous with !py..?
        if len(msg) > 2000:
            msg = f"{msg[:2000]}... (truncated)"
            player.enqueue(
                app.packets.notification(
                    "Your message was truncated\n(exceeded 2000 characters).",
                ),
            )

        if msg.startswith(app.settings.COMMAND_PREFIX):
            command_response = await commands.process_commands(player, channel, msg)
        else:
            command_response = None

        if command_response:
            # a command was triggered.
            if not command_response["hidden"]:
                usecases.channels.send_msg_to_clients(channel, msg, sender=player)
                if command_response["resp"] is not None:
                    usecases.channels.send_bot(channel, command_response["resp"])
            else:
                # hidden message
                staff = app.state.sessions.players.staff

                # send player's command trigger to staff
                usecases.channels.send_selective(
                    channel,
                    msg=msg,
                    sender=player,
                    recipients=staff - {player},
                )

                # send bot response to player & staff
                if command_response["resp"] is not None:
                    usecases.channels.send_selective(
                        channel,
                        msg=command_response["resp"],
                        sender=app.state.sessions.bot,
                        recipients=staff | {player},
                    )

        else:
            # no commands were triggered

            # check if the user is /np'ing a map.
            # even though this is a public channel,
            # we'll update the player's last np stored.
            if r_match := NOW_PLAYING_RGX.match(msg):
                # the player is /np'ing a map.
                # save it to their player instance
                # so we can use this elsewhere owo..

                beatmap = await repositories.beatmaps.fetch_by_id(
                    int(r_match["bid"]),
                )

                if beatmap is not None:
                    # parse mode_vn int from regex
                    if r_match["mode_vn"] is not None:
                        mode_vn = {"Taiko": 1, "CatchTheBeat": 2, "osu!mania": 3}[
                            r_match["mode_vn"]
                        ]
                    else:
                        # use player mode if not specified
                        mode_vn = player.status.mode.as_vanilla

                    player.last_np = {
                        "bmap": beatmap,
                        "mode_vn": mode_vn,
                        "timeout": time.time() + 300,  # /np's last 5mins
                    }
                else:
                    # time out their previous /np
                    player.last_np["timeout"] = 0.0

            usecases.channels.send_msg_to_clients(channel, msg, sender=player)

        asyncio.create_task(usecases.players.update_latest_activity(player))
        log(f"{player} @ {channel}: {msg}", Ansi.LCYAN, file=".data/logs/chat.log")


@register(ClientPackets.LOGOUT, restricted=True)
class Logout(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        reader.read_i32()  # reserved

    async def handle(self, player: Player) -> None:
        if (time.time() - player.login_time) < 1:
            # osu! has a weird tendency to log out immediately after login.
            # i've tested the times and they're generally 300-800ms, so
            # we'll block any logout request within 1 second from login.
            return

        await usecases.players.logout(player)

        asyncio.create_task(usecases.players.update_latest_activity(player))


@register(ClientPackets.REQUEST_STATUS_UPDATE, restricted=True)
class StatsUpdateRequest(BasePacket):
    async def handle(self, p: Player) -> None:
        p.enqueue(app.packets.user_stats(p))


# Some messages to send on welcome/restricted/etc.
# TODO: these should probably be moved to the config.
WELCOME_MSG = "\n".join(
    (
        f"Welcome to {BASE_DOMAIN}.",
        "To see a list of commands, use !help.",
        "We have a public (Discord)[https://discord.gg/ShEQgUx]!",
        "Enjoy the server!",
    ),
)

RESTRICTED_MSG = (
    "Your account is currently in restricted mode. "
    "If you believe this is a mistake, or have waited a period "
    "greater than 3 months, you may appeal via the form on the site."
)

WELCOME_NOTIFICATION = app.packets.notification(
    f"Welcome back to {BASE_DOMAIN}!\nRunning bancho.py v{app.settings.VERSION}.",
)

OFFLINE_NOTIFICATION = app.packets.notification(
    "The server is currently running in offline mode; "
    "some features will be unavailble.",
)

DELTA_90_DAYS = timedelta(days=90)


class LoginResponse(TypedDict):
    osu_token: str
    response_body: bytes


class LoginData(TypedDict):
    username: str
    password_md5: bytes
    osu_version: str
    utc_offset: int
    display_city: bool
    pm_private: bool
    osu_path_md5: str
    adapters_str: str
    adapters_md5: str
    uninstall_md5: str
    disk_signature_md5: str


def parse_login_data(data: bytes) -> LoginData:
    """\
    Parse data from the body of a login request.

    Format:
      username\npasswd_md5\nosu_version|utc_offset|display_city|client_hashes|pm_private\n
    """
    (
        username,
        password_md5,
        remainder,
    ) = data.decode().split("\n", maxsplit=2)

    (
        osu_version,
        utc_offset,
        display_city,
        client_hashes,
        pm_private,
    ) = remainder.split("|", maxsplit=4)

    (
        osu_path_md5,
        adapters_str,
        adapters_md5,
        uninstall_md5,
        disk_signature_md5,
    ) = client_hashes[:-1].split(":", maxsplit=4)

    return {
        "username": username,
        "password_md5": password_md5.encode(),
        "osu_version": osu_version,
        "utc_offset": int(utc_offset),
        "display_city": display_city == "1",
        "pm_private": pm_private == "1",
        "osu_path_md5": osu_path_md5,
        "adapters_str": adapters_str,
        "adapters_md5": adapters_md5,
        "uninstall_md5": uninstall_md5,
        "disk_signature_md5": disk_signature_md5,
    }


async def login(
    body: bytes,
    ip: IPAddress,
    db_conn: databases.core.Connection,
) -> LoginResponse:
    """\
    Login has no specific packet, but happens when the osu!
    client sends a request without an 'osu-token' header.

    Request format:
      username\npasswd_md5\nosu_version|utc_offset|display_city|client_hashes|pm_private\n

    Response format:
      Packet 5 (userid), with ID:
      -1: authentication failed
      -2: old client
      -3: banned
      -4: banned
      -5: error occurred
      -6: needs supporter
      -7: password reset
      -8: requires verification
      other: valid id, logged in
    """

    # parse login data
    login_data = parse_login_data(body)

    # perform some validation & further parsing on the data

    match = regexes.OSU_VERSION.match(login_data["osu_version"])
    if match is None:
        return {
            "osu_token": "invalid-request",
            "response_body": b"",
        }

    osu_version = OsuVersion(
        date=date(
            year=int(match["date"][0:4]),
            month=int(match["date"][4:6]),
            day=int(match["date"][6:8]),
        ),
        revision=int(match["revision"]) if match["revision"] else None,
        stream=match["stream"] or "stable",
    )

    # disallow login for clients older than 90 days
    if osu_version.date < (date.today() - DELTA_90_DAYS):
        return {
            "osu_token": "client-too-old",
            "response_body": (
                app.packets.version_update_forced() + app.packets.user_id(-2)
            ),
        }

    running_under_wine = login_data["adapters_str"] == "runningunderwine"
    adapters = [a for a in login_data["adapters_str"][:-1].split(".")]

    if not (running_under_wine or any(adapters)):
        return {
            "osu_token": "empty-adapters",
            "response_body": (
                app.packets.user_id(-1)
                + app.packets.notification("Please restart your osu! and try again.")
            ),
        }

    ## parsing successful

    login_time = time.time()

    # TODO: improve tournament client support

    online_player = app.state.sessions.players.get(name=login_data["username"])
    if online_player is not None:
        # player is already logged in - allow this only for tournament clients

        if not (osu_version.stream == "tourney" or online_player.tourney_client):
            # neither session is a tournament client, disallow

            if (login_time - online_player.last_recv_time) > 10:
                # let this session overrule the existing one
                # (this is made to help prevent user ghosting)
                await usecases.players.logout(online_player)
            else:
                # current session is still active, disallow
                return {
                    "osu_token": "user-ghosted",
                    "response_body": (
                        app.packets.user_id(-1)
                        + app.packets.notification("User already logged in.")
                    ),
                }

    player = await repositories.players.fetch(name=login_data["username"])

    if player is None:
        # no account by this name exists.
        return {
            "osu_token": "login-failed",
            "response_body": (
                app.packets.notification(
                    f"Login attempt failed.\n"
                    "Incorrect username or password.\n"
                    "\n"
                    f"Server: {BASE_DOMAIN}",
                )
                + app.packets.user_id(-1)
            ),
        }

    # validate login credentials
    correct_login = usecases.players.validate_credentials(
        password=login_data["password_md5"],
        hashed_password=player.pw_bcrypt,  # type: ignore
    )

    if not correct_login:
        # incorrect username:password combination.
        return {
            "osu_token": "login-failed",
            "response_body": (
                app.packets.notification(
                    f"Login attempt failed.\n"
                    "Incorrect username or password.\n"
                    "\n"
                    f"Server: {BASE_DOMAIN}",
                )
                + app.packets.user_id(-1)
            ),
        }

    if osu_version.stream == "tourney" and not (
        player.priv & Privileges.DONATOR and player.priv & Privileges.UNRESTRICTED
    ):
        # trying to use tourney client with insufficient privileges.
        return {
            "osu_token": "no",
            "response_body": app.packets.user_id(-1),
        }

    """ login credentials verified """

    # TODO: move queries into repositories (perhaps with usecase layers)

    await db_conn.execute(
        "INSERT INTO ingame_logins "
        "(userid, ip, osu_ver, osu_stream, datetime) "
        "VALUES (:id, :ip, :osu_ver, :osu_stream, NOW())",
        {
            "id": player.id,
            "ip": str(ip),
            "osu_ver": osu_version.date,
            "osu_stream": osu_version.stream,
        },
    )

    await db_conn.execute(
        "INSERT INTO client_hashes "
        "(userid, osupath, adapters, uninstall_id,"
        " disk_serial, latest_time, occurrences) "
        "VALUES (:id, :osupath, :adapters, :uninstall, :disk_serial, NOW(), 1) "
        "ON DUPLICATE KEY UPDATE "
        "occurrences = occurrences + 1, "
        "latest_time = NOW() ",
        {
            "id": player.id,
            "osupath": login_data["osu_path_md5"],
            "adapters": login_data["adapters_md5"],
            "uninstall": login_data["uninstall_md5"],
            "disk_serial": login_data["disk_signature_md5"],
        },
    )

    # TODO: store adapters individually

    if running_under_wine:
        hw_checks = "h.uninstall_id = :uninstall"
        hw_args = {"uninstall": login_data["uninstall_md5"]}
    else:
        hw_checks = (
            "h.adapters = :adapters OR "
            "h.uninstall_id = :uninstall OR "
            "h.disk_serial = :disk_serial"
        )
        hw_args = {
            "adapters": login_data["adapters_md5"],
            "uninstall": login_data["uninstall_md5"],
            "disk_serial": login_data["disk_signature_md5"],
        }

    hw_matches = await db_conn.fetch_all(
        "SELECT u.name, u.priv, h.occurrences "
        "FROM client_hashes h "
        "INNER JOIN users u ON h.userid = u.id "
        "WHERE h.userid != :user_id AND "
        f"({hw_checks})",
        {"user_id": player.id, **hw_args},
    )

    if hw_matches:
        # we have other accounts with matching hashes
        if player.priv & Privileges.VERIFIED:
            # TODO: this is a normal, registered & verified player.
            ...
        else:
            # this player is not verified yet, this is their first
            # time connecting in-game and submitting their hwid set.
            # we will not allow any banned matches; if there are any,
            # then ask the user to contact staff and resolve manually.
            if not all(
                [hw_match["priv"] & Privileges.UNRESTRICTED for hw_match in hw_matches],
            ):
                return {
                    "osu_token": "contact-staff",
                    "response_body": (
                        app.packets.notification(
                            "Please contact staff directly to create an account.",
                        )
                        + app.packets.user_id(-1)
                    ),
                }

    """ All checks passed, player is safe to login """

    ## set session-specific player attributes

    player.token = usecases.players.generate_token()
    player.login_time = login_time
    player.utc_offset = login_data["utc_offset"]
    player.pm_private = login_data["pm_private"]
    player.tourney_client = osu_version.stream == "tourney"
    player.current_menu = menus.default.MAIN_MENU

    if not ip.is_private:
        geoloc = await usecases.geolocation.lookup(ip)

        if geoloc is not None:
            player.geoloc = geoloc
        else:
            log(f"Geolocation lookup for {ip} failed", Ansi.LRED)

    player.client_details = ClientDetails(
        osu_version=osu_version,
        osu_path_md5=login_data["osu_path_md5"],
        adapters_md5=login_data["adapters_md5"],
        uninstall_md5=login_data["uninstall_md5"],
        disk_signature_md5=login_data["disk_signature_md5"],
        adapters=adapters,
        ip=ip,
    )

    data = bytearray(app.packets.protocol_version(19))
    data += app.packets.user_id(player.id)

    # *real* client privileges are sent with this packet,
    # then the user's apparent privileges are sent in the
    # userPresence packets to other players. we'll send
    # supporter along with the user's privileges here,
    # but not in userPresence (so that only donators
    # show up with the yellow name in-game, but everyone
    # gets osu!direct & other in-game perks).
    data += app.packets.bancho_privileges(
        player.bancho_priv | ClientPrivileges.SUPPORTER,
    )

    data += WELCOME_NOTIFICATION

    # send all appropriate channel info to our player.
    # the osu! client will attempt to join the channels.
    for channel in repositories.channels.cache.values():
        if (
            not channel.auto_join  # TODO: is this correct?
            or not usecases.channels.can_read(channel, player.priv)
            or channel._name == "#lobby"  # (can't be in mp lobby @ login)
        ):
            continue

        # send chan info to all players who can see
        # the channel (to update their playercounts)
        channel_info_packet = app.packets.channel_info(
            channel._name,
            channel.topic,
            len(channel.players),
        )

        data += channel_info_packet

        for other in app.state.sessions.players:
            if usecases.channels.can_read(channel, other.priv):
                other.enqueue(channel_info_packet)

    # tells osu! to reorder channels based on config.
    data += app.packets.channel_info_end_marker()

    data += app.packets.main_menu_icon(
        icon_url=app.settings.MENU_ICON_URL,
        onclick_url=app.settings.MENU_ONCLICK_URL,
    )
    data += app.packets.friends_list(player.friends)
    data += app.packets.silence_end(player.remaining_silence)

    # update our new player's stats, and broadcast them.
    user_data = app.packets.user_presence(player) + app.packets.user_stats(player)

    data += user_data

    if not player.restricted:  # player is unrestricted, two way communication
        for other in app.state.sessions.players:
            # enqueue us to them
            other.enqueue(user_data)

            # enqueue them to us.
            if not other.restricted:
                if other is app.state.sessions.bot:
                    # optimization for bot since it's
                    # the most frequently requested user
                    data += app.packets.bot_presence(other)
                    data += app.packets.bot_stats(other)
                else:
                    data += app.packets.user_presence(other)
                    data += app.packets.user_stats(other)

        # the player may have been sent mail while offline,
        # enqueue any messages from their respective authors.
        mail_rows = await usecases.mail.fetch_unread(player.id)

        if mail_rows:
            received_from = set()  # ids

            for msg in mail_rows:
                if msg["from"] not in received_from:
                    data += app.packets.send_message(
                        sender=msg["from"],
                        msg="Unread messages",
                        recipient=msg["to"],
                        sender_id=msg["from_id"],
                    )
                    received_from.add(msg["from"])

                msg_time = datetime.fromtimestamp(msg["time"])

                data += app.packets.send_message(
                    sender=msg["from"],
                    msg=f'[{msg_time:%a %b %d @ %H:%M%p}] {msg["msg"]}',
                    recipient=msg["to"],
                    sender_id=msg["from_id"],
                )

        if not player.priv & Privileges.VERIFIED:
            # this is the player's first login, verify their
            # account & send info about the server/its usage.
            await usecases.players.add_privileges(player, Privileges.VERIFIED)

            if player.id == FIRST_BANCHOPY_USER_ID:
                # this is the first player registering on
                # the server, grant them full privileges.
                new_privileges = (
                    Privileges.STAFF
                    | Privileges.NOMINATOR
                    | Privileges.WHITELISTED
                    | Privileges.TOURNEY_MANAGER
                    | Privileges.DONATOR
                    | Privileges.ALUMNI
                )
                await usecases.players.add_privileges(player, new_privileges)

            data += app.packets.send_message(
                sender=app.state.sessions.bot.name,
                msg=WELCOME_MSG,
                recipient=player.name,
                sender_id=app.state.sessions.bot.id,
            )

    else:  # player is restricted, one way communication
        for other in app.state.sessions.players.unrestricted:
            # enqueue them to us.
            if other is app.state.sessions.bot:
                # optimization for bot since it's
                # the most frequently requested user
                data += app.packets.bot_presence(other)
                data += app.packets.bot_stats(other)
            else:
                data += app.packets.user_presence(other)
                data += app.packets.user_stats(other)

        data += app.packets.account_restricted()
        data += app.packets.send_message(
            sender=app.state.sessions.bot.name,
            msg=RESTRICTED_MSG,
            recipient=player.name,
            sender_id=app.state.sessions.bot.id,
        )

    # TODO: some sort of admin panel for staff members?

    # add the player to the sessions list,
    # making them officially logged in.
    app.state.sessions.players.append(player)

    if app.state.services.datadog is not None:
        if not player.restricted:
            app.state.services.datadog.increment("bancho.online_players")

        time_taken = time.time() - login_time
        app.state.services.datadog.histogram("bancho.login_time", time_taken)

    user_os = "unix (wine)" if running_under_wine else "win32"
    country_code = player.geoloc["country"]["acronym"].upper()

    log(
        f"{player} logged in from {country_code} using {login_data['osu_version']} on {user_os}",
        Ansi.LCYAN,
    )

    asyncio.create_task(usecases.players.update_latest_activity(player))

    return {"osu_token": player.token, "response_body": bytes(data)}


@register(ClientPackets.START_SPECTATING)
class StartSpectating(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.target_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        new_host = app.state.sessions.players.get(id=self.target_id)
        if new_host is None:
            log(
                f"{player} tried to spectate nonexistant id {self.target_id}.",
                Ansi.LYELLOW,
            )
            return

        if current_host := player.spectating:
            if current_host == new_host:
                # host hasn't changed, they didn't have
                # the map but have downloaded it.

                if not player.stealth:
                    # NOTE: player would have already received the other
                    # fellow spectators, so no need to resend them.
                    new_host.enqueue(app.packets.spectator_joined(player.id))

                    spectator_joined_packet = app.packets.fellow_spectator_joined(
                        player.id,
                    )
                    for spectator in new_host.spectators:
                        if spectator is not player:
                            spectator.enqueue(spectator_joined_packet)

                return

            await usecases.players.remove_spectator(current_host, player)

        await usecases.players.add_spectator(new_host, player)


@register(ClientPackets.STOP_SPECTATING)
class StopSpectating(BasePacket):
    async def handle(self, player: Player) -> None:
        host = player.spectating

        if not host:
            log(f"{player} tried to stop spectating when they're not..?", Ansi.LRED)
            return

        await usecases.players.remove_spectator(host, player)


@register(ClientPackets.SPECTATE_FRAMES)
class SpectateFrames(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.frame_bundle = reader.read_replayframe_bundle()

    async def handle(self, player: Player) -> None:
        # TODO: perform validations on the parsed frame bundle
        # to ensure it's not being tamperated with or weaponized.

        # NOTE: this is given a fastpath here for efficiency due to the
        # sheer rate of usage of these packets in spectator mode.

        # data = app.packets.spectateFrames(self.frame_bundle.raw_data)
        data = (
            struct.pack("<HxI", 15, len(self.frame_bundle.raw_data))
            + self.frame_bundle.raw_data
        )

        # enqueue the data
        # to all spectators.
        for spectator in player.spectators:
            spectator.enqueue(data)


@register(ClientPackets.CANT_SPECTATE)
class CantSpectate(BasePacket):
    async def handle(self, player: Player) -> None:
        if not player.spectating:
            log(f"{player} sent can't spectate while not spectating?", Ansi.LRED)
            return

        if player.stealth:
            # don't send spectator packets in stealth mode
            return

        data = app.packets.spectator_cant_spectate(player.id)

        host = player.spectating
        host.enqueue(data)

        for spectator in host.spectators:
            spectator.enqueue(data)


async def handle_bot_message(player: Player, target: Player, msg: str) -> None:
    """Handle a message to the bot; namely commands and /np support."""
    if msg.startswith(app.settings.COMMAND_PREFIX):
        # use is executing a command, e.g. `!help`
        command_response = await commands.process_commands(player, target, msg)
        if command_response is None:
            return None

        if command_response["resp"] is not None:
            usecases.players.send(
                player=player,
                msg=command_response["resp"],
                sender=target,
            )

        return None

    if r_match := NOW_PLAYING_RGX.match(msg):
        # user is `/np`ing a map in chat
        # save it to their player instance
        # so we can use it later contextually
        # also, send pp values for different
        # accs in the current contextual gamemode

        beatmap = await repositories.beatmaps.fetch_by_id(int(r_match["bid"]))
        if beatmap is None:
            if player.last_np is not None:
                # time out their previous /np
                player.last_np["timeout"] = 0.0

            usecases.players.send(player, "Could not find map.", sender=target)
            return

        # parse mode_vn int from regex
        if r_match["mode_vn"] is not None:
            mode_vn = {"Taiko": 1, "CatchTheBeat": 2, "osu!mania": 3}[
                r_match["mode_vn"]
            ]
        else:
            # use player mode if not specified
            mode_vn = player.status.mode.as_vanilla

        # TODO: think of a name better than 'np'
        new_np: LastNp = {
            "bmap": beatmap,
            "mode_vn": mode_vn,
            "timeout": time.time() + 300,  # /np's last 5mins
        }
        player.last_np = new_np

        # calculate generic pp values from their /np

        osu_file_path = BEATMAPS_PATH / f"{beatmap.id}.osu"
        if not await usecases.beatmaps.ensure_local_osu_file(
            osu_file_path,
            beatmap.id,
            beatmap.md5,
        ):
            usecases.players.send(
                player,
                "Mapfile could not be found; " "this incident has been reported.",
                sender=target,
            )
        else:
            # calculate pp for common generic values
            pp_calc_st = time.time_ns()

            if r_match["mods"] is not None:
                # [1:] to remove leading whitespace
                mods_str = r_match["mods"][1:]
                mods = Mods.from_np(mods_str, mode_vn)
            else:
                mods = None

            if mode_vn in (0, 1, 2):
                scores: list[ScoreDifficultyParams] = [
                    {"acc": acc} for acc in app.settings.PP_CACHED_ACCURACIES
                ]
            else:  # mode_vn == 3
                scores: list[ScoreDifficultyParams] = [
                    {"score": score} for score in app.settings.PP_CACHED_SCORES
                ]

            results = usecases.performance.calculate_performances(
                osu_file_path=str(osu_file_path),
                mode=mode_vn,
                mods=int(mods) if mods is not None else None,
                scores=scores,
            )

            if mode_vn in (0, 1, 2):
                resp_msg = " | ".join(
                    f"{acc}%: {result['performance']:,.2f}pp"
                    for acc, result in zip(
                        app.settings.PP_CACHED_ACCURACIES,
                        results,
                    )
                )
            else:  # mode_vn == 3
                resp_msg = " | ".join(
                    f"{score // 1000:.0f}k: {result['performance']:,.2f}pp"
                    for score, result in zip(
                        app.settings.PP_CACHED_SCORES,
                        results,
                    )
                )

            elapsed = time.time_ns() - pp_calc_st
            resp_msg += f" | Elapsed: {magnitude_fmt_time(elapsed)}"

            usecases.players.send(player, resp_msg, sender=target)


@register(ClientPackets.SEND_PRIVATE_MESSAGE)
class SendPrivateMessage(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.msg = reader.read_message()

    async def handle(self, player: Player) -> None:
        if player.silenced:
            if app.settings.DEBUG:
                log(f"{player} tried to send a dm while silenced.", Ansi.LYELLOW)
            return

        # remove leading/trailing whitespace
        msg = self.msg.text.strip()

        if not msg:
            return

        target_name = self.msg.recipient

        # NOTE: this intentionally fetches offline players
        # players can receive messages offline through the mail system
        target = await repositories.players.fetch(name=target_name)
        if target is None:
            if app.settings.DEBUG:
                log(
                    f"{player} tried to write to non-existent user {target_name}.",
                    Ansi.LYELLOW,
                )
            return

        if player.id in target.blocks:
            player.enqueue(app.packets.user_dm_blocked(target_name))

            if app.settings.DEBUG:
                log(f"{player} tried to message {target}, but they have them blocked.")

            return

        if target.pm_private and player.id not in target.friends:
            player.enqueue(app.packets.user_dm_blocked(target_name))

            if app.settings.DEBUG:
                log(f"{player} tried to message {target}, but they are blocking dms.")

            return

        if target.silenced:
            # if target is silenced, inform player.
            player.enqueue(app.packets.target_silenced(target_name))

            if app.settings.DEBUG:
                log(f"{player} tried to message {target}, but they are silenced.")

            return

        # limit message length to 2k chars
        # perhaps this could be dangerous with !py..?
        if len(msg) > 2000:
            msg = f"{msg[:2000]}... (truncated)"
            player.enqueue(
                app.packets.notification(
                    "Your message was truncated\n(exceeded 2000 characters).",
                ),
            )

        if target.status.action == Action.Afk and target.away_msg is not None:
            usecases.players.send(player, target.away_msg, sender=target)

        if target is app.state.sessions.bot:
            # TODO: remove special case for internal bot
            # have it authenticate and hold a session like a regular user,
            # extract functional into another (external) microservice
            await handle_bot_message(player, target, msg)
        else:
            # target is not bot, send the message normally if online
            if target.online:
                usecases.players.send(target, msg, sender=player)
            else:
                # inform user they're offline, but
                # will receive the mail @ next login.
                player.enqueue(
                    app.packets.notification(
                        f"{target.name} is currently offline, but will "
                        "receive your messsage on their next login.",
                    ),
                )

            # insert mail into db, marked as unread.
            await usecases.mail.send(
                source_id=player.id,
                target_id=target.id,
                msg=msg,
            )

        asyncio.create_task(usecases.players.update_latest_activity(player))
        log(f"{player} @ {target}: {msg}", Ansi.LCYAN, file=".data/logs/chat.log")


@register(ClientPackets.PART_LOBBY)
class LobbyPart(BasePacket):
    async def handle(self, player: Player) -> None:
        player.in_lobby = False


@register(ClientPackets.JOIN_LOBBY)
class LobbyJoin(BasePacket):
    async def handle(self, player: Player) -> None:
        player.in_lobby = True

        for match in app.state.sessions.matches:
            if match is not None:
                player.enqueue(app.packets.new_match(match))


@register(ClientPackets.CREATE_MATCH)
class MatchCreate(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.match = Match.from_parsed_match(reader.read_match())

    async def handle(self, player: Player) -> None:
        # TODO: match validation..?
        if player.restricted:
            player.enqueue(
                app.packets.match_join_fail()
                + app.packets.notification(
                    "Multiplayer is not available while restricted.",
                ),
            )
            return

        if player.silenced:
            player.enqueue(
                app.packets.match_join_fail()
                + app.packets.notification(
                    "Multiplayer is not available while silenced.",
                ),
            )
            return

        if not app.state.sessions.matches.append(self.match):
            # failed to create match (match slots full).
            usecases.players.send_bot(
                player,
                "Failed to create match (no slots available).",
            )
            player.enqueue(app.packets.match_join_fail())
            return

        # create a new channel for this multiplayer match
        channel = await repositories.channels.create(
            name=f"#multi_{self.match.id}",
            topic=f"MID {self.match.id}'s multiplayer channel.",
            read_priv=Privileges.UNRESTRICTED,
            write_priv=Privileges.UNRESTRICTED,
            auto_join=False,
            instance=True,
        )
        if channel is None:
            player.enqueue(
                app.packets.match_join_fail()
                + app.packets.notification(
                    "Failed to create #multiplayer channel.",
                ),
            )
            return

        # attach the new channel to our multiplayer match
        self.match.chat = channel

        await usecases.players.join_match(player, self.match, self.match.passwd)
        await usecases.multiplayer.send_match_state_to_clients(self.match)
        asyncio.create_task(usecases.players.update_latest_activity(player))

        usecases.channels.send_bot(self.match.chat, f"Match created by {player.name}.")
        log(f"{player} created a new multiplayer match.")


@register(ClientPackets.JOIN_MATCH)
class MatchJoin(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.match_id = reader.read_i32()
        self.match_passwd = reader.read_string()

    async def handle(self, player: Player) -> None:
        if self.match_id >= 64:
            # XXX: this is an implementation for in-game menus.
            #      it is not related to regular endpoint behaviour

            # we sent the menu id as the map set id, so we can send
            # menu options through beatmapset urls in the osu! chat
            await usecases.players.execute_menu_option(player, self.match_id)
            player.enqueue(app.packets.match_join_fail())
            return

        if self.match_id < 0:
            player.enqueue(app.packets.match_join_fail())
            return

        if not (match := app.state.sessions.matches[self.match_id]):
            log(f"{player} tried to join a non-existant mp lobby?")
            player.enqueue(app.packets.match_join_fail())
            return

        if player.restricted:
            player.enqueue(
                app.packets.match_join_fail()
                + app.packets.notification(
                    "Multiplayer is not available while restricted.",
                ),
            )
            return

        if player.silenced:
            player.enqueue(
                app.packets.match_join_fail()
                + app.packets.notification(
                    "Multiplayer is not available while silenced.",
                ),
            )
            return

        asyncio.create_task(usecases.players.update_latest_activity(player))
        await usecases.players.join_match(player, match, self.match_passwd)
        await usecases.multiplayer.send_match_state_to_clients(match)


@register(ClientPackets.PART_MATCH)
class MatchPart(BasePacket):
    async def handle(self, player: Player) -> None:
        asyncio.create_task(usecases.players.update_latest_activity(player))
        await usecases.players.leave_match(player)


@register(ClientPackets.MATCH_CHANGE_SLOT)
class MatchChangeSlot(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.slot_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        # read new slot ID
        if not 0 <= self.slot_id < 16:
            return

        if match.slots[self.slot_id].status != SlotStatus.open:
            log(f"{player} tried to move into non-open slot.", Ansi.LYELLOW)
            return

        # swap with current slot.
        slot = match.get_slot(player)
        assert slot is not None

        match.slots[self.slot_id].copy_from(slot)
        slot.reset()

        # technically not needed for host?
        await usecases.multiplayer.send_match_state_to_clients(match)


@register(ClientPackets.MATCH_READY)
class MatchReady(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        slot = match.get_slot(player)
        assert slot is not None

        slot.status = SlotStatus.ready
        await usecases.multiplayer.send_match_state_to_clients(match, lobby=False)


@register(ClientPackets.MATCH_LOCK)
class MatchLock(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.slot_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        if player is not match.host:
            log(f"{player} attempted to lock match as non-host.", Ansi.LYELLOW)
            return

        # read new slot ID
        if not 0 <= self.slot_id < 16:
            return

        slot = match.slots[self.slot_id]

        if slot.status == SlotStatus.locked:
            slot.status = SlotStatus.open
        else:
            if slot.player is match.host:
                # don't allow the match host to kick
                # themselves by clicking their crown
                return

            if slot.player:
                # uggggggh i hate trusting the osu! client
                # man why is it designed like this
                # TODO: probably going to end up changing
                ...  # slot.reset()

            slot.status = SlotStatus.locked

        await usecases.multiplayer.send_match_state_to_clients(match)


@register(ClientPackets.MATCH_CHANGE_SETTINGS)
class MatchChangeSettings(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.new = Match.from_parsed_match(reader.read_match())

    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        if player is not match.host:
            log(f"{player} attempted to change settings as non-host.", Ansi.LYELLOW)
            return

        if self.new.freemods != match.freemods:
            # freemods status has been changed.
            match.freemods = self.new.freemods

            if self.new.freemods:
                # match mods -> active slot mods.
                for slot in match.slots:
                    if slot.status & SlotStatus.has_player:
                        # the slot takes any non-speed
                        # changing mods from the match.
                        slot.mods = match.mods & ~SPEED_CHANGING_MODS

                # keep only speed-changing mods.
                match.mods &= SPEED_CHANGING_MODS
            else:
                # host mods -> match mods.
                host = match.get_host_slot()  # should always exist
                assert host is not None

                # the match keeps any speed-changing mods,
                # and also takes any mods the host has enabled.
                match.mods &= SPEED_CHANGING_MODS
                match.mods |= host.mods

                for slot in match.slots:
                    if slot.status & SlotStatus.has_player:
                        slot.mods = Mods.NOMOD

        if self.new.map_id == -1:
            # map being changed, unready players.
            usecases.multiplayer.unready_players(match, expected=SlotStatus.ready)
            match.prev_map_id = match.map_id

            match.map_id = -1
            match.map_md5 = ""
            match.map_name = ""
        elif match.map_id == -1:
            if match.prev_map_id != self.new.map_id:
                # new map has been chosen, send to match chat.
                usecases.channels.send_bot(
                    match.chat,
                    f"Selected: {self.new.map_embed}.",
                )

            # use our serverside version if we have it, but
            # still allow for users to pick unknown maps.
            bmap = await repositories.beatmaps.fetch_by_md5(self.new.map_md5)

            if bmap:
                match.map_id = bmap.id
                match.map_md5 = bmap.md5
                match.map_name = bmap.full_name
                match.mode = bmap.mode
            else:
                match.map_id = self.new.map_id
                match.map_md5 = self.new.map_md5
                match.map_name = self.new.map_name
                match.mode = self.new.mode

        if match.team_type != self.new.team_type:
            # if theres currently a scrim going on, only allow
            # team type to change by using the !mp teams command.
            if match.is_scrimming:
                _team = ("head-to-head", "tag-coop", "team-vs", "tag-team-vs")[
                    self.new.team_type
                ]

                msg = (
                    "Changing team type while scrimming will reset "
                    "the overall score - to do so, please use the "
                    f"!mp teams {_team} command."
                )
                usecases.channels.send_bot(match.chat, msg)
            else:
                # find the new appropriate default team.
                # defaults are (ffa: neutral, teams: red).
                if self.new.team_type in (
                    MatchTeamTypes.head_to_head,
                    MatchTeamTypes.tag_coop,
                ):
                    new_t = MatchTeams.neutral
                else:
                    new_t = MatchTeams.red

                # change each active slots team to
                # fit the correspoding team type.
                for slot in match.slots:
                    if slot.status & SlotStatus.has_player:
                        slot.team = new_t

                # change the matches'.
                match.team_type = self.new.team_type

        if match.win_condition != self.new.win_condition:
            # win condition changing; if `use_pp_scoring`
            # is enabled, disable it. always use new cond.
            if match.use_pp_scoring:
                match.use_pp_scoring = False

            match.win_condition = self.new.win_condition

        match.name = self.new.name

        await usecases.multiplayer.send_match_state_to_clients(match)


@register(ClientPackets.MATCH_START)
class MatchStart(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        if player is not match.host:
            log(f"{player} attempted to start match as non-host.", Ansi.LYELLOW)
            return

        await usecases.multiplayer.start(match)


@register(ClientPackets.MATCH_SCORE_UPDATE)
class MatchScoreUpdate(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.play_data = reader.read_raw()  # TODO: probably not necessary

    async def handle(self, player: Player) -> None:
        # this runs very frequently in matches,
        # so it's written to run pretty quick.

        if player.match is None:
            return

        # if scorev2 is enabled, read an extra 8 bytes.
        buf = bytearray(b"0\x00\x00")
        buf += len(self.play_data).to_bytes(4, "little")
        buf += self.play_data

        slot_id = player.match.get_slot_id(player)
        assert slot_id is not None
        buf[11] = slot_id

        await usecases.multiplayer.send_data_to_clients(
            player.match,
            bytes(buf),
            lobby=False,
        )


async def update_matchpoints(match: Match, was_playing: Sequence[Slot]) -> None:
    """\
    Determine the winner from `scores`, increment & inform players.

    This automatically works with the match settings (such as
    win condition, teams, & co-op) to determine the appropriate
    winner, and will use any team names included in the match name,
    along with the match name (fmt: OWC2020: (Team1) vs. (Team2)).

    For the examples, we'll use accuracy as a win condition.

    Teams, match title: `OWC2015: (United States) vs. (China)`.
        United States takes the point! (293.32% vs 292.12%)
        Total Score: United States | 7 - 2 | China
        United States takes the match, finishing OWC2015 with a score of 7 - 2!

    FFA, the top <=3 players will be listed for the total score.
        Justice takes the point! (94.32% [Match avg. 91.22%])
        Total Score: Justice - 3 | cmyui - 2 | FrostiDrinks - 2
        Justice takes the match, finishing with a score of 4 - 2!
    """

    assert match.chat is not None
    scores, didnt_submit = await usecases.multiplayer.await_submissions(
        match,
        was_playing,
    )

    for p in didnt_submit:
        usecases.channels.send_bot(
            match.chat,
            f"{p} didn't submit a score (timeout: 10s).",
        )

    if scores:
        ffa = match.team_type in (
            MatchTeamTypes.head_to_head,
            MatchTeamTypes.tag_coop,
        )

        # all scores are equal, it was a tie.
        if len(scores) != 1 and len(set(scores.values())) == 1:
            match.winners.append(None)
            usecases.channels.send_bot(match.chat, "The point has ended in a tie!")
            return None

        # Find the winner & increment their matchpoints.
        winner: Union[Player, MatchTeams] = max(scores, key=lambda k: scores[k])
        match.winners.append(winner)
        match.match_points[winner] += 1

        msg: list[str] = []

        def add_suffix(score: Union[int, float]) -> Union[str, int, float]:
            if match.use_pp_scoring:
                return f"{score:.2f}pp"
            elif match.win_condition == MatchWinConditions.accuracy:
                return f"{score:.2f}%"
            elif match.win_condition == MatchWinConditions.combo:
                return f"{score}x"
            else:
                return str(score)

        if ffa:
            msg.append(
                f"{winner.name} takes the point! ({add_suffix(scores[winner])} "
                f"[Match avg. {add_suffix(int(sum(scores.values()) / len(scores)))}])",
            )

            wmp = match.match_points[winner]

            # check if match point #1 has enough points to win.
            if match.winning_pts and wmp == match.winning_pts:
                # we have a champion, announce & reset our match.
                match.is_scrimming = False
                usecases.multiplayer.reset_scrimmage_state(match)
                match.bans.clear()

                m = f"{winner.name} takes the match! Congratulations!"
            else:
                # no winner, just announce the match points so far.
                # for ffa, we'll only announce the top <=3 players.
                m_points = sorted(match.match_points.items(), key=lambda x: x[1])
                m = f"Total Score: {' | '.join([f'{k.name} - {v}' for k, v in m_points])}"

            msg.append(m)
            del m

        else:  # teams
            if r_match := regexes.TOURNEY_MATCHNAME.match(match.name):
                match_name = r_match["name"]
                team_names = {
                    MatchTeams.blue: r_match["T1"],
                    MatchTeams.red: r_match["T2"],
                }
            else:
                match_name = match.name
                team_names = {MatchTeams.blue: "Blue", MatchTeams.red: "Red"}

            # teams are binary, so we have a loser.
            loser = MatchTeams({1: 2, 2: 1}[winner])

            # from match name if available, else blue/red.
            wname = team_names[winner]
            lname = team_names[loser]

            # scores from the recent play
            # (according to win condition)
            ws = add_suffix(scores[winner])
            ls = add_suffix(scores[loser])

            # total win/loss score in the match.
            wmp = match.match_points[winner]
            lmp = match.match_points[loser]

            # announce the score for the most recent play.
            msg.append(f"{wname} takes the point! ({ws} vs. {ls})")

            # check if the winner has enough match points to win the match.
            if match.winning_pts and wmp == match.winning_pts:
                # we have a champion, announce & reset our match.
                match.is_scrimming = False
                usecases.multiplayer.reset_scrimmage_state(match)

                msg.append(
                    f"{wname} takes the match, finishing {match_name} "
                    f"with a score of {wmp} - {lmp}! Congratulations!",
                )
            else:
                # no winner, just announce the match points so far.
                msg.append(f"Total Score: {wname} | {wmp} - {lmp} | {lname}")

        if didnt_submit:
            usecases.channels.send_bot(
                match.chat,
                "If you'd like to perform a rematch, "
                "please use the `!mp rematch` command.",
            )

        for line in msg:
            usecases.channels.send_bot(match.chat, line)

    else:
        usecases.channels.send_bot(match.chat, "Scores could not be calculated.")


@register(ClientPackets.MATCH_COMPLETE)
class MatchComplete(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        slot = match.get_slot(player)
        assert slot is not None

        slot.status = SlotStatus.complete

        # check if there are any players that haven't finished.
        if any([s.status == SlotStatus.playing for s in match.slots]):
            return

        # find any players just sitting in the multi room
        # that have not been playing the map; they don't
        # need to know all the players have completed, only
        # the ones who are playing (just new match info).
        not_playing = [
            s.player.id
            for s in match.slots
            if s.status & SlotStatus.has_player and s.status != SlotStatus.complete
        ]

        was_playing = [
            s for s in match.slots if s.player and s.player.id not in not_playing
        ]

        match.in_progress = False

        usecases.multiplayer.unready_players(
            match,
            expected=SlotStatus.complete,
        )

        await usecases.multiplayer.send_data_to_clients(
            match,
            data=app.packets.match_complete(),
            lobby=False,
            immune=not_playing,
        )
        await usecases.multiplayer.send_match_state_to_clients(match)

        if match.is_scrimming:
            # determine winner, update match points & inform players.
            asyncio.create_task(update_matchpoints(match, was_playing))


@register(ClientPackets.MATCH_CHANGE_MODS)
class MatchChangeMods(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.mods = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        if match.freemods:
            if player is match.host:
                # allow host to set speed-changing mods.
                match.mods = Mods(self.mods & SPEED_CHANGING_MODS)

            # set slot mods
            slot = match.get_slot(player)
            assert slot is not None

            slot.mods = Mods(self.mods & ~SPEED_CHANGING_MODS)
        else:
            if player is not match.host:
                log(f"{player} attempted to change mods as non-host.", Ansi.LYELLOW)
                return

            # not freemods, set match mods.
            match.mods = Mods(self.mods)

        await usecases.multiplayer.send_match_state_to_clients(match)


def is_playing(slot: Slot) -> bool:
    return slot.status == SlotStatus.playing and not slot.loaded


@register(ClientPackets.MATCH_LOAD_COMPLETE)
class MatchLoadComplete(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        # our player has loaded in and is ready to play.
        slot = match.get_slot(player)
        assert slot is not None

        slot.loaded = True

        # check if all players are loaded,
        # if so, tell all players to begin.
        if not any(is_playing(slot) for slot in match.slots):
            await usecases.multiplayer.send_data_to_clients(
                match,
                data=app.packets.match_all_players_loaded(),
                lobby=False,
            )


@register(ClientPackets.MATCH_NO_BEATMAP)
class MatchNoBeatmap(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        slot = match.get_slot(player)
        assert slot is not None

        slot.status = SlotStatus.no_map
        await usecases.multiplayer.send_match_state_to_clients(match, lobby=False)


@register(ClientPackets.MATCH_NOT_READY)
class MatchNotReady(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        slot = match.get_slot(player)
        assert slot is not None

        slot.status = SlotStatus.not_ready
        await usecases.multiplayer.send_match_state_to_clients(match, lobby=False)


@register(ClientPackets.MATCH_FAILED)
class MatchFailed(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        # find the player's slot id, and enqueue that
        # they've failed to all other players in the match.
        slot_id = match.get_slot_id(player)
        assert slot_id is not None

        await usecases.multiplayer.send_data_to_clients(
            match,
            data=app.packets.match_player_failed(slot_id),
            lobby=False,
        )


@register(ClientPackets.MATCH_HAS_BEATMAP)
class MatchHasBeatmap(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        slot = match.get_slot(player)
        assert slot is not None

        slot.status = SlotStatus.not_ready
        await usecases.multiplayer.send_match_state_to_clients(match, lobby=False)


@register(ClientPackets.MATCH_SKIP_REQUEST)
class MatchSkipRequest(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        slot = match.get_slot(player)
        assert slot is not None

        slot.skipped = True
        await usecases.multiplayer.send_data_to_clients(
            match,
            app.packets.match_player_skipped(player.id),
        )

        for slot in match.slots:
            if slot.status == SlotStatus.playing and not slot.skipped:
                return

        # all users have skipped, enqueue a skip.
        await usecases.multiplayer.send_data_to_clients(
            match,
            app.packets.match_skip(),
            lobby=False,
        )


@register(ClientPackets.CHANNEL_JOIN, restricted=True)
class ChannelJoin(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.channel_name = reader.read_string()

    async def handle(self, player: Player) -> None:
        if self.channel_name in IGNORED_CHANNELS:
            return

        channel = await repositories.channels.fetch(self.channel_name)

        if channel is None:
            log(
                f"{player} tried to join non-existent {self.channel_name}",
                Ansi.LYELLOW,
            )
            return

        if not usecases.players.join_channel(player, channel):
            log(
                f"{player} failed to join {self.channel_name}.",
                Ansi.LYELLOW,
            )
            return


@register(ClientPackets.MATCH_TRANSFER_HOST)
class MatchTransferHost(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.slot_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        if player is not match.host:
            log(f"{player} attempted to transfer host as non-host.", Ansi.LYELLOW)
            return

        # read new slot ID
        if not 0 <= self.slot_id < 16:
            return

        target = match[self.slot_id].player
        if target is None:
            log(f"{player} tried to transfer host to an empty slot?")
            return

        match.host_id = target.id
        match.host.enqueue(app.packets.match_transfer_host())
        await usecases.multiplayer.send_match_state_to_clients(match)


@register(ClientPackets.TOURNAMENT_MATCH_INFO_REQUEST)
class TourneyMatchInfoRequest(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.match_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not 0 <= self.match_id < 64:
            return  # invalid match id

        if not player.priv & Privileges.DONATOR:
            return  # insufficient privs

        match = app.state.sessions.matches[self.match_id]
        if match is None:
            return  # match not found

        player.enqueue(app.packets.update_match(match, send_pw=False))


@register(ClientPackets.TOURNAMENT_JOIN_MATCH_CHANNEL)
class TourneyMatchJoinChannel(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.match_id = reader.read_i32()

    async def handle(self, p: Player) -> None:
        if not 0 <= self.match_id < 64:
            return  # invalid match id

        if not p.priv & Privileges.DONATOR:
            return  # insufficient privs

        if not (m := app.state.sessions.matches[self.match_id]):
            return  # match not found

        for s in m.slots:
            if s.player is not None:
                if p.id == s.player.id:
                    return  # playing in the match

        # attempt to join match chan
        if usecases.players.join_channel(p, m.chat):
            m.tourney_clients.add(p.id)


@register(ClientPackets.TOURNAMENT_LEAVE_MATCH_CHANNEL)
class TourneyMatchLeaveChannel(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.match_id = reader.read_i32()

    async def handle(self, p: Player) -> None:
        if not 0 <= self.match_id < 64:
            return  # invalid match id

        if not p.priv & Privileges.DONATOR:
            return  # insufficient privs

        if not (m := app.state.sessions.matches[self.match_id]):
            return  # match not found

        # attempt to join match chan
        usecases.players.leave_channel(p, m.chat)
        m.tourney_clients.remove(p.id)


@register(ClientPackets.FRIEND_ADD)
class FriendAdd(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.user_id = reader.read_i32()

    async def handle(self, player: Player) -> None:

        target = app.state.sessions.players.get(id=self.user_id)
        if target is None:
            log(f"{player} tried to add a user who is not online! ({self.user_id})")
            return

        if target is app.state.sessions.bot:
            return

        if target.id in player.blocks:
            player.blocks.remove(target.id)

        asyncio.create_task(usecases.players.update_latest_activity(player))
        await usecases.players.add_friend(player, target)


@register(ClientPackets.FRIEND_REMOVE)
class FriendRemove(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.user_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        target = app.state.sessions.players.get(id=self.user_id)
        if target is None:
            log(f"{player} tried to remove a user who is not online! ({self.user_id})")
            return

        if target is app.state.sessions.bot:
            return

        asyncio.create_task(usecases.players.update_latest_activity(player))
        await usecases.players.remove_friend(player, target)


@register(ClientPackets.MATCH_CHANGE_TEAM)
class MatchChangeTeam(BasePacket):
    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        # toggle team
        slot = match.get_slot(player)
        assert slot is not None

        if slot.team == MatchTeams.blue:
            slot.team = MatchTeams.red
        else:
            slot.team = MatchTeams.blue

        await usecases.multiplayer.send_match_state_to_clients(match, lobby=False)


@register(ClientPackets.CHANNEL_PART, restricted=True)
class ChannelPart(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.name = reader.read_string()

    async def handle(self, player: Player) -> None:
        if self.name in IGNORED_CHANNELS:
            return

        channel = await repositories.channels.fetch(self.name)

        if channel is None:
            log(f"{player} tried to leave non-existent {self.name}.", Ansi.LYELLOW)
            return

        if player not in channel:
            log(f"{player} tried to leave {self.name} before joining.", Ansi.LYELLOW)
            return

        # leave the chan server-side.
        usecases.players.leave_channel(player, channel)


@register(ClientPackets.RECEIVE_UPDATES, restricted=True)
class ReceiveUpdates(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.value = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not 0 <= self.value < 3:
            log(f"{player} tried to set his presence filter to {self.value}?")
            return

        player.pres_filter = PresenceFilter(self.value)


@register(ClientPackets.SET_AWAY_MESSAGE)
class SetAwayMessage(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.msg = reader.read_message()

    async def handle(self, player: Player) -> None:
        player.away_msg = self.msg.text


@register(ClientPackets.USER_STATS_REQUEST, restricted=True)
class StatsRequest(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.user_ids = reader.read_i32_list_i16l()

    async def handle(self, player: Player) -> None:
        unrestrcted_ids = [p.id for p in app.state.sessions.players.unrestricted]
        is_online = lambda o: o in unrestrcted_ids and o != player.id

        for online in filter(is_online, self.user_ids):
            if target := app.state.sessions.players.get(id=online):
                if target is app.state.sessions.bot:
                    # optimization for bot since it's
                    # the most frequently requested user
                    packet = app.packets.bot_stats(target)
                else:
                    packet = app.packets.user_stats(target)

                player.enqueue(packet)


@register(ClientPackets.MATCH_INVITE)
class MatchInvite(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.user_id = reader.read_i32()

    async def handle(self, player: Player) -> None:
        if not player.match:
            return

        target = app.state.sessions.players.get(id=self.user_id)
        if target is None:
            log(f"{player} tried to invite a user who is not online! ({self.user_id})")
            return

        if target is app.state.sessions.bot:
            usecases.players.send_bot(player, "I'm too busy!")
            return

        target.enqueue(app.packets.match_invite(player, target.name))
        asyncio.create_task(usecases.players.update_latest_activity(player))

        log(f"{player} invited {target} to their match.")


@register(ClientPackets.MATCH_CHANGE_PASSWORD)
class MatchChangePassword(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.match = Match.from_parsed_match(reader.read_match())

    async def handle(self, player: Player) -> None:
        if not (match := player.match):
            return

        if player is not match.host:
            log(f"{player} attempted to change pw as non-host.", Ansi.LYELLOW)
            return

        match.passwd = self.match.passwd
        await usecases.multiplayer.send_match_state_to_clients(match)


@register(ClientPackets.USER_PRESENCE_REQUEST)
class UserPresenceRequest(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.user_ids = reader.read_i32_list_i16l()

    async def handle(self, player: Player) -> None:
        for pid in self.user_ids:
            if t := app.state.sessions.players.get(id=pid):
                if t is app.state.sessions.bot:
                    # optimization for bot since it's
                    # the most frequently requested user
                    packet = app.packets.bot_presence(t)
                else:
                    packet = app.packets.user_presence(t)

                player.enqueue(packet)


@register(ClientPackets.USER_PRESENCE_REQUEST_ALL)
class UserPresenceRequestAll(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        # TODO: should probably ratelimit with this (300k s)
        self.ingame_time = reader.read_i32()

    async def handle(self, player: Player) -> None:
        # NOTE: this packet is only used when there
        # are >256 players visible to the client.

        buffer = bytearray()

        for player in app.state.sessions.players.unrestricted:
            buffer += app.packets.user_presence(player)

        player.enqueue(bytes(buffer))


@register(ClientPackets.TOGGLE_BLOCK_NON_FRIEND_DMS)
class ToggleBlockingDMs(BasePacket):
    def __init__(self, reader: BanchoPacketReader) -> None:
        self.value = reader.read_i32()

    async def handle(self, player: Player) -> None:
        player.pm_private = self.value == 1

        asyncio.create_task(usecases.players.update_latest_activity(player))
