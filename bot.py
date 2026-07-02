import discord
import os
import asyncio
import json
from collections import deque
import yt_dlp
from keep_alive import keep_alive

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

client = discord.Client(intents=intents)

# Per-guild state
target_channels = {}   # guild_id -> channel_id (where bot should stay)
queues = {}            # guild_id -> deque of (title, url) tuples
now_playing = {}       # guild_id -> title string

SAVE_FILE = 'channels.json'


def save_channels():
    with open(SAVE_FILE, 'w') as f:
        json.dump({str(k): v for k, v in target_channels.items()}, f)


def load_channels():
    if not os.path.exists(SAVE_FILE):
        return {}
    try:
        with open(SAVE_FILE, 'r') as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


# ── YouTube helpers ────────────────────────────────────────────────────────────

YTDL_OPTS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'ytsearch1',
    'source_address': '0.0.0.0',
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],
        }
    },
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    },
}

YTDL_OPTS_SC = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'scsearch1',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}


def fetch_info(query):
    """Fetch stream URL and title for a YouTube query or URL (blocking).
    Falls back to SoundCloud if YouTube blocks the request."""
    def _extract(opts, q):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(q, download=False)
            if info is None:
                raise ValueError('No results found')
            if 'entries' in info:
                entries = [e for e in info['entries'] if e]
                if not entries:
                    raise ValueError('No results found')
                info = entries[0]
            if not info.get('url'):
                raise ValueError('No playable stream found')
            return info.get('url'), info.get('title', 'Unknown')

    try:
        return _extract(YTDL_OPTS, query)
    except Exception as e:
        err = str(e)
        if 'Sign in' in err or 'bot' in err.lower() or 'confirm' in err.lower():
            # YouTube blocked — try SoundCloud instead
            return _extract(YTDL_OPTS_SC, query)
        raise


async def play_next(guild):
    """Play the next track in the queue for a guild."""
    queue = queues.get(guild.id)
    vc = guild.voice_client

    if not queue or not vc or not vc.is_connected():
        now_playing.pop(guild.id, None)
        return

    title, stream_url = queue.popleft()
    now_playing[guild.id] = title

    source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS)

    def after_play(error):
        if error:
            print(f'Playback error: {error}')
        asyncio.run_coroutine_threadsafe(play_next(guild), client.loop)

    vc.play(discord.PCMVolumeTransformer(source, volume=0.5), after=after_play)
    print(f'Now playing: {title}')


# ── Events ─────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    global target_channels
    target_channels = load_channels()

    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print(f'Connected to {len(client.guilds)} server(s)')
    print('Commands: !دخول  !خروج  !انقل  $<url/search>  !skip  !stop  !queue')

    domain = os.environ.get('REPLIT_DEV_DOMAIN', '')
    if domain:
        print(f'\n📡 Keep-alive URL (add this to UptimeRobot):')
        print(f'   https://{domain}/api/ping\n')
    print('------')

    # Auto-rejoin saved channels after restart
    for guild_id, channel_id in target_channels.items():
        guild = client.get_guild(guild_id)
        if guild:
            await reconnect(guild, channel_id)


