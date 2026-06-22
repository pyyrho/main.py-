"""cogs/moderacao.py — Moderação segura com avisos persistentes e Components V2."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db import database as db
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.mod")
MAX_TIMEOUT_MINUTES = 40_320


class Moderacao(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _log(
        self,
        guild: discord.Guild,
        title: str,
        description: str = "",
        *,
        fields: list[tuple[str, object]] | tuple[tuple[str, object], ...] = (),
    ) -> None:
        cfg = await db.get_guild_config(guild.id)
        channel_id = cfg.get("log_channel") or cfg.get("logs_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(
                view=card(title, description, fields=fields, accent=WHITE, timeout=None),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Falha ao enviar log de moderação no servidor %s", guild.id)

    def _hierarchy_error(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
    ) -> Optional[str]:
        me = guild.me
        if target.id == actor.id:
            return "Você não pode aplicar esta ação em si mesmo."
        if target.id == guild.owner_id:
            return "O dono do servidor não pode ser moderado."
        if me and target.id == me.id:
            return "Não posso aplicar esta ação em mim mesmo."
        if not me:
            return "Não consegui verificar meu cargo no servidor."
        if target.top_role >= me.top_role:
            return "Meu cargo precisa ficar acima do cargo do membro."
        if actor.id != guild.owner_id and target.top_role >= actor.top_role:
            return "Seu cargo precisa ficar acima do cargo do membro."
        return None

    async def _notify_member(self, member: discord.Member, text: str) -> bool:
        try:
            await member.send(text)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    mod_group = app_commands.Group(
        name="mod",
        description="Ferramentas de moderação",
        default_permissions=discord.Permissions(moderate_members=True),
    )

    @mod_group.command(name="ban", description="Bane um membro do servidor")
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        motivo: app_commands.Range[str, 1, 500] = "Sem motivo",
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        await inter.response.defer(ephemeral=True)
        error = self._hierarchy_error(inter.guild, inter.user, membro)
        if error:
            await inter.followup.send(view=card("Hierarquia", error), ephemeral=True)
            return
        dm_sent = await self._notify_member(
            membro,
            f"Você foi banido de **{inter.guild.name}**.\nMotivo: {motivo}",
        )
        try:
            await membro.ban(
                reason=f"{inter.user} ({inter.user.id}) • {motivo}",
                delete_message_seconds=delete_days * 86400,
            )
        except discord.Forbidden:
            await inter.followup.send(view=card("Sem permissão", "Não consegui banir este membro."), ephemeral=True)
            return
        await inter.followup.send(
            view=card(
                "Membro banido",
                f"**{clip(membro, 100)}** (`{membro.id}`) foi banido.",
                fields=[("Motivo", motivo), ("DM enviada", "Sim" if dm_sent else "Não")],
            ),
            ephemeral=True,
        )
        await self._log(
            inter.guild,
            "🔨 Banimento",
            f"{membro} foi banido por {inter.user}.",
            fields=[("Motivo", motivo), ("Mensagens removidas", f"{delete_days} dia(s)")],
        )

    @mod_group.command(name="unban", description="Desbane um usuário pelo ID")
    @app_commands.default_permissions(ban_members=True)
    async def unban(
        self,
        inter: discord.Interaction,
        user_id: str,
        motivo: app_commands.Range[str, 1, 500] = "Sem motivo",
    ) -> None:
        await inter.response.defer(ephemeral=True)
        try:
            uid = int(user_id.strip())
        except ValueError:
            await inter.followup.send(view=card("ID inválido", "Informe apenas números."), ephemeral=True)
            return
        try:
            user = await self.bot.fetch_user(uid)
            await inter.guild.unban(user, reason=f"{inter.user} ({inter.user.id}) • {motivo}")
        except discord.NotFound:
            await inter.followup.send(view=card("Usuário não encontrado", "O usuário não está banido ou o ID é inválido."), ephemeral=True)
            return
        except discord.Forbidden:
            await inter.followup.send(view=card("Sem permissão", "Não consigo desbanir usuários neste servidor."), ephemeral=True)
            return
        await inter.followup.send(view=card("Usuário desbanido", f"**{user}** (`{uid}`) foi desbanido.", fields=[("Motivo", motivo)]), ephemeral=True)
        await self._log(inter.guild, "✅ Desbanimento", f"{user} foi desbanido por {inter.user}.", fields=[("Motivo", motivo)])

    @mod_group.command(name="kick", description="Expulsa um membro do servidor")
    @app_commands.default_permissions(kick_members=True)
    async def kick(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        motivo: app_commands.Range[str, 1, 500] = "Sem motivo",
    ) -> None:
        await inter.response.defer(ephemeral=True)
        error = self._hierarchy_error(inter.guild, inter.user, membro)
        if error:
            await inter.followup.send(view=card("Hierarquia", error), ephemeral=True)
            return
        dm_sent = await self._notify_member(membro, f"Você foi expulso de **{inter.guild.name}**.\nMotivo: {motivo}")
        try:
            await membro.kick(reason=f"{inter.user} ({inter.user.id}) • {motivo}")
        except discord.Forbidden:
            await inter.followup.send(view=card("Sem permissão", "Não consegui expulsar este membro."), ephemeral=True)
            return
        await inter.followup.send(
            view=card("Membro expulso", f"{membro.mention} foi removido do servidor.", fields=[("Motivo", motivo), ("DM enviada", "Sim" if dm_sent else "Não")]),
            ephemeral=True,
        )
        await self._log(inter.guild, "🚪 Expulsão", f"{membro} foi expulso por {inter.user}.", fields=[("Motivo", motivo)])

    @mod_group.command(name="mute", description="Aplica timeout em um membro")
    @app_commands.default_permissions(moderate_members=True)
    async def mute(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        minutos: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
        motivo: app_commands.Range[str, 1, 500] = "Sem motivo",
    ) -> None:
        await inter.response.defer(ephemeral=True)
        error = self._hierarchy_error(inter.guild, inter.user, membro)
        if error:
            await inter.followup.send(view=card("Hierarquia", error), ephemeral=True)
            return
        until = discord.utils.utcnow() + timedelta(minutes=minutos)
        try:
            await membro.timeout(until, reason=f"{inter.user} ({inter.user.id}) • {motivo}")
        except discord.Forbidden:
            await inter.followup.send(view=card("Sem permissão", "Não consegui aplicar o timeout."), ephemeral=True)
            return
        await inter.followup.send(
            view=card(
                "Timeout aplicado",
                f"{membro.mention} ficará sem falar até {discord.utils.format_dt(until, 'F')}.",
                fields=[("Duração", f"{minutos} minuto(s)"), ("Motivo", motivo)],
            ),
            ephemeral=True,
        )
        await self._log(inter.guild, "🔇 Timeout", f"{membro} recebeu timeout de {inter.user}.", fields=[("Duração", f"{minutos} minuto(s)"), ("Motivo", motivo)])

    @mod_group.command(name="unmute", description="Remove o timeout de um membro")
    @app_commands.default_permissions(moderate_members=True)
    async def unmute(self, inter: discord.Interaction, membro: discord.Member) -> None:
        await inter.response.defer(ephemeral=True)
        error = self._hierarchy_error(inter.guild, inter.user, membro)
        if error:
            await inter.followup.send(view=card("Hierarquia", error), ephemeral=True)
            return
        if not membro.timed_out_until or membro.timed_out_until <= discord.utils.utcnow():
            await inter.followup.send(view=card("Sem timeout", f"{membro.mention} não está silenciado."), ephemeral=True)
            return
        try:
            await membro.timeout(None, reason=f"Timeout removido por {inter.user} ({inter.user.id})")
        except discord.Forbidden:
            await inter.followup.send(view=card("Sem permissão", "Não consegui remover o timeout."), ephemeral=True)
            return
        await inter.followup.send(view=card("Timeout removido", f"{membro.mention} pode falar novamente."), ephemeral=True)
        await self._log(inter.guild, "🔊 Timeout removido", f"{inter.user} removeu o timeout de {membro}.")

    @mod_group.command(name="limpar", description="Apaga mensagens do canal com filtros opcionais")
    @app_commands.default_permissions(manage_messages=True)
    async def limpar(
        self,
        inter: discord.Interaction,
        quantidade: app_commands.Range[int, 1, 100],
        membro: Optional[discord.Member] = None,
        contem: Optional[app_commands.Range[str, 1, 100]] = None,
        bots_apenas: bool = False,
    ) -> None:
        await inter.response.defer(ephemeral=True)

        def check(message: discord.Message) -> bool:
            if membro and message.author.id != membro.id:
                return False
            if bots_apenas and not message.author.bot:
                return False
            if contem and contem.casefold() not in (message.content or "").casefold():
                return False
            return True

        try:
            deleted = await inter.channel.purge(limit=quantidade, check=check, reason=f"Purge por {inter.user}")
        except discord.Forbidden:
            await inter.followup.send(view=card("Sem permissão", "Não consigo apagar mensagens neste canal."), ephemeral=True)
            return
        filters = []
        if membro:
            filters.append(f"membro: {membro}")
        if contem:
            filters.append(f"contém: {contem}")
        if bots_apenas:
            filters.append("somente bots")
        await inter.followup.send(view=card("Limpeza concluída", f"Foram apagadas **{len(deleted)}** mensagens."), ephemeral=True)
        await self._log(
            inter.guild,
            "🧹 Limpeza de mensagens",
            f"{inter.user} apagou {len(deleted)} mensagens em {inter.channel.mention}.",
            fields=[("Filtros", ", ".join(filters) if filters else "Nenhum")],
        )

    @mod_group.command(name="warn", description="Aplica um aviso formal")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        motivo: app_commands.Range[str, 1, 500],
    ) -> None:
        await inter.response.defer(ephemeral=True)
        error = self._hierarchy_error(inter.guild, inter.user, membro)
        if error:
            await inter.followup.send(view=card("Hierarquia", error), ephemeral=True)
            return
        total = await db.add_warn(inter.guild_id, membro.id, motivo, inter.user.id)
        dm_sent = await self._notify_member(
            membro,
            f"Você recebeu um aviso em **{inter.guild.name}**.\nMotivo: {motivo}\nTotal: {total}",
        )
        await inter.followup.send(
            view=card("Aviso aplicado", f"{membro.mention} recebeu o aviso **#{total}**.", fields=[("Motivo", motivo), ("DM enviada", "Sim" if dm_sent else "Não")]),
            ephemeral=True,
        )
        await self._log(inter.guild, "⚠️ Aviso", f"{membro} foi avisado por {inter.user}.", fields=[("Motivo", motivo), ("Total", total)])

    @mod_group.command(name="warns", description="Lista os avisos de um membro")
    @app_commands.default_permissions(moderate_members=True)
    async def warns(self, inter: discord.Interaction, membro: discord.Member) -> None:
        warnings = await db.get_warns(inter.guild_id, membro.id)
        if not warnings:
            await inter.response.send_message(view=card("Sem avisos", f"{membro.mention} não possui avisos."), ephemeral=True)
            return
        lines = []
        for index, warning in enumerate(warnings[:20], start=1):
            created = warning.get("created_at")
            date = discord.utils.format_dt(created, "d") if created else "data desconhecida"
            moderator_id = warning.get("moderator") or warning.get("moderator_id")
            moderator = f"<@{moderator_id}>" if moderator_id else "Sistema"
            lines.append(f"**{index}.** {clip(warning.get('motivo') or warning.get('reason'), 220)}\n-# {date} • {moderator}")
        await inter.response.send_message(
            view=card(
                f"Avisos de {membro.display_name}",
                "\n\n".join(lines),
                thumbnail=membro.display_avatar.url,
                footer=f"Total carregado: {len(warnings)}",
            ),
            ephemeral=True,
        )

    @mod_group.command(name="clearwarns", description="Remove todos os avisos de um membro")
    @app_commands.default_permissions(administrator=True)
    async def clearwarns(self, inter: discord.Interaction, membro: discord.Member) -> None:
        await db.clear_warns(inter.guild_id, membro.id)
        await inter.response.send_message(view=card("Avisos removidos", f"O histórico de {membro.mention} foi limpo."), ephemeral=True)
        await self._log(inter.guild, "🧽 Avisos limpos", f"{inter.user} removeu todos os avisos de {membro}.")

    @mod_group.command(name="userinfo", description="Informações detalhadas sobre um membro")
    async def userinfo(self, inter: discord.Interaction, membro: Optional[discord.Member] = None) -> None:
        member = membro or inter.user
        xp_data = await db.get_xp(inter.guild_id, member.id) or {"level": 0, "xp": 0}
        warnings = await db.get_warns(inter.guild_id, member.id)
        roles = [role.mention for role in reversed(member.roles) if role != inter.guild.default_role]
        fields = [
            ("Tag e ID", f"{member} • `{member.id}`"),
            ("Conta criada", discord.utils.format_dt(member.created_at, "R")),
            ("Entrou no servidor", discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Desconhecido"),
            ("XP", f"Nível `{xp_data.get('level', 0)}` • `{xp_data.get('xp', 0):,}` XP"),
            ("Avisos", len(warnings)),
            (f"Cargos ({len(roles)})", " ".join(roles[:15]) or "Nenhum"),
        ]
        await inter.response.send_message(
            view=card(member.display_name, fields=fields, thumbnail=member.display_avatar.url, footer=f"Solicitado por {inter.user}"),
            ephemeral=True,
        )

    async def cog_app_command_error(self, inter: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, discord.Forbidden):
            text = "O Discord recusou a ação por falta de permissão ou hierarquia."
        elif isinstance(error, app_commands.MissingPermissions):
            text = "Você não possui as permissões necessárias para este comando."
        else:
            log.error(
                "Erro em comando de moderação: %s",
                original,
                exc_info=(type(original), original, original.__traceback__),
            )
            text = "Ocorreu um erro inesperado. Verifique os logs do Railway."
        if inter.response.is_done():
            await inter.followup.send(view=card("Falha na moderação", text), ephemeral=True)
        else:
            await inter.response.send_message(view=card("Falha na moderação", text), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderacao(bot))
