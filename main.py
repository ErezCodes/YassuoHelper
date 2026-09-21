import json
import random
import re
import logging
import calendar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

from web_intent_detector import get_presence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("yassuo_helper")

# Keep privileged intents disabled until Discord approves them.
# Set these to True only after enabling the matching switches in the Developer Portal.
ENABLE_MEMBERS_INTENT = False
ENABLE_PRESENCES_INTENT = False

# Load role mapping once at startup
ROLES_PATH = Path("roles.json")
with ROLES_PATH.open("r", encoding="utf-8") as fp:
    raw_role_maps: dict[str, dict[str, int]] = json.load(fp)
    ROLE_MAPS_BY_GUILD: dict[int, dict[str, int]] = {
        int(guild_id): role_map
        for guild_id, role_map in raw_role_maps.items()
    }

SUMMARY_DM_RECIPIENTS_PATH = Path("summary_dm_recipients.json")
with SUMMARY_DM_RECIPIENTS_PATH.open("r", encoding="utf-8") as fp:
    raw_summary_dm_recipients: dict[str, list[int]] = json.load(fp)
    SUMMARY_DM_RECIPIENTS_BY_GUILD: dict[int, tuple[int, ...]] = {
        int(guild_id): tuple(recipient_ids)
        for guild_id, recipient_ids in raw_summary_dm_recipients.items()
    }

RULES_PATH = Path("rules.json")
RANDOM_PICKS_LOG_PATH = Path(__file__).with_name("random_picks.log")
CST = timezone(timedelta(hours=-6), name="CST")
DEFAULT_RULES: dict[str, int | bool] = {
    "minimum_account_age_months": 6,
    "allow_web_browsers": False,
    "session_repetition_cooldown_seconds": 4 * 60 * 60,
}


def load_rules() -> dict[int, dict[str, int | bool]]:
    """Load per-server rule overrides, falling back safely to defaults."""
    if not RULES_PATH.exists():
        return {}
    try:
        with RULES_PATH.open("r", encoding="utf-8") as fp:
            raw_rules = json.load(fp)
        if not isinstance(raw_rules, dict):
            raise ValueError("rules.json must contain a JSON object")

        loaded: dict[int, dict[str, int | bool]] = {}
        for guild_id, values in raw_rules.items():
            if not isinstance(values, dict):
                continue
            merged = DEFAULT_RULES.copy()
            for key, default_value in DEFAULT_RULES.items():
                value = values.get(key, default_value)
                if isinstance(default_value, bool):
                    if isinstance(value, bool):
                        merged[key] = value
                elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    merged[key] = value
            loaded[int(guild_id)] = merged
        return loaded
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Could not load rules.json; using default rules")
        return {}


RULES_BY_GUILD = load_rules()

# Track users pulled randomly via /pull until next /disconnect_all
PULLED_HISTORY: dict[int, list[str]] = {}  # guild_id -> pulled user mentions
GIVEAWAY_MESSAGES: dict[int, tuple[int, int]] = {}  # guild_id -> (channel_id, message_id)
GIVEAWAY_PARTICIPANTS: dict[int, set[int]] = {}  # guild_id -> {user_ids}
RANDOM_PICK_TIMES: dict[int, dict[int, datetime]] = {}  # guild_id -> {user_id: last_pick_utc}
# Precompute slash-command choices: "None" first, then roles.json order
ROLE_CHOICES = [app_commands.Choice(name="None", value="None")]
role_names = dict.fromkeys(
    role_name
    for role_map in ROLE_MAPS_BY_GUILD.values()
    for role_name in role_map
)
ROLE_CHOICES.extend(app_commands.Choice(name=name, value=name) for name in role_names)
RULE_CHOICES = [
    app_commands.Choice(name="Account age", value="account_age"),
    app_commands.Choice(name="Allow web browsers", value="allow_web_browsers"),
    app_commands.Choice(name="Session repetition cooldown", value="session_cooldown"),
]

MESSAGE_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:(?:canary|ptb)\.)?discord(?:app)?\.com/"
    r"channels/(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)/?",
    re.IGNORECASE,
)

# Bot setup
intents = discord.Intents.default()
intents.members = ENABLE_MEMBERS_INTENT
intents.voice_states = True
intents.guilds = True
intents.presences = ENABLE_PRESENCES_INTENT

bot = commands.Bot(command_prefix="!", intents=intents)


def member_has_role(member: discord.Member, role_name: str) -> bool:
    """Return True if member has the configured role for their server."""
    role_id = ROLE_MAPS_BY_GUILD.get(member.guild.id, {}).get(role_name)
    if not role_id:
        return False
    return any(role.id == role_id for role in member.roles)


def is_privileged(member: discord.Member, guild: discord.Guild) -> bool:
    """Server owner or has Admin role."""
    return guild.owner_id == member.id or member_has_role(member, "Admin")


def can_manage_rules(member: discord.Member, guild: discord.Guild) -> bool:
    """Server owner, Admin, or Moderator."""
    return is_privileged(member, guild) or member_has_role(member, "Moderator")


def get_rules(guild_id: int) -> dict[str, int | bool]:
    return RULES_BY_GUILD.setdefault(guild_id, DEFAULT_RULES.copy())


def save_rules():
    """Persist all server rules using an atomic file replacement."""
    serialized = {
        str(guild_id): values
        for guild_id, values in sorted(RULES_BY_GUILD.items())
    }
    temporary_path = RULES_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as fp:
        json.dump(serialized, fp, indent=4)
        fp.write("\n")
    temporary_path.replace(RULES_PATH)