@client.event
async def on_message(message):
    if message.author.bot:
        return

    # Guard: ignore DMs (no guild)
    if message.guild is None:
        return


    content = message.content.strip()
    guild = message.guild
    member = guild.get_member(message.author.id)

    # ── !مساعدة ────────────────────────────────────────────────────────────────
    if content == '!مساعدة':
        msg = (
            '**📋 قائمة الأوامر**\n'
            '━━━━━━━━━━━━━━━━━━━━\n'
            '🎙️ **أوامر الروم الصوتي**\n'
            '`!دخول` — يدخل رومك الصوتي ويبقى فيه\n'
            '`!خروج` — يخرج من الروم\n'
            '`!انقل` — ينتقل لرومك الحالي\n'
            '`!setup` — يحفظ رومك ويدخله تلقائياً عند كل تشغيل\n\n'
            '🎵 **أوامر الموسيقى**\n'
            '`$<رابط أو اسم>` — يشغل أغنية من يوتيوب\n'
            '`!skip` — يتخطى الأغنية الحالية\n'
            '`!stop` — يوقف الموسيقى ويمسح القائمة\n'
            '`!queue` — يعرض قائمة الأغاني\n\n'
            'ℹ️ **معلومات**\n'
            '`!حالة` — يعرض حالة البوت\n'
            '`!مساعدة` — يعرض هذه القائمة\n'
            '━━━━━━━━━━━━━━━━━━━━'
        )
        await message.channel.send(msg)
        return

    # ── !setup ─────────────────────────────────────────────────────────────────
    if content == '!setup':
        if not member.voice or not member.voice.channel:
            await message.channel.send('❌ ادخل روم صوتي أولاً ثم اكتب `!setup`')
            return

        channel = member.voice.channel
        target_channels[guild.id] = channel.id
        save_channels()

        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect()

        await message.channel.send(
            f'✅ تم الإعداد!\n'
            f'البوت سيدخل **{channel.name}** تلقائياً عند كل تشغيل بدون أي أوامر 🎙️'
        )
        print(f'Setup: will auto-join "{channel.name}" in "{guild.name}"')
        return

    # ── !دخول ──────────────────────────────────────────────────────────────────
    if content == '!دخول':
        if not member.voice or not member.voice.channel:
            await message.channel.send('You need to be in a voice channel first!')
            return

        channel = member.voice.channel
        target_channels[guild.id] = channel.id
        save_channels()
        vc = guild.voice_client

        if vc and vc.is_connected():
            if vc.channel.id == channel.id:
                await message.channel.send(f'Already in **{channel.name}**!')
                return
            await vc.move_to(channel)
        else:
            await channel.connect()

        await message.channel.send(f'Joined **{channel.name}** and will stay there 24/7!')
        print(f'Joined "{channel.name}" in "{guild.name}"')

    # ── !انقل ──────────────────────────────────────────────────────────────────
    elif content == '!انقل':
        if not member.voice or not member.voice.channel:
            await message.channel.send('You need to be in a voice channel for me to move to!')
            return

        channel = member.voice.channel
        vc = guild.voice_client

        if not vc or not vc.is_connected():
            await message.channel.send('I\'m not in a voice channel. Use **!join** first.')
            return

        if vc.channel.id == channel.id:
            await message.channel.send(f'I\'m already in **{channel.name}**!')
            return

        target_channels[guild.id] = channel.id
        save_channels()
        await vc.move_to(channel)
        await message.channel.send(f'Moved to **{channel.name}**!')
        print(f'Moved to "{channel.name}" in "{guild.name}"')

    # ── !خروج ─────────────────────────────────────────────────────────────────
    elif content == '!خروج':
        vc = guild.voice_client
        if vc and vc.is_connected():
            target_channels.pop(guild.id, None)
            save_channels()
            queues.pop(guild.id, None)
            now_playing.pop(guild.id, None)
            await vc.disconnect()
            await message.channel.send('Left the voice channel.')
        else:
            await message.channel.send('I\'m not in a voice channel.')

    # ── $ (play) ───────────────────────────────────────────────────────────────
    elif content.startswith('$'):
        query = content[1:].strip()
        if not query:
            await message.channel.send('الاستخدام: `$<رابط يوتيوب أو اسم الأغنية>`')
            return

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            if not member.voice or not member.voice.channel:
                await message.channel.send('Join a voice channel first, or use **!join**.')
                return
            channel = member.voice.channel
            target_channels[guild.id] = channel.id
            save_channels()
            vc = await channel.connect()

        searching_msg = await message.channel.send(f'Searching for `{query}`...')

        try:
            stream_url, title = await asyncio.get_event_loop().run_in_executor(
                None, fetch_info, query
            )
        except Exception as e:
            await searching_msg.edit(content=f'Could not find that track. ({e})')
            return

        if guild.id not in queues:
            queues[guild.id] = deque()

        if vc.is_playing() or vc.is_paused():
            queues[guild.id].append((title, stream_url))
            await searching_msg.edit(content=f'Added to queue: **{title}**')
        else:
            queues[guild.id].appendleft((title, stream_url))
            await searching_msg.edit(content=f'Now playing: **{title}**')
            await play_next(guild)

    # ── !skip ──────────────────────────────────────────────────────────────────
    elif content.lower() == '!skip':
        vc = guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await message.channel.send('Skipped!')
        else:
            await message.channel.send('Nothing is playing right now.')

    # ── !stop ──────────────────────────────────────────────────────────────────
    elif content.lower() == '!stop':
        vc = guild.voice_client
        queues.pop(guild.id, None)
        now_playing.pop(guild.id, None)
        if vc and vc.is_playing():
            vc.stop()
            await message.channel.send('Stopped playback and cleared the queue.')
        else:
            await message.channel.send('Nothing is playing.')

    # ── !حالة ──────────────────────────────────────────────────────────────────
    elif content == '!حالة':
        vc = guild.voice_client
        current = now_playing.get(guild.id)
        queue = queues.get(guild.id, deque())

        if vc and vc.is_connected():
            voice_status = f'✅ متصل بـ **{vc.channel.name}**'
        else:
            voice_status = '❌ غير متصل بأي روم صوتي'

        if current:
            music_status = f'🎵 يشتغل: **{current}**'
            if queue:
                music_status += f'\n📋 في الانتظار: **{len(queue)}** أغنية'
        else:
            music_status = '🔇 ما في موسيقى تشتغل'

        msg = (
            f'**حالة البوت**\n'
            f'━━━━━━━━━━━━━━━\n'
            f'{voice_status}\n'
            f'{music_status}\n'
            f'━━━━━━━━━━━━━━━\n'
            f'🤖 {client.user.name}'
        )
        await message.channel.send(msg)

    # ── !queue ─────────────────────────────────────────────────────────────────
    elif content.lower() in ('!queue', '!q'):
        current = now_playing.get(guild.id)
        queue = queues.get(guild.id, deque())

        if not current and not queue:
            await message.channel.send('The queue is empty.')
            return

        lines = []
        if current:
            lines.append(f'**Now playing:** {current}')
        if queue:
            lines.append('**Up next:**')
            for i, (title, _) in enumerate(queue, 1):
                lines.append(f'`{i}.` {title}')

        await message.channel.send('\n'.join(lines))


