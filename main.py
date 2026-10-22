import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
COMMAND_ROLE = 951477680245731408
LEADERBOARD_CATEGORY_ID = 796566346493591552
FORUM_ID = 1132608152660103199
PLUS_FORUM_ID = 1044623380575440898
FORUM_IDS = (FORUM_ID, PLUS_FORUM_ID)
LISTS = {'both': 'Combined', 'regular': 'Shitty List', 'plus': 'Shitty List Plus'}
SUMMARY_CHANNEL_ID = 1132608904522637322
GUILD_ID = 713151800932433972

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('tsl')
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
db = None
scan_lock = asyncio.Lock()
summary_lock = asyncio.Lock()
scan_task = None

PATTERNS = {
    'RA': re.compile(r'\breliable\s+accept\b', re.I),
    'RR': re.compile(r'\breliable\s+reject\b', re.I),
    'FA': re.compile(r'\bfeedback\s+accept\b', re.I),
    'FR': re.compile(r'\bfeedback\s+reject\b', re.I),
}
FILTERS = {
    'total': ('All Opinions', ('RA', 'RR', 'FA', 'FR')),
    'RA': ('Reliable Accepts', ('RA',)),
    'RR': ('Reliable Rejects', ('RR',)),
    'FA': ('Feedback Accepts', ('FA',)),
    'FR': ('Feedback Rejects', ('FR',)),
    'accepts': ('All Accepts', ('RA', 'FA')),
    'rejects': ('All Rejects', ('RR', 'FR')),
}
PERIODS = {
    'all': 'All Time', 'month': 'This Month',
    'last_month': 'Last Month', 'week': 'This Week',
}


def categories(content):
    return [key for key, pattern in PATTERNS.items() if pattern.search(content or '')]


def is_tsl_thread(channel):
    return isinstance(channel, discord.Thread) and channel.parent_id in FORUM_IDS


def is_regular_thread(channel):
    return isinstance(channel, discord.Thread) and channel.parent_id == FORUM_ID


def permitted(interaction):
    return isinstance(interaction.user, discord.Member) and any(
        role.id == COMMAND_ROLE for role in interaction.user.roles
    )


def leaderboard_permitted(interaction):
    if interaction.guild_id != GUILD_ID:
        return False
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent or interaction.guild.get_channel(channel.parent_id)
        return parent is not None and parent.category_id == LEADERBOARD_CATEGORY_ID
    return channel is not None and getattr(channel, 'category_id', None) == LEADERBOARD_CATEGORY_ID


async def get_target_channel():
    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SUMMARY_CHANNEL_ID)
        except discord.HTTPException:
            logger.exception('Could not fetch summary channel')
            return None
    return channel


async def get_summary_message(channel):
    messages = []
    async for message in channel.history(limit=100):
        if message.author.id == bot.user.id and any(
            embed.title == 'TSL Opinions' for embed in message.embeds
        ):
            messages.append(message)
    return list(reversed(messages))


async def count_pins(thread):
    counts = dict.fromkeys(PATTERNS, 0)
    async for msg in thread.pins():
        content = (msg.content or '').lower()
        for key, pattern in PATTERNS.items():
            counts[key] += len(pattern.findall(content))
    return counts


def build_embed(data):
    embed = discord.Embed(title='TSL Opinions', color=discord.Color.blue())
    if not data:
        embed.description = 'No active threads.'
    for thread, counts in data.items():
        embed.add_field(
            name=thread.mention,
            value=(f"**Reliables:** ✅ {counts['RA']} | ❌ {counts['RR']}\n"
                   f"**Feedbacks:** ✅ {counts['FA']} | ❌ {counts['FR']}"),
            inline=False,
        )
    return embed


async def update_summary():
    async with summary_lock:
        target = await get_target_channel()
        if target is None:
            return
        data = {}
        guild = bot.get_guild(GUILD_ID)
        if guild:
            for thread in guild.threads:
                if is_regular_thread(thread) and not (thread.archived or thread.locked):
                    data[thread] = await count_pins(thread)
        ordered = sorted(data.items(), key=lambda x: (x[1]['RA'], x[1]['FA']), reverse=True)
        embeds = [build_embed(dict(ordered[i:i + 25])) for i in range(0, len(ordered), 25)] or [build_embed({})]
        messages = await get_summary_message(target)
        for i, embed in enumerate(embeds):
            if i < len(messages):
                await messages[i].edit(embed=embed)
            else:
                await target.send(embed=embed)
        for old in messages[len(embeds):]:
            await old.delete()


