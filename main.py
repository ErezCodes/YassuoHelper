import json
import random
import re
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("yassuo_helper")


# Load role mapping once at startup
ROLES_PATH = Path("roles.json")
with ROLES_PATH.open("r", encoding="utf-8") as fp:
    ROLE_MAP: dict[str, int] = json.load(fp)

# Track users pulled randomly via /pull until next /disconnect_all
PULLED_HISTORY: list[str] = []
GIVEAWAY_MESSAGES: dict[int, tuple[int, int]] = {}  # guild_id -> (channel_id, message_id)
GIVEAWAY_PARTICIPANTS: dict[int, set[int]] = {}  # guild_id -> {user_ids}
MIN_ACCOUNT_AGE = timedelta(days=180)  # ~6 months

# Precompute slash-command choices: "None" first, then roles.json order
ROLE_CHOICES = [app_commands.Choice(name="None", value="None")]
ROLE_CHOICES.extend(app_commands.Choice(name=name, value=name) for name in ROLE_MAP.keys())

# Bot setup
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


def member_has_role(member: discord.Member, role_name: str) -> bool:
    """Return True if member has the role specified by name from ROLE_MAP."""
    role_id = ROLE_MAP.get(role_name)
    if not role_id:
        return False
    return any(role.id == role_id for role in member.roles)


def is_privileged(member: discord.Member, guild: discord.Guild) -> bool:
    """Server owner or has Admin role."""
    return guild.owner_id == member.id or member_has_role(member, "Admin")


def get_participant_set(guild_id: int) -> set[int]:
    return GIVEAWAY_PARTICIPANTS.setdefault(guild_id, set())


def account_old_enough(member: discord.Member) -> bool:
    """Require Discord account age >= MIN_ACCOUNT_AGE (ignores how long they've been in the server)."""
    if not member.created_at:
        logger.debug("Missing created_at for member %s (%s)", member, member.id)
        return False
    age_ok = (datetime.now(timezone.utc) - member.created_at) >= MIN_ACCOUNT_AGE
    logger.debug(
        "Account age check for %s (%s): created_at=%s age_ok=%s",
        member,
        member.id,
        member.created_at,
        age_ok,
    )
    return age_ok


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


