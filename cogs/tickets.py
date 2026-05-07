"""cogs/tickets.py — Sistema de tickets com persistência PostgreSQL."""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import io
import logging
import re
from datetime import timezone
from db import database as db
from utils.constants import Colors, E, success_embed, error_embed, mod_embed, _now

log = logging.getLogger("multibot.tickets")

TICKET_CATEGORIES = [
    discord.SelectOption(label="Suporte Geral",    value="suporte",    description="Dúvidas gerais ou ajuda técnica",        emoji="<:1000006244:1475982552488607815>"),
    discord.SelectOption(label="Denúncias",         value="denuncia",   description="Denunciar um membro ou situação",        emoji="<:1000006242:1475982573846139001>"),
    discord.SelectOption(label="Compra de VIP",     value="vip",        description="Adquirir um cargo ou benefício VIP",     emoji="<:1000006239:1475982464928452678>"),
    discord.SelectOption(label="Resgate de Prêmio", value="premio",     description="Resgatar um prêmio conquistado",         emoji="<:1000006240:1475982529243643967>"),
    discord.SelectOption(label="Parceria",          value="parceria",   description="Proposta de parceria ou patrocínio",     emoji="<:1000006247:1475982600463187990>"),
    discord.SelectOption(label="Outros",            value="outros",     description="Outros assuntos não listados acima",     emoji="<:1000006236:1475982635384836126>"),
]
LABEL_MAP = {
    "suporte": "Suporte Geral", "denuncia": "Denúncias",
    "vip": "Compra de VIP", "premio": "Resgate de Prêmio",
    "parceria": "Parceria", "outros": "Outros",
}


def _clean_channel_name(raw: str, *, prefix: str = "ticket") -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.lower()).strip("-") or "usuario"
    return f"{prefix}-{base}"[:50]


def _trim_reason(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    return text[:limit] if text else "Sem motivo informado."


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _gerar_transcript(channel: discord.TextChannel) -> discord.File:
    """Gera um arquivo .txt com o histórico do ticket."""
    linhas = [f"═══ Transcript — #{channel.name} ═══\n"]
    async for msg in channel.history(limit=500, oldest_first=True):
        ts = msg.created_at.replace(tzinfo=timezone.utc).strftime("%d/%m/%Y %H:%M")
        conteudo = msg.content or ""
        if msg.embeds:
            conteudo += " [embed]"
        if msg.attachments:
            conteudo += " " + " ".join(a.url for a in msg.attachments)
        linhas.append(f"[{ts}] {msg.author} ({msg.author.id}): {conteudo}")
    texto = "\n".join(linhas) or "Sem mensagens."
    return discord.File(fp=io.BytesIO(texto.encode()), filename=f"transcript-{channel.name}.txt")


# ── Modals ────────────────────────────────────────────────────────────────────

class TicketMotivoModal(discord.ui.Modal, title="Descreva seu ticket"):
    motivo = discord.ui.TextInput(
        label="Qual é o motivo?",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, categoria: str):
        super().__init__()
        self.categoria = categoria

    async def on_submit(self, inter: discord.Interaction):
        cog = inter.client.cogs.get("Tickets")
        if cog:
            await cog.criar_ticket(inter, self.categoria, self.motivo.value)


class AdicionarMembroModal(discord.ui.Modal, title="Adicionar membro ao ticket"):
    user_id = discord.ui.TextInput(label="ID do usuário", max_length=20)

    async def on_submit(self, inter: discord.Interaction):
        try:
            member = inter.guild.get_member(int(self.user_id.value.strip()))
            if not member:
                return await inter.response.send_message(
                    embed=error_embed("Não encontrado", "Membro não está no servidor."), ephemeral=True
                )
            await inter.channel.set_permissions(member, view_channel=True, send_messages=True)
            await inter.response.send_message(
                embed=success_embed("Adicionado", f"{member.mention} foi adicionado ao ticket.")
            )
        except (ValueError, Exception) as exc:
            await inter.response.send_message(embed=error_embed("Erro", str(exc)), ephemeral=True)


class RemoverMembroModal(discord.ui.Modal, title="Remover membro do ticket"):
    user_id = discord.ui.TextInput(label="ID do usuário", max_length=20)

    async def on_submit(self, inter: discord.Interaction):
        try:
            member = inter.guild.get_member(int(self.user_id.value.strip()))
            if not member:
                return await inter.response.send_message(
                    embed=error_embed("Não encontrado", "Membro não está no servidor."), ephemeral=True
                )
            await inter.channel.set_permissions(member, overwrite=None)
            await inter.response.send_message(
                embed=success_embed("Removido", f"{member.mention} foi removido do ticket.")
            )
        except (ValueError, Exception) as exc:
            await inter.response.send_message(embed=error_embed("Erro", str(exc)), ephemeral=True)


class RenomearModal(discord.ui.Modal, title="Renomear canal do ticket"):
    nome = discord.ui.TextInput(label="Novo nome", max_length=50)

    async def on_submit(self, inter: discord.Interaction):
        nome = self.nome.value.strip().lower().replace(" ", "-")
        try:
            await inter.channel.edit(name=nome)
            await inter.response.send_message(
                embed=success_embed("Renomeado", f"Canal renomeado para `{nome}`."), ephemeral=True
            )
        except discord.HTTPException as exc:
            await inter.response.send_message(embed=error_embed("Erro", str(exc)), ephemeral=True)


# ── Views ─────────────────────────────────────────────────────────────────────

class TicketSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class TicketSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Selecione o motivo do seu ticket...",
            options=TICKET_CATEGORIES,
            custom_id="ticket:category_select",
        )

    async def callback(self, inter: discord.Interaction):
        categoria = self.values[0]
        ticket = await db.get_ticket_by_user(inter.guild.id, inter.user.id)
        if ticket:
            ch = inter.guild.get_channel(ticket["channel_id"])
            if ch:
                return await inter.response.send_message(
                    embed=error_embed("Ticket já aberto",
                        f"Você já tem um ticket aberto: {ch.mention}\nFeche-o antes de abrir outro."
                    ),
                    ephemeral=True,
                )
            else:
                await db.close_ticket(ticket["channel_id"])

        cfg = await db.get_guild_config(inter.guild.id)
        if not cfg.get("ticket_category"):
            return await inter.response.send_message(
                embed=error_embed("Não configurado", "Use `/ticket setup` primeiro."),
                ephemeral=True,
            )
        await inter.response.send_modal(TicketMotivoModal(categoria))


