import json
import random
import re
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands


# Load role mapping once at startup
ROLES_PATH = Path("roles.json")
with ROLES_PATH.open("r", encoding="utf-8") as fp:
    ROLE_MAP: dict[str, int] = json.load(fp)

# Track users pulled randomly via /pull until next /disconnect_all
PULLED_HISTORY: list[str] = []

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


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="pull", description="Pull a random member with a required role into your voice channel.")
@app_commands.describe(required_role="Role name from roles.json or 'None' to ignore role requirements.")
@app_commands.choices(required_role=ROLE_CHOICES)
async def pull(interaction: discord.Interaction, required_role: app_commands.Choice[str]):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.", ephemeral=True)
        return

    # Permission check: server owner or Admin role
    admin_role_id = ROLE_MAP.get("Admin")
    is_owner = guild.owner_id == executor.id
    is_admin_role = admin_role_id is not None and any(r.id == admin_role_id for r in executor.roles)
    if not (is_owner or is_admin_role):
        await interaction.response.send_message("Only the server owner or members with the Admin role may use this command.", ephemeral=True)
        return

    # Ensure executor is in a voice channel
    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /pull.", ephemeral=True)
        return
    executor_channel = voice_state.channel

    # Build candidate list
    role_filter = required_role.value
    candidates: list[discord.Member] = []
    for channel in guild.voice_channels:
        if channel.id == executor_channel.id:
            continue  # exclude members already with the executor
        for member in channel.members:
            if role_filter != "None" and not member_has_role(member, role_filter):
                continue
            candidates.append(member)

    if not candidates:
        await interaction.response.send_message("No eligible members found in other voice channels.", ephemeral=True)
        return

    # Randomly pick a member
    chosen_member = random.choice(candidates)
    chosen_index = candidates.index(chosen_member) + 1  # 1-based for display

    # Move the member
    try:
        await chosen_member.move_to(executor_channel)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to move that member.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("Failed to move the member due to a Discord error.", ephemeral=True)
        return

    # Build the numbered list text
    list_lines = []
    for idx, member in enumerate(candidates, start=1):
        list_lines.append(f"{idx}. {member.mention}")
    list_text = "\n".join(list_lines)

    await interaction.response.send_message(
        f"Eligible members ({len(candidates)}):\n{list_text}\n\nChosen number: {chosen_index} → {chosen_member.mention}",
        allowed_mentions=discord.AllowedMentions(users=True)
    )
    PULLED_HISTORY.append(chosen_member.mention)


@bot.tree.command(name="disconnect_all", description="Disconnect everyone in your voice channel except owner/Admin/Moderator.")
async def disconnect_all(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.", ephemeral=True)
        return

    # Permission check: server owner or Admin role
    if not is_privileged(executor, guild):
        await interaction.response.send_message("Only the server owner or members with the Admin role may use this command.", ephemeral=True)
        return

    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /disconnect_all.", ephemeral=True)
        return
    channel = voice_state.channel

    to_disconnect: list[discord.Member] = []
    for member in channel.members:
        if member.id == executor.id:
            continue  # never disconnect the executor
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
            await member.move_to(None)  # disconnect from voice
            disconnected.append(member)
        except discord.Forbidden:
            failed.append((member, "Missing permissions"))
        except discord.HTTPException:
            failed.append((member, "Discord error"))

    lines: list[str] = []
    if disconnected:
        lines.append("Disconnected:")
        lines.extend(f"- {m.mention}" for m in disconnected)
    if failed:
        lines.append("Failed:")
        lines.extend(f"- {m.mention} ({reason})" for m, reason in failed)
    if not lines:
        lines.append("No members to disconnect (all present are exempt).")

    # Append summary of pulled users so far
    if PULLED_HISTORY:
        summary = ["Pulled randomly so far:", f"Count: {len(PULLED_HISTORY)}"]
        summary.extend(f"- {mention}" for mention in PULLED_HISTORY)
    else:
        summary = ["Pulled randomly so far: none"]

    lines.append("")  # spacer
    lines.extend(summary)

    await interaction.response.send_message(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions(users=True)
    )

    # Reset history after reporting
    PULLED_HISTORY.clear()


@bot.tree.command(name="pull_specific", description="Pull a specific user into your voice channel.")
@app_commands.describe(user="User to pull")
async def pull_specific(interaction: discord.Interaction, user: str):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    executor = interaction.user
    if not isinstance(executor, discord.Member):
        await interaction.response.send_message("Cannot resolve your member info.", ephemeral=True)
        return

    if not is_privileged(executor, guild):
        await interaction.response.send_message("Only the server owner or members with the Admin role may use this command.", ephemeral=True)
        return

    voice_state = executor.voice
    if voice_state is None or voice_state.channel is None:
        await interaction.response.send_message("You need to be connected to a voice channel to use /pull_specific.", ephemeral=True)
        return
    executor_channel = voice_state.channel

    # Accept raw ID, autocomplete value, or mention format
    digits = re.findall(r"\d+", user)
    if not digits:
        await interaction.response.send_message("Invalid user selection. Provide a user from the autocomplete list or a user ID.", ephemeral=True)
        return
    target_id = int(digits[0])

    member = guild.get_member(target_id)
    if member is None:
        await interaction.response.send_message("Could not find that user in this server.", ephemeral=True)
        return

    if member.voice is None or member.voice.channel is None:
        await interaction.response.send_message(f"{member.mention} is not in a voice channel.", ephemeral=True, allowed_mentions=discord.AllowedMentions(users=True))
        return

    if member.voice.channel.id == executor_channel.id:
        await interaction.response.send_message(f"{member.mention} is already in your voice channel.", ephemeral=True, allowed_mentions=discord.AllowedMentions(users=True))
        return

    try:
        await member.move_to(executor_channel)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to move that member.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("Failed to move the member due to a Discord error.", ephemeral=True)
        return

    await interaction.response.send_message(
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
