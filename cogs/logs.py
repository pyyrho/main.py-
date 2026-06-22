"""cogs/logs.py — Logs de auditoria em Components V2."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db.database import get_guild_config, upsert_guild_config
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.logs")

_COLORS = {
    "join": 0x57F287,
    "leave": 0xED4245,
    "ban": 0xED4245,
    "unban": 0x57F287,
    "msg_edit": 0xFEE75C,
    "msg_delete": 0xED4245,
    "role": 0x5865F2,
    "channel": 0x9B59B6,
    "nick": 0xFEE75C,
}


class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cfg = await get_guild_config(guild.id)
        channel_id = cfg.get("logs_channel") or cfg.get("log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _send(
        self,
        guild: discord.Guild,
        title: str,
        description: str = "",
        *,
        fields: list[tuple[str, object]] | tuple[tuple[str, object], ...] = (),
        kind: str = "channel",
        thumbnail: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> None:
        channel = await self._log_channel(guild)
        if not channel:
            return
        try:
            await channel.send(
                view=card(
                    title,
                    description,
                    fields=fields,
                    thumbnail=thumbnail,
                    footer=footer,
                    accent=_COLORS.get(kind, WHITE),
                    timeout=None,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Falha ao enviar log no servidor %s", guild.id)

    async def _audit_actor(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int,
    ) -> Optional[discord.User | discord.Member]:
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None
        try:
            now = discord.utils.utcnow()
            async for entry in guild.audit_logs(limit=6, action=action):
                if not entry.target or getattr(entry.target, "id", None) != target_id:
                    continue
                if abs((now - entry.created_at).total_seconds()) <= 8:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._send(
            member.guild,
            "📥 Membro entrou",
            f"{member.mention} entrou no servidor.",
            fields=[
                ("ID", f"`{member.id}`"),
                ("Conta criada", discord.utils.format_dt(member.created_at, "R")),
                ("Total de membros", f"`{member.guild.member_count}`"),
            ],
            kind="join",
            thumbnail=member.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        roles = [role.mention for role in reversed(member.roles) if role != member.guild.default_role]
        await self._send(
            member.guild,
            "📤 Membro saiu",
            f"**{clip(member, 100)}** (`{member.id}`) saiu do servidor.",
            fields=[("Cargos", " ".join(roles[:15]) or "Nenhum")],
            kind="leave",
            thumbnail=member.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        actor = await self._audit_actor(guild, discord.AuditLogAction.ban, user.id)
        await self._send(
            guild,
            "🔨 Membro banido",
            f"**{clip(user, 100)}** (`{user.id}`) foi banido.",
            fields=[("Responsável", actor.mention if actor else "Não identificado")],
            kind="ban",
            thumbnail=user.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        actor = await self._audit_actor(guild, discord.AuditLogAction.unban, user.id)
        await self._send(
            guild,
            "✅ Membro desbanido",
            f"**{clip(user, 100)}** (`{user.id}`) foi desbanido.",
            fields=[("Responsável", actor.mention if actor else "Não identificado")],
            kind="unban",
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not before.guild or before.author.bot or before.content == after.content:
            return
        await self._send(
            before.guild,
            "✏️ Mensagem editada",
            f"Mensagem de {before.author.mention} em {before.channel.mention}.",
            fields=[
                ("Antes", clip(before.content or "(vazio)", 900)),
                ("Depois", clip(after.content or "(vazio)", 900)),
                ("Acesso", f"[Ir para a mensagem]({after.jump_url})"),
            ],
            kind="msg_edit",
            thumbnail=before.author.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        fields: list[tuple[str, object]] = [
            ("Canal", message.channel.mention),
            ("Autor", f"{message.author.mention} (`{message.author.id}`)"),
        ]
        if message.content:
            fields.append(("Conteúdo", clip(message.content, 1200)))
        if message.attachments:
            fields.append(
                ("Anexos", "\n".join(f"[{clip(item.filename, 100)}]({item.url})" for item in message.attachments[:10]))
            )
        if message.stickers:
            fields.append(("Stickers", ", ".join(clip(sticker.name, 60) for sticker in message.stickers)))
        await self._send(
            message.guild,
            "🗑️ Mensagem deletada",
            fields=fields,
            kind="msg_delete",
            thumbnail=message.author.display_avatar.url,
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or not messages[0].guild:
            return
        guild = messages[0].guild
        channel = messages[0].channel
        users = {message.author.id for message in messages if not message.author.bot}
        await self._send(
            guild,
            "🧹 Mensagens apagadas em massa",
            f"Foram removidas **{len(messages)}** mensagens em {channel.mention}.",
            fields=[("Autores envolvidos", f"`{len(users)}`")],
            kind="msg_delete",
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.member_update, after.id)
            await self._send(
                after.guild,
                "📝 Apelido alterado",
                f"O apelido de {after.mention} foi alterado.",
                fields=[
                    ("Antes", before.nick or before.name),
                    ("Depois", after.nick or after.name),
                    ("Responsável", actor.mention if actor else "O próprio membro ou não identificado"),
                ],
                kind="nick",
                thumbnail=after.display_avatar.url,
            )

        added = [role for role in after.roles if role not in before.roles]
        removed = [role for role in before.roles if role not in after.roles]
        if added or removed:
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
            fields = [("Membro", after.mention)]
            if added:
                fields.append(("Adicionados", " ".join(role.mention for role in added[:15])))
            if removed:
                fields.append(("Removidos", " ".join(role.mention for role in removed[:15])))
            fields.append(("Responsável", actor.mention if actor else "Não identificado"))
            await self._send(after.guild, "🏷️ Cargos atualizados", fields=fields, kind="role")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        await self._send(
            channel.guild,
            "📁 Canal criado",
            f"**{clip(channel.name, 100)}** foi criado.",
            fields=[
                ("Tipo", str(channel.type).replace("ChannelType.", "")),
                ("Responsável", actor.mention if actor else "Não identificado"),
            ],
            kind="channel",
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        await self._send(
            channel.guild,
            "🗑️ Canal deletado",
            f"**{clip(channel.name, 100)}** foi removido.",
            fields=[
                ("Tipo", str(channel.type).replace("ChannelType.", "")),
                ("Responsável", actor.mention if actor else "Não identificado"),
            ],
            kind="channel",
        )

    logs_group = app_commands.Group(
        name="logs",
        description="Sistema de logs do servidor",
        default_permissions=discord.Permissions(administrator=True),
    )

    @logs_group.command(name="setup", description="Define o canal de logs")
    async def logs_setup(self, inter: discord.Interaction, canal: discord.TextChannel) -> None:
        me = inter.guild.me
        permissions = canal.permissions_for(me) if me else None
        if not permissions or not permissions.send_messages:
            await inter.response.send_message(view=card("Sem permissão", f"Não consigo enviar mensagens em {canal.mention}."), ephemeral=True)
            return
        await upsert_guild_config(inter.guild_id, logs_channel=canal.id, log_channel=canal.id)
        await inter.response.send_message(
            view=card(
                "Logs configurados",
                f"Os eventos serão registrados em {canal.mention}.",
                fields=[
                    ("Eventos", "Entradas, saídas, bans, mensagens, cargos, apelidos e canais"),
                    ("Auditoria", "O responsável é exibido quando o bot possui **Ver Registro de Auditoria**"),
                ],
            ),
            ephemeral=True,
        )

    @logs_group.command(name="desativar", description="Desativa o sistema de logs")
    async def logs_off(self, inter: discord.Interaction) -> None:
        await upsert_guild_config(inter.guild_id, logs_channel=None, log_channel=None)
        await inter.response.send_message(view=card("Logs desativados", "Nenhum evento novo será registrado."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logs(bot))
