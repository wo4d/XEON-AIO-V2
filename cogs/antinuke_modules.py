import asyncio
import time
from collections import OrderedDict

import discord
from discord.ext import commands
from discord.ui import (
    LayoutView, Container, Section, TextDisplay,
    Separator, Thumbnail
)

from .antinuke_core import (
    ACCENT_COLOR, OWNER_IDS, E, DBManager,
    is_whitelisted, is_filter_enabled,
    get_punishment, get_log_channel, SECURITY_ROLE_NAMES
)

A = discord.AuditLogAction

ACTION_MAP = {
    A.ban: ("ban", "Banned a member"),
    A.kick: ("kick", "Kicked a member"),
    A.member_prune: ("prune", "Pruned members"),
    A.role_create: ("rlcr", "Created a role"),
    A.role_delete: ("rldl", "Deleted a role"),
    A.role_update: ("rlup", "Updated a role"),
    A.channel_create: ("chcr", "Created a channel"),
    A.channel_delete: ("chdl", "Deleted a channel"),
    A.channel_update: ("chup", "Updated a channel"),
    A.overwrite_create: ("chup", "Created a permission overwrite"),
    A.overwrite_update: ("chup", "Updated a permission overwrite"),
    A.overwrite_delete: ("chup", "Deleted a permission overwrite"),
    A.webhook_create: ("webhookcr", "Created a webhook"),
    A.webhook_delete: ("webhookdl", "Deleted a webhook"),
    A.webhook_update: ("webhookup", "Updated a webhook"),
    A.bot_add: ("botadd", "Added a bot"),
    A.guild_update: ("serverup", "Updated server settings"),
    A.member_role_update: ("memup", "Gave a dangerous role"),
}

DANGEROUS = discord.Permissions(
    administrator=True, ban_members=True, kick_members=True,
    manage_guild=True, manage_roles=True, manage_channels=True,
    manage_webhooks=True, mention_everyone=True,
    manage_messages=True, manage_threads=True, moderate_members=True,
)

PUNISH_COOLDOWN = 20.0
SNAPSHOT_TTL = 20.0


def is_dangerous(permissions: discord.Permissions) -> bool:
    return bool(permissions.value & DANGEROUS.value)


def _live_overwrites(guild: discord.Guild, overwrites: dict) -> dict:
    return {
        target: overwrite for target, overwrite in overwrites.items()
        if guild.get_role(target.id) or guild.get_member(target.id)
    }


def _channel_state(channel) -> dict:
    state = {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        "position": channel.position,
        "category_id": channel.category_id,
        "overwrites": dict(channel.overwrites),
    }
    if isinstance(channel, discord.TextChannel):
        state.update(
            topic=channel.topic, nsfw=channel.nsfw,
            slowmode_delay=channel.slowmode_delay,
        )
    elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        state.update(bitrate=channel.bitrate, user_limit=channel.user_limit)
    return state


def _role_state(role: discord.Role) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions,
        "colour": role.colour,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
    }


async def execute_punishment(
    guild: discord.Guild,
    offender,
    punishment: str,
    reason: str
) -> bool:
    tag = f"[Antinuke] {reason}"
    member = offender if isinstance(offender, discord.Member) else guild.get_member(offender.id)
    try:
        if punishment == "ban":
            await guild.ban(offender, reason=tag, delete_message_days=0)
            return True
        if punishment == "kick":
            await guild.kick(offender, reason=tag)
            return True
    except (discord.Forbidden, discord.HTTPException):
        pass
    if member is None:
        return False
    return await strip_staff(guild, member, tag)