# ── Voice reconnection ─────────────────────────────────────────────────────────

@client.event
async def on_voice_state_update(member, before, after):
    if member != client.user:
        return

    guild = member.guild
    target_channel_id = target_channels.get(guild.id)
    if target_channel_id is None:
        return

    # Bot got fully disconnected — reconnect
    if before.channel is not None and after.channel is None:
        print(f'Disconnected from voice in "{guild.name}". Reconnecting in 5 seconds...')
        await asyncio.sleep(5)
        await reconnect(guild, target_channel_id)

    # Bot got moved to a different channel (e.g. AFK) — move back
    elif (before.channel is not None and after.channel is not None
          and after.channel.id != target_channel_id):
        print(f'Moved to wrong channel in "{guild.name}". Moving back...')
        await asyncio.sleep(2)
        target_channel = guild.get_channel(target_channel_id)
        if target_channel and guild.voice_client:
            await guild.voice_client.move_to(target_channel)


async def reconnect(guild, channel_id):
    channel = guild.get_channel(channel_id)
    if channel is None:
        target_channels.pop(guild.id, None)
        return

    vc = guild.voice_client
    if vc and vc.is_connected():
        return

    try:
        await channel.connect()
        print(f'Reconnected to "{channel.name}" in "{guild.name}"')
    except Exception as e:
        print(f'Failed to reconnect: {e}')


async def watchdog():
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(60)
        for guild in client.guilds:
            target_channel_id = target_channels.get(guild.id)
            if target_channel_id is None:
                continue
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                print(f'Watchdog: Reconnecting in "{guild.name}"...')
                await reconnect(guild, target_channel_id)


@client.event
async def setup_hook():
    client.loop.create_task(watchdog())


# ── Start ──────────────────────────────────────────────────────────────────────

keep_alive()

token = os.environ.get('DISCORD_TOKEN')
if not token:
    raise RuntimeError('DISCORD_TOKEN environment variable is not set.')

client.run(token)
