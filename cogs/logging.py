import discord
from discord.ext import commands
from discord import ui
import aiosqlite
from pathlib import Path


from XEON.emojis import E as _EMGR, FALLBACK_EMOJI
import re as _re

_LOCAL_KEY_MAP = {
    "success": "SUCCESS",
    "error": "ERROR",
    "info": "INFO",
    "dot": "DOT",
    "log": "LOG",
    "msg": "MSG",
    "edit": "EDIT_ICO",
    "trash": "TRASH",
    "user": "USER",
    "leave": "LEAVE",
    "join": "JOIN",
    "ban": "BAN_ICO",
    "channel": "CHANNEL",
    "role": "ROLE",
    "settings": "SETTINGS",
}

def E(key: str) -> discord.PartialEmoji:
    s = _EMGR[_LOCAL_KEY_MAP.get(key, key.upper())]
    m = _re.match(r'<(a)?:([A-Za-z0-9_]+):(\d+)>', s)
    if m:
        return discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
    return FALLBACK_EMOJI

def ES(key: str) -> str:
    return _EMGR[_LOCAL_KEY_MAP.get(key, key.upper())]

DB_DIR = Path("db")
DB_PATH = DB_DIR / "logging.db"
DEFAULT_ACCENT = discord.Colour(0x000000)

LOG_TYPES = [
    "message_delete", "message_edit", "bulk_delete",
    "member_join", "member_leave", "member_update",
    "member_ban", "member_unban",
    "role_create", "role_delete", "role_update",
    "channel_create", "channel_delete", "channel_update",
    "voice", "invite",
]

LOG_CATEGORY_NAME = "📋 Server Logs"

LOG_CHANNEL_NAMES = {
    "message_delete":  "message-delete-logs",
    "message_edit":    "message-edit-logs",
    "bulk_delete":     "bulk-delete-logs",
    "member_join":     "member-join-logs",
    "member_leave":    "member-leave-logs",
    "member_update":   "member-update-logs",
    "member_ban":      "member-ban-logs",
    "member_unban":    "member-unban-logs",
    "role_create":     "role-create-logs",
    "role_delete":     "role-delete-logs",
    "role_update":     "role-update-logs",
    "channel_create":  "channel-create-logs",
    "channel_delete":  "channel-delete-logs",
    "channel_update":  "channel-update-logs",
    "voice":           "voice-logs",
    "invite":          "invite-logs",
}


