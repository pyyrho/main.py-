"""cogs/utilidade.py — Utilidades públicas, ajuda dinâmica e interações."""
from __future__ import annotations

import logging
import random
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.constants import Colors, E, _now
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.util")


def _parse_hex_color(raw: str | None, default: int = Colors.MAIN) -> int:
    if not raw:
        return default
    value = raw.strip().lstrip("#")
    if len(value) != 6 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ValueError("Use uma cor no formato #RRGGBB.")
    return int(value, 16)


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


_ACOES: dict[str, dict[str, object]] = {
    "kiss": {"frases": ["{a} beijou {b}!", "{a} deu um beijinho em {b}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} retribuiu o beijo de {a}!", "emoji": E.HEARTS_S, "emoji2": E.HEART},
    "hug": {"frases": ["{a} abraçou {b}!", "{b} ganhou um abraço de {a}!"], "frase_solo": "{a} precisa de um abraço!", "retribuir": True, "frase_ret": "{b} retribuiu o abraço!", "emoji": E.RING, "emoji2": E.HEARTS_S},
    "pat": {"frases": ["{a} fez carinho em {b}!", "{b} ganhou um pat de {a}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} retribuiu o carinho!", "emoji": E.SPARKLE, "emoji2": E.CROWN_PINK},
    "slap": {"frases": ["{a} deu um tapa em {b}!", "{b} levou um tapa de {a}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} devolveu o tapa em {a}!", "emoji": E.WARN_IC, "emoji2": E.FLAME_ORG},
    "poke": {"frases": ["{a} cutucou {b}!", "{b} foi cutucado por {a}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} cutucou {a} de volta!", "emoji": E.ARROW, "emoji2": E.SPARKLE},
    "bite": {"frases": ["{a} mordeu {b}!", "{b} foi mordido(a) por {a}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} mordeu {a} de volta!", "emoji": E.FLAME_PUR, "emoji2": E.HEART},
    "cry": {"frases": ["{a} consolou {b}!"], "frase_solo": "{a} está chorando...", "retribuir": False, "emoji": E.HEARTS_S, "emoji2": E.RING},
    "blush": {"frases": ["{b} fez {a} corar!"], "frase_solo": "{a} ficou vermelhinho(a)!", "retribuir": False, "emoji": E.HEART, "emoji2": E.SPARKLE},
    "dance": {"frases": ["{a} chamou {b} para dançar!"], "frase_solo": "{a} está dançando!", "retribuir": True, "frase_ret": "{b} aceitou dançar com {a}!", "emoji": E.SPARKLE, "emoji2": E.GEM_SHINE},
    "highfive": {"frases": ["{a} deu um toca aqui em {b}!"], "frase_solo": None, "retribuir": True, "frase_ret": "TOCA AQUI! {b} tocou com {a}!", "emoji": E.ORB_GREEN, "emoji2": E.VERIFY},
    "wave": {"frases": ["{a} acenou para {b}!"], "frase_solo": "{a} acenou para todo mundo!", "retribuir": False, "emoji": E.ARROW_W, "emoji2": E.HEARTS_S},
    "cuddle": {"frases": ["{a} se aconchegou com {b}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} se aconchegou com {a}!", "emoji": E.HEART, "emoji2": E.RING},
    "lick": {"frases": ["{a} lambeu {b}!", "{b} foi lambido(a) por {a}!"], "frase_solo": None, "retribuir": False, "emoji": E.FLAME_PUR, "emoji2": E.HEARTS_S},
    "yeet": {"frases": ["{a} arremessou {b} para o espaço. YEET!"], "frase_solo": None, "retribuir": False, "emoji": E.FIRE, "emoji2": E.FLAME_ORG},
    "nuzzle": {"frases": ["{a} se aninhou em {b}!"], "frase_solo": None, "retribuir": True, "frase_ret": "{b} retribuiu o carinho de {a}!", "emoji": E.CROWN_PINK, "emoji2": E.HEART},
}


async def _fetch_gif(session: aiohttp.ClientSession, action: str) -> str | None:
    try:
        async with session.get(
            f"https://nekos.best/api/v2/{action}",
            timeout=aiohttp.ClientTimeout(total=7),
        ) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
            results = data.get("results") or []
            url = results[0].get("url") if results else None
            return url if _valid_http_url(url) else None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        log.debug("Falha ao buscar GIF %s: %s", action, exc)
        return None


class InteractionLayout(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        cog: "Utilidade",
        action: str,
        author: discord.Member,
        target: discord.Member | None,
        text: str,
        gif: str | None,
        allow_return: bool,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.action = action
        self.author = author
        self.target = target
        container = discord.ui.Container(accent_color=WHITE)
        data = _ACOES[action]
        container.add_item(discord.ui.TextDisplay(f"## Interação\n\n{data['emoji']} {text} {data['emoji2']}"))
        if gif:
            container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=gif, description=action)))
        if allow_return:
            button = discord.ui.Button(label="Retribuir", style=discord.ButtonStyle.primary, emoji="💞")
            button.callback = self._return_action
            container.add_item(discord.ui.ActionRow(button))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# Pedido por {author.display_name}"))
        self.add_item(container)

    async def _return_action(self, interaction: discord.Interaction) -> None:
        if not self.target or interaction.user.id != self.target.id:
            await interaction.response.send_message(
                view=card("Botão reservado", f"Apenas {self.target.mention if self.target else 'o destinatário'} pode retribuir."),
                ephemeral=True,
            )
            return
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                item.label = "Retribuído"
        await interaction.response.edit_message(view=self)
        data = _ACOES[self.action]
        template = str(data.get("frase_ret") or "{b} retribuiu a interação de {a}!")
        text = template.format(a=self.author.mention, b=self.target.mention)
        gif = await _fetch_gif(self.cog.session, self.action) if self.cog.session else None
        await interaction.followup.send(
            view=InteractionLayout(
                cog=self.cog,
                action=self.action,
                author=self.target,
                target=self.author,
                text=text,
                gif=gif,
                allow_return=False,
            )
        )
        self.stop()


class EmbedModal(discord.ui.Modal, title="Criar embed"):
    titulo = discord.ui.TextInput(label="Título", max_length=256)
    descricao = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=4000)
    cor = discord.ui.TextInput(label="Cor hex (ex.: #590CEA)", required=False, max_length=7)
    rodape = discord.ui.TextInput(label="Rodapé", required=False, max_length=256)
    imagem = discord.ui.TextInput(label="URL da imagem", required=False, max_length=500)

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            color = _parse_hex_color(str(self.cor.value), Colors.MAIN)
        except ValueError as exc:
            await interaction.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
            return
        image = str(self.imagem.value).strip()
        if image and not _valid_http_url(image):
            await interaction.response.send_message(view=card("URL inválida", "A imagem deve começar com http:// ou https://."), ephemeral=True)
            return
        embed = discord.Embed(title=str(self.titulo.value), description=str(self.descricao.value), color=color, timestamp=_now())
        footer = str(self.rodape.value).strip()
        if footer:
            embed.set_footer(text=footer)
        if image:
            embed.set_image(url=image)
        try:
            await self.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(view=card("Sem permissão", f"Não consigo enviar mensagens em {self.channel.mention}."), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message(view=card("Falha ao enviar", "O Discord recusou a embed. Verifique os campos e a URL."), ephemeral=True)
            return
        await interaction.response.send_message(view=card("Embed enviada", f"Publicada em {self.channel.mention}."), ephemeral=True)


class EmbedEditModal(discord.ui.Modal, title="Editar embed"):
    novo_titulo = discord.ui.TextInput(label="Título", required=False, max_length=256)
    nova_desc = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, required=False, max_length=4000)
    nova_cor = discord.ui.TextInput(label="Cor hex", required=False, max_length=7)
    novo_rodape = discord.ui.TextInput(label="Rodapé", required=False, max_length=256)
    nova_imagem = discord.ui.TextInput(label="URL da imagem", required=False, max_length=500)

    def __init__(self, message: discord.Message) -> None:
        super().__init__()
        self.target = message
        old = message.embeds[0]
        self.novo_titulo.default = old.title or ""
        self.nova_desc.default = clip(old.description or "", 4000, fallback="")
        self.nova_cor.default = f"#{old.color.value:06X}" if old.color else ""
        self.novo_rodape.default = old.footer.text or ""
        self.nova_imagem.default = old.image.url or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        old = self.target.embeds[0]
        try:
            color = _parse_hex_color(str(self.nova_cor.value), old.color.value if old.color else Colors.MAIN)
        except ValueError as exc:
            await interaction.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
            return
        image = str(self.nova_imagem.value).strip()
        if image and not _valid_http_url(image):
            await interaction.response.send_message(view=card("URL inválida", "A imagem deve começar com http:// ou https://."), ephemeral=True)
            return
        embed = discord.Embed(
            title=str(self.novo_titulo.value).strip() or None,
            description=str(self.nova_desc.value).strip() or None,
            color=color,
            timestamp=_now(),
        )
        footer = str(self.novo_rodape.value).strip()
        if footer:
            embed.set_footer(text=footer)
        if image:
            embed.set_image(url=image)
        try:
            await self.target.edit(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(view=card("Sem permissão", "Não consigo editar essa mensagem."), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message(view=card("Falha ao editar", "O Discord recusou os novos dados."), ephemeral=True)
            return
        await interaction.response.send_message(view=card("Embed atualizada", "As alterações foram aplicadas."), ephemeral=True)


class HelpLayout(discord.ui.LayoutView):
    def __init__(self, pages: list[tuple[str, str]], author_id: int, bot_avatar: str | None = None) -> None:
        super().__init__(timeout=180)
        self.pages = pages
        self.author_id = author_id
        self.bot_avatar = bot_avatar
        self.page = 0
        self._build()

    def _build(self) -> None:
        self.clear_items()
        title, description = self.pages[self.page]
        container = discord.ui.Container(accent_color=WHITE)
        text = f"## {clip(title, 220)}\n\n{clip(description, 3000)}"
        if self.bot_avatar:
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(text), accessory=discord.ui.Thumbnail(self.bot_avatar)))
        else:
            container.add_item(discord.ui.TextDisplay(text))
        options = [
            discord.SelectOption(label=clip(page_title, 100), value=str(index), default=index == self.page)
            for index, (page_title, _) in enumerate(self.pages[:25])
        ]
        selector = discord.ui.Select(placeholder="Escolha uma categoria", options=options, min_values=1, max_values=1)
        selector.callback = self._select
        previous = discord.ui.Button(label="Anterior", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        next_button = discord.ui.Button(label="Próxima", style=discord.ButtonStyle.secondary, disabled=self.page >= len(self.pages) - 1)
        close = discord.ui.Button(label="Fechar", style=discord.ButtonStyle.danger)
        previous.callback = self._previous
        next_button.callback = self._next
        close.callback = self._close
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(selector))
        container.add_item(discord.ui.ActionRow(previous, next_button, close))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# Página {self.page + 1}/{len(self.pages)}"))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(view=card("Painel reservado", "Use `/ajuda` para abrir seu próprio painel."), ephemeral=True)
        return False

    async def _select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        self.page = max(0, min(int(values[0]) if values else 0, len(self.pages) - 1))
        self._build()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(len(self.pages) - 1, self.page + 1)
        self._build()
        await interaction.response.edit_message(view=self)

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=card("Ajuda fechada", "Use `/ajuda` quando precisar novamente.", timeout=None))
        self.stop()


class Utilidade(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"User-Agent": "DiscordMultibot/3.0"})

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    def _help_pages(self, guild: discord.Guild | None) -> list[tuple[str, str]]:
        commands_found = sorted(self.bot.tree.get_commands(), key=lambda command: command.name)
        pages: list[tuple[str, str]] = [
            (
                "Central de ajuda",
                "Este painel é montado a partir dos comandos realmente carregados no bot. "
                "Escolha uma categoria abaixo. As permissões do servidor ainda determinam quais comandos cada pessoa pode usar.",
            )
        ]
        standalone: list[str] = []
        for command in commands_found:
            if isinstance(command, app_commands.Group):
                lines: list[str] = []
                for child in sorted(command.commands, key=lambda item: item.name):
                    lines.append(f"`/{command.name} {child.name}` — {clip(child.description, 110)}")
                if lines:
                    pages.append((command.name.replace("-", " ").title(), "\n".join(lines[:22])))
            else:
                standalone.append(f"`/{command.name}` — {clip(command.description, 110)}")
        for start in range(0, len(standalone), 20):
            suffix = f" {start // 20 + 1}" if len(standalone) > 20 else ""
            pages.append((f"Comandos gerais{suffix}", "\n".join(standalone[start:start + 20])))
        return pages[:25]

    @app_commands.command(name="ping", description="Mostra a latência do bot")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        status = "Excelente" if latency < 100 else "Normal" if latency < 220 else "Alta"
        await interaction.response.send_message(
            view=card("Latência", f"A conexão WebSocket está em **{latency} ms**.", fields=[("Status", status)], footer="Resposta medida em tempo real"),
        )

    @app_commands.command(name="serverinfo", description="Exibe informações do servidor")
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        bots = sum(member.bot for member in guild.members)
        humans = max(0, (guild.member_count or len(guild.members)) - bots)
        await interaction.response.send_message(
            view=card(
                guild.name,
                "Resumo público deste servidor.",
                fields=[
                    ("Dono", f"<@{guild.owner_id}>"),
                    ("Membros", f"{guild.member_count or len(guild.members):,} total · {humans:,} pessoas · {bots:,} bots"),
                    ("Canais", f"{len(guild.text_channels)} texto · {len(guild.voice_channels)} voz · {len(guild.categories)} categorias"),
                    ("Cargos", f"{len(guild.roles)}"),
                    ("Boosts", f"{guild.premium_subscription_count} · nível {guild.premium_tier}"),
                    ("Criado", discord.utils.format_dt(guild.created_at, "F")),
                    ("ID", f"`{guild.id}`"),
                ],
                thumbnail=guild.icon.url if guild.icon else None,
                image=guild.banner.url if guild.banner else None,
            )
        )

    @app_commands.command(name="avatar", description="Exibe o avatar de um membro")
    @app_commands.describe(membro="Membro consultado; por padrão, você")
    async def avatar(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        member = membro or interaction.user
        asset = member.display_avatar.with_size(1024)
        links: list[str] = []
        for format_name in ("png", "jpg", "webp"):
            try:
                links.append(f"[{format_name.upper()}]({asset.with_format(format_name).url})")
            except (ValueError, TypeError):
                continue
        if asset.is_animated():
            links.append(f"[GIF]({asset.with_format('gif').url})")
        await interaction.response.send_message(
            view=card(
                f"Avatar de {member.display_name}",
                " · ".join(links),
                image=asset.url,
                footer=f"ID: {member.id}",
            )
        )

    @app_commands.command(name="ajuda", description="Lista os comandos carregados no bot")
    async def ajuda(self, interaction: discord.Interaction) -> None:
        pages = self._help_pages(interaction.guild)
        avatar = self.bot.user.display_avatar.url if self.bot.user else None
        await interaction.response.send_message(view=HelpLayout(pages, interaction.user.id, avatar))

    embed_group = app_commands.Group(
        name="embed",
        description="Criar e editar embeds",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @embed_group.command(name="criar", description="Cria e envia uma embed personalizada")
    @app_commands.describe(canal="Canal de destino")
    async def embed_criar(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        await interaction.response.send_modal(EmbedModal(canal))

    @embed_group.command(name="rapido", description="Envia uma embed simples rapidamente")
    @app_commands.describe(canal="Canal", titulo="Título", descricao="Conteúdo", cor="Cor hex")
    async def embed_rapido(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
        titulo: str,
        descricao: str,
        cor: str = "#590CEA",
    ) -> None:
        try:
            color = _parse_hex_color(cor)
        except ValueError as exc:
            await interaction.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
            return
        embed = discord.Embed(title=titulo, description=descricao, color=color, timestamp=_now())
        embed.set_footer(text=f"Criado por {interaction.user.display_name}")
        try:
            await canal.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(view=card("Sem permissão", f"Não consigo enviar em {canal.mention}."), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.response.send_message(view=card("Falha ao enviar", "O Discord recusou a embed."), ephemeral=True)
            return
        await interaction.response.send_message(view=card("Embed enviada", f"Publicada em {canal.mention}."), ephemeral=True)

    @embed_group.command(name="editar", description="Edita uma embed existente pelo ID da mensagem")
    @app_commands.describe(canal="Canal", message_id="ID da mensagem")
    async def embed_editar(self, interaction: discord.Interaction, canal: discord.TextChannel, message_id: str) -> None:
        try:
            message = await canal.fetch_message(int(message_id.strip()))
        except (ValueError, discord.NotFound):
            await interaction.response.send_message(view=card("Mensagem não encontrada", "Confira o canal e o ID informado."), ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message(view=card("Sem permissão", "Não consigo ler essa mensagem."), ephemeral=True)
            return
        if not self.bot.user or message.author.id != self.bot.user.id:
            await interaction.response.send_message(view=card("Mensagem de outro autor", "Só consigo editar mensagens enviadas por este bot."), ephemeral=True)
            return
        if not message.embeds:
            await interaction.response.send_message(view=card("Sem embed", "A mensagem não contém uma embed tradicional."), ephemeral=True)
            return
        await interaction.response.send_modal(EmbedEditModal(message))

    async def _interaction_command(
        self,
        interaction: discord.Interaction,
        action: str,
        member: discord.Member | None,
        *,
        solo_allowed: bool = False,
    ) -> None:
        if not member and not solo_allowed:
            await interaction.response.send_message(view=card("Membro necessário", "Escolha alguém para esta interação."), ephemeral=True)
            return
        if member and member.id == interaction.user.id and not solo_allowed:
            await interaction.response.send_message(view=card("Alvo inválido", "Escolha outra pessoa."), ephemeral=True)
            return
        if member and member.bot:
            await interaction.response.send_message(view=card("Alvo inválido", "Bots não participam destas interações."), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        data = _ACOES[action]
        if member and member.id != interaction.user.id:
            phrases = list(data["frases"])
            text = random.choice(phrases).format(a=interaction.user.mention, b=member.mention)
        else:
            template = data.get("frase_solo") or list(data["frases"])[0]
            text = str(template).format(a=interaction.user.mention, b="")
        gif = await _fetch_gif(self.session, action) if self.session else None
        await interaction.followup.send(
            view=InteractionLayout(
                cog=self,
                action=action,
                author=interaction.user,
                target=member,
                text=text,
                gif=gif,
                allow_return=bool(member and member.id != interaction.user.id and data.get("retribuir")),
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    @app_commands.command(name="kiss", description="Beije alguém")
    async def kiss(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "kiss", membro)

    @app_commands.command(name="hug", description="Abrace alguém")
    async def hug(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        await self._interaction_command(interaction, "hug", membro, solo_allowed=True)

    @app_commands.command(name="pat", description="Faça carinho em alguém")
    async def pat(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "pat", membro)

    @app_commands.command(name="slap", description="Dê um tapa em alguém")
    async def slap(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "slap", membro)

    @app_commands.command(name="poke", description="Cutuque alguém")
    async def poke(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "poke", membro)

    @app_commands.command(name="bite", description="Morda alguém")
    async def bite(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "bite", membro)

    @app_commands.command(name="cry", description="Chore ou console alguém")
    async def cry(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        await self._interaction_command(interaction, "cry", membro, solo_allowed=True)

    @app_commands.command(name="blush", description="Core ou elogie alguém")
    async def blush(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        await self._interaction_command(interaction, "blush", membro, solo_allowed=True)

    @app_commands.command(name="dance", description="Dance ou convide alguém")
    async def dance(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        await self._interaction_command(interaction, "dance", membro, solo_allowed=True)

    @app_commands.command(name="highfive", description="Dê um toca aqui")
    async def highfive(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "highfive", membro)

    @app_commands.command(name="wave", description="Acene para alguém")
    async def wave(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        await self._interaction_command(interaction, "wave", membro, solo_allowed=True)

    @app_commands.command(name="cuddle", description="Aconchegue-se com alguém")
    async def cuddle(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "cuddle", membro)

    @app_commands.command(name="lick", description="Lamba alguém")
    async def lick(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "lick", membro)

    @app_commands.command(name="yeet", description="Arremesse alguém para o espaço")
    async def yeet(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "yeet", membro)

    @app_commands.command(name="nuzzle", description="Faça carinho em alguém")
    async def nuzzle(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        await self._interaction_command(interaction, "nuzzle", membro)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        log.exception("Erro em utilidade: %s", original)
        if interaction.response.is_done():
            await interaction.followup.send(view=card("Erro inesperado", "Não foi possível concluir esta ação."), ephemeral=True)
        else:
            await interaction.response.send_message(view=card("Erro inesperado", "Não foi possível concluir esta ação."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utilidade(bot))
