"""cogs/config.py — Configurações de logs, boas-vindas e AutoMod."""
from __future__ import annotations

import logging
import os
import re
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from db import database as db
from utils.constants import E, _now
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.config")


def _looks_like_http_url(value: Optional[str]) -> bool:
    if not value:
        return True
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_hex(raw: str, default: int = WHITE) -> int:
    value = (raw or "").strip().lstrip("#")
    if not value:
        return default
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError("Use uma cor no formato #RRGGBB.")
    return int(value, 16)


def _render_welcome(template: str, guild: discord.Guild, member: discord.Member) -> str:
    replacements = {
        "{nome}": member.display_name,
        "{mencao}": member.mention,
        "{servidor}": guild.name,
        "{count}": str(guild.member_count or 0),
    }
    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)
    return clip(result, 4000, fallback="Seja bem-vindo(a)!")


def _welcome_embed(cfg: dict, guild: discord.Guild, member: discord.Member) -> discord.Embed:
    default_message = (
        f"{E.CROWN_PINK} Seja muito bem-vindo(a), **{{nome}}**!\n\n"
        f"{E.SPARKLE} Você é o **{{count}}º** membro!\n"
        f"{E.ARROW} Leia as regras e aproveite. {E.HEARTS_S}"
    )
    embed = discord.Embed(
        title=clip(cfg.get("welcome_titulo") or f"{E.RING} Novo membro chegou!", 256),
        description=_render_welcome(cfg.get("welcome_msg") or default_message, guild, member),
        color=int(cfg.get("welcome_cor") or WHITE),
        timestamp=_now(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=clip(cfg.get("welcome_rodape") or f"{guild.name} • Bem-vindo(a)!", 200))
    banner = cfg.get("welcome_banner")
    if _looks_like_http_url(banner) and banner:
        embed.set_image(url=banner)
    return embed


class BoasVindasModal(discord.ui.Modal, title="Editar boas-vindas"):
    titulo_f = discord.ui.TextInput(label="Título", required=False, max_length=256)
    desc_f = discord.ui.TextInput(
        label="Mensagem: {nome}, {mencao}, {servidor}, {count}",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000,
    )
    cor_f = discord.ui.TextInput(label="Cor hexadecimal", required=False, max_length=7)
    rodape_f = discord.ui.TextInput(label="Rodapé", required=False, max_length=256)
    banner_f = discord.ui.TextInput(label="URL do banner", required=False, max_length=500)

    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self.titulo_f.default = cfg.get("welcome_titulo") or ""
        self.desc_f.default = (cfg.get("welcome_msg") or "")[:2000]
        self.cor_f.default = f"#{int(cfg.get('welcome_cor') or WHITE):06X}"
        self.rodape_f.default = cfg.get("welcome_rodape") or ""
        self.banner_f.default = cfg.get("welcome_banner") or ""

    async def on_submit(self, inter: discord.Interaction) -> None:
        banner = self.banner_f.value.strip() or None
        if not _looks_like_http_url(banner):
            await inter.response.send_message(view=card("URL inválida", "Use uma URL http:// ou https://."), ephemeral=True)
            return
        try:
            color = _parse_hex(self.cor_f.value, WHITE)
        except ValueError as exc:
            await inter.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
            return
        await db.upsert_guild_config(
            inter.guild_id,
            welcome_titulo=self.titulo_f.value.strip() or None,
            welcome_msg=self.desc_f.value.strip() or None,
            welcome_cor=color,
            welcome_rodape=self.rodape_f.value.strip() or None,
            welcome_banner=banner,
        )
        cfg = await db.get_guild_config(inter.guild_id)
        await inter.response.send_message(
            content="Preview atualizado:",
            embed=_welcome_embed(cfg, inter.guild, inter.user),
            ephemeral=True,
        )


class BoasVindasView(discord.ui.View):
    def __init__(self, author_id: int, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.author_id = author_id
        self.guild_id = guild_id

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.author_id:
            await inter.response.send_message("Apenas quem abriu o painel pode usá-lo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.primary, emoji="✏️")
    async def editar(self, inter: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await db.get_guild_config(self.guild_id)
        await inter.response.send_modal(BoasVindasModal(cfg))

    @discord.ui.button(label="Alternar DM", style=discord.ButtonStyle.secondary, emoji="📩")
    async def toggle_dm(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = await db.get_guild_config(self.guild_id)
        enabled = not bool(cfg.get("welcome_dm"))
        await db.upsert_guild_config(self.guild_id, welcome_dm=enabled)
        button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        await inter.followup.send(view=card("DM de boas-vindas", f"DM {'ativada' if enabled else 'desativada'}."), ephemeral=True)

    @discord.ui.button(label="Testar", style=discord.ButtonStyle.success, emoji="🚀")
    async def testar(self, inter: discord.Interaction, _: discord.ui.Button) -> None:
        cog = inter.client.get_cog("Config")
        if cog:
            sent = await cog._send_welcome(inter.guild, inter.user)
            text = "Teste enviado no canal configurado." if sent else "Configure um canal válido primeiro."
        else:
            text = "O módulo de configurações não está carregado."
        await inter.response.send_message(view=card("Teste de boas-vindas", text), ephemeral=True)

    @discord.ui.button(label="Resetar", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def resetar(self, inter: discord.Interaction, _: discord.ui.Button) -> None:
        await db.upsert_guild_config(
            self.guild_id,
            welcome_msg=None,
            welcome_banner=None,
            welcome_dm=False,
            welcome_cor=WHITE,
            welcome_titulo=None,
            welcome_rodape=None,
        )
        await inter.response.send_message(view=card("Configurações resetadas", "A aparência voltou ao padrão."), ephemeral=True)


class Config(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _send_welcome(self, guild: discord.Guild, member: discord.Member) -> bool:
        cfg = await db.get_guild_config(guild.id)
        channel = guild.get_channel(cfg.get("welcome_canal")) if cfg.get("welcome_canal") else None
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            await channel.send(
                content=member.mention,
                embed=_welcome_embed(cfg, guild, member),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.exception("Falha ao enviar boas-vindas no servidor %s", guild.id)
            return False

        if cfg.get("welcome_dm"):
            dm_embed = discord.Embed(
                title=f"Olá, {member.display_name}!",
                description=f"Você entrou em **{guild.name}**. Leia as regras e aproveite a comunidade.",
                color=WHITE,
                timestamp=_now(),
            )
            if guild.icon:
                dm_embed.set_thumbnail(url=guild.icon.url)
            try:
                await member.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
        return True

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._send_welcome(member.guild, member)

    config_group = app_commands.Group(
        name="config",
        description="Configurações do servidor",
        default_permissions=discord.Permissions(administrator=True),
    )

    @config_group.command(name="log", description="Define o canal de logs de moderação")
    async def cfg_log(self, inter: discord.Interaction, canal: discord.TextChannel) -> None:
        await db.upsert_guild_config(inter.guild_id, log_channel=canal.id, logs_channel=canal.id)
        await inter.response.send_message(view=card("Canal de logs definido", f"Os registros serão enviados em {canal.mention}."), ephemeral=True)

    @config_group.command(name="boas-vindas", description="Configura o sistema de boas-vindas")
    async def cfg_welcome(self, inter: discord.Interaction, canal: discord.TextChannel, dm: bool = False) -> None:
        await db.upsert_guild_config(inter.guild_id, welcome_canal=canal.id, welcome_dm=dm)
        cfg = await db.get_guild_config(inter.guild_id)
        embed = discord.Embed(
            title="Boas-vindas configuradas",
            description=(
                f"**Canal:** {canal.mention}\n"
                f"**DM:** {'Ativada' if dm else 'Desativada'}\n"
                f"**Cor:** `#{int(cfg.get('welcome_cor') or WHITE):06X}`\n\n"
                "Use os botões para editar, testar ou resetar."
            ),
            color=WHITE,
            timestamp=_now(),
        )
        await inter.response.send_message(embed=embed, view=BoasVindasView(inter.user.id, inter.guild_id), ephemeral=True)

    @config_group.command(name="boas-vindas-ver", description="Exibe as configurações de boas-vindas")
    async def cfg_welcome_ver(self, inter: discord.Interaction) -> None:
        cfg = await db.get_guild_config(inter.guild_id)
        channel = inter.guild.get_channel(cfg.get("welcome_canal")) if cfg.get("welcome_canal") else None
        embed = discord.Embed(
            title="Boas-vindas • Configuração",
            description=(
                f"**Canal:** {channel.mention if channel else 'Não configurado'}\n"
                f"**DM:** {'Ativada' if cfg.get('welcome_dm') else 'Desativada'}\n"
                f"**Cor:** `#{int(cfg.get('welcome_cor') or WHITE):06X}`\n"
                f"**Título:** {clip(cfg.get('welcome_titulo') or 'Padrão', 200)}\n"
                f"**Banner:** {'Configurado' if cfg.get('welcome_banner') else 'Nenhum'}"
            ),
            color=WHITE,
            timestamp=_now(),
        )
        await inter.response.send_message(embed=embed, view=BoasVindasView(inter.user.id, inter.guild_id), ephemeral=True)

    @config_group.command(name="boas-vindas-testar", description="Simula a mensagem de boas-vindas")
    async def cfg_welcome_test(self, inter: discord.Interaction) -> None:
        sent = await self._send_welcome(inter.guild, inter.user)
        await inter.response.send_message(
            view=card("Teste de boas-vindas", "Mensagem enviada." if sent else "Nenhum canal válido está configurado."),
            ephemeral=True,
        )

    @config_group.command(name="automod", description="Cria um conjunto básico de regras nativas do AutoMod")
    @app_commands.describe(canal_log="Canal opcional para alertas do AutoMod")
    async def cfg_automod(self, inter: discord.Interaction, canal_log: Optional[discord.TextChannel] = None) -> None:
        await inter.response.defer(ephemeral=True)
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            await inter.followup.send(view=card("BOT_TOKEN ausente", "Configure a variável BOT_TOKEN no Railway."), ephemeral=True)
            return

        rules = [
            ("[Bot] Ofensas diretas", ["idiota", "imbecil", "babaca", "otário", "fdp", "vsf"]),
            ("[Bot] Ameaças explícitas", ["*vou te matar*", "*te mato*", "*vou explodir*", "*vou atirar em*"]),
            ("[Bot] Golpes e convites", ["*free nitro*", "*nitro grátis*", "bit.ly/*", "tinyurl.com/*"]),
            ("[Bot] Conteúdo sexual explícito", ["*porn*", "*nudes*", "*pack de nudes*", "onlyfans.com/*"]),
        ]
        base_url = f"https://discord.com/api/v10/guilds/{inter.guild_id}/auto-moderation/rules"
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        created = skipped = failed = 0
        errors: list[str] = []
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as session:
            try:
                async with session.get(base_url) as response:
                    existing_data = await response.json(content_type=None) if response.status == 200 else []
                existing_names = {item.get("name") for item in existing_data if isinstance(item, dict)}
            except (aiohttp.ClientError, ValueError):
                existing_names = set()

            for name, keywords in rules:
                if name in existing_names:
                    skipped += 1
                    continue
                actions = [{"type": 1, "metadata": {"custom_message": "Mensagem bloqueada pelas regras do servidor."}}]
                if canal_log:
                    actions.append({"type": 2, "metadata": {"channel_id": str(canal_log.id)}})
                payload = {
                    "name": name,
                    "event_type": 1,
                    "trigger_type": 1,
                    "trigger_metadata": {"keyword_filter": keywords},
                    "actions": actions,
                    "enabled": True,
                }
                try:
                    async with session.post(base_url, json=payload) as response:
                        if response.status in {200, 201}:
                            created += 1
                        else:
                            failed += 1
                            body = await response.text()
                            errors.append(f"{name}: HTTP {response.status} {clip(body, 120)}")
                except aiohttp.ClientError as exc:
                    failed += 1
                    errors.append(f"{name}: {type(exc).__name__}")

        if canal_log:
            await db.upsert_guild_config(inter.guild_id, log_channel=canal_log.id, logs_channel=canal_log.id)
        description = f"Criadas: **{created}**\nJá existentes: **{skipped}**\nFalhas: **{failed}**"
        if errors:
            description += "\n\n" + "\n".join(f"- {clip(item, 180)}" for item in errors[:4])
        await inter.followup.send(view=card("AutoMod configurado", description), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Config(bot))
