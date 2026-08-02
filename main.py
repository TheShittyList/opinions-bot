from dataclasses import fields

import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
from keep_alive import keep_alive

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)

COMMAND_ROLE = 951477680245731408

FORUM_ID = 1132608152660103199
SUMMARY_CHANNEL_ID = 1132608904522637322

GUILD_ID = 713151800932433972

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)

    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)

    print(f"Synced {len(synced)} commands to guild.")
    print(f"Bot is online as {bot.user}")

# helpers

async def get_target_channel():
    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SUMMARY_CHANNEL_ID)
        except Exception as exc:
            print("Target channel not found:", exc)
            return None
    return channel

async def get_summary_message(channel):
    messages = []

    async for message in channel.history(limit=100):
        if message.author == bot.user and message.embeds:
            messages.append(message)

    return list(reversed(messages))

def is_tsl_thread(channel):
    return (
        isinstance(channel, discord.Thread)
        and channel.parent
        and channel.parent.id == FORUM_ID
    )

async def count_pins(thread):
    counts = {"RA": 0, "RR": 0, "FA": 0, "FR": 0}

    async for msg in thread.pins():
        content = (msg.content or "").lower()

        counts["RA"] += content.count("reliable accept")
        counts["RR"] += content.count("reliable reject")
        counts["FA"] += content.count("feedback accept")
        counts["FR"] += content.count("feedback reject")

    return counts

def build_embed(data):
    embed = discord.Embed(
        title="TSL Opinions",
        color=discord.Color.blue()
    )

    if not data:
        embed.description = "No active threads."
        return embed

    for thread, counts in data.items():
        embed.add_field(
            name=f"{thread.mention}",
            value=(
                f"**Reliables:** ✅ {counts['RA']} | ❌ {counts['RR']}\n"
                f"**Feedbacks:** ✅ {counts['FA']} | ❌ {counts['FR']}"
            ),
            inline=False
        )

    return embed

# logic

async def update_summary():
    target_channel = await get_target_channel()

    if target_channel is None:
        return

    data = {}

    for guild in bot.guilds:
        for thread in guild.threads:
            if not is_tsl_thread(thread):
                continue

            if thread.id == SUMMARY_CHANNEL_ID:
                continue

            if thread.archived or thread.locked:
                continue

            counts = await count_pins(thread)
            data[thread] = counts


# Sort everything first, then split into pages of 25 fields
    embeds = []

    sorted_data = sorted(
        data.items(),
        key=lambda x: (x[1]["RA"], x[1]["FA"]),
        reverse=True
    )

    if sorted_data:
        for i in range(0, len(sorted_data), 25):
            chunk = dict(sorted_data[i:i + 25])
            embeds.append(build_embed(chunk))
    else:
        embeds.append(build_embed({}))

    summary_messages = await get_summary_message(target_channel)

    # Update existing messages
    if summary_messages:

        for i, embed in enumerate(embeds):

            if i < len(summary_messages):
                await summary_messages[i].edit(embed=embed)

            else:
                await target_channel.send(embed=embed)

        # Delete unused old pages
        if len(summary_messages) > len(embeds):
            for old_message in summary_messages[len(embeds):]:
                await old_message.delete()


    # First time creating messages
    else:

        for embed in embeds:
            await target_channel.send(embed=embed)

# events

@bot.event
async def on_guild_channel_pins_update(channel, last_pin):
    if is_tsl_thread(channel):
        await update_summary()

@bot.event
async def on_thread_create(thread):
    if is_tsl_thread(thread):
        await update_summary()

@bot.event
async def on_thread_update(before, after):
    if is_tsl_thread(after):
        await update_summary()

@bot.event
async def on_guild_channel_delete(channel):
    if is_tsl_thread(channel):
        await update_summary()



# commands

@bot.tree.command(name="refresh", description="Refresh the TSL summary")
async def refresh(interaction: discord.Interaction):

    if not any(role.id == COMMAND_ROLE for role in interaction.user.roles):
        await interaction.response.send_message(
            "You don’t have permission to use this command.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    await update_summary()

    await interaction.followup.send("Refreshed ✅", ephemeral=True)

# joke command

@bot.tree.command(name="humble", description="Opinions Manager, humble him.")
async def humble(interaction: discord.Interaction, message: str):

    if not any(role.id == COMMAND_ROLE for role in interaction.user.roles):
        await interaction.response.send_message(
            "You don’t have permission to use this command.",
            ephemeral=True
        )
        return

    await interaction.response.send_message("Sent ✅", ephemeral=True)

    await interaction.channel.send(message)




# flask server
keep_alive()

bot.run(token, log_handler=handler, log_level=logging.DEBUG)