async def init_db():
    DB_DIR.mkdir(exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS log_config (
                guild_id   INTEGER NOT NULL,
                log_type   TEXT NOT NULL,
                channel_id INTEGER,
                enabled    INTEGER DEFAULT 1,
                PRIMARY KEY (guild_id, log_type)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS log_default (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER
            )
        """)
        await db.commit()


async def get_log_channel(guild_id: int, log_type: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        async with db.execute(
            "SELECT channel_id, enabled FROM log_config WHERE guild_id=? AND log_type=?",
            (guild_id, log_type),
        ) as cur:
            row = await cur.fetchone()

        if row:
            if not row[1]:
                return None
            if row[0]:
                return row[0]

        async with db.execute(
            "SELECT channel_id FROM log_default WHERE guild_id=?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None


def _err(t):
    v = ui.LayoutView(); c = ui.Container(accent_color=DEFAULT_ACCENT)
    c.add_item(ui.TextDisplay(f"{ES('error')} {t}")); v.add_item(c); return v

def _ok(t):
    v = ui.LayoutView(); c = ui.Container(accent_color=DEFAULT_ACCENT)
    c.add_item(ui.TextDisplay(f"{ES('success')} {t}")); v.add_item(c); return v


def _log_view(emoji_key: str, title: str, fields: list[tuple[str, str]], footer: str = None):
    v = ui.LayoutView()
    c = ui.Container(accent_color=DEFAULT_ACCENT)
    c.add_item(ui.TextDisplay(f"{ES(emoji_key)} **{title}**"))
    c.add_item(ui.Separator())
    lines = []
    for name, value in fields:
        lines.append(f"{ES('dot')} **{name}:** {value}")
    c.add_item(ui.TextDisplay("\n".join(lines)))
    if footer:
        c.add_item(ui.Separator(visible=False))
    v.add_item(c)
    return v


async def send_log(bot, guild_id: int, log_type: str, view: ui.LayoutView):
    ch_id = await get_log_channel(guild_id, log_type)
    if not ch_id:
        return
    channel = bot.get_channel(ch_id)
    if not channel:
        return
    try:
        await channel.send(view=view)
    except Exception:
        pass


class Logging(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await init_db()


    @commands.group(name="logging", aliases=["log"], invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def logging(self, ctx):
        try:
            from utils.category_help import send_category_help
            if await send_category_help(ctx, "logging", self.bot):
                return
        except Exception:
            pass
        v = ui.LayoutView(); c = ui.Container(accent_color=DEFAULT_ACCENT)
        c.add_item(ui.TextDisplay(f"### {ES('settings')} Logging Commands"))
        c.add_item(ui.Separator())
        c.add_item(ui.TextDisplay(
            f"`{ctx.prefix}log all` — Auto-create ALL log channels & set everything up\n"
            f"`{ctx.prefix}log setup #ch` — Set default log channel (all events)\n"
            f"`{ctx.prefix}log set <type> #ch` — Override per-type channel\n"
            f"`{ctx.prefix}log enable <type>` — Enable a log type\n"
            f"`{ctx.prefix}log disable <type>` — Disable a log type\n"
            f"`{ctx.prefix}log config` — Show current setup\n"
            f"`{ctx.prefix}log types` — List all event types\n"
            f"`{ctx.prefix}log reset` — Reset all logging settings"
        ))
        v.add_item(c); await ctx.send(view=v)

    @logging.command(name="all", aliases=["enableall", "setupall", "auto", "enable_all"])
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def log_all(self, ctx):
        """Auto-create a channel for every log type and configure it."""
        guild = ctx.guild
        wait = await ctx.send(view=_ok("Setting up all logging channels, please wait..."))

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True,
                read_message_history=True,
            ),
        }

        category = discord.utils.get(guild.categories, name=LOG_CATEGORY_NAME)
        try:
            if category is None:
                category = await guild.create_category(
                    LOG_CATEGORY_NAME, overwrites=overwrites,
                    reason=f"Logging auto-setup by {ctx.author}",
                )
        except discord.Forbidden:
            await ctx.send(view=_err("I don't have permission to create channels."))
            return
        except discord.HTTPException as e:
            await ctx.send(view=_err(f"Failed to create category: `{e}`"))
            return

        created, reused, failed = [], [], []
        async with aiosqlite.connect(str(DB_PATH)) as db:
            for log_type in LOG_TYPES:
                name = LOG_CHANNEL_NAMES.get(log_type, f"{log_type.replace('_', '-')}-logs")
                channel = discord.utils.get(category.text_channels, name=name)
                try:
                    if channel is None:
                        channel = await guild.create_text_channel(
                            name, category=category, overwrites=overwrites,
                            reason=f"Logging auto-setup by {ctx.author}",
                        )
                        created.append(channel)
                    else:
                        reused.append(channel)
                    await db.execute(
                        "INSERT OR REPLACE INTO log_config (guild_id, log_type, channel_id, enabled) "
                        "VALUES (?,?,?,1)",
                        (guild.id, log_type, channel.id),
                    )
                except Exception:
                    failed.append(log_type)
            await db.commit()

        try:
            await wait.delete()
        except Exception:
            pass

        v = ui.LayoutView(); c = ui.Container(accent_color=DEFAULT_ACCENT)
        c.add_item(ui.TextDisplay(f"### {ES('success')} All Logging Enabled"))
        c.add_item(ui.Separator())
        lines = [f"{ES('dot')} **Category:** {category.mention}"]
        lines.append(f"{ES('dot')} **Created:** `{len(created)}` channels")
        if reused:
            lines.append(f"{ES('dot')} **Already existed (reused):** `{len(reused)}`")
        if failed:
            lines.append(f"{ES('error')} **Failed:** {', '.join(f'`{t}`' for t in failed)}")
        c.add_item(ui.TextDisplay("\n".join(lines)))
        c.add_item(ui.Separator())
        all_ch = created + reused
        c.add_item(ui.TextDisplay(
            "\n".join(f"{ES('dot')} {ch.mention}" for ch in all_ch)[:3500] or "No channels."
        ))
        c.add_item(ui.Separator(visible=False))
        c.add_item(ui.TextDisplay(
            f"Channels are private (only admins & the bot can see them).\n"
            f"Use `{ctx.prefix}log disable <type>` to turn off one log, or `{ctx.prefix}log config` to review."
        ))
        v.add_item(c)
        await ctx.send(view=v)

    @logging.command(name="setup")
    @commands.has_permissions(administrator=True)
    async def setup_cmd(self, ctx, channel: discord.TextChannel):
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT OR REPLACE INTO log_default VALUES (?,?)",
                (ctx.guild.id, channel.id),
            )
            await db.commit()
        await ctx.send(view=_ok(f"Default log channel set to {channel.mention}\nAll events will be logged there unless overridden."))

    @logging.command(name="set")
    @commands.has_permissions(administrator=True)
    async def set_cmd(self, ctx, log_type: str, channel: discord.TextChannel):
        log_type = log_type.lower()
        if log_type not in LOG_TYPES:
            await ctx.send(view=_err(f"Unknown log type. Use `{ctx.prefix}log types` to see all."))
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT OR REPLACE INTO log_config VALUES (?,?,?,1)",
                (ctx.guild.id, log_type, channel.id),
            )
            await db.commit()
        await ctx.send(view=_ok(f"`{log_type}` logs will go to {channel.mention}"))

    @logging.command(name="enable")
    @commands.has_permissions(administrator=True)
    async def enable(self, ctx, log_type: str):
        log_type = log_type.lower()
        if log_type not in LOG_TYPES:
            await ctx.send(view=_err(f"Unknown log type."))
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT OR IGNORE INTO log_config (guild_id, log_type, enabled) VALUES (?,?,1)",
                (ctx.guild.id, log_type),
            )
            await db.execute(
                "UPDATE log_config SET enabled=1 WHERE guild_id=? AND log_type=?",
                (ctx.guild.id, log_type),
            )
            await db.commit()
        await ctx.send(view=_ok(f"Enabled `{log_type}` logging."))

    @logging.command(name="disable")
    @commands.has_permissions(administrator=True)
    async def disable(self, ctx, log_type: str):
        log_type = log_type.lower()
        if log_type not in LOG_TYPES:
            await ctx.send(view=_err(f"Unknown log type."))
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT OR IGNORE INTO log_config (guild_id, log_type, enabled) VALUES (?,?,0)",
                (ctx.guild.id, log_type),
            )
            await db.execute(
                "UPDATE log_config SET enabled=0 WHERE guild_id=? AND log_type=?",
                (ctx.guild.id, log_type),
            )
            await db.commit()
        await ctx.send(view=_ok(f"Disabled `{log_type}` logging."))

    @logging.command(name="types")
    async def types(self, ctx):
        v = ui.LayoutView(); c = ui.Container(accent_color=DEFAULT_ACCENT)
        c.add_item(ui.TextDisplay(f"### {ES('log')} Available Log Types"))
        c.add_item(ui.Separator())
        lines = [f"{ES('dot')} `{t}`" for t in LOG_TYPES]
        c.add_item(ui.TextDisplay("\n".join(lines)))
        v.add_item(c); await ctx.send(view=v)

    @logging.command(name="config", aliases=["show"])
    @commands.has_permissions(administrator=True)
    async def config(self, ctx):
        async with aiosqlite.connect(str(DB_PATH)) as db:
            async with db.execute(
                "SELECT channel_id FROM log_default WHERE guild_id=?", (ctx.guild.id,)
            ) as cur:
                default_row = await cur.fetchone()

            async with db.execute(
                "SELECT log_type, channel_id, enabled FROM log_config WHERE guild_id=?",
                (ctx.guild.id,),
            ) as cur:
                rows = await cur.fetchall()

        v = ui.LayoutView(); c = ui.Container(accent_color=DEFAULT_ACCENT)
        c.add_item(ui.TextDisplay(f"### {ES('settings')} Logging Configuration"))
        c.add_item(ui.Separator())

        if default_row and default_row[0]:
            default_ch = ctx.guild.get_channel(default_row[0])
            c.add_item(ui.TextDisplay(
                f"{ES('dot')} **Default Channel:** {default_ch.mention if default_ch else '`Deleted`'}"
            ))
        else:
            c.add_item(ui.TextDisplay(f"{ES('dot')} **Default Channel:** Not set"))

        c.add_item(ui.Separator(visible=False))

        if rows:
            lines = ["**Per-type overrides:**"]
            for log_type, cid, enabled in rows:
                status = ES("success") if enabled else ES("error")
                if cid:
                    ch = ctx.guild.get_channel(cid)
                    lines.append(f"{status} `{log_type}` → {ch.mention if ch else f'`{cid}`'}")
                else:
                    lines.append(f"{status} `{log_type}` → *(uses default)*")
            c.add_item(ui.TextDisplay("\n".join(lines)))

        v.add_item(c); await ctx.send(view=v)

    @logging.command(name="reset")
    @commands.has_permissions(administrator=True)
    async def reset(self, ctx):
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("DELETE FROM log_default WHERE guild_id=?", (ctx.guild.id,))
            await db.execute("DELETE FROM log_config WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
        await ctx.send(view=_ok("Logging settings reset."))


    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.guild:
            return
        content = message.content[:1000] if message.content else "*(no content)*"
        view = _log_view("trash", "Message Deleted", [
            ("Author", f"{message.author.mention} (`{message.author.id}`)"),
            ("Channel", message.channel.mention),
            ("Content", f"```\n{content}\n```"),
        ], footer=f"Message ID: {message.id}")
        await send_log(self.bot, message.guild.id, "message_delete", view)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author.bot or not before.guild or before.content == after.content:
            return
        view = _log_view("edit", "Message Edited", [
            ("Author", f"{before.author.mention}"),
            ("Channel", before.channel.mention),
            ("Before", f"```\n{(before.content or '*(empty)*')[:500]}\n```"),
            ("After", f"```\n{(after.content or '*(empty)*')[:500]}\n```"),
            ("Jump", f"[Click here]({after.jump_url})"),
        ])
        await send_log(self.bot, before.guild.id, "message_edit", view)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        if not messages:
            return
        guild = messages[0].guild
        if not guild:
            return
        view = _log_view("trash", "Bulk Messages Deleted", [
            ("Count", str(len(messages))),
            ("Channel", messages[0].channel.mention),
        ])
        await send_log(self.bot, guild.id, "bulk_delete", view)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        view = _log_view("join", "Member Joined", [
            ("User", f"{member.mention} (`{member.id}`)"),
            ("Account Created", f"<t:{int(member.created_at.timestamp())}:R>"),
            ("Member Count", str(member.guild.member_count)),
        ])
        await send_log(self.bot, member.guild.id, "member_join", view)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone") or "None"
        view = _log_view("leave", "Member Left", [
            ("User", f"{member} (`{member.id}`)"),
            ("Joined", f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown"),
            ("Roles", roles[:1000]),
        ])
        await send_log(self.bot, member.guild.id, "member_leave", view)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            fields = [("User", after.mention)]
            if added:
                fields.append(("Added Roles", ", ".join(added)))
            if removed:
                fields.append(("Removed Roles", ", ".join(removed)))
            view = _log_view("role", "Member Roles Updated", fields)
            await send_log(self.bot, after.guild.id, "member_update", view)

        if before.nick != after.nick:
            view = _log_view("user", "Nickname Changed", [
                ("User", after.mention),
                ("Before", before.nick or "*(none)*"),
                ("After", after.nick or "*(none)*"),
            ])
            await send_log(self.bot, after.guild.id, "member_update", view)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        view = _log_view("ban", "Member Banned", [
            ("User", f"{user} (`{user.id}`)"),
        ])
        await send_log(self.bot, guild.id, "member_ban", view)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        view = _log_view("user", "Member Unbanned", [
            ("User", f"{user} (`{user.id}`)"),
        ])
        await send_log(self.bot, guild.id, "member_unban", view)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        view = _log_view("role", "Role Created", [
            ("Name", role.name),
            ("ID", f"`{role.id}`"),
            ("Color", f"`{role.color}`"),
            ("Mentionable", str(role.mentionable)),
        ])
        await send_log(self.bot, role.guild.id, "role_create", view)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        view = _log_view("trash", "Role Deleted", [
            ("Name", role.name),
            ("ID", f"`{role.id}`"),
        ])
        await send_log(self.bot, role.guild.id, "role_delete", view)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`"))
        if before.color != after.color:
            changes.append(("Color", f"`{before.color}` → `{after.color}`"))
        if before.permissions != after.permissions:
            changes.append(("Permissions", "Updated"))
        if not changes:
            return
        view = _log_view("role", "Role Updated", [("Role", after.mention)] + changes)
        await send_log(self.bot, after.guild.id, "role_update", view)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        view = _log_view("channel", "Channel Created", [
            ("Name", channel.name),
            ("Type", str(channel.type)),
            ("ID", f"`{channel.id}`"),
        ])
        await send_log(self.bot, channel.guild.id, "channel_create", view)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        view = _log_view("trash", "Channel Deleted", [
            ("Name", channel.name),
            ("Type", str(channel.type)),
            ("ID", f"`{channel.id}`"),
        ])
        await send_log(self.bot, channel.guild.id, "channel_delete", view)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`"))
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            if before.topic != after.topic:
                changes.append(("Topic", f"`{before.topic or 'none'}` → `{after.topic or 'none'}`"))
            if before.slowmode_delay != after.slowmode_delay:
                changes.append(("Slowmode", f"`{before.slowmode_delay}s` → `{after.slowmode_delay}s`"))
        if not changes:
            return
        view = _log_view("channel", "Channel Updated", [("Channel", after.mention)] + changes)
        await send_log(self.bot, after.guild.id, "channel_update", view)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        if before.channel is None and after.channel is not None:
            view = _log_view("join", "Voice Channel Joined", [
                ("Member", member.mention),
                ("Channel", after.channel.mention),
            ])
        elif before.channel is not None and after.channel is None:
            view = _log_view("leave", "Voice Channel Left", [
                ("Member", member.mention),
                ("Channel", before.channel.mention),
            ])
        else:
            view = _log_view("user", "Voice Channel Moved", [
                ("Member", member.mention),
                ("From", before.channel.mention),
                ("To", after.channel.mention),
            ])
        await send_log(self.bot, member.guild.id, "voice", view)


async def setup(bot):
    await bot.add_cog(Logging(bot))