class ConfirmarFechamentoView(discord.ui.View):
    """View de confirmação antes de fechar o ticket."""
    def __init__(self, opener_id: int, gerar_transcript: bool = True):
        super().__init__(timeout=30)
        self.opener_id = opener_id
        self.gerar_transcript = gerar_transcript

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirmar(self, inter: discord.Interaction, _):
        cog = inter.client.cogs.get("Tickets")
        if cog:
            await cog.fechar_ticket_confirmado(inter, self.opener_id, self.gerar_transcript)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancelar(self, inter: discord.Interaction, _):
        await inter.response.edit_message(
            embed=success_embed("Cancelado", "O fechamento do ticket foi cancelado."),
            view=None,
        )
        self.stop()


class TicketMainView(discord.ui.View):
    def __init__(self, opener_id: int = 0):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @discord.ui.button(label="Atender", style=discord.ButtonStyle.success,
                       emoji="<a:1000006152:1475983799568433355>", custom_id="ticket:atender")
    async def atender(self, inter: discord.Interaction, _):
        cog = inter.client.cogs.get("Tickets")
        if not cog or not await cog._check_staff(inter):
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Apenas a staff pode assumir tickets."), ephemeral=True
            )
        await db.set_ticket_atendente(inter.channel.id, inter.user.id)
        emb = discord.Embed(
            title=f"{E.VERIFY} Ticket Assumido",
            description=(
                f"{E.STAR} **Atendente:** {inter.user.mention}\n\n"
                f"{E.ARROW_BLUE} Em que posso ajudar?"
            ),
            color=Colors.MAIN,
        )
        emb.set_thumbnail(url=inter.user.display_avatar.url)
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @discord.ui.button(label="Painel Admin", style=discord.ButtonStyle.primary,
                       emoji="<:1000006182:1475983151712174290>", custom_id="ticket:admin")
    async def admin(self, inter: discord.Interaction, _):
        cog = inter.client.cogs.get("Tickets")
        if not cog or not await cog._check_staff(inter):
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Apenas a staff."), ephemeral=True
            )
        view = TicketAdminView(self.opener_id)
        emb = discord.Embed(
            title=f"{E.SETTINGS} Painel Admin",
            description=(
                f"{E.PIN} **Adicionar** — adiciona membro ao canal\n"
                f"➖ **Remover** — remove acesso de um membro\n"
                f"{E.PEN} **Renomear** — altera o nome do ticket\n"
                f"{E.RULES} **Transcript** — gera log das mensagens\n"
                f"{E.FIRE} **Fechar silenciosamente** — deleta sem aviso"
            ),
            color=Colors.MAIN,
        )
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb, view=view, ephemeral=True)

    @discord.ui.button(label="Fechar", style=discord.ButtonStyle.danger,
                       emoji="<a:i_exclamation:1446591025622679644>", custom_id="ticket:fechar")
    async def fechar(self, inter: discord.Interaction, _):
        cog = inter.client.cogs.get("Tickets")
        if not cog:
            return
        ticket = await db.get_ticket_by_channel(inter.channel.id)
        if not ticket:
            return await inter.response.send_message(
                embed=error_embed("Erro", "Este canal não é um ticket."), ephemeral=True
            )
        is_staff = await cog._check_staff(inter)
        is_owner = inter.user.id == ticket["user_id"]
        if not (is_staff or is_owner):
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Apenas a staff ou quem abriu pode fechar."),
                ephemeral=True,
            )
        emb = discord.Embed(
            title="⚠️ Fechar Ticket",
            description=(
                f"{E.ARROW_YELLOW} Tem certeza que deseja fechar este ticket?\n\n"
                f"{E.RULES} Um **transcript** será gerado automaticamente.\n"
                f"{E.LOADING} O canal será deletado em seguida."
            ),
            color=0xf39c12,
        )
        emb.timestamp = _now()
        await inter.response.send_message(
            embed=emb,
            view=ConfirmarFechamentoView(ticket["user_id"], gerar_transcript=True),
            ephemeral=True,
        )

    @discord.ui.button(label="Notificar", style=discord.ButtonStyle.secondary,
                       emoji="<a:1503hearts:1430339028720549908>", custom_id="ticket:notificar")
    async def notificar(self, inter: discord.Interaction, _):
        ticket = await db.get_ticket_by_channel(inter.channel.id)
        if not ticket:
            return await inter.response.send_message(
                embed=error_embed("Erro", "Este canal não é um ticket."), ephemeral=True
            )
        if inter.user.id != ticket["user_id"]:
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Apenas o dono do ticket pode notificar."), ephemeral=True
            )
        atendente_id = ticket.get("atendente")
        if atendente_id:
            at = inter.guild.get_member(atendente_id)
            if at:
                await inter.response.send_message(
                    content=at.mention,
                    embed=mod_embed(f"{E.WARN_IC} Notificação", f"{inter.user.mention} aguarda atendimento.")
                )
                return
        cfg = await db.get_guild_config(inter.guild.id)
        staff_roles = cfg.get("staff_roles") or []
        if staff_roles:
            mentions = " ".join(f"<@&{rid}>" for rid in staff_roles)
            await inter.response.send_message(
                content=mentions,
                embed=mod_embed(f"{E.WARN_IC} Staff Notificada", f"{inter.user.mention} aguarda atendimento.")
            )
        else:
            await inter.response.send_message(
                embed=error_embed("Sem staff", "Nenhum cargo de staff configurado."), ephemeral=True
            )


