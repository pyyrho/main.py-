"""cogs/tickets.py — Tickets persistentes, seguros e com Components V2."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from collections import defaultdict
from datetime import timezone
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from db import database as db
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.tickets")

TRANSCRIPT_LIMIT = max(100, min(int(os.getenv("TICKET_TRANSCRIPT_LIMIT", "1000")), 5000))

TICKET_CATEGORIES = [
    discord.SelectOption(label="Suporte Geral", value="suporte", description="Dúvidas gerais ou ajuda técnica", emoji="<:1000006244:1475982552488607815>"),
    discord.SelectOption(label="Denúncias", value="denuncia", description="Denunciar um membro ou situação", emoji="<:1000006242:1475982573846139001>"),
    discord.SelectOption(label="Compra de VIP", value="vip", description="Adquirir um cargo ou benefício VIP", emoji="<:1000006239:1475982464928452678>"),
    discord.SelectOption(label="Resgate de Prêmio", value="premio", description="Resgatar um prêmio conquistado", emoji="<:1000006240:1475982529243643967>"),
    discord.SelectOption(label="Parceria", value="parceria", description="Proposta de parceria ou patrocínio", emoji="<:1000006247:1475982600463187990>"),
    discord.SelectOption(label="Outros", value="outros", description="Outros assuntos não listados", emoji="<:1000006236:1475982635384836126>"),
]
LABEL_MAP = {
    "suporte": "Suporte Geral",
    "denuncia": "Denúncias",
    "vip": "Compra de VIP",
    "premio": "Resgate de Prêmio",
    "parceria": "Parceria",
    "outros": "Outros",
}


def _clean_channel_name(raw: str, *, prefix: str = "ticket") -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", raw.casefold()).strip("-") or "usuario"
    return f"{prefix}-{base}"[:90]


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _trim_reason(text: str, limit: int = 500) -> str:
    value = (text or "").strip()
    return clip(value, limit, fallback="Sem motivo informado.")


def _ticket_panel_text(description: str) -> str:
    categories = "\n".join(f"• **{option.label}** — {option.description}" for option in TICKET_CATEGORIES)
    return f"{clip(description, 1200)}\n\n{categories}\n\nSelecione uma categoria abaixo para abrir seu atendimento."


async def _gerar_transcript(channel: discord.TextChannel) -> discord.File:
    """Gera um transcript TXT legível, incluindo respostas, anexos e embeds."""
    lines = [
        f"TRANSCRIPT — #{channel.name}",
        f"Servidor: {channel.guild.name} ({channel.guild.id})",
        f"Canal: {channel.id}",
        "=" * 72,
        "",
    ]
    async for message in channel.history(limit=TRANSCRIPT_LIMIT, oldest_first=True):
        created = message.created_at.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")
        body = message.clean_content or ""
        if message.reference and message.reference.message_id:
            body = f"[resposta a {message.reference.message_id}] {body}".strip()
        if message.embeds:
            rendered: list[str] = []
            for embed in message.embeds[:5]:
                title = embed.title or "Embed"
                description = clip(embed.description or "", 1000, fallback="")
                rendered.append(f"[EMBED: {title}] {description}".strip())
            body = f"{body}\n" + "\n".join(rendered)
        if message.attachments:
            body = f"{body}\nAnexos: " + " | ".join(a.url for a in message.attachments)
        if message.stickers:
            body = f"{body}\nStickers: " + ", ".join(s.name for s in message.stickers)
        body = body.strip() or "(sem conteúdo textual)"
        lines.append(f"[{created}] {message.author} ({message.author.id}) | mensagem {message.id}")
        lines.append(body)
        lines.append("-" * 72)
    payload = "\n".join(lines).encode("utf-8", errors="replace")
    return discord.File(io.BytesIO(payload), filename=f"transcript-{channel.name}.txt")


class TicketMotivoModal(discord.ui.Modal, title="Descreva seu ticket"):
    motivo = discord.ui.TextInput(
        label="Qual é o motivo?",
        placeholder="Explique o que aconteceu e o que você precisa.",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=500,
    )

    def __init__(self, categoria: str) -> None:
        super().__init__()
        self.categoria = categoria

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if isinstance(cog, Tickets):
            await cog.criar_ticket(interaction, self.categoria, str(self.motivo.value))


class TicketMemberModal(discord.ui.Modal):
    user_id = discord.ui.TextInput(label="ID do usuário", min_length=17, max_length=20)

    def __init__(self, *, remove: bool = False) -> None:
        super().__init__(title="Remover membro do ticket" if remove else "Adicionar membro ao ticket")
        self.remove = remove

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if not isinstance(cog, Tickets) or not await cog._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode alterar membros do ticket."), ephemeral=True)
            return
        ticket = await db.get_ticket_by_channel(interaction.channel_id)
        if not ticket or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(view=card("Canal inválido", "Este canal não é um ticket aberto."), ephemeral=True)
            return
        try:
            user_id = int(str(self.user_id.value).strip())
        except ValueError:
            await interaction.response.send_message(view=card("ID inválido", "Informe somente o ID numérico."), ephemeral=True)
            return
        member = interaction.guild.get_member(user_id)
        if not member:
            await interaction.response.send_message(view=card("Membro não encontrado", "O usuário não está neste servidor."), ephemeral=True)
            return
        if member.id == interaction.guild.default_role.id:
            await interaction.response.send_message(view=card("Operação inválida", "Este alvo não pode ser alterado."), ephemeral=True)
            return
        try:
            if self.remove:
                if member.id == ticket["user_id"]:
                    await interaction.response.send_message(view=card("Dono do ticket", "Use o fechamento do ticket em vez de remover quem o abriu."), ephemeral=True)
                    return
                await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removido do ticket por {interaction.user}")
                title, text = "Membro removido", f"{member.mention} não possui mais acesso ao ticket."
            else:
                await interaction.channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    reason=f"Adicionado ao ticket por {interaction.user}",
                )
                title, text = "Membro adicionado", f"{member.mention} agora possui acesso ao ticket."
        except discord.Forbidden:
            await interaction.response.send_message(view=card("Sem permissão", "Não consegui alterar as permissões deste canal."), ephemeral=True)
            return
        await interaction.response.send_message(view=card(title, text), ephemeral=True)


class RenomearModal(discord.ui.Modal, title="Renomear canal do ticket"):
    nome = discord.ui.TextInput(label="Novo nome", min_length=2, max_length=50)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if not isinstance(cog, Tickets) or not await cog._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode renomear tickets."), ephemeral=True)
            return
        if not await db.get_ticket_by_channel(interaction.channel_id) or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(view=card("Canal inválido", "Este canal não é um ticket."), ephemeral=True)
            return
        new_name = _clean_channel_name(str(self.nome.value), prefix="ticket")
        try:
            await interaction.channel.edit(name=new_name, reason=f"Ticket renomeado por {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(view=card("Falha ao renomear", "O Discord recusou o novo nome ou a alteração foi limitada."), ephemeral=True)
            return
        await interaction.response.send_message(view=card("Ticket renomeado", f"Novo nome: `{new_name}`."), ephemeral=True)


class TicketSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Selecione o motivo do seu ticket...",
            options=TICKET_CATEGORIES,
            custom_id="ticket:category_select:v2",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if not isinstance(cog, Tickets):
            await interaction.response.send_message(view=card("Indisponível", "O sistema de tickets ainda está iniciando."), ephemeral=True)
            return
        await cog.iniciar_abertura(interaction, self.values[0])


class TicketPanelLayout(discord.ui.LayoutView):
    def __init__(self, title: str = "Suporte | Ticket", description: str = "Abra um ticket selecionando a categoria abaixo.", image: str | None = None) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=WHITE)
        container.add_item(discord.ui.TextDisplay(f"## {clip(title, 200)}\n\n{_ticket_panel_text(description)}"))
        if image and _valid_http_url(image):
            container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=image, description="Painel de tickets")))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(TicketSelect()))
        self.add_item(container)


class TicketMainLayout(discord.ui.LayoutView):
    def __init__(self, *, opener_id: int = 0, category: str = "Ticket", reason: str = "", banner: str | None = None) -> None:
        super().__init__(timeout=None)
        self.opener_id = opener_id
        container = discord.ui.Container(accent_color=WHITE)
        container.add_item(
            discord.ui.TextDisplay(
                "## Atendimento aberto\n\n"
                f"**Aberto por:** <@{opener_id}>\n"
                f"**Categoria:** {clip(category, 100)}\n"
                f"**Motivo:** {clip(reason, 700)}\n\n"
                "Descreva os detalhes necessários e aguarde a equipe."
            )
        )
        if banner and _valid_http_url(banner):
            container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=banner, description="Banner do ticket")))
        attend = discord.ui.Button(label="Atender", style=discord.ButtonStyle.success, emoji="✅", custom_id="ticket:attend:v2")
        admin = discord.ui.Button(label="Painel Admin", style=discord.ButtonStyle.primary, emoji="🛠️", custom_id="ticket:admin:v2")
        close = discord.ui.Button(label="Fechar", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close:v2")
        notify = discord.ui.Button(label="Notificar", style=discord.ButtonStyle.secondary, emoji="🔔", custom_id="ticket:notify:v2")
        attend.callback = self._attend
        admin.callback = self._admin
        close.callback = self._close
        notify.callback = self._notify
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(attend, admin, close, notify))
        self.add_item(container)

    async def _ticket_and_cog(self, interaction: discord.Interaction) -> tuple[dict | None, Tickets | None]:
        cog = interaction.client.get_cog("Tickets")
        ticket = await db.get_ticket_by_channel(interaction.channel_id) if interaction.channel_id else None
        return ticket, cog if isinstance(cog, Tickets) else None

    async def _attend(self, interaction: discord.Interaction) -> None:
        ticket, cog = await self._ticket_and_cog(interaction)
        if not ticket or not cog:
            await interaction.response.send_message(view=card("Ticket encerrado", "Este atendimento não está mais aberto."), ephemeral=True)
            return
        if not await cog._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode assumir tickets."), ephemeral=True)
            return
        await db.set_ticket_atendente(interaction.channel_id, interaction.user.id)
        await interaction.response.send_message(
            view=card("Ticket assumido", f"{interaction.user.mention} assumiu este atendimento.\nA equipe já pode prosseguir com o suporte."),
        )

    async def _admin(self, interaction: discord.Interaction) -> None:
        ticket, cog = await self._ticket_and_cog(interaction)
        if not ticket or not cog or not await cog._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode abrir este painel."), ephemeral=True)
            return
        await interaction.response.send_message(view=TicketAdminLayout(), ephemeral=True)

    async def _close(self, interaction: discord.Interaction) -> None:
        ticket, cog = await self._ticket_and_cog(interaction)
        if not ticket or not cog:
            await interaction.response.send_message(view=card("Ticket encerrado", "Este canal não está registrado como ticket aberto."), ephemeral=True)
            return
        is_staff = await cog._check_staff(interaction)
        if not is_staff and interaction.user.id != ticket["user_id"]:
            await interaction.response.send_message(view=card("Sem permissão", "Apenas quem abriu ou a equipe pode fechar este ticket."), ephemeral=True)
            return
        await interaction.response.send_message(view=TicketCloseLayout(), ephemeral=True)

    async def _notify(self, interaction: discord.Interaction) -> None:
        ticket, cog = await self._ticket_and_cog(interaction)
        if not ticket or not cog or not await cog._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode notificar o autor."), ephemeral=True)
            return
        user = interaction.guild.get_member(ticket["user_id"])
        if not user:
            await interaction.response.send_message(view=card("Usuário ausente", "Quem abriu o ticket não está mais no servidor."), ephemeral=True)
            return
        try:
            await user.send(f"🔔 A equipe respondeu ao seu ticket em **{interaction.guild.name}**: {interaction.channel.mention}")
        except discord.Forbidden:
            await interaction.response.send_message(view=card("DM bloqueada", "Não foi possível enviar mensagem privada ao usuário."), ephemeral=True)
            return
        await interaction.response.send_message(view=card("Usuário notificado", f"Enviei uma mensagem privada para {user.mention}."), ephemeral=True)


class TicketAdminLayout(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_color=WHITE)
        container.add_item(discord.ui.TextDisplay("## Administração do ticket\n\nGerencie participantes, nome, transcript e encerramento."))
        add = discord.ui.Button(label="Adicionar membro", style=discord.ButtonStyle.primary, emoji="➕")
        remove = discord.ui.Button(label="Remover membro", style=discord.ButtonStyle.secondary, emoji="➖")
        rename = discord.ui.Button(label="Renomear", style=discord.ButtonStyle.secondary, emoji="✏️")
        transcript = discord.ui.Button(label="Transcript", style=discord.ButtonStyle.success, emoji="📄")
        silent_close = discord.ui.Button(label="Fechar silenciosamente", style=discord.ButtonStyle.danger, emoji="🗑️")
        add.callback = self._add
        remove.callback = self._remove
        rename.callback = self._rename
        transcript.callback = self._transcript
        silent_close.callback = self._silent_close
        container.add_item(discord.ui.ActionRow(add, remove, rename, transcript))
        container.add_item(discord.ui.ActionRow(silent_close))
        self.add_item(container)

    async def _staff(self, interaction: discord.Interaction) -> Tickets | None:
        cog = interaction.client.get_cog("Tickets")
        if isinstance(cog, Tickets) and await cog._check_staff(interaction):
            return cog
        await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode usar esta função."), ephemeral=True)
        return None

    async def _add(self, interaction: discord.Interaction) -> None:
        if await self._staff(interaction):
            await interaction.response.send_modal(TicketMemberModal(remove=False))

    async def _remove(self, interaction: discord.Interaction) -> None:
        if await self._staff(interaction):
            await interaction.response.send_modal(TicketMemberModal(remove=True))

    async def _rename(self, interaction: discord.Interaction) -> None:
        if await self._staff(interaction):
            await interaction.response.send_modal(RenomearModal())

    async def _transcript(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(view=card("Canal inválido", "Não é possível gerar transcript aqui."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        file = await _gerar_transcript(interaction.channel)
        await interaction.followup.send(view=card("Transcript gerado", f"Histórico de `{interaction.channel.name}`.", file=file), file=file, ephemeral=True)

    async def _silent_close(self, interaction: discord.Interaction) -> None:
        cog = await self._staff(interaction)
        if cog:
            await cog.fechar_ticket_confirmado(interaction, gerar_transcript_auto=True, silent=True)


class TicketCloseLayout(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=45)
        container = discord.ui.Container(accent_color=WHITE)
        container.add_item(discord.ui.TextDisplay("## Fechar ticket?\n\nUm transcript será criado quando houver canal de logs configurado. Depois, o canal será excluído."))
        confirm = discord.ui.Button(label="Confirmar", style=discord.ButtonStyle.danger, emoji="✅")
        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        container.add_item(discord.ui.ActionRow(confirm, cancel))
        self.add_item(container)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if not isinstance(cog, Tickets):
            await interaction.response.edit_message(view=card("Indisponível", "O sistema de tickets não está carregado.", timeout=None))
            return
        await cog.fechar_ticket_confirmado(interaction, gerar_transcript_auto=True, silent=False)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=card("Fechamento cancelado", "O ticket continuará aberto.", timeout=None))
        self.stop()


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._creation_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._closing_channels: set[int] = set()
        bot.add_view(TicketPanelLayout())
        bot.add_view(TicketMainLayout())

    async def _check_staff(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        config = await db.get_guild_config(interaction.guild.id)
        staff_ids = {int(role_id) for role_id in (config.get("staff_roles") or [])}
        return bool({role.id for role in interaction.user.roles} & staff_ids)

    async def _log_ticket(self, guild: discord.Guild, title: str, description: str, *, file: discord.File | None = None) -> None:
        config = await db.get_guild_config(guild.id)
        channel_id = config.get("ticket_log") or config.get("log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(view=card(title, description, file=file, footer="Sistema de tickets", timeout=None), file=file)
        except discord.HTTPException as exc:
            log.warning("Falha ao enviar log de ticket em %s: %s", guild.id, exc)

    async def iniciar_abertura(self, interaction: discord.Interaction, category: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message(view=card("Servidor necessário", "Tickets só podem ser abertos dentro de um servidor."), ephemeral=True)
            return
        existing = await db.get_ticket_by_user(interaction.guild.id, interaction.user.id)
        if existing:
            channel = interaction.guild.get_channel(existing["channel_id"])
            if channel:
                await interaction.response.send_message(view=card("Ticket já aberto", f"Você já possui um atendimento em {channel.mention}."), ephemeral=True)
                return
            await db.close_ticket(existing["channel_id"])
        config = await db.get_guild_config(interaction.guild.id)
        if not config.get("ticket_category"):
            await interaction.response.send_message(view=card("Sistema não configurado", "Um administrador precisa usar `/ticket setup`."), ephemeral=True)
            return
        await interaction.response.send_modal(TicketMotivoModal(category))

    async def criar_ticket(self, interaction: discord.Interaction, category: str, reason: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(view=card("Servidor necessário", "Não foi possível criar o ticket."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._creation_locks[(interaction.guild.id, interaction.user.id)]
        async with lock:
            existing = await db.get_ticket_by_user(interaction.guild.id, interaction.user.id)
            if existing:
                channel = interaction.guild.get_channel(existing["channel_id"])
                if channel:
                    await interaction.followup.send(view=card("Ticket já aberto", f"Use o atendimento existente: {channel.mention}."), ephemeral=True)
                    return
                await db.close_ticket(existing["channel_id"])

            config = await db.get_guild_config(interaction.guild.id)
            category_channel = interaction.guild.get_channel(config.get("ticket_category"))
            if not isinstance(category_channel, discord.CategoryChannel):
                await interaction.followup.send(view=card("Configuração inválida", "A categoria configurada não existe mais. Execute `/ticket setup` novamente."), ephemeral=True)
                return
            me = interaction.guild.me
            if not me or not me.guild_permissions.manage_channels:
                await interaction.followup.send(view=card("Sem permissão", "Preciso da permissão **Gerenciar Canais**."), ephemeral=True)
                return

            staff_roles = [
                role for role_id in (config.get("staff_roles") or [])
                if (role := interaction.guild.get_role(int(role_id))) is not None
            ]
            overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
            }
            for role in staff_roles:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)

            try:
                channel = await interaction.guild.create_text_channel(
                    name=_clean_channel_name(interaction.user.display_name),
                    category=category_channel,
                    overwrites=overwrites,
                    topic=f"Ticket de {interaction.user} ({interaction.user.id}) • {LABEL_MAP.get(category, category)}",
                    reason=f"Ticket aberto por {interaction.user}",
                )
            except discord.Forbidden:
                await interaction.followup.send(view=card("Sem permissão", "Não consegui criar o canal do ticket."), ephemeral=True)
                return
            except discord.HTTPException as exc:
                log.exception("Falha ao criar ticket: %s", exc)
                await interaction.followup.send(view=card("Falha ao criar", "O Discord recusou a criação do canal. Tente novamente."), ephemeral=True)
                return

            try:
                await db.open_ticket(interaction.guild.id, interaction.user.id, channel.id, category)
            except Exception:
                log.exception("Falha ao registrar ticket no banco")
                try:
                    await channel.delete(reason="Rollback: falha ao registrar ticket")
                except discord.HTTPException:
                    pass
                await interaction.followup.send(view=card("Falha no banco de dados", "O canal foi revertido para evitar um ticket órfão."), ephemeral=True)
                return

            label = LABEL_MAP.get(category, category.title())
            mentions = [interaction.user.mention, *(role.mention for role in staff_roles)]
            try:
                await channel.send(
                    view=TicketMainLayout(
                        opener_id=interaction.user.id,
                        category=label,
                        reason=_trim_reason(reason),
                        banner=config.get("ticket_banner"),
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
                )
                if mentions:
                    await channel.send(" ".join(mentions), allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False), delete_after=2)
            except discord.HTTPException:
                log.exception("Falha ao enviar painel principal do ticket %s", channel.id)

            await self._log_ticket(
                interaction.guild,
                "Ticket aberto",
                f"**Usuário:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Categoria:** {label}\n**Canal:** {channel.mention}\n**Motivo:** {_trim_reason(reason, 600)}",
            )
            await interaction.followup.send(view=card("Ticket criado", f"Seu atendimento foi aberto em {channel.mention}."), ephemeral=True)

    async def fechar_ticket_confirmado(
        self,
        interaction: discord.Interaction,
        *,
        gerar_transcript_auto: bool = True,
        silent: bool = False,
    ) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            if not interaction.response.is_done():
                await interaction.response.send_message(view=card("Canal inválido", "Este comando precisa ser usado dentro de um ticket."), ephemeral=True)
            return
        channel = interaction.channel
        if channel.id in self._closing_channels:
            if not interaction.response.is_done():
                await interaction.response.send_message(view=card("Fechamento em andamento", "Este ticket já está sendo encerrado."), ephemeral=True)
            return
        ticket = await db.get_ticket_by_channel(channel.id)
        if not ticket:
            if not interaction.response.is_done():
                await interaction.response.send_message(view=card("Ticket não encontrado", "O registro deste ticket já foi encerrado."), ephemeral=True)
            return

        is_staff = await self._check_staff(interaction)
        if interaction.user.id != ticket["user_id"] and not is_staff:
            if not interaction.response.is_done():
                await interaction.response.send_message(view=card("Sem permissão", "Apenas quem abriu ou a equipe pode fechar."), ephemeral=True)
            return

        self._closing_channels.add(channel.id)
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=card("Fechando ticket", "O transcript está sendo preparado e o canal será removido em instantes.", timeout=None))
            else:
                await interaction.response.edit_message(view=card("Fechando ticket", "O transcript está sendo preparado e o canal será removido em instantes.", timeout=None))

            transcript: discord.File | None = None
            if gerar_transcript_auto:
                try:
                    transcript = await _gerar_transcript(channel)
                except Exception as exc:
                    log.exception("Falha ao gerar transcript do canal %s: %s", channel.id, exc)

            owner = interaction.guild.get_member(ticket["user_id"])
            description = (
                f"**Canal:** `{channel.name}` (`{channel.id}`)\n"
                f"**Dono:** {owner.mention if owner else ticket['user_id']}\n"
                f"**Fechado por:** {interaction.user.mention}"
            )
            await self._log_ticket(interaction.guild, "Ticket fechado", description, file=transcript)
            await db.close_ticket(channel.id)
            if not silent:
                try:
                    await channel.send(view=card("Ticket encerrado", "Este canal será excluído em 5 segundos.", timeout=None))
                except discord.HTTPException:
                    pass
                await asyncio.sleep(5)
            try:
                await channel.delete(reason=f"Ticket fechado por {interaction.user}")
            except discord.HTTPException as exc:
                log.warning("Falha ao excluir ticket %s: %s", channel.id, exc)
        finally:
            self._closing_channels.discard(channel.id)

    ticket_group = app_commands.Group(
        name="ticket",
        description="Sistema de tickets",
        default_permissions=discord.Permissions(administrator=True),
    )

    @ticket_group.command(name="setup", description="Configura o sistema de tickets")
    @app_commands.describe(
        categoria="Categoria para os tickets",
        cargo_staff="Cargo principal da equipe",
        cargo_staff_2="Segundo cargo opcional",
        cargo_staff_3="Terceiro cargo opcional",
        canal_log="Canal de logs",
        banner_url="URL do banner do ticket",
    )
    async def ticket_setup(
        self,
        interaction: discord.Interaction,
        categoria: discord.CategoryChannel,
        cargo_staff: discord.Role,
        cargo_staff_2: discord.Role | None = None,
        cargo_staff_3: discord.Role | None = None,
        canal_log: discord.TextChannel | None = None,
        banner_url: str | None = None,
    ) -> None:
        roles: list[discord.Role] = []
        for role in (cargo_staff, cargo_staff_2, cargo_staff_3):
            if role and role.id not in {item.id for item in roles}:
                roles.append(role)
        if banner_url and not _valid_http_url(banner_url):
            await interaction.response.send_message(view=card("URL inválida", "O banner deve começar com http:// ou https://."), ephemeral=True)
            return
        await db.upsert_guild_config(
            interaction.guild_id,
            ticket_category=categoria.id,
            staff_roles=[role.id for role in roles],
            **({"ticket_log": canal_log.id} if canal_log else {}),
            **({"ticket_banner": banner_url.strip()} if banner_url else {}),
        )
        await interaction.response.send_message(
            view=card(
                "Tickets configurados",
                "O sistema está pronto para receber um painel.",
                fields=[
                    ("Categoria", categoria.mention),
                    ("Equipe", " ".join(role.mention for role in roles)),
                    ("Logs", canal_log.mention if canal_log else "Não definido"),
                    ("Banner", "Configurado" if banner_url else "Mantido/ausente"),
                ],
            ),
            ephemeral=True,
        )

    @ticket_group.command(name="painel", description="Publica o painel de abertura de tickets")
    @app_commands.describe(canal="Canal de destino", titulo="Título", descricao="Descrição", imagem="URL da imagem")
    async def ticket_painel(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
        titulo: str = "Suporte | Ticket",
        descricao: str = "Abra um ticket selecionando a categoria abaixo.",
        imagem: str | None = None,
    ) -> None:
        if imagem and not _valid_http_url(imagem):
            await interaction.response.send_message(view=card("URL inválida", "A imagem deve começar com http:// ou https://."), ephemeral=True)
            return
        permissions = canal.permissions_for(interaction.guild.me)
        if not permissions.send_messages:
            await interaction.response.send_message(view=card("Sem permissão", f"Não consigo enviar mensagens em {canal.mention}."), ephemeral=True)
            return
        try:
            await canal.send(view=TicketPanelLayout(titulo, descricao, imagem))
        except discord.HTTPException:
            await interaction.response.send_message(view=card("Falha ao publicar", "O painel não pôde ser enviado."), ephemeral=True)
            return
        await interaction.response.send_message(view=card("Painel publicado", f"O painel foi enviado em {canal.mention}."), ephemeral=True)

    @ticket_group.command(name="lista", description="Lista os tickets abertos")
    async def ticket_lista(self, interaction: discord.Interaction) -> None:
        tickets = await db.list_open_tickets(interaction.guild_id)
        if not tickets:
            await interaction.response.send_message(view=card("Nenhum ticket aberto", "A fila está vazia."), ephemeral=True)
            return
        lines: list[str] = []
        stale: list[int] = []
        for ticket in tickets[:30]:
            channel = interaction.guild.get_channel(ticket["channel_id"])
            member = interaction.guild.get_member(ticket["user_id"])
            if not channel:
                stale.append(ticket["channel_id"])
                continue
            member_text = member.mention if member else f"`{ticket['user_id']}`"
            lines.append(
                f"• {channel.mention} · {member_text} · "
                f"{LABEL_MAP.get(ticket['categoria'], ticket['categoria'])}"
            )
        for channel_id in stale:
            try:
                await db.close_ticket(channel_id)
            except Exception:
                log.debug("Não foi possível limpar ticket órfão %s", channel_id)
        await interaction.response.send_message(
            view=card("Tickets abertos", "\n".join(lines) if lines else "Os registros antigos foram limpos; nenhum canal ativo permaneceu."),
            ephemeral=True,
        )

    @ticket_group.command(name="transcript", description="Gera o transcript do ticket atual")
    async def ticket_transcript(self, interaction: discord.Interaction) -> None:
        if not await self._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas a equipe pode gerar transcripts."), ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel) or not await db.get_ticket_by_channel(interaction.channel_id):
            await interaction.response.send_message(view=card("Canal inválido", "Este canal não é um ticket aberto."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        file = await _gerar_transcript(interaction.channel)
        await interaction.followup.send(view=card("Transcript gerado", f"Histórico de `{interaction.channel.name}`.", file=file), file=file, ephemeral=True)

    @ticket_group.command(name="fechar", description="Fecha o ticket do canal atual")
    async def ticket_fechar(self, interaction: discord.Interaction) -> None:
        ticket = await db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(view=card("Canal inválido", "Este canal não é um ticket aberto."), ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"] and not await self._check_staff(interaction):
            await interaction.response.send_message(view=card("Sem permissão", "Apenas quem abriu ou a equipe pode fechar."), ephemeral=True)
            return
        await interaction.response.send_message(view=TicketCloseLayout(), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        log.exception("Erro em comando de tickets: %s", original)
        if interaction.response.is_done():
            await interaction.followup.send(view=card("Erro inesperado", "Não foi possível concluir a operação. Tente novamente."), ephemeral=True)
        else:
            await interaction.response.send_message(view=card("Erro inesperado", "Não foi possível concluir a operação. Tente novamente."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