async def strip_staff(guild: discord.Guild, member: discord.Member, reason: str) -> bool:
    top = guild.me.top_role
    removable = [
        r for r in member.roles
        if r != guild.default_role and not r.managed and r < top and is_dangerous(r.permissions)
    ]
    if not removable:
        return False
    try:
        await member.remove_roles(*removable, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def send_log(bot, guild, action: str, offender, punishment: str, recovered: str):
    db = await DBManager().connect()
    channel = await get_log_channel(db, guild)
    if not channel:
        return

    mention = getattr(offender, "mention", str(offender))
    avatar = (
        offender.display_avatar.url
        if hasattr(offender, "display_avatar") else bot.user.display_avatar.url
    )

    body = (
        f"{E('ARROW')} **Action:** `{action}`\n"
        f"{E('ARROW')} **Offender:** {mention} (`{offender.id}`)\n"
        f"{E('ARROW')} **Punishment:** `{punishment}`\n"
        f"{E('ARROW')} **Recovery:** {recovered}\n"
        f"{E('ARROW')} **Time:** <t:{int(time.time())}:F>"
    )

    section = Section(
        TextDisplay(f"### {E('SHIELD')} Antinuke Triggered\n{body}"),
        accessory=Thumbnail(
            media=discord.UnfurledMediaItem(url=avatar),
            description="Offender",
        ),
    )
    container = Container(
        section,
        accent_color=ACCENT_COLOR,
    )
    view = LayoutView(timeout=None)
    view.add_item(container)
    try:
        await channel.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def is_exempt(bot, db, guild: discord.Guild, user_id: int, filter_key: str, role_ids: list[int]) -> bool:
    if user_id == bot.user.id or user_id in OWNER_IDS or user_id == guild.owner_id:
        return True
    async with db.execute(
        "SELECT 1 FROM extraowners WHERE guild_id = ? AND owner_id = ?",
        (guild.id, user_id),
    ) as cur:
        if await cur.fetchone():
            return True
    return await is_whitelisted(db, guild.id, user_id, filter_key, role_ids)


class AntiNukeModules(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.roles: dict[int, dict[int, dict]] = {}
        self.channels: dict[int, dict[int, dict]] = {}
        self.guild_names: dict[int, str] = {}
        self.prior: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self.suppressed: dict[tuple, float] = {}
        self.punished: dict[tuple, float] = {}
        self.tasks: set[asyncio.Task] = set()

    async def cog_load(self):
        await DBManager().connect()
        if self.bot.is_ready():
            for guild in self.bot.guilds:
                self._snapshot_guild(guild)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _snapshot_guild(self, guild: discord.Guild):
        self.roles[guild.id] = {r.id: _role_state(r) for r in guild.roles}
        self.channels[guild.id] = {c.id: _channel_state(c) for c in guild.channels}
        self.guild_names[guild.id] = guild.name

    def _remember_prior(self, kind: str, entity_id: int, state: dict):
        now = time.time()
        self.prior[(kind, entity_id)] = (now, state)
        while len(self.prior) > 500:
            self.prior.popitem(last=False)

    def _prior_state(self, kind: str, entity_id: int, fallback: dict | None):
        hit = self.prior.get((kind, entity_id))
        if hit and time.time() - hit[0] <= SNAPSHOT_TTL:
            return hit[1]
        return fallback

    def _suppress(self, kind: str, entity_id: int):
        self.suppressed[(kind, entity_id)] = time.time()

    def _is_suppressed(self, kind: str, entity_id: int) -> bool:
        ts = self.suppressed.get((kind, entity_id))
        if ts is None:
            return False
        if time.time() - ts > SNAPSHOT_TTL:
            self.suppressed.pop((kind, entity_id), None)
            return False
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            self._snapshot_guild(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self._snapshot_guild(guild)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        self.roles.setdefault(role.guild.id, {})[role.id] = _role_state(role)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        store = self.roles.setdefault(after.guild.id, {})
        self._remember_prior("role", after.id, store.get(after.id) or _role_state(before))
        if self._is_suppressed("role", after.id):
            return
        store[after.id] = _role_state(after)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        store = self.roles.setdefault(role.guild.id, {})
        self._remember_prior("role", role.id, store.get(role.id) or _role_state(role))
        store.pop(role.id, None)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        self.channels.setdefault(channel.guild.id, {})[channel.id] = _channel_state(channel)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        store = self.channels.setdefault(after.guild.id, {})
        self._remember_prior("channel", after.id, store.get(after.id) or _channel_state(before))
        if self._is_suppressed("channel", after.id):
            return
        store[after.id] = _channel_state(after)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        store = self.channels.setdefault(channel.guild.id, {})
        self._remember_prior("channel", channel.id, store.get(channel.id) or _channel_state(channel))
        store.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        if self._is_suppressed("guild", after.id):
            return
        self.guild_names.setdefault(after.id, before.name)
        self._remember_prior("guild", after.id, {"name": self.guild_names[after.id]})
        self.guild_names[after.id] = after.name

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        mapped = ACTION_MAP.get(entry.action)
        guild = entry.guild
        if not mapped or guild is None or entry.user_id is None:
            return
        if entry.user_id == self.bot.user.id:
            return
        if entry.action in (A.ban, A.kick, A.member_prune):
            self._spawn(self._mass_action_check(entry))
        if entry.action in (A.role_delete, A.role_update, A.member_role_update):
            if await self._security_role_guard(entry):
                return
        self._spawn(self._handle(entry, *mapped))

    async def _security_role_guard(self, entry: discord.AuditLogEntry) -> bool:
        guild = entry.guild
        names = SECURITY_ROLE_NAMES
        reason_text = None
        recover_key = None

        db = await DBManager().connect()
        async with db.execute(
            "SELECT 1 FROM antinuke WHERE guild_id = ? AND status = 1", (guild.id,)
        ) as cur:
            if not await cur.fetchone():
                return False

        if entry.action == A.role_delete:
            if getattr(entry.before, "name", None) in names:
                reason_text, recover_key = "Deleted a security role", "rldl"
        elif entry.action == A.role_update:
            role = guild.get_role(entry.target.id) if entry.target else None
            old_name = getattr(entry.before, "name", None)
            if (role and role.name in names) or old_name in names:
                changed = {k for k, _ in entry.before} | {k for k, _ in entry.after}
                if changed & {"name", "permissions", "colour", "hoist", "mentionable"}:
                    reason_text, recover_key = "Edited a security role", "rlup"
        elif entry.action == A.member_role_update:
            if entry.target and entry.target.id == self.bot.user.id:
                removed = getattr(entry.before, "roles", None) or []
                lost = [
                    guild.get_role(ref.id) for ref in removed
                    if guild.get_role(ref.id) and guild.get_role(ref.id).name in names
                ]
                if lost:
                    reason_text = "Removed a security role from the bot"
            elif entry.target:
                added = getattr(entry.after, "roles", None) or []
                given = [
                    guild.get_role(ref.id) for ref in added
                    if guild.get_role(ref.id) and guild.get_role(ref.id).name in names
                ]
                if given:
                    member = guild.get_member(entry.target.id)
                    if member:
                        try:
                            await member.remove_roles(
                                *given, reason="[Antinuke] Security roles are bot-only"
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                    return True

        if not reason_text:
            return False

        if (
            entry.user_id == self.bot.user.id
            or entry.user_id in OWNER_IDS
            or entry.user_id == guild.owner_id
        ):
            return True

        offender = guild.get_member(entry.user_id) or entry.user
        recovered = "Nothing to restore"
        try:
            if recover_key:
                recovered = await self._recover(entry, recover_key)
            else:
                await guild.me.add_roles(*lost, reason="[Antinuke Recovery]")
                recovered = "Security role given back to the bot"
        except (discord.Forbidden, discord.HTTPException):
            recovered = "Recovery failed"

        await execute_punishment(guild, offender, "ban", reason_text)
        await send_log(self.bot, guild, reason_text, offender, "ban", recovered)
        await self._dm_owner(guild, offender, reason_text, recovered)
        return True

    async def _dm_owner(self, guild: discord.Guild, offender, action: str, recovered: str):
        owner = guild.owner
        if owner is None:
            try:
                owner = await self.bot.fetch_user(guild.owner_id)
            except (discord.NotFound, discord.HTTPException):
                return
        embed = discord.Embed(
            title="Security Role Tampering Detected",
            description=f"Someone tried to tamper with my security roles in **{guild.name}**.",
            color=0xF4C2C2,
        )
        embed.add_field(
            name="Offender",
            value=f"{getattr(offender, 'mention', offender)} (`{offender.id}`)",
            inline=False,
        )
        embed.add_field(name="Action", value=action, inline=True)
        embed.add_field(name="Punishment", value="Banned", inline=True)
        embed.add_field(name="Recovery", value=recovered, inline=False)
        embed.add_field(name="Time", value=f"<t:{int(time.time())}:F>", inline=False)
        try:
            await owner.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    MASS_LIMIT = 20
    MASS_WINDOW = 6 * 60 * 60
    MASS_COOLDOWN = 600
    _mass_locks: dict = {}
    _mass_until: dict = {}
    _mass_ready = False

    async def _mass_action_check(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        user_id = entry.user_id
        if guild is None or user_id is None:
            return
        if (
            user_id == self.bot.user.id
            or user_id in OWNER_IDS
            or user_id == guild.owner_id
        ):
            return

        if entry.action == A.member_prune:
            try:
                weight = int(getattr(entry.extra, "members_removed", 0) or 0)
            except (TypeError, ValueError):
                weight = 0
        else:
            weight = 1
        if weight <= 0:
            return

        lock = self._mass_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            db = await DBManager().connect()
            async with db.execute(
                "SELECT 1 FROM antinuke WHERE guild_id = ? AND status = 1", (guild.id,)
            ) as cur:
                if not await cur.fetchone():
                    return
            if not self._mass_ready:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS antinuke_mass "
                    "(guild_id INTEGER, user_id INTEGER, ts REAL, weight INTEGER)"
                )
                self._mass_ready = True
            now = time.time()
            await db.execute("DELETE FROM antinuke_mass WHERE ts < ?", (now - self.MASS_WINDOW,))
            await db.execute(
                "INSERT INTO antinuke_mass (guild_id, user_id, ts, weight) VALUES (?, ?, ?, ?)",
                (guild.id, user_id, now, weight),
            )
            async with db.execute(
                "SELECT COALESCE(SUM(weight), 0) FROM antinuke_mass "
                "WHERE guild_id = ? AND user_id = ? AND ts > ?",
                (guild.id, user_id, now - self.MASS_WINDOW),
            ) as cur:
                total = (await cur.fetchone())[0]
            await db.commit()

            if total <= self.MASS_LIMIT:
                return
            if now < self._mass_until.get(guild.id, 0):
                return
            self._mass_until[guild.id] = now + self.MASS_COOLDOWN
            await db.execute(
                "DELETE FROM antinuke_mass WHERE guild_id = ? AND user_id = ?",
                (guild.id, user_id),
            )
            await db.commit()

        await self._mass_lockdown(guild, user_id, total)

    async def _mass_lockdown(self, guild: discord.Guild, user_id: int, total: int):
        keep = {r.id for r in guild.roles if r.name in SECURITY_ROLE_NAMES}
        me_top = guild.me.top_role
        member = guild.get_member(user_id)
        member_role_ids = {r.id for r in member.roles} if member else set()

        roles = sorted(
            guild.roles, key=lambda r: 0 if r.id in member_role_ids else 1
        )
        stripped = []
        blocked = 0
        for role in roles:
            if role.id in keep or role == me_top:
                continue
            perms = role.permissions
            if not (perms.administrator or perms.ban_members or perms.kick_members):
                continue
            if role >= me_top:
                blocked += 1
                continue
            new_perms = discord.Permissions(perms.value)
            new_perms.update(administrator=False, ban_members=False, kick_members=False)
            try:
                await role.edit(
                    permissions=new_perms,
                    reason="[Antinuke] Mass ban/kick/prune detected",
                )
                stripped.append(role.name)
            except (discord.Forbidden, discord.HTTPException):
                blocked += 1
            await asyncio.sleep(0.5)

        try:
            offender = member or await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            offender = discord.Object(id=user_id)

        summary = f"{len(stripped)} role(s) changed"
        if blocked:
            summary += f", {blocked} could not be changed"
        await send_log(
            self.bot, guild,
            f"Mass ban/kick/prune ({total} in 6h)", offender,
            "Admin/Ban/Kick removed from roles", summary,
        )
        await self._dm_owner_mass(guild, offender, total, stripped, blocked)

    async def _dm_owner_mass(self, guild: discord.Guild, offender, total: int, stripped: list, blocked: int):
        owner = guild.owner
        if owner is None:
            try:
                owner = await self.bot.fetch_user(guild.owner_id)
            except (discord.NotFound, discord.HTTPException):
                return
        names = ", ".join(stripped) or "None"
        if len(names) > 900:
            names = names[:900] + "..."
        embed = discord.Embed(
            title="Mass Ban/Kick/Prune Detected",
            description=(
                f"More than {self.MASS_LIMIT} bans, kicks or prunes in 6 hours in **{guild.name}**. "
                "Administrator, Ban Members and Kick Members were removed from every role "
                "except my security roles."
            ),
            color=0xF4C2C2,
        )
        embed.add_field(
            name="Offender",
            value=f"{getattr(offender, 'mention', f'<@{offender.id}>')} (`{offender.id}`)",
            inline=False,
        )
        embed.add_field(name="Actions in 6h", value=str(total), inline=True)
        embed.add_field(name="Roles changed", value=str(len(stripped)), inline=True)
        embed.add_field(name="Roles changed (names)", value=names, inline=False)
        if blocked:
            embed.add_field(
                name="Could not change",
                value=f"{blocked} role(s) above my top role or not editable",
                inline=False,
            )
        embed.add_field(name="Time", value=f"<t:{int(time.time())}:F>", inline=False)
        embed.set_footer(text="Restore permissions manually in Server Settings > Roles.")
        try:
            await owner.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _handle(self, entry: discord.AuditLogEntry, key: str, text: str):
        guild = entry.guild
        db = await DBManager().connect()

        if not await is_filter_enabled(db, guild.id, key):
            return

        actor = guild.get_member(entry.user_id)
        role_ids = [r.id for r in actor.roles] if actor else []

        if key == "memup" and not self._dangerous_roles_added(guild, entry):
            return

        if await is_exempt(self.bot, db, guild, entry.user_id, key, role_ids):
            return

        offender = actor or entry.user
        punishment = await get_punishment(db, guild.id, key)

        recovery_task = self._spawn(self._recover(entry, key))

        punish_key = (guild.id, entry.user_id)
        last = self.punished.get(punish_key, 0.0)
        fresh = time.time() - last > PUNISH_COOLDOWN
        if fresh:
            self.punished[punish_key] = time.time()
            await execute_punishment(guild, offender, punishment, text)

        recovered = await recovery_task
        if fresh:
            await send_log(self.bot, guild, text, offender, punishment, recovered)

        if len(self.punished) > 200:
            cutoff = time.time() - PUNISH_COOLDOWN
            self.punished = {k: v for k, v in self.punished.items() if v > cutoff}

    def _dangerous_roles_added(self, guild: discord.Guild, entry: discord.AuditLogEntry) -> list[discord.Role]:
        found = []
        for role_ref in getattr(entry.after, "roles", None) or []:
            role = guild.get_role(role_ref.id)
            if role and (is_dangerous(role.permissions) or role.name in SECURITY_ROLE_NAMES):
                found.append(role)
        return found

    async def _recover(self, entry: discord.AuditLogEntry, key: str) -> str:
        guild = entry.guild
        reason = "[Antinuke Recovery]"
        try:
            if entry.action == A.channel_create and entry.target:
                channel = guild.get_channel(entry.target.id)
                if channel:
                    await channel.delete(reason=reason)
                    return "Created channel removed"

            elif entry.action == A.channel_delete and entry.target:
                state = self._prior_state(
                    "channel", entry.target.id,
                    self.channels.get(guild.id, {}).get(entry.target.id),
                )
                if state:
                    await self._restore_channel(guild, state, reason)
                    return "Deleted channel restored"

            elif entry.action in (A.channel_update, A.overwrite_create, A.overwrite_update, A.overwrite_delete) and entry.target:
                channel = guild.get_channel(entry.target.id)
                state = self._prior_state(
                    "channel", entry.target.id,
                    self.channels.get(guild.id, {}).get(entry.target.id),
                )
                if channel and state:
                    self._suppress("channel", channel.id)
                    await channel.edit(
                        name=state["name"],
                        overwrites=_live_overwrites(guild, state["overwrites"]),
                        reason=reason,
                    )
                    return "Channel changes reverted"

            elif entry.action == A.role_create and entry.target:
                role = guild.get_role(entry.target.id)
                if role:
                    await role.delete(reason=reason)
                    return "Created role removed"

            elif entry.action == A.role_delete and entry.target:
                state = self._prior_state(
                    "role", entry.target.id,
                    self.roles.get(guild.id, {}).get(entry.target.id),
                )
                if state:
                    await self._restore_role(guild, state, reason)
                    return "Deleted role restored"

            elif entry.action == A.role_update and entry.target:
                role = guild.get_role(entry.target.id)
                state = self._prior_state(
                    "role", entry.target.id,
                    self.roles.get(guild.id, {}).get(entry.target.id),
                )
                if role and state and role < guild.me.top_role:
                    self._suppress("role", role.id)
                    await role.edit(
                        name=state["name"],
                        permissions=state["permissions"],
                        colour=state["colour"],
                        hoist=state["hoist"],
                        mentionable=state["mentionable"],
                        reason=reason,
                    )
                    return "Role changes reverted"

            elif entry.action == A.ban and entry.target:
                await guild.unban(discord.Object(id=entry.target.id), reason=reason)
                return "Member unbanned"

            elif entry.action == A.bot_add and entry.target:
                await guild.kick(discord.Object(id=entry.target.id), reason=reason)
                return "Added bot removed"

            elif entry.action == A.webhook_create and entry.target:
                webhook = await self.bot.fetch_webhook(entry.target.id)
                await webhook.delete(reason=reason)
                return "Created webhook removed"

            elif entry.action == A.guild_update:
                state = self._prior_state("guild", guild.id, None)
                if state and state["name"] != guild.name:
                    self._suppress("guild", guild.id)
                    await guild.edit(name=state["name"], reason=reason)
                    return "Server name reverted"

            elif entry.action == A.member_role_update and entry.target:
                target = guild.get_member(entry.target.id)
                roles = [r for r in self._dangerous_roles_added(guild, entry) if r < guild.me.top_role]
                if target and roles:
                    await target.remove_roles(*roles, reason=reason)
                    return "Dangerous roles removed"
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return "Recovery failed"
        return "Nothing to recover"

    async def _restore_channel(self, guild: discord.Guild, state: dict, reason: str):
        category = guild.get_channel(state["category_id"]) if state["category_id"] else None
        kind = state["type"]
        kwargs = {
            "name": state["name"],
            "overwrites": _live_overwrites(guild, state["overwrites"]),
            "category": category,
            "reason": reason,
        }
        if kind == discord.ChannelType.text or kind == discord.ChannelType.news:
            channel = await guild.create_text_channel(
                topic=state.get("topic"), nsfw=state.get("nsfw", False),
                slowmode_delay=state.get("slowmode_delay", 0), **kwargs,
            )
        elif kind == discord.ChannelType.voice:
            channel = await guild.create_voice_channel(
                bitrate=min(state.get("bitrate", 64000), guild.bitrate_limit),
                user_limit=state.get("user_limit", 0), **kwargs,
            )
        elif kind == discord.ChannelType.stage_voice:
            channel = await guild.create_stage_channel(**kwargs)
        elif kind == discord.ChannelType.category:
            channel = await guild.create_category(
                name=state["name"],
                overwrites=_live_overwrites(guild, state["overwrites"]),
                reason=reason,
            )
        else:
            return
        try:
            await channel.edit(position=state["position"], reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _restore_role(self, guild: discord.Guild, state: dict, reason: str):
        role = await guild.create_role(
            name=state["name"],
            permissions=state["permissions"],
            colour=state["colour"],
            hoist=state["hoist"],
            mentionable=state["mentionable"],
            reason=reason,
        )
        if state["name"] in SECURITY_ROLE_NAMES:
            try:
                await guild.me.add_roles(role, reason=reason)
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            await role.edit(position=min(state["position"], guild.me.top_role.position - 1), reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not message.mention_everyone:
            return
        self._spawn(self._everyone_ping(message))

    async def _everyone_ping(self, message: discord.Message):
        guild = message.guild
        db = await DBManager().connect()
        if not await is_filter_enabled(db, guild.id, "meneve"):
            return
        author = message.author
        role_ids = [r.id for r in getattr(author, "roles", [])]
        if await is_exempt(self.bot, db, guild, author.id, "meneve", role_ids):
            return

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        punish_key = (guild.id, author.id)
        if time.time() - self.punished.get(punish_key, 0.0) <= PUNISH_COOLDOWN:
            return
        self.punished[punish_key] = time.time()

        punishment = await get_punishment(db, guild.id, "meneve")
        await execute_punishment(guild, author, punishment, "Mass @everyone / @here mention")
        await send_log(
            self.bot, guild, "Mass @everyone / @here mention", author,
            punishment, "Offending message deleted",
        )


async def setup(bot):
    await bot.add_cog(AntiNukeModules(bot))