async def init_db():
    global db
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is required. Add a persistent PostgreSQL database in Render.')
    db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, ssl='require')
    async with db.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS opinions (
                message_id BIGINT PRIMARY KEY,
                author_id BIGINT NOT NULL,
                thread_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                categories TEXT[] NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ''')
        # Existing records were all imported from the regular forum.
        await conn.execute('ALTER TABLE opinions ADD COLUMN IF NOT EXISTS forum_id BIGINT')
        await conn.execute('UPDATE opinions SET forum_id = $1 WHERE forum_id IS NULL', FORUM_ID)
        await conn.execute('CREATE INDEX IF NOT EXISTS opinions_created_idx ON opinions(created_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS opinions_forum_idx ON opinions(forum_id)')


async def record_pins(thread):
    """Insert only; never delete old records after unpinning or archiving."""
    inserted = 0
    async with db.acquire() as conn:
        async for msg in thread.pins():
            kinds = categories(msg.content)
            if not kinds or msg.author.bot:
                continue
            result = await conn.execute('''
                INSERT INTO opinions(message_id, author_id, thread_id, created_at, categories, forum_id)
                VALUES($1, $2, $3, $4, $5, $6)
                ON CONFLICT (message_id) DO NOTHING
            ''', msg.id, msg.author.id, thread.id, msg.created_at, kinds, thread.parent_id)
            inserted += int(result.split()[-1])
    return inserted


async def iter_forum_threads(forum):
    """Yield active and accessible archived threads, without duplicates."""
    seen = set()
    for thread in forum.threads:
        if thread.id not in seen:
            seen.add(thread.id)
            yield thread
    async for thread in forum.archived_threads(limit=None):
        if thread.id not in seen:
            seen.add(thread.id)
            yield thread
    # Discord forum archived_threads() does not accept private/joined parameters.
    # The public archived threads above are the supported archive source.


async def scan_all():
    async with scan_lock:
        scanned = inserted = failed = 0
        for forum_id in FORUM_IDS:
            forum = bot.get_channel(forum_id)
            if forum is None:
                forum = await bot.fetch_channel(forum_id)
            logger.info('Starting scan of forum %s', forum_id)
            async for thread in iter_forum_threads(forum):
                try:
                    inserted += await record_pins(thread)
                    scanned += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
                    logger.warning('Could not scan thread %s', thread.id)
                if (scanned + failed) % 50 == 0:
                    logger.info('Scan progress: %s threads scanned, %s new opinions, %s inaccessible',
                                scanned, inserted, failed)
            logger.info('Finished scanning forum %s', forum_id)
        logger.info('Leaderboard scan: %s threads, %s new opinions, %s inaccessible', scanned, inserted, failed)
        return scanned, inserted, failed


@tasks.loop(hours=6)
async def periodic_scan():
    try:
        await scan_all()
    except Exception:
        logger.exception('Periodic leaderboard scan failed')


@periodic_scan.before_loop
async def before_periodic_scan():
    await bot.wait_until_ready()


def date_bounds(period):
    now = datetime.now(timezone.utc)
    if period == 'month':
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), None
    if period == 'last_month':
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(day=1)
        return start, end
    if period == 'week':
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None
    return None, None


async def get_ranking(category, period, list_filter='both'):
    start, end = date_bounds(period)
    async with db.acquire() as conn:
        return await conn.fetch('''
            SELECT author_id, COUNT(*)::int AS total
            FROM opinions
            WHERE categories && $1::text[]
              AND ($2::timestamptz IS NULL OR created_at >= $2)
              AND ($3::timestamptz IS NULL OR created_at < $3)
              AND ($4::bigint IS NULL OR forum_id = $4)
            GROUP BY author_id
            ORDER BY total DESC, author_id ASC
        ''', list(FILTERS[category][1]), start, end,
        None if list_filter == 'both' else FORUM_ID if list_filter == 'regular' else PLUS_FORUM_ID)


class OpinionSelect(discord.ui.Select):
    def __init__(self, board):
        self.board = board
        super().__init__(placeholder='Opinion category', row=0, options=[
            discord.SelectOption(label=label, value=key, default=key == board.category)
            for key, (label, _) in FILTERS.items()
        ])

    async def callback(self, interaction):
        self.board.category = self.values[0]
        self.board.page = 0
        await self.board.redraw(interaction)


class PeriodSelect(discord.ui.Select):
    def __init__(self, board):
        self.board = board
        super().__init__(placeholder='Time period', row=1, options=[
            discord.SelectOption(label=label, value=key, default=key == board.period)
            for key, label in PERIODS.items()
        ])

    async def callback(self, interaction):
        self.board.period = self.values[0]
        self.board.page = 0
        await self.board.redraw(interaction)


class ListSelect(discord.ui.Select):
    def __init__(self, board):
        self.board = board
        super().__init__(placeholder='Which list?', row=2, options=[
            discord.SelectOption(label=label, value=key, default=key == board.list_filter)
            for key, label in LISTS.items()
        ])

    async def callback(self, interaction):
        self.board.list_filter = self.values[0]
        self.board.page = 0
        await self.board.redraw(interaction)


class LeaderboardView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not leaderboard_permitted(interaction):
            await interaction.response.send_message(
                'This leaderboard can only be used in channels under the approved Discord category.', ephemeral=True
            )
            return False
        return True

    def __init__(self, category='total', period='all', page=0, list_filter='both'):
        super().__init__(timeout=300)
        self.category, self.period, self.page = category, period, page
        self.list_filter = list_filter
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(OpinionSelect(self))
        self.add_item(PeriodSelect(self))
        self.add_item(ListSelect(self))
        previous = discord.ui.Button(label='◀ Previous', style=discord.ButtonStyle.secondary, row=3)
        next_page = discord.ui.Button(label='Next ▶', style=discord.ButtonStyle.secondary, row=3)
        previous.callback = self.previous
        next_page.callback = self.next
        self.add_item(previous)
        self.add_item(next_page)

    async def render(self):
        ranking = await get_ranking(self.category, self.period, self.list_filter)
        pages = max(1, (len(ranking) + 9) // 10)
        self.page = min(max(0, self.page), pages - 1)
        lines = []
        for index, record in enumerate(ranking[self.page * 10:(self.page + 1) * 10], self.page * 10 + 1):
            medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(index, f'**{index}.**')
            lines.append(f'{medal} <@{record["author_id"]}> — **{record["total"]}**')
        embed = discord.Embed(
            title='🏆 TSL Opinions Leaderboard',
            description='\n'.join(lines) or 'No recorded opinions match these filters.',
            color=discord.Color.gold(),
        )
        embed.add_field(name='Category', value=FILTERS[self.category][0])
        embed.add_field(name='Period', value=PERIODS[self.period])
        embed.add_field(name='List', value=LISTS[self.list_filter])
        embed.set_footer(text=f'Page {self.page + 1}/{pages} • UTC dates • Historical records remain after unpinning')
        return embed

    async def redraw(self, interaction):
        self.rebuild()
        await interaction.response.edit_message(embed=await self.render(), view=self)

    async def previous(self, interaction):
        self.page = max(0, self.page - 1)
        await self.redraw(interaction)

    async def next(self, interaction):
        self.page += 1
        await self.redraw(interaction)


@bot.event
async def on_ready():
    global scan_task
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    logger.info('Synced %s commands; online as %s', len(synced), bot.user)
    if not periodic_scan.is_running():
        periodic_scan.start()


@bot.event
async def on_guild_channel_pins_update(channel, last_pin):
    if is_tsl_thread(channel):
        try:
            await record_pins(channel)
            if is_regular_thread(channel):
                await update_summary()
        except Exception:
            logger.exception('Pin update failed')


@bot.event
async def on_thread_create(thread):
    if is_tsl_thread(thread):
        await record_pins(thread)
        if is_regular_thread(thread):
            await update_summary()


@bot.event
async def on_thread_update(before, after):
    if is_tsl_thread(after):
        await record_pins(after)
        if is_regular_thread(after):
            await update_summary()


@bot.event
async def on_guild_channel_delete(channel):
    if is_regular_thread(channel):
        await update_summary()


@bot.tree.command(name='refresh', description='Refresh the TSL summary')
async def refresh(interaction: discord.Interaction):
    if not permitted(interaction):
        await interaction.response.send_message('You don’t have permission to use this command.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await update_summary()
    await interaction.followup.send('Refreshed ✅', ephemeral=True)


@bot.tree.command(name='leaderboard', description='View the filterable TSL opinions leaderboard')
async def leaderboard(interaction: discord.Interaction):
    if not leaderboard_permitted(interaction):
        await interaction.response.send_message(
            'Use `/leaderboard` in a channel under the approved Discord category.', ephemeral=True
        )
        return
    view = LeaderboardView()
    await interaction.response.send_message(embed=await view.render(), view=view)


@bot.tree.command(name='scanopinions', description='Import pinned opinions from all TSL threads')
async def scanopinions(interaction: discord.Interaction):
    if not permitted(interaction):
        await interaction.response.send_message('You don’t have permission to use this command.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        scanned, inserted, failed = await scan_all()
        await interaction.followup.send(
            f'Scanned **{scanned}** threads, saved **{inserted}** new opinions. '
            f'Inaccessible threads: **{failed}**.', ephemeral=True
        )
    except Exception:
        logger.exception('Manual scan failed')
        await interaction.followup.send('Scan failed; check the Render logs.', ephemeral=True)


@bot.tree.command(name='humble', description='Opinions Manager, humble him.')
async def humble(interaction: discord.Interaction, message: str):
    if not permitted(interaction):
        await interaction.response.send_message('You don’t have permission to use this command.', ephemeral=True)
        return
    await interaction.response.send_message('Sent ✅', ephemeral=True)
    await interaction.channel.send(message)


async def main():
    if not TOKEN:
        raise RuntimeError('DISCORD_TOKEN is missing')
    await init_db()
    keep_alive()
    try:
        async with bot:
            await bot.start(TOKEN)
    finally:
        if db:
            await db.close()


if __name__ == '__main__':
    asyncio.run(main())