def role_id_by_name(name: str) -> Optional[int]:
    return ROLE_MAP.get(name)


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
        await interaction.response.send_message(
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
            placeholder="e.g., 123.45",
            required=True,
            max_length=20,
        )
        self.add_item(self.ending_balance)

    async def on_submit(self, interaction: discord.Interaction):
        # Accept numbers that may include commas and/or dollar signs.
        raw_value = (self.ending_balance.value or "").replace("$", "").replace(",", "").strip()
        try:
            balance = float(raw_value)
        except ValueError:
            await interaction.response.send_message(
                "Please enter a valid number for the ending balance.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        summary = await perform_disconnect_all(self.executor, balance)

        # Public response in-channel.
        await interaction.response.send_message(
            summary,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        # DM the same summary to specified users.
        target_ids = (656576358662537227, 128660686057242625, 166927407285010434)
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

        logger.info(
            "GiveawayView.end_giveaway invoked | user=%s guild=%s",
            interaction.user.id,
            guild.id,
        )
        await interaction.response.send_modal(EndGiveawayModal(interaction.user))


async def perform_disconnect_all(executor: discord.Member, ending_balance: float) -> str:
    """Shared logic for ending giveaway/disconnecting, returns summary text."""
    guild = executor.guild
    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        return "You need to be connected to a voice channel to end the giveaway."
    channel = voice_state.channel
    logger.info(
        "perform_disconnect_all | executor=%s guild=%s channel=%s ending_balance=%s",
        executor.id,
        guild.id,
        channel.id,
        ending_balance,
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
    # Build formatted participant list for summary output
    participant_lines: list[str] = []
    for user_id in participants:
        member = guild.get_member(user_id)
        if member:
            participant_lines.append(f"- {member.mention} [{member.name}] ({member.id})")
        else:
            participant_lines.append(f"- <@{user_id}> ({user_id})")

    participant_section = "\n".join(participant_lines) if participant_lines else "- None"
    participant_count = len(participants)
    total_winnings = f"${ending_balance:.2f}"
    per_person = (
        f"${(ending_balance / participant_count):.2f}"
        if participant_count > 0
        else "N/A (no participants)"
    )

    lines = [
        "Group summary:",
        participant_section,
        f"Total winnings: {total_winnings}",
        f"Amount for each person when divided equally: {per_person}",
    ]

    # Reset participants
    participants.clear()
    PULLED_HISTORY.clear()
    await update_giveaway_message(guild)
    logger.info(
        "perform_disconnect_all summary | guild=%s disconnected=%s failed=%s participants_cleared",
        guild.id,
        [m.id for m in disconnected],
        [(m.id, reason) for m, reason in failed],
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

    def candidates_for_role(role_name: str) -> list[discord.Member]:
        pool: list[discord.Member] = []
        for channel in guild.voice_channels:
            if channel.id == executor_channel.id:
                continue
            for member in channel.members:
                if member.id in already_selected:
                    continue
                if not account_old_enough(member):
                    continue
                if role_name != "Normal" and not member_has_role(member, role_name):
                    continue
                pool.append(member)
        return pool

    for role_name, requested in counts.items():
        if requested <= 0:
            continue
        pool = candidates_for_role(role_name)
        if not pool:
            notes.append(f"No available {role_name} to pull.")
            continue
        take = min(requested, len(pool))
        if take < requested:
            notes.append(f"Requested {requested} {role_name}, only found {take}.")
        selected = random.sample(pool, k=take)
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
    await bot.tree.sync()
    logger.info("Logged in as %s (ID: %s) | guilds=%d", bot.user, bot.user.id, len(bot.guilds))
    # Cache any existing giveaway messages with buttons so we can update them.
    for guild in bot.guilds:
        logger.info("Connected guild: %s (%s)", guild.name, guild.id)
        await find_existing_giveaway_message(guild)


@bot.event
async def setup_hook():
    # Re-register persistent view so old giveaway messages keep working after restarts.
    bot.add_view(GiveawayView())


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


@bot.tree.command(name="pull", description="Pull random member(s) with an optional required role into your voice channel.")
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
    admin_role_id = ROLE_MAP.get("Admin")
    is_owner = guild.owner_id == executor.id
    is_admin_role = admin_role_id is not None and any(r.id == admin_role_id for r in executor.roles)
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
    for channel in guild.voice_channels:
        if channel.id == executor_channel.id:
            continue  # exclude members already with the executor
        for member in channel.members:
            if not account_old_enough(member):
                continue
            if role_filter != "None" and not member_has_role(member, role_filter):
                continue
            candidates.append(member)
    logger.info(
        "pull invoked | executor=%s guild=%s channel=%s role_filter=%s candidates=%d amount=%s",
        executor.id,
        guild.id,
        executor_channel.id,
        role_filter,
        len(candidates),
        amount,
    )

    if not candidates:
        await interaction.followup.send("No eligible members found in other voice channels.")
        return

    if amount > len(candidates):
        await interaction.followup.send(
            f"Requested {amount} member(s) but only {len(candidates)} eligible.",
        )
        return

    # Randomly pick member(s)
    chosen_members = random.sample(candidates, k=amount)

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
        pulled_mentions = ", ".join(m.mention for m in moved)
        messages.append(f"Pulled {len(moved)} member(s) into {executor_channel.mention}: {pulled_mentions}")
        PULLED_HISTORY.extend(m.mention for m in moved)
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
@app_commands.describe(ending_balance="Ending balance to report (numbers allowed).")
async def disconnect_all(interaction: discord.Interaction, ending_balance: float):
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

    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /disconnect_all.")
        return

    # Acknowledge quickly to avoid interaction expiry if the loop below takes time.
    if not interaction.response.is_done():
        await interaction.response.defer()

    logger.info(
        "disconnect_all invoked | executor=%s guild=%s channel=%s ending_balance=%s",
        executor.id,
        guild.id,
        voice_state.channel.id,
        ending_balance,
    )
    summary = await perform_disconnect_all(executor, ending_balance)
    await interaction.followup.send(summary, allowed_mentions=discord.AllowedMentions(users=True))


@bot.tree.command(name="pull_specific", description="Pull a specific user into your voice channel.")
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
    for member in guild.members:
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