def format_duration(seconds: int) -> str:
    if seconds == 0:
        return "disabled"
    units = (
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
        (1, "second"),
    )
    for unit_seconds, label in units:
        if seconds % unit_seconds == 0:
            amount = seconds // unit_seconds
            return f"{amount} {label}{'' if amount == 1 else 's'}"
    return f"{seconds} seconds"


def format_rules(guild_id: int) -> str:
    rules = get_rules(guild_id)
    age = int(rules["minimum_account_age_months"])
    browsers = "true" if rules["allow_web_browsers"] else "false"
    cooldown = format_duration(int(rules["session_repetition_cooldown_seconds"]))
    return (
        "**Current rules**\n"
        f"- Discord account has to be at least **{age} month{'s' if age != 1 else ''}** old\n"
        f"- Allow people in web browsers? **{browsers}**\n"
        f"- Session repetition cooldown: **{cooldown}**\n\n"
        "`/pull_specific` bypasses all of these rules."
    )


def parse_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    raise ValueError("Use `true` or `false` for this rule.")


def parse_months(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(?:months?|mo)?\s*", value, re.IGNORECASE)
    if match is None:
        raise ValueError("Enter a whole number of months, such as `6 months`.")
    months = int(match.group(1))
    if months > 120:
        raise ValueError("Account age cannot be more than 120 months.")
    return months


def parse_duration(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"off", "disabled", "none", "0"}:
        return 0
    match = re.fullmatch(
        r"\s*(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\s*",
        value,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError("Enter a duration such as `4 hours`, `30 minutes`, or `off`.")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    multiplier = 1
    if unit.startswith("m"):
        multiplier = 60
    elif unit.startswith("h"):
        multiplier = 3600
    elif unit.startswith("d"):
        multiplier = 86400
    seconds = amount * multiplier
    if seconds > 30 * 86400:
        raise ValueError("The cooldown cannot be longer than 30 days.")
    return seconds


def get_participant_set(guild_id: int) -> set[int]:
    return GIVEAWAY_PARTICIPANTS.setdefault(guild_id, set())


def has_active_giveaway(guild_id: int) -> bool:
    """Return whether at least one person has been pulled for the giveaway."""
    return bool(
        GIVEAWAY_PARTICIPANTS.get(guild_id)
        or PULLED_HISTORY.get(guild_id)
    )


async def member_uses_browser_client(member: discord.Member) -> bool:
    """Ask the dedicated Gateway detector whether this member uses Discord web."""
    try:
        return await get_presence(str(member.id))
    except Exception:
        logger.exception("Web-client lookup failed for member %s", member.id)
        return False


def parse_ending_balance(value: str) -> tuple[Optional[float], str]:
    """Parse ending balance and optional currency prefix (supports C$...)."""
    raw_value = (value or "").strip()
    if not raw_value:
        return None, "$"

    currency_prefix = "$"
    lowered = raw_value.lower()
    if lowered.startswith("c$"):
        currency_prefix = "C$"
        raw_value = raw_value[2:].strip()
    elif raw_value.startswith("$"):
        raw_value = raw_value[1:].strip()

    normalized = raw_value.replace(",", "")
    try:
        return float(normalized), currency_prefix
    except ValueError:
        return None, currency_prefix


def parse_message_reference(
    value: str,
    guild_id: int,
    current_channel_id: int,
) -> tuple[int, int]:
    """Return (channel_id, message_id) for a same-server message link or ID."""
    reference = (value or "").strip().strip("<>")
    if reference.isdigit():
        return current_channel_id, int(reference)

    match = MESSAGE_LINK_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError("Please provide a valid Discord message link or message ID.")
    if int(match.group("guild_id")) != guild_id:
        raise ValueError("That message link is from a different server.")
    return int(match.group("channel_id")), int(match.group("message_id"))


def split_discord_lines(lines: list[str], limit: int = 2000) -> list[str]:
    """Split newline-delimited output without exceeding Discord's message limit."""
    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0

    for line in lines:
        added_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + added_length > limit:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0
            added_length = len(line)
        current_lines.append(line)
        current_length += added_length

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def get_random_pick_time_map(guild_id: int) -> dict[int, datetime]:
    return RANDOM_PICK_TIMES.setdefault(guild_id, {})


def is_random_pick_eligible(guild_id: int, user_id: int, now: Optional[datetime] = None) -> bool:
    pick_times = get_random_pick_time_map(guild_id)
    picked_at = pick_times.get(user_id)
    if picked_at is None:
        return True

    current = now or datetime.now(timezone.utc)
    cooldown_seconds = int(get_rules(guild_id)["session_repetition_cooldown_seconds"])
    if picked_at + timedelta(seconds=cooldown_seconds) <= current:
        pick_times.pop(user_id, None)
        if not pick_times:
            RANDOM_PICK_TIMES.pop(guild_id, None)
        return True
    return False


def mark_randomly_picked(guild_id: int, user_ids: list[int]):
    if not user_ids:
        return
    pick_times = get_random_pick_time_map(guild_id)
    picked_at = datetime.now(timezone.utc)
    for user_id in user_ids:
        pick_times[user_id] = picked_at


def add_calendar_months(value: datetime, months: int) -> datetime:
    """Advance a datetime by whole calendar months, clamping its day if needed."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def account_old_enough(member: discord.Member, guild_id: int) -> bool:
    """Apply this server's minimum Discord-account age in calendar months."""
    if not member.created_at:
        logger.debug("Missing created_at for member %s (%s)", member, member.id)
        return False
    minimum_months = int(get_rules(guild_id)["minimum_account_age_months"])
    age_ok = add_calendar_months(member.created_at, minimum_months) <= datetime.now(timezone.utc)
    logger.debug(
        "Account age check for %s (%s): created_at=%s months=%s age_ok=%s",
        member,
        member.id,
        member.created_at,
        minimum_months,
        age_ok,
    )
    return age_ok


async def browser_allowed(guild_id: int, member: discord.Member) -> bool:
    uses_browser = await member_uses_browser_client(member)
    return bool(get_rules(guild_id)["allow_web_browsers"]) or not uses_browser


def _single_line(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def append_random_pick_log(
    guild: discord.Guild,
    context: str,
    voice_occupants: list[str],
    numbered_candidates: list[tuple[int, discord.Member]],
    draw_results: list[str],
):
    """Append one human-readable random-draw audit block."""
    lines = [
        "=" * 72,
        f"Time: {datetime.now(CST):%Y-%m-%d %H:%M:%S %Z}",
        f"Server: {_single_line(guild.name)} ({guild.id})",
        f"Draw: {_single_line(context)}",
        "Voice-channel occupants:",
        *(voice_occupants or ["  None"]),
        "Numbered eligible pool:",
    ]
    if numbered_candidates:
        lines.extend(
            f"  {number}. {_single_line(member.display_name)} ({member.id})"
            for number, member in numbered_candidates
        )
    else:
        lines.append("  None")
    lines.append("Draw results:")
    lines.extend(draw_results or ["  No number was drawn."])

    try:
        with RANDOM_PICKS_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write("\n".join(lines) + "\n")
    except OSError:
        logger.exception("Could not append to %s", RANDOM_PICKS_LOG_PATH)


async def draw_members(
    guild: discord.Guild,
    candidates: list[discord.Member],
    amount: int,
    context: str,
) -> tuple[list[discord.Member], int]:
    """Draw members randomly, replacing anyone rejected for using Discord web."""
    voice_occupants: list[str] = []
    for channel in guild.voice_channels:
        if not channel.members:
            continue
        voice_occupants.append(f"  [{_single_line(channel.name)}] ({channel.id})")
        voice_occupants.extend(
            f"    - {_single_line(member.display_name)} ({member.id})"
            for member in channel.members
        )

    numbered_candidates = list(enumerate(candidates, start=1))
    remaining_candidates = numbered_candidates.copy()
    selected: list[discord.Member] = []
    browser_blocked = 0
    draw_results: list[str] = []

    while remaining_candidates and len(selected) < amount:
        position = random.randrange(len(remaining_candidates))
        drawn_number, member = remaining_candidates.pop(position)
        if not await browser_allowed(guild.id, member):
            browser_blocked += 1
            draw_results.append(
                f"  Number {drawn_number} -> {_single_line(member.display_name)} "
                f"({member.id}) - SKIPPED (web-only client)"
            )
            logger.info(
                "Skipping browser user drawn for giveaway | guild=%s member=%s",
                guild.id,
                member.id,
            )
            continue
        selected.append(member)
        draw_results.append(
            f"  Number {drawn_number} -> {_single_line(member.display_name)} "
            f"({member.id}) - PICKED"
        )

    append_random_pick_log(
        guild,
        context,
        voice_occupants,
        numbered_candidates,
        draw_results,
    )

    return selected, browser_blocked


async def find_existing_giveaway_message(guild: discord.Guild) -> Optional[discord.Message]:
    """Locate an existing giveaway message sent by this bot that has buttons/components."""
    # First try cached ids
    cached = GIVEAWAY_MESSAGES.get(guild.id)
    if cached:
        channel = guild.get_channel(cached[0])
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(cached[1])
                if msg.author.id == guild.me.id and msg.components:
                    logger.info(
                        "Using cached giveaway message | guild=%s channel=%s message=%s",
                        guild.id,
                        channel.id,
                        msg.id,
                    )
                    return msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # fall through to search

    # Search recent history of each text channel (limited to keep it light)
    for channel in guild.text_channels:
        try:
            async for message in channel.history(limit=50):
                if message.author.id == guild.me.id and message.components:
                    GIVEAWAY_MESSAGES[guild.id] = (channel.id, message.id)
                    logger.info(
                        "Cached giveaway message | guild=%s channel=%s message=%s",
                        guild.id,
                        channel.id,
                        message.id,
                    )
                    return message
        except (discord.Forbidden, discord.HTTPException):
            continue
    return None


async def update_giveaway_message(guild: discord.Guild):
    """Refresh the main giveaway message to show current participants."""
    message = await find_existing_giveaway_message(guild)
    if message is None:
        logger.debug("No giveaway message to update for guild %s", guild.id)
        return  # no message to update

    participants = get_participant_set(guild.id)
    logger.info("Updating giveaway message | guild=%s participants=%d", guild.id, len(participants))
    if participants:
        # Resolve names for nicer display
        display_lines = []
        for user_id in participants:
            member = guild.get_member(user_id)
            if member:
                display_lines.append(f"- {member.mention} [{member.name}] ({member.id})")
            else:
                display_lines.append(f"- <@{user_id}> ({user_id})")
        participant_text = "\n".join(display_lines)
    else:
        participant_text = "None yet."

    content = (
        "```ini\n"
        "[ Start a voice chat giveaway! ]\n"
        "```\n"
        "📝 **Current giveaway participants:**\n"
        f"{participant_text}"
    )
    try:
        await message.edit(content=content, view=GiveawayView())
    except (discord.Forbidden, discord.HTTPException):
        pass


def role_id_by_name(guild_id: int, name: str) -> Optional[int]:
    return ROLE_MAPS_BY_GUILD.get(guild_id, {}).get(name)


def member_in_roles(member: discord.Member, role_names: set[str]) -> bool:
    return any(member_has_role(member, role) for role in role_names)


class PullPeopleModal(discord.ui.Modal, title="Pull people"):
    def __init__(self, executor: discord.Member, executor_channel: discord.VoiceChannel):
        super().__init__(timeout=300)
        self.executor = executor
        self.executor_channel = executor_channel
        self.guild = executor.guild

        self.moe_count = discord.ui.TextInput(
            label="How many Moe Loyals to pull?",
            placeholder="0",
            required=False,
            max_length=3,
        )
        self.niviour_count = discord.ui.TextInput(
            label="How many Niviour Supporters to pull?",
            placeholder="0",
            required=False,
            max_length=3,
        )
        self.code_count = discord.ui.TextInput(
            label="How many Code Yassuo to pull?",
            placeholder="0",
            required=False,
            max_length=3,
        )
        self.normal_count = discord.ui.TextInput(
            label="How many normal users to pull?",
            placeholder="0",
            required=False,
            max_length=3,
        )

        for field in (self.moe_count, self.niviour_count, self.code_count, self.normal_count):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        counts = {
            "Moe Loyals": self._parse_int(self.moe_count.value),
            "Niviour Supporter": self._parse_int(self.niviour_count.value),
            "Code Yassuo": self._parse_int(self.code_count.value),
            "Normal": self._parse_int(self.normal_count.value),
        }
        summary = await pull_people_with_counts(
            interaction=interaction,
            executor=self.executor,
            executor_channel=self.executor_channel,
            counts=counts,
        )
        await interaction.followup.send(
            summary,
            ephemeral=True,  # input-driven result; keep private to the submitter
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @staticmethod
    def _parse_int(value: str) -> int:
        value = (value or "").strip()
        if not value:
            return 0
        try:
            return max(0, int(value))
        except ValueError:
            return 0


class EndGiveawayModal(discord.ui.Modal, title="End giveaway"):
    def __init__(self, executor: discord.Member):
        super().__init__(timeout=180)
        self.executor = executor
        self.guild = executor.guild

        self.ending_balance = discord.ui.TextInput(
            label="What is the ending balance of this group?",
            placeholder="e.g., 123.45 or C$123.45",
            required=True,
            max_length=20,
        )
        self.add_item(self.ending_balance)

    async def on_submit(self, interaction: discord.Interaction):
        # Another admin may have ended the giveaway while this modal was open.
        if not has_active_giveaway(self.guild.id):
            await interaction.response.send_message(
                "There is no active giveaway to end. Pull at least one person first.",
                ephemeral=True,
            )
            return

        balance, currency_prefix = parse_ending_balance(self.ending_balance.value)
        if balance is None:
            await interaction.response.send_message(
                "Please enter a valid number for the ending balance (or use C$123.45).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(thinking=True)
        summary = await perform_disconnect_all(self.executor, balance, currency_prefix)

        # Public response in-channel.
        await interaction.followup.send(
            summary,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await reset_giveaway_state(self.guild)

        # DM the same summary to the configured recipients.
        target_ids = SUMMARY_DM_RECIPIENTS_BY_GUILD.get(self.guild.id, ())
        for uid in target_ids:
            try:
                user = await interaction.client.fetch_user(uid)
                await user.send(summary, allowed_mentions=discord.AllowedMentions(users=True))
            except Exception:
                # Swallow DM errors to avoid breaking flow.
                continue


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Pull people", style=discord.ButtonStyle.success, custom_id="giveaway_pull_people")
    async def pull_people(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.")
            return
        if not is_privileged(interaction.user, guild):
            await interaction.response.send_message("Only Admins or the server owner can use this.")
            return
        voice_state = interaction.user.voice
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message("Join a voice channel first.")
            return

        logger.info(
            "GiveawayView.pull_people invoked | user=%s guild=%s channel=%s",
            interaction.user.id,
            guild.id,
            voice_state.channel.id if voice_state.channel else None,
        )
        await interaction.response.send_modal(PullPeopleModal(interaction.user, voice_state.channel))

    @discord.ui.button(label="Pull a specific person", style=discord.ButtonStyle.success, custom_id="giveaway_pull_specific")
    async def pull_specific_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Use the `/pull_specific` command to select a user to pull.",
            ephemeral=True,
        )

    @discord.ui.button(label="End giveaway", style=discord.ButtonStyle.danger, custom_id="giveaway_end")
    async def end_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.")
            return
        if not is_privileged(interaction.user, guild):
            await interaction.response.send_message("Only Admins or the server owner can end the giveaway.")
            return
        if not has_active_giveaway(guild.id):
            await interaction.response.send_message(
                "There is no active giveaway to end. Pull at least one person first.",
                ephemeral=True,
            )
            return

        logger.info(
            "GiveawayView.end_giveaway invoked | user=%s guild=%s",
            interaction.user.id,
            guild.id,
        )
        await interaction.response.send_modal(EndGiveawayModal(interaction.user))


async def reset_giveaway_state(guild: discord.Guild):
    participants = get_participant_set(guild.id)
    participants.clear()
    PULLED_HISTORY.setdefault(guild.id, []).clear()
    await update_giveaway_message(guild)
    logger.info("reset_giveaway_state | guild=%s participants_cleared", guild.id)


async def perform_disconnect_all(executor: discord.Member, ending_balance: float, currency_prefix: str = "$") -> str:
    """Shared logic for ending giveaway/disconnecting, returns summary text."""
    guild = executor.guild
    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        return "You need to be connected to a voice channel to end the giveaway."
    channel = voice_state.channel
    logger.info(
        "perform_disconnect_all | executor=%s guild=%s channel=%s ending_balance=%s currency=%s",
        executor.id,
        guild.id,
        channel.id,
        ending_balance,
        currency_prefix,
    )

    to_disconnect: list[discord.Member] = []
    for member in channel.members:
        if member.id == executor.id:
            continue
        if member.id == guild.owner_id:
            continue
        if member_has_role(member, "Admin"):
            continue
        if member_has_role(member, "Moderator"):
            continue
        to_disconnect.append(member)

    disconnected = []
    failed = []
    for member in to_disconnect:
        try:
            await member.move_to(None)
            disconnected.append(member)
        except discord.Forbidden:
            failed.append((member, "Missing permissions"))
        except discord.HTTPException:
            failed.append((member, "Discord error"))

    lines: list[str] = []
    if disconnected:
        lines.append("Disconnected:")
        lines.extend(f"- {m.mention} [{m.name}] ({m.id})" for m in disconnected)
    if failed:
        lines.append("Failed:")
        lines.extend(f"- {m.mention} [{m.name}] ({m.id}) ({reason})" for m, reason in failed)
    if not lines:
        lines.append("No members to disconnect (all present are exempt).")

    participants = get_participant_set(guild.id)
    participant_ids = set(participants)
    if not participant_ids:
        # Fallback for cases where in-memory participant tracking was lost/restarted.
        participant_ids.update(m.id for m in disconnected)
        participant_ids.update(m.id for m, _ in failed)

    # Build formatted participant list for summary output
    participant_lines: list[str] = []
    for user_id in sorted(participant_ids):
        member = guild.get_member(user_id)
        if member:
            participant_lines.append(f"- {member.mention} [{member.name}] ({member.id})")
        else:
            participant_lines.append(f"- <@{user_id}> ({user_id})")

    participant_section = "\n".join(participant_lines) if participant_lines else "- None"
    participant_count = len(participant_ids)
    total_winnings = f"{currency_prefix}{ending_balance:.2f}"
    per_person = (
        f"{currency_prefix}{(ending_balance / participant_count):.2f}"
        if participant_count > 0
        else "N/A (no participants)"
    )

    lines = [
        "Group summary:",
        participant_section,
        f"Total winnings: {total_winnings}",
        f"Amount for each person when divided equally: {per_person}",
    ]
    logger.info(
        "perform_disconnect_all summary | guild=%s disconnected=%s failed=%s participants_in_summary=%d",
        guild.id,
        [m.id for m in disconnected],
        [(m.id, reason) for m, reason in failed],
        participant_count,
    )
    return "\n".join(lines)


async def pull_people_with_counts(
    interaction: discord.Interaction,
    executor: discord.Member,
    executor_channel: discord.VoiceChannel,
    counts: dict[str, int],
) -> str:
    guild = executor.guild
    logger.info(
        "pull_people_with_counts | executor=%s guild=%s channel=%s counts=%s",
        executor.id,
        guild.id,
        executor_channel.id,
        counts,
    )
    already_selected: set[int] = set()
    chosen_members: list[discord.Member] = []
    notes: list[str] = []

    def candidates_for_role(role_name: str) -> tuple[list[discord.Member], int]:
        pool: list[discord.Member] = []
        cooldown_blocked = 0
        now = datetime.now(timezone.utc)
        for channel in guild.voice_channels:
            if channel.id == executor_channel.id:
                continue
            for member in channel.members:
                if member.id in already_selected:
                    continue
                if not account_old_enough(member, guild.id):
                    continue
                if role_name != "Normal" and not member_has_role(member, role_name):
                    continue
                if not is_random_pick_eligible(guild.id, member.id, now):
                    cooldown_blocked += 1
                    continue
                pool.append(member)
        return pool, cooldown_blocked

    for role_name, requested in counts.items():
        if requested <= 0:
            continue
        pool, cooldown_blocked = candidates_for_role(role_name)
        if not pool:
            reasons: list[str] = []
            if cooldown_blocked:
                reasons.append(f"{cooldown_blocked} on random-pick cooldown")
            suffix = f" ({', '.join(reasons)})" if reasons else ""
            notes.append(f"No available {role_name} to pull{suffix}.")
            continue
        take = min(requested, len(pool))
        selected, browser_blocked = await draw_members(
            guild,
            pool,
            take,
            context=f"Giveaway modal - {role_name}",
        )
        if len(selected) < requested:
            reason = f"; skipped {browser_blocked} using browser" if browser_blocked else ""
            notes.append(
                f"Requested {requested} {role_name}, only found {len(selected)}{reason}."
            )
        chosen_members.extend(selected)
        already_selected.update(m.id for m in selected)

    moved: list[discord.Member] = []
    failed: list[tuple[discord.Member, str]] = []
    for member in chosen_members:
        try:
            await member.move_to(executor_channel)
            moved.append(member)
        except discord.Forbidden:
            failed.append((member, "Missing permissions"))
        except discord.HTTPException:
            failed.append((member, "Discord error"))

    if moved:
        mark_randomly_picked(guild.id, [m.id for m in moved])
        participants = get_participant_set(guild.id)
        participants.update(m.id for m in moved)
        await update_giveaway_message(guild)
    logger.info(
        "pull_people_with_counts outcome | moved=%s failed=%s notes=%s",
        [m.id for m in moved],
        [(m.id, reason) for m, reason in failed],
        notes,
    )

    parts: list[str] = []
    if moved:
        parts.append(f"Pulled {len(moved)} member(s): " + ", ".join(m.mention for m in moved))
    if failed:
        parts.append("Failed to move: " + ", ".join(f"{m.mention} ({reason})" for m, reason in failed))
    if notes:
        parts.extend(notes)
    if not parts:
        parts.append("No members were moved.")

    return "\n".join(parts)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s) | guilds=%d", bot.user, bot.user.id, len(bot.guilds))
    # Cache any existing giveaway messages with buttons so we can update them.
    for guild in bot.guilds:
        logger.info("Connected guild: %s (%s)", guild.name, guild.id)
        await find_existing_giveaway_message(guild)


@bot.event
async def setup_hook():
    # Re-register persistent view so old giveaway messages keep working after restarts.
    bot.add_view(GiveawayView())

    # Register commands only in the two configured servers. Keeping them out of
    # the global command set prevents Discord from displaying duplicate entries.
    for guild_id in ROLE_MAPS_BY_GUILD:
        guild_object = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild_object)
        synced_commands = await bot.tree.sync(guild=guild_object)
        logger.info(
            "Synced %d application commands to guild %s",
            len(synced_commands),
            guild_id,
        )

    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    logger.info("Cleared global application commands")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    logger.exception(
        "App command error | user=%s guild=%s command=%s",
        getattr(interaction.user, "id", None),
        getattr(interaction.guild, "id", None),
        getattr(interaction.command, "name", None),
        exc_info=error,
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong handling that command.", ephemeral=True)
        else:
            await interaction.response.send_message("Something went wrong handling that command.", ephemeral=True)
    except Exception:
        # Avoid raising from the error handler itself.
        pass


@bot.tree.command(name="display_message", description="Post the main giveaway control panel.")
@app_commands.guild_only()
async def display_message(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.")
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.")
        return

    if not is_privileged(executor, guild):
        await interaction.response.send_message("Only the server owner or Admins can use this command.")
        return
    logger.info(
        "display_message invoked | user=%s guild=%s",
        executor.id,
        guild.id,
    )

    participants = get_participant_set(guild.id)
    participants.clear()

    content = (
        "```ini\n"
        "[ Start a voice chat giveaway! ]\n"
        "```\n"
        "📝 **Current giveaway participants:**\n"
        "None yet."
    )
    await interaction.response.send_message(content, view=GiveawayView())
    message = await interaction.original_response()
    GIVEAWAY_MESSAGES[guild.id] = (message.channel.id, message.id)


@bot.tree.command(name="whoreacted", description="List users for the most-used reaction on a message.")
@app_commands.guild_only()
@app_commands.describe(message="A Discord message link, or a message ID from this channel.")
async def whoreacted(interaction: discord.Interaction, message: str):
    guild = interaction.guild
    if guild is None or interaction.channel_id is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.",
            ephemeral=True,
        )
        return

    try:
        channel_id, message_id = parse_message_reference(
            message,
            guild.id,
            interaction.channel_id,
        )
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            channel = await interaction.client.fetch_channel(channel_id)

        channel_guild = getattr(channel, "guild", None)
        fetch_message = getattr(channel, "fetch_message", None)
        if channel_guild is None or channel_guild.id != guild.id or fetch_message is None:
            await interaction.followup.send(
                "That message is not in an accessible server channel.",
                ephemeral=True,
            )
            return

        target_message = await fetch_message(message_id)
    except discord.NotFound:
        await interaction.followup.send(
            "I couldn't find that message. Check the link or ID and try again.",
            ephemeral=True,
        )
        return
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to view that channel or message history.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "Discord couldn't retrieve that message. Please try again.",
            ephemeral=True,
        )
        return

    if not target_message.reactions:
        await interaction.followup.send(
            "That message has no reactions.",
            ephemeral=True,
        )
        return

    top_reaction = max(target_message.reactions, key=lambda reaction: reaction.count)
    reactors: dict[int, discord.User | discord.Member] = {}

    try:
        normal_count = getattr(top_reaction, "normal_count", None)
        async for user in top_reaction.users(limit=normal_count):
            reactors[user.id] = user

        # discord.py 2.4+ exposes super-reaction users separately. Include them
        # as well, while keeping compatibility with the project's 2.3 minimum.
        burst_count = getattr(top_reaction, "burst_count", 0)
        reaction_type = getattr(discord, "ReactionType", None)
        if burst_count and reaction_type is not None:
            async for user in top_reaction.users(
                limit=burst_count,
                type=reaction_type.burst,
            ):
                reactors[user.id] = user
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to retrieve the users for that reaction.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "Discord couldn't retrieve the reaction users. Please try again.",
            ephemeral=True,
        )
        return

    if not reactors:
        await interaction.followup.send(
            "No users are currently listed for that reaction.",
            ephemeral=True,
        )
        return

    lines: list[str] = []
    for user in reactors.values():
        cached_member = guild.get_member(user.id)
        display_name = (cached_member or user).display_name.replace("\r", " ").replace("\n", " ")
        username = user.name.replace("\r", " ").replace("\n", " ")
        lines.append(f"{display_name} ({username})")

    logger.info(
        "whoreacted invoked | executor=%s guild=%s channel=%s message=%s emoji=%s users=%d",
        interaction.user.id,
        guild.id,
        channel_id,
        message_id,
        top_reaction.emoji,
        len(lines),
    )
    # Reserve eight characters for the opening/closing code fences so every
    # response stays within Discord's 2,000-character message limit.
    for chunk in split_discord_lines(lines, limit=1992):
        await interaction.followup.send(
            f"```\n{chunk}\n```",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


@bot.tree.command(name="rules", description="Display the current giveaway rules.")
@app_commands.guild_only()
async def rules(interaction: discord.Interaction):
    guild = interaction.guild
    executor = interaction.user
    if guild is None or not isinstance(executor, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return
    if not can_manage_rules(executor, guild):
        await interaction.response.send_message(
            "Only server owners, Admins, or Moderators can view the rules.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        format_rules(guild.id),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="rule", description="Change one of the giveaway rules.")
@app_commands.guild_only()
@app_commands.describe(
    rule="Rule to change.",
    parameter="New value, for example: 6 months, false, or 4 hours.",
)
@app_commands.choices(rule=RULE_CHOICES)
async def change_rule(
    interaction: discord.Interaction,
    rule: app_commands.Choice[str],
    parameter: str,
):
    guild = interaction.guild
    executor = interaction.user
    if guild is None or not isinstance(executor, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return
    if not can_manage_rules(executor, guild):
        await interaction.response.send_message(
            "Only server owners, Admins, or Moderators can change the rules.",
            ephemeral=True,
        )
        return

    guild_rules = get_rules(guild.id)
    try:
        if rule.value == "account_age":
            key = "minimum_account_age_months"
            new_value: int | bool = parse_months(parameter)
        elif rule.value == "allow_web_browsers":
            key = "allow_web_browsers"
            new_value = parse_boolean(parameter)
        elif rule.value == "session_cooldown":
            key = "session_repetition_cooldown_seconds"
            new_value = parse_duration(parameter)
        else:
            raise ValueError("Unknown rule selection.")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    old_value = guild_rules[key]
    guild_rules[key] = new_value
    try:
        save_rules()
    except OSError:
        guild_rules[key] = old_value
        logger.exception("Could not save rules for guild %s", guild.id)
        await interaction.response.send_message(
            "I couldn't save that rule. Please check the bot's file permissions and try again.",
            ephemeral=True,
        )
        return

    logger.info(
        "Rule changed | executor=%s guild=%s rule=%s old=%r new=%r",
        executor.id,
        guild.id,
        rule.value,
        old_value,
        new_value,
    )
    await interaction.response.send_message(
        "Rule updated.\n\n" + format_rules(guild.id),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="pull", description="Pull random member(s) with an optional required role into your voice channel.")
@app_commands.guild_only()
@app_commands.describe(
    required_role="Role name from roles.json or 'None' to ignore role requirements.",
    amount="Number of members to pull (minimum 1).",
)
@app_commands.choices(required_role=ROLE_CHOICES)
async def pull(
    interaction: discord.Interaction,
    required_role: app_commands.Choice[str],
    amount: app_commands.Range[int, 1],
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.")
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.")
        return

    # Permission check: server owner or Admin role
    is_owner = guild.owner_id == executor.id
    is_admin_role = member_has_role(executor, "Admin")
    if not (is_owner or is_admin_role):
        await interaction.response.send_message("Only the server owner or members with the Admin role may use this command.")
        return

    # Ensure executor is in a voice channel
    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /pull.")
        return

    if not interaction.response.is_done():
        await interaction.response.defer()

    executor_channel = voice_state.channel

    # Build candidate list
    role_filter = required_role.value
    candidates: list[discord.Member] = []
    cooldown_blocked = 0
    now = datetime.now(timezone.utc)
    for channel in guild.voice_channels:
        if channel.id == executor_channel.id:
            continue  # exclude members already with the executor
        for member in channel.members:
            if not account_old_enough(member, guild.id):
                continue
            if role_filter != "None" and not member_has_role(member, role_filter):
                continue
            if not is_random_pick_eligible(guild.id, member.id, now):
                cooldown_blocked += 1
                continue
            candidates.append(member)

    if not candidates:
        reasons: list[str] = []
        if cooldown_blocked:
            reasons.append(f"{cooldown_blocked} on random-pick cooldown")
        suffix = f" ({', '.join(reasons)})" if reasons else ""
        await interaction.followup.send(f"No eligible members found in other voice channels{suffix}.")
        return

    if amount > len(candidates):
        reasons: list[str] = []
        if cooldown_blocked:
            reasons.append(f"{cooldown_blocked} on random-pick cooldown")
        extra = f" ({', '.join(reasons)})." if reasons else "."
        await interaction.followup.send(
            f"Requested {amount} member(s) but only {len(candidates)} eligible{extra}",
        )
        return

    # Draw in random order, checking each drawn member with web_intent_detector.
    chosen_members, browser_blocked = await draw_members(
        guild,
        candidates,
        amount,
        context=f"/pull - role filter: {role_filter}",
    )
    logger.info(
        "pull invoked | executor=%s guild=%s channel=%s role_filter=%s candidates=%d cooldown_blocked=%d browser_blocked=%d amount=%s",
        executor.id,
        guild.id,
        executor_channel.id,
        role_filter,
        len(candidates),
        cooldown_blocked,
        browser_blocked,
        amount,
    )
    if len(chosen_members) < amount:
        await interaction.followup.send(
            f"Requested {amount} member(s) but only {len(chosen_members)} eligible "
            f"after skipping {browser_blocked} using Discord in a browser."
        )
        return

    # Move the member(s)
    moved: list[discord.Member] = []
    failed: list[tuple[discord.Member, str]] = []
    for member in chosen_members:
        try:
            await member.move_to(executor_channel)
            moved.append(member)
        except discord.Forbidden:
            failed.append((member, "Missing permissions"))
        except discord.HTTPException:
            failed.append((member, "Discord error"))
    logger.info(
        "pull outcome | executor=%s moved=%s failed=%s",
        executor.id,
        [m.id for m in moved],
        [(m.id, reason) for m, reason in failed],
    )

    messages: list[str] = []
    if moved:
        mark_randomly_picked(guild.id, [m.id for m in moved])
        pulled_mentions = ", ".join(m.mention for m in moved)
        messages.append(f"Pulled {len(moved)} member(s) into {executor_channel.mention}: {pulled_mentions}")
        PULLED_HISTORY.setdefault(guild.id, []).extend(m.mention for m in moved)
        participants = get_participant_set(guild.id)
        participants.update(m.id for m in moved)
        await update_giveaway_message(guild)
    if failed:
        failed_parts = ", ".join(f"{m.mention} ({reason})" for m, reason in failed)
        messages.append(f"Failed to move: {failed_parts}")
    if not messages:
        messages.append("No members were moved.")

    await interaction.followup.send(
        "\n".join(messages),
        allowed_mentions=discord.AllowedMentions(users=True)
    )


@bot.tree.command(name="disconnect_all", description="Disconnect everyone in your voice channel except owner/Admin/Moderator.")
@app_commands.guild_only()
@app_commands.describe(ending_balance="Ending balance to report (example: 123.45 or C$123.45).")
async def disconnect_all(interaction: discord.Interaction, ending_balance: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.")
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.")
        return

    # Permission check: server owner or Admin role
    if not is_privileged(executor, guild):
        await interaction.response.send_message("Only the server owner or members with the Admin role may use this command.")
        return
    if not has_active_giveaway(guild.id):
        await interaction.response.send_message(
            "There is no active giveaway to end. Pull at least one person first.",
            ephemeral=True,
        )
        return

    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /disconnect_all.")
        return
    parsed_balance, currency_prefix = parse_ending_balance(ending_balance)
    if parsed_balance is None:
        await interaction.response.send_message(
            "Please enter a valid ending balance (example: 123.45 or C$123.45)."
        )
        return

    # Acknowledge quickly to avoid interaction expiry if the loop below takes time.
    if not interaction.response.is_done():
        await interaction.response.defer()

    logger.info(
        "disconnect_all invoked | executor=%s guild=%s channel=%s ending_balance=%s currency=%s",
        executor.id,
        guild.id,
        voice_state.channel.id,
        parsed_balance,
        currency_prefix,
    )
    summary = await perform_disconnect_all(executor, parsed_balance, currency_prefix)
    await interaction.followup.send(summary, allowed_mentions=discord.AllowedMentions(users=True))
    await reset_giveaway_state(guild)


@bot.tree.command(name="pull_specific", description="Pull a specific user into your voice channel.")
@app_commands.guild_only()
@app_commands.describe(user="User to pull")
async def pull_specific(interaction: discord.Interaction, user: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.")
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.")
        return

    if not is_privileged(executor, guild):
        await interaction.response.send_message("Only the server owner or members with the Admin role may use this command.")
        return

    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /pull_specific.")
        return

    if not interaction.response.is_done():
        await interaction.response.defer()

    executor_channel = voice_state.channel
    logger.info(
        "pull_specific invoked | executor=%s guild=%s channel=%s input=%s",
        executor.id,
        guild.id,
        executor_channel.id,
        user,
    )

    # Accept raw ID, autocomplete value, or mention format
    digits = re.findall(r"\d+", user)
    if not digits:
        await interaction.followup.send("Invalid user selection. Provide a user from the autocomplete list or a user ID.")
        return
    target_id = int(digits[0])

    member = guild.get_member(target_id)
    if member is None:
        await interaction.followup.send("Could not find that user in this server.")
        return

    if member.voice is None or member.voice.channel is None:
        await interaction.followup.send(f"{member.mention} is not in a voice channel.", allowed_mentions=discord.AllowedMentions(users=True))
        return

    if member.voice.channel.id == executor_channel.id:
        await interaction.followup.send(f"{member.mention} is already in your voice channel.", allowed_mentions=discord.AllowedMentions(users=True))
        return

    try:
        await member.move_to(executor_channel)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to move that member.")
        return
    except discord.HTTPException:
        await interaction.followup.send("Failed to move the member due to a Discord error.")
        return

    participants = get_participant_set(guild.id)
    participants.add(member.id)
    await update_giveaway_message(guild)
    logger.info(
        "pull_specific moved | executor=%s target=%s guild=%s channel=%s",
        executor.id,
        member.id,
        guild.id,
        executor_channel.id,
    )

    await interaction.followup.send(
        f"Moved {member.mention} to {executor_channel.mention}.",
        allowed_mentions=discord.AllowedMentions(users=True)
    )


@pull_specific.autocomplete("user")
async def pull_specific_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if guild is None:
        return []

    current_lower = current.lower()
    choices: list[app_commands.Choice[str]] = []
    if ENABLE_MEMBERS_INTENT:
        members = guild.members
    else:
        # Voice-state payloads include the members this command can actually pull,
        # so autocomplete can remain useful without requesting the full member list.
        members_by_id = {
            member.id: member
            for channel in guild.voice_channels
            for member in channel.members
        }
        members = list(members_by_id.values())

    for member in members:
        display = member.display_name or member.name
        if current_lower in display.lower() or current_lower in member.name.lower():
            label = f"{display} ({member.name})"
            choices.append(app_commands.Choice(name=label[:100], value=str(member.id)))
            if len(choices) >= 25:
                break
    return choices


def main():
    token_path = Path("token.txt")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("token.txt is empty.")
    bot.run(token)


if __name__ == "__main__":
    main()
