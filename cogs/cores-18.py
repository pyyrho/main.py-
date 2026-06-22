"""cogs/cores.py — Sistema persistente de Nick Color e gradientes."""
from __future__ import annotations

import logging
import re
from typing import Optional, Type
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from db.database import get_pool
from utils.ui_components import WHITE, card

log = logging.getLogger("multibot.cores")
BOT_COLOR = WHITE

CORES_NORMAIS = [
    ("vermelho", "Vermelho", "<:1000010231:1482084219814416464>", 0xE74C3C),
    ("laranja", "Laranja", "<:1000010232:1482092477946134579>", 0xE67E22),
    ("amarelo", "Amarelo", "<:1000010233:1482092507755188244>", 0xF1C40F),
    ("verde", "Verde", "<:1000010234:1482092539195424768>", 0x2ECC71),
    ("azul", "Azul", "<:1000010235:1482092570023825591>", 0x3498DB),
    ("rosa", "Rosa", "<:1000010236:1482092600172351589>", 0xFF69B4),
    ("marrom", "Marrom", "<:1000010263:1482103964215279706>", 0x8B4513),
    ("branco", "Branco", "<:1000010273:1482103994913525892>", 0xFFFFFF),
    ("roxo", "Roxo", "<:1000018289:1492560763682951178>", 0x8A2BE2),
]
CORES_DEGRADE = [
    ("grad_1", "Degradê 1", "<:1000010250:1482092724428603412>", "Degradê"),
    ("grad_2", "Degradê 2", "<:1000010264:1482104044699779173>", "Degradê"),
    ("grad_3", "Degradê 3", "<:1000010265:1482104072898347028>", "Degradê"),
    ("grad_4", "Degradê 4", "<:1000010266:1482104100320710778>", "Degradê"),
    ("grad_5", "Degradê 5", "<:1000010267:1482104126753079498>", "Degradê"),
    ("grad_6", "Degradê 6", "<:1000010268:1482104151751004421>", "Degradê"),
    ("grad_7", "Degradê 7", "<:1000010269:1482104177302966342>", "Degradê"),
    ("grad_8", "Degradê 8", "<:1000010270:1482104213315260466>", "Degradê"),
]


def _valid_url(value: Optional[str]) -> bool:
    if not value:
        return True
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_hex(raw: Optional[str], default: int = BOT_COLOR) -> int:
    value = (raw or "").strip().lstrip("#")
    if not value:
        return default
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError("Use uma cor no formato #RRGGBB.")
    return int(value, 16)


