import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Iterable, Optional

import websockets
from websockets.exceptions import ConnectionClosed


GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
GUILD_INTENT = 1 << 0
GUILD_MEMBERS_INTENT = 1 << 1
PRESENCE_INTENT = 1 << 8
CONFIG_PATH = Path(__file__).with_name("config.json")


class GatewayError(RuntimeError):
    """Raised when Discord rejects or interrupts the Gateway session."""


def _load_user_token(config_path: Path = CONFIG_PATH) -> str:
    with config_path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    token = config.get("user_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(f"Missing non-empty 'user_token' in {config_path}")
    return token


def _presence_candidates(event: object, data: object) -> Iterable[dict[str, Any]]:
    """Yield presences from live and initial Gateway events."""
    if not isinstance(data, dict):
        return

    if event == "PRESENCE_UPDATE":
        yield data
        return

    if event in {"GUILD_CREATE", "GUILD_MEMBERS_CHUNK", "READY"}:
        presences = data.get("presences", [])
    elif event == "READY_SUPPLEMENTAL":
        # User Gateway sessions group initial presences by relationship/guild.
        presences = data.get("merged_presences", {})
    else:
        return

    def walk(value: object) -> Iterable[dict[str, Any]]:
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            if isinstance(value.get("user"), dict):
                yield value
            else:
                for item in value.values():
                    yield from walk(item)

    yield from walk(presences)


async def _request_guild_presence(
    websocket: Any,
    guild_id: str,
    user_id: str,
) -> None:
    """Ask a guild for one member and include that member's presence."""
    await websocket.send(json.dumps({
        "op": 8,
        "d": {
            "guild_id": guild_id,
            "user_ids": [user_id],
            "limit": 0,
            "presences": True,
            "nonce": f"presence:{user_id}",
        },
    }))


def _offline_presence_from_chunk(
    data: object,
    user_id: str,
) -> Optional[dict[str, Any]]:
    """Build an offline presence when a requested member has no presence entry."""
    if not isinstance(data, dict) or data.get("nonce") != f"presence:{user_id}":
        return None
    if data.get("presences"):
        return None

    members = data.get("members", [])
    if not isinstance(members, list):
        return None

    for member in members:
        if not isinstance(member, dict):
            continue
        user = member.get("user")
        if isinstance(user, dict) and user.get("id") == user_id:
            return {
                "user": user,
                "guild_id": data.get("guild_id"),
                "status": "offline",
                "activities": [],
                "client_status": {},
            }
    return None


async def _receive_packet(websocket: Any, deadline: float) -> dict[str, Any]:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError

    raw_packet = await asyncio.wait_for(websocket.recv(), remaining)
    packet = json.loads(raw_packet)
    if not isinstance(packet, dict):
        raise GatewayError("Discord sent a malformed Gateway packet")
    return packet


def _has_web_device(presence: dict[str, Any]) -> bool:
    """Return True only when web is the user's sole active Discord client."""
    client_status = presence.get("client_status")
    if not isinstance(client_status, dict):
        return False

    active_statuses = {"online", "idle", "dnd"}
    active_clients = {
        platform
        for platform, status in client_status.items()
        if status in active_statuses
    }
    return active_clients == {"web"}


async def get_presence(
    user_id: str,
    *,
    timeout: float = 30.0,
    config_path: Path = CONFIG_PATH,
) -> bool:
    """Return whether Discord reports an active web client for the user."""
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    token = _load_user_token(config_path)
    heartbeat_task: Optional[asyncio.Task[None]] = None
    last_sequence: Optional[int] = None
    requested_guilds: set[str] = set()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        async with websockets.connect(
            GATEWAY_URL,
            max_size=None,
            open_timeout=timeout,
        ) as websocket:
            hello = await _receive_packet(websocket, deadline)
            if hello.get("op") != 10:
                raise GatewayError("Discord did not start the Gateway handshake")

            try:
                heartbeat_interval = float(hello["d"]["heartbeat_interval"]) / 1000
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayError("Discord sent an invalid heartbeat interval") from exc

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(heartbeat_interval)
                    await websocket.send(json.dumps({"op": 1, "d": last_sequence}))

            heartbeat_task = asyncio.create_task(heartbeat())
            await websocket.send(json.dumps({
                "op": 2,
                "d": {
                    "token": token,
                    "intents": GUILD_INTENT | GUILD_MEMBERS_INTENT | PRESENCE_INTENT,
                    "properties": {
                        "$os": "windows",
                        "$browser": "presence-checker",
                        "$device": "presence-checker",
                    },
                },
            }))

            while True:
                packet = await _receive_packet(websocket, deadline)

                sequence = packet.get("s")
                if isinstance(sequence, int):
                    last_sequence = sequence

                opcode = packet.get("op")
                if opcode == 1:
                    await websocket.send(json.dumps({"op": 1, "d": last_sequence}))
                elif opcode == 7:
                    raise GatewayError("Discord requested a Gateway reconnect")
                elif opcode == 9:
                    raise GatewayError("Discord invalidated the Gateway session")

                for presence in _presence_candidates(packet.get("t"), packet.get("d")):
                    if presence.get("user", {}).get("id") == user_id:
                        return _has_web_device(presence)

                event = packet.get("t")
                data = packet.get("d")
                if event == "READY" and isinstance(data, dict):
                    guilds = data.get("guilds", [])
                    if isinstance(guilds, list):
                        for guild in guilds:
                            if not isinstance(guild, dict) or guild.get("unavailable"):
                                continue
                            guild_id = guild.get("id")
                            if isinstance(guild_id, str) and guild_id not in requested_guilds:
                                await _request_guild_presence(websocket, guild_id, user_id)
                                requested_guilds.add(guild_id)
                elif event == "GUILD_CREATE" and isinstance(data, dict):
                    guild_id = data.get("id")
                    if isinstance(guild_id, str) and guild_id not in requested_guilds:
                        await _request_guild_presence(websocket, guild_id, user_id)
                        requested_guilds.add(guild_id)
                elif event == "GUILD_MEMBERS_CHUNK":
                    offline_presence = _offline_presence_from_chunk(data, user_id)
                    if offline_presence is not None:
                        return False
    except TimeoutError:
        return False
    except ConnectionClosed as exc:
        if exc.code == 4004:
            raise GatewayError("Discord rejected the token in config.json") from exc
        if exc.code in {4013, 4014}:
            raise GatewayError(
                "Discord rejected the required member/presence intents; "
                "enable them for the bot in the Developer Portal"
            ) from exc
        raise GatewayError(
            f"Discord closed the Gateway connection (code {exc.code})"
        ) from exc
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _run(user_id: str, timeout: float) -> None:
    has_web_device = await get_presence(user_id, timeout=timeout)
    print(json.dumps(has_web_device))


def main() -> None:
    parser = argparse.ArgumentParser(description="Look up a Discord user's Gateway presence.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait (default: 30)")
    args = parser.parse_args()

    try:
        user_id = input("Discord user ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "\nerror: no user ID provided\n")

    if not user_id.isdigit():
        parser.error("user ID must be numeric")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    try:
        asyncio.run(_run(user_id, args.timeout))
    except (GatewayError, OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