class TicketAdminView(discord.ui.View):
    def __init__(self, opener_id: int = 0):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @discord.ui.button(label="Adicionar Membro", style=discord.ButtonStyle.primary, emoji="➕")
    async def add(self, inter: discord.Interaction, _):
        await inter.response.send_modal(AdicionarMembroModal())

    @discord.ui.button(label="Remover Membro", style=discord.ButtonStyle.secondary, emoji="➖")
    async def remove(self, inter: discord.Interaction, _):
        await inter.response.send_modal(RemoverMembroModal())

    @discord.ui.button(label="Renomear", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def rename(self, inter: discord.Interaction, _):
        await inter.response.send_modal(RenomearModal())

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.success, emoji="📄")
    async def transcript(self, inter: discord.Interaction, _):
        await inter.response.defer(ephemeral=True)
        file = await _gerar_transcript(inter.channel)
        await inter.followup.send(
            embed=success_embed("Transcript gerado", f"Log do canal `{inter.channel.name}`."),
            file=file, ephemeral=True,
        )

    @discord.ui.button(label="Fechar Silenciosamente", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def fechar_silencioso(self, inter: discord.Interaction, _):
        await inter.response.send_message(
            embed=mod_embed(f"{E.ARROW_RED} Fechando...", f"{E.LOADING} Canal deletado em 3 segundos.")
        )
        await db.close_ticket(inter.channel.id)
        await asyncio.sleep(3)
        try:
            await inter.channel.delete()
        except discord.HTTPException:
            pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(TicketSelectView())
        bot.add_view(TicketMainView(0))

    async def _check_staff(self, inter: discord.Interaction) -> bool:
        if inter.user.guild_permissions.administrator:
            return True
        cfg = await db.get_guild_config(inter.guild.id)
        staff_ids = set(cfg.get("staff_roles") or [])
        return bool({r.id for r in inter.user.roles} & staff_ids)

    async def criar_ticket(self, inter: discord.Interaction, categoria: str, motivo: str):
        await inter.response.defer(ephemeral=True)
        cfg   = await db.get_guild_config(inter.guild.id)
        cat   = inter.guild.get_channel(cfg.get("ticket_category"))
        label = LABEL_MAP.get(categoria, categoria)

        staff_ids   = cfg.get("staff_roles") or []
        staff_roles = [inter.guild.get_role(rid) for rid in staff_ids if inter.guild.get_role(rid)]

        overwrites = {
            inter.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            inter.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for sr in staff_roles:
            overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        try:
            channel = await inter.guild.create_text_channel(
                name=_clean_channel_name(inter.user.name),
                category=cat if isinstance(cat, discord.CategoryChannel) else None,
                overwrites=overwrites,
            )
        except discord.Forbidden:
            return await inter.followup.send(
                embed=error_embed("Sem permissão", "Não consigo criar canais."), ephemeral=True
            )

        await db.open_ticket(inter.guild.id, inter.user.id, channel.id, categoria)

        emb = discord.Embed(
            title="*__Boas vindas__*! <:1000011429:1483272519522848849>",
            description=(
                f"- <:w_p:1445474432893063299> **Aberto por:** {inter.user.mention}\n"
                f"- <a:51047animatedarrowwhite:1430338988765347850> **Categoria:** {label}\n"
                "﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌﹌\n"
                f"*Precisando de ajuda? Este é o seu ticket de **atendimento**. Descreva com clareza o que você precisa* <:1000011430:1483272588066029739>\n\n"
                f"<a:1000006139:1475984068251226245> Agora {inter.user.mention} **aguarde a equipe**!"
            ),
            color=Colors.MAIN,
        )
        emb.set_thumbnail(url=inter.user.display_avatar.url)
        if cfg.get("ticket_banner"):
            emb.set_image(url=cfg["ticket_banner"])
        emb.set_footer(text=f"{inter.guild.name} • ID: {inter.user.id}")
        emb.timestamp = _now()

        staff_ping = " ".join(r.mention for r in staff_roles)
        await channel.send(
            content=f"{inter.user.mention} {staff_ping}".strip(),
            embed=emb,
            view=TicketMainView(inter.user.id),
        )

        # Log de abertura
        log_id = cfg.get("ticket_log") or cfg.get("log_channel")
        if log_id:
            lch = inter.guild.get_channel(log_id)
            if isinstance(lch, discord.TextChannel):
                le = discord.Embed(title=f"{E.FIRE} Ticket Aberto", color=Colors.SUCCESS)
                le.add_field(name="Usuário",   value=f"{inter.user.mention} (`{inter.user.id}`)", inline=True)
                le.add_field(name="Categoria", value=label, inline=True)
                le.add_field(name="Canal",     value=channel.mention, inline=True)
                le.add_field(name="Motivo",    value=motivo[:200], inline=False)
                le.timestamp = _now()
                try:
                    await lch.send(embed=le)
                except discord.HTTPException:
                    pass

        await inter.followup.send(
            embed=success_embed("Ticket criado!", f"Seu ticket foi aberto em {channel.mention}."),
            ephemeral=True,
        )

    async def fechar_ticket_confirmado(self, inter: discord.Interaction, opener_id: int, gerar_transcript_auto: bool = True):
        """Fecha o ticket após confirmação, gerando transcript automático no canal de log."""
        cfg    = await db.get_guild_config(inter.guild.id)
        ticket = await db.get_ticket_by_channel(inter.channel.id)

        if gerar_transcript_auto:
            try:
                file = await _gerar_transcript(inter.channel)
                log_id = cfg.get("ticket_log") or cfg.get("log_channel")
                if log_id:
                    lch = inter.guild.get_channel(log_id)
                    if isinstance(lch, discord.TextChannel):
                        opener = inter.guild.get_member(ticket["user_id"]) if ticket else None
                        le = discord.Embed(
                            title=f"{E.RULES} Ticket Fechado — Transcript",
                            color=0xe74c3c,
                        )
                        le.add_field(name="Canal",       value=inter.channel.name, inline=True)
                        le.add_field(name="Fechado por", value=inter.user.mention,  inline=True)
                        if opener:
                            le.add_field(name="Dono", value=opener.mention, inline=True)
                        le.timestamp = _now()
                        await lch.send(embed=le, file=file)
            except Exception as exc:
                log.warning(f"[TICKETS] Erro ao gerar transcript: {exc}")

        await inter.response.edit_message(
            embed=mod_embed(f"{E.ARROW_YELLOW} Fechando...", f"{E.LOADING} Canal deletado em 5 segundos."),
            view=None,
        )
        await db.close_ticket(inter.channel.id)
        await asyncio.sleep(5)
        try:
            await inter.channel.delete()
        except discord.HTTPException:
            pass

    # ── Slash commands ─────────────────────────────────────────────────────
    ticket_group = app_commands.Group(
        name="ticket",
        description="Sistema de tickets",
        default_permissions=discord.Permissions(administrator=True),
    )

    @ticket_group.command(name="setup", description="Configura o sistema de tickets")
    @app_commands.describe(
        categoria="Categoria para os tickets",
        cargo_staff="Cargo principal da staff",
        cargo_staff_2="2º cargo (opcional)",
        cargo_staff_3="3º cargo (opcional)",
        canal_log="Canal de logs",
        banner_url="URL do banner no ticket",
    )
    async def ticket_setup(
        self, inter: discord.Interaction,
        categoria: discord.CategoryChannel,
        cargo_staff: discord.Role,
        cargo_staff_2: discord.Role = None,
        cargo_staff_3: discord.Role = None,
        canal_log: discord.TextChannel = None,
        banner_url: str = None,
    ):
        cargos = [cargo_staff]
        for c in [cargo_staff_2, cargo_staff_3]:
            if c and c.id not in {x.id for x in cargos}:
                cargos.append(c)

        fields: dict = {
            "ticket_category": categoria.id,
            "staff_roles":     [c.id for c in cargos],
        }
        if canal_log:
            fields["ticket_log"] = canal_log.id
        if banner_url:
            fields["ticket_banner"] = banner_url
        await db.upsert_guild_config(inter.guild.id, **fields)

        await inter.response.send_message(embed=success_embed("Tickets configurados!",
            f"{E.SYMBOL} Categoria: {categoria.name}\n"
            f"{E.CALENDAR} Staff: {', '.join(c.mention for c in cargos)}\n"
            f"{E.LINK} Log: {canal_log.mention if canal_log else 'Não definido'}\n"
            f"{E.GEM} Banner: {'Configurado' if banner_url else 'Nenhum'}\n\n"
            f"{E.ARROW_BLUE} Use `/ticket painel` para enviar o painel."
        ), ephemeral=True)

    @ticket_group.command(name="painel", description="Envia o painel de tickets em um canal")
    @app_commands.describe(
        canal="Canal", titulo="Título", descricao="Descrição", imagem="URL da imagem"
    )
    async def ticket_painel(self, inter: discord.Interaction,
                             canal: discord.TextChannel,
                             titulo: str = "Suporte | Ticket",
                             descricao: str = "Abra um ticket selecionando a categoria abaixo.",
                             imagem: str = None):
        emb = discord.Embed(
            title=f"{E.FIRE} {titulo}",
            description=(
                f"{E.ARROW_BLUE} {descricao}\n\n"
                f"{E.SPARKLE} **Categorias disponíveis:**\n"
                f"{E.ARROW} Suporte Geral\n"
                f"{E.ARROW} Denúncias\n"
                f"{E.ARROW} Compra de VIP\n"
                f"{E.ARROW} Resgate de Prêmio\n"
                f"{E.ARROW} Parceria\n"
                f"{E.ARROW} Outros\n\n"
                f"{E.ORB_GREEN} Selecione abaixo e aguarde! {E.HEARTS_S}"
            ),
            color=Colors.MAIN,
        )
        if imagem:
            emb.set_image(url=imagem)
        emb.set_footer(text=f"{inter.guild.name} • Ticket")
        emb.timestamp = _now()
        try:
            await canal.send(embed=emb, view=TicketSelectView())
            await inter.response.send_message(
                embed=success_embed("Painel enviado!", f"Painel publicado em {canal.mention}."),
                ephemeral=True,
            )
        except discord.Forbidden:
            await inter.response.send_message(
                embed=error_embed("Sem permissão", f"Não posso enviar em {canal.mention}."),
                ephemeral=True,
            )

    @ticket_group.command(name="lista", description="Lista todos os tickets abertos")
    async def ticket_lista(self, inter: discord.Interaction):
        tickets = await db.list_open_tickets(inter.guild.id)
        if not tickets:
            return await inter.response.send_message(
                embed=success_embed("Sem tickets", "Nenhum ticket aberto."), ephemeral=True
            )
        emb = discord.Embed(title=f"{E.FIRE} Tickets Abertos ({len(tickets)})", color=Colors.MAIN)
        for t in tickets[:15]:
            ch     = inter.guild.get_channel(t["channel_id"])
            member = inter.guild.get_member(t["user_id"])
            emb.add_field(
                name=ch.name if ch else f"Canal {t['channel_id']}",
                value=f"{member.mention if member else t['user_id']} · {LABEL_MAP.get(t['categoria'], t['categoria'])}",
                inline=False,
            )
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb, ephemeral=True)

    @ticket_group.command(name="transcript", description="Gera o transcript do ticket atual")
    async def ticket_transcript(self, inter: discord.Interaction):
        if not await self._check_staff(inter):
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Apenas a staff pode usar este comando."), ephemeral=True
            )
        ticket = await db.get_ticket_by_channel(inter.channel.id)
        if not ticket:
            return await inter.response.send_message(
                embed=error_embed("Erro", "Este canal não é um ticket."), ephemeral=True
            )
        await inter.response.defer(ephemeral=True)
        file = await _gerar_transcript(inter.channel)
        await inter.followup.send(
            embed=success_embed("Transcript gerado", f"Log do canal `{inter.channel.name}`."),
            file=file, ephemeral=True,
        )

    @ticket_group.command(name="fechar", description="Fecha o ticket do canal atual")
    async def ticket_fechar(self, inter: discord.Interaction):
        ticket = await db.get_ticket_by_channel(inter.channel.id)
        if not ticket:
            return await inter.response.send_message(
                embed=error_embed("Erro", "Este canal não é um ticket."), ephemeral=True
            )
        is_staff = await self._check_staff(inter)
        is_owner = inter.user.id == ticket["user_id"]
        if not (is_staff or is_owner):
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Apenas a staff ou quem abriu pode fechar."),
                ephemeral=True,
            )
        emb = discord.Embed(
            title="⚠️ Fechar Ticket",
            description=(
                f"{E.ARROW_YELLOW} Tem certeza que deseja fechar este ticket?\n\n"
                f"{E.RULES} Um **transcript** será gerado automaticamente.\n"
                f"{E.LOADING} O canal será deletado em seguida."
            ),
            color=0xf39c12,
        )
        emb.timestamp = _now()
        await inter.response.send_message(
            embed=emb,
            view=ConfirmarFechamentoView(ticket["user_id"], gerar_transcript=True),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