def _role_manageable(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(
        me
        and me.guild_permissions.manage_roles
        and role < me.top_role
        and not role.managed
        and role != guild.default_role
    )


class CoresStorage:
    def __init__(self) -> None:
        self._cache: dict[int, dict[str, str]] = {}

    async def ensure_table(self) -> None:
        await get_pool().execute(
            """
            CREATE TABLE IF NOT EXISTS cores_config (
                guild_id BIGINT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            )
            """
        )

    async def preload(self) -> None:
        await self.ensure_table()
        rows = await get_pool().fetch("SELECT guild_id, key, value FROM cores_config")
        self._cache.clear()
        for row in rows:
            self._cache.setdefault(row["guild_id"], {})[row["key"]] = row["value"]

    async def get(self, guild_id: int, key: str) -> Optional[str]:
        return self._cache.get(guild_id, {}).get(key)

    async def set(self, guild_id: int, key: str, value: str) -> None:
        await get_pool().execute(
            """
            INSERT INTO cores_config (guild_id, key, value)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, key) DO UPDATE SET value=$3
            """,
            guild_id,
            key,
            value,
        )
        self._cache.setdefault(guild_id, {})[key] = value

    async def delete(self, guild_id: int, key: str) -> None:
        await get_pool().execute("DELETE FROM cores_config WHERE guild_id=$1 AND key=$2", guild_id, key)
        self._cache.get(guild_id, {}).pop(key, None)


storage = CoresStorage()


async def _all_role_ids_for_type(guild_id: int, key: str) -> set[int]:
    source = CORES_DEGRADE if key.startswith("grad_") else CORES_NORMAIS
    result: set[int] = set()
    for item_key, *_ in source:
        raw = await storage.get(guild_id, f"role_{item_key}")
        if raw and raw.isdigit():
            result.add(int(raw))
    return result


class ColorButton(discord.ui.Button):
    def __init__(self, key: str, label: str, emoji_str: str, row: int) -> None:
        super().__init__(
            label=None,
            emoji=discord.PartialEmoji.from_str(emoji_str),
            style=discord.ButtonStyle.secondary,
            custom_id=f"cores:{key}",
            row=row,
        )
        self.role_key = key
        self.color_label = label

    async def callback(self, inter: discord.Interaction) -> None:
        if not inter.guild or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Este botão só funciona em servidores.", ephemeral=True)
            return
        role_id = await storage.get(inter.guild.id, f"role_{self.role_key}")
        if not role_id or not role_id.isdigit():
            await inter.response.send_message(view=card("Cor não configurada", "A equipe ainda não vinculou um cargo a esta cor."), ephemeral=True)
            return
        role = inter.guild.get_role(int(role_id))
        if not role:
            await inter.response.send_message(view=card("Cargo removido", "O cargo desta cor não existe mais."), ephemeral=True)
            return
        if not _role_manageable(inter.guild, role):
            await inter.response.send_message(view=card("Cargo inalcançável", "O cargo do bot precisa ficar acima do cargo de cor."), ephemeral=True)
            return

        if self.role_key.startswith("grad_"):
            vip_channel_id = await storage.get(inter.guild.id, "vip_channel")
            if vip_channel_id and vip_channel_id.isdigit():
                vip_channel = inter.guild.get_channel(int(vip_channel_id))
                if isinstance(vip_channel, discord.abc.GuildChannel):
                    if not vip_channel.permissions_for(inter.user).view_channel:
                        await inter.response.send_message(
                            view=card("Cor exclusiva", "Você não possui acesso às cores degradê deste servidor."),
                            ephemeral=True,
                        )
                        return

        all_ids = await _all_role_ids_for_type(inter.guild.id, self.role_key)
        remove = [member_role for member_role in inter.user.roles if member_role.id in all_ids and member_role.id != role.id]
        try:
            if remove:
                await inter.user.remove_roles(*remove, reason="Nick Color: troca de cor")
            if role in inter.user.roles:
                await inter.user.remove_roles(role, reason="Nick Color: remoção voluntária")
                title, description = "Cor removida", f"**{role.name}** foi removida do seu perfil."
            else:
                await inter.user.add_roles(role, reason="Nick Color: seleção voluntária")
                title, description = "Cor aplicada", f"**{role.name}** foi adicionada ao seu perfil."
        except discord.HTTPException:
            log.exception("Falha ao alterar cor %s para %s", role.id, inter.user.id)
            await inter.response.send_message(view=card("Falha ao alterar cor", "Tente novamente ou avise a equipe."), ephemeral=True)
            return
        await inter.response.send_message(view=card(title, description), ephemeral=True)


class ColorNormalView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        for index, (key, label, emoji, _) in enumerate(CORES_NORMAIS):
            self.add_item(ColorButton(key, label, emoji, index // 3))


class ColorDegradeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        for index, (key, label, emoji, _) in enumerate(CORES_DEGRADE):
            self.add_item(ColorButton(key, label, emoji, index // 3))


class EmbedEditorModal(discord.ui.Modal, title="Personalizar painel"):
    titulo = discord.ui.TextInput(label="Título", max_length=256)
    descricao = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=2000)
    cor = discord.ui.TextInput(label="Cor #RRGGBB", required=False, max_length=7)
    thumbnail = discord.ui.TextInput(label="URL da miniatura", required=False, max_length=500)
    banner = discord.ui.TextInput(label="URL do banner", required=False, max_length=500)

    def __init__(
        self,
        channel: discord.TextChannel,
        view_cls: Type[discord.ui.View],
        default_title: str,
        default_description: str,
        default_color: int,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.view_cls = view_cls
        self.titulo.default = default_title
        self.descricao.default = default_description[:2000]
        self.cor.default = f"#{default_color:06X}"

    async def on_submit(self, inter: discord.Interaction) -> None:
        thumbnail = self.thumbnail.value.strip() or None
        banner = self.banner.value.strip() or None
        if not _valid_url(thumbnail) or not _valid_url(banner):
            await inter.response.send_message(view=card("URL inválida", "Use uma URL http:// ou https://."), ephemeral=True)
            return
        try:
            color = _parse_hex(self.cor.value, BOT_COLOR)
        except ValueError as exc:
            await inter.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
            return
        embed = discord.Embed(
            title=self.titulo.value or "Nick Color",
            description=self.descricao.value or "Escolha uma cor abaixo.",
            color=color,
        )
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        if banner:
            embed.set_image(url=banner)
        embed.set_footer(text="Nick Color • apenas uma cor de cada tipo por vez")
        try:
            await self.channel.send(embed=embed, view=self.view_cls())
        except discord.HTTPException:
            await inter.response.send_message(view=card("Falha ao enviar", "Não consegui publicar o painel neste canal."), ephemeral=True)
            return
        await inter.response.send_message(view=card("Painel enviado", f"Publicado em {self.channel.mention}."), ephemeral=True)


class PainelConfirmView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        channel: discord.TextChannel,
        view_cls: Type[discord.ui.View],
        title: str,
        description: str,
        color: int,
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.channel = channel
        self.view_cls = view_cls
        self.title = title
        self.description = description
        self.color = color

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.author_id:
            await inter.response.send_message("Apenas quem abriu o painel pode usá-lo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Enviar padrão", style=discord.ButtonStyle.success, emoji="➡️")
    async def send_default(self, inter: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(title=self.title, description=self.description, color=self.color)
        embed.set_footer(text="Nick Color • apenas uma cor de cada tipo por vez")
        try:
            await self.channel.send(embed=embed, view=self.view_cls())
        except discord.HTTPException:
            await inter.response.send_message(view=card("Falha ao enviar", "Verifique as permissões do bot."), ephemeral=True)
            return
        await inter.response.send_message(view=card("Painel enviado", f"Publicado em {self.channel.mention}."), ephemeral=True)
        self.stop()

    @discord.ui.button(label="Personalizar", style=discord.ButtonStyle.primary, emoji="📌")
    async def customize(self, inter: discord.Interaction, _: discord.ui.Button) -> None:
        await inter.response.send_modal(
            EmbedEditorModal(self.channel, self.view_cls, self.title, self.description, self.color)
        )


class Cores(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await storage.preload()
        self.bot.add_view(ColorNormalView())
        self.bot.add_view(ColorDegradeView())

    async def _role_id(self, guild_id: int, key: str) -> Optional[int]:
        raw = await storage.get(guild_id, f"role_{key}")
        return int(raw) if raw and raw.isdigit() else None

    async def _all_color_role_ids(self, guild_id: int) -> set[int]:
        result: set[int] = set()
        for key, *_ in CORES_NORMAIS + CORES_DEGRADE:
            role_id = await self._role_id(guild_id, key)
            if role_id:
                result.add(role_id)
        return result

    cores_group = app_commands.Group(name="cores", description="Sistema de Nick Color")

    @cores_group.command(name="setup_normal", description="Vincula um cargo a uma cor normal")
    @app_commands.choices(cor=[app_commands.Choice(name=label, value=key) for key, label, *_ in CORES_NORMAIS])
    @app_commands.default_permissions(administrator=True)
    async def cores_setup_normal(self, inter: discord.Interaction, cor: str, cargo: discord.Role) -> None:
        if not _role_manageable(inter.guild, cargo):
            await inter.response.send_message(view=card("Cargo inalcançável", "Coloque o cargo do bot acima deste cargo."), ephemeral=True)
            return
        await storage.set(inter.guild_id, f"role_{cor}", str(cargo.id))
        label = next((label for key, label, *_ in CORES_NORMAIS if key == cor), cor)
        await inter.response.send_message(view=card("Cor normal configurada", f"**{label}** → {cargo.mention}"), ephemeral=True)

    @cores_group.command(name="setup_degrade", description="Vincula um cargo a uma cor degradê")
    @app_commands.choices(cor=[app_commands.Choice(name=label, value=key) for key, label, *_ in CORES_DEGRADE])
    @app_commands.default_permissions(administrator=True)
    async def cores_setup_degrade(self, inter: discord.Interaction, cor: str, cargo: discord.Role) -> None:
        if not _role_manageable(inter.guild, cargo):
            await inter.response.send_message(view=card("Cargo inalcançável", "Coloque o cargo do bot acima deste cargo."), ephemeral=True)
            return
        await storage.set(inter.guild_id, f"role_{cor}", str(cargo.id))
        label = next((label for key, label, *_ in CORES_DEGRADE if key == cor), cor)
        await inter.response.send_message(
            view=card("Cor degradê configurada", f"**{label}** → {cargo.mention}\nConfigure o gradiente nas opções do cargo."),
            ephemeral=True,
        )

    @cores_group.command(name="setup_vip", description="Define o canal usado como requisito para cores degradê")
    @app_commands.default_permissions(administrator=True)
    async def cores_setup_vip(self, inter: discord.Interaction, canal: discord.TextChannel) -> None:
        await storage.set(inter.guild_id, "vip_channel", str(canal.id))
        await inter.response.send_message(
            view=card("Acesso VIP configurado", f"Somente membros que enxergam {canal.mention} poderão usar degradês."),
            ephemeral=True,
        )

    async def _panel_data(self, guild: discord.Guild, degrade: bool) -> tuple[str, str, int, Type[discord.ui.View]]:
        source = CORES_DEGRADE if degrade else CORES_NORMAIS
        lines = []
        for key, label, emoji, _ in source:
            role_id = await self._role_id(guild.id, key)
            role = guild.get_role(role_id) if role_id else None
            lines.append(f"{emoji} **{label}** → {role.mention if role else '*não configurada*'}")
        if degrade:
            return (
                "✨ Nick Color • Degradês",
                "Cores exclusivas com gradiente.\n\n" + "\n".join(lines) + "\n\nClique novamente para remover.",
                0x9B59B6,
                ColorDegradeView,
            )
        return (
            "🎨 Nick Color",
            "Escolha uma cor para o seu nome.\n\n" + "\n".join(lines) + "\n\nClique novamente para remover.",
            BOT_COLOR,
            ColorNormalView,
        )

    @cores_group.command(name="painel", description="Envia o painel de cores normais")
    @app_commands.default_permissions(manage_guild=True)
    async def cores_painel(self, inter: discord.Interaction, canal: Optional[discord.TextChannel] = None) -> None:
        channel = canal or inter.channel
        if not isinstance(channel, discord.TextChannel):
            await inter.response.send_message(view=card("Canal inválido", "Escolha um canal de texto."), ephemeral=True)
            return
        title, description, color, view_cls = await self._panel_data(inter.guild, False)
        prompt = discord.Embed(
            title="Painel de cores normais",
            description=f"Destino: {channel.mention}\nEscolha entre o modelo padrão e o editor personalizado.",
            color=WHITE,
        )
        await inter.response.send_message(
            embed=prompt,
            view=PainelConfirmView(inter.user.id, channel, view_cls, title, description, color),
            ephemeral=True,
        )

    @cores_group.command(name="painel_vip", description="Envia o painel de cores degradê")
    @app_commands.default_permissions(manage_guild=True)
    async def cores_painel_vip(self, inter: discord.Interaction, canal: Optional[discord.TextChannel] = None) -> None:
        channel = canal or inter.channel
        if not isinstance(channel, discord.TextChannel):
            await inter.response.send_message(view=card("Canal inválido", "Escolha um canal de texto."), ephemeral=True)
            return
        title, description, color, view_cls = await self._panel_data(inter.guild, True)
        prompt = discord.Embed(
            title="Painel de cores degradê",
            description=f"Destino: {channel.mention}\nEscolha entre o modelo padrão e o editor personalizado.",
            color=WHITE,
        )
        await inter.response.send_message(
            embed=prompt,
            view=PainelConfirmView(inter.user.id, channel, view_cls, title, description, color),
            ephemeral=True,
        )

    @cores_group.command(name="lista", description="Lista os cargos de cor configurados")
    @app_commands.default_permissions(manage_guild=True)
    async def cores_lista(self, inter: discord.Interaction) -> None:
        normal_lines = []
        for key, label, emoji, _ in CORES_NORMAIS:
            role_id = await self._role_id(inter.guild_id, key)
            role = inter.guild.get_role(role_id) if role_id else None
            normal_lines.append(f"{emoji} {label}: {role.mention if role else '`não configurado`'}")
        degrade_lines = []
        for key, label, emoji, _ in CORES_DEGRADE:
            role_id = await self._role_id(inter.guild_id, key)
            role = inter.guild.get_role(role_id) if role_id else None
            degrade_lines.append(f"{emoji} {label}: {role.mention if role else '`não configurado`'}")
        vip_raw = await storage.get(inter.guild_id, "vip_channel")
        vip = inter.guild.get_channel(int(vip_raw)) if vip_raw and vip_raw.isdigit() else None
        await inter.response.send_message(
            view=card(
                "Nick Color • Configuração",
                "**Cores normais**\n" + "\n".join(normal_lines) +
                "\n\n**Cores degradê**\n" + "\n".join(degrade_lines),
                fields=[("Canal VIP", vip.mention if vip else "Não configurado")],
            ),
            ephemeral=True,
        )

    @cores_group.command(name="remover", description="Remove todos os cargos de cor de um membro")
    @app_commands.default_permissions(manage_roles=True)
    async def cores_remover(self, inter: discord.Interaction, membro: discord.Member) -> None:
        await inter.response.defer(ephemeral=True)
        role_ids = await self._all_color_role_ids(inter.guild_id)
        roles = [role for role in membro.roles if role.id in role_ids and _role_manageable(inter.guild, role)]
        if roles:
            await membro.remove_roles(*roles, reason=f"Nick Color removido por {inter.user}")
        await inter.followup.send(view=card("Cores removidas", f"Foram removidos `{len(roles)}` cargo(s) de {membro.mention}."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Cores(bot))
