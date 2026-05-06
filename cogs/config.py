"""cogs/config.py — Configurações do servidor (log, boas-vindas, automod).

v2 — correções:
- import aiohttp estava dentro do corpo de cfg_automod() (lazy import desnecessário).
  Movido para o topo do módulo, como os outros imports.
- import os (para BOT_TOKEN no automod) também movido para o topo.
"""

import asyncio
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from db import database as db
from utils.constants import Colors, E, success_embed, error_embed, _now

log = logging.getLogger("logos.config")


# ── Modal de boas-vindas ──────────────────────────────────────────────────────
class BoasVindasModal(discord.ui.Modal, title="Editar Embed de Boas-vindas"):
    titulo_f = discord.ui.TextInput(label="Título", required=False, max_length=256)
    desc_f   = discord.ui.TextInput(label="Mensagem (use {nome}, {mencao}, {count})",
                                    style=discord.TextStyle.paragraph, required=False, max_length=2000)
    cor_f    = discord.ui.TextInput(label="Cor hex (ex: #590CEA)", required=False, max_length=7)
    rodape_f = discord.ui.TextInput(label="Rodapé", required=False, max_length=256)
    banner_f = discord.ui.TextInput(label="URL do banner", required=False, max_length=500)

    def __init__(self, cfg: dict):
        super().__init__()
        self.titulo_f.default  = cfg.get("welcome_titulo") or ""
        self.desc_f.default    = (cfg.get("welcome_msg") or "")[:2000]
        cor = cfg.get("welcome_cor", Colors.MAIN)
        self.cor_f.default     = f"#{cor:06X}"
        self.rodape_f.default  = cfg.get("welcome_rodape") or ""
        self.banner_f.default  = cfg.get("welcome_banner") or ""

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        fields = {}
        if self.titulo_f.value.strip():
            fields["welcome_titulo"] = self.titulo_f.value.strip()
        if self.desc_f.value.strip():
            fields["welcome_msg"] = self.desc_f.value.strip()
        if self.rodape_f.value.strip():
            fields["welcome_rodape"] = self.rodape_f.value.strip()
        if self.banner_f.value.strip():
            fields["welcome_banner"] = self.banner_f.value.strip()
        if self.cor_f.value.strip():
            try:
                fields["welcome_cor"] = int(self.cor_f.value.strip().lstrip("#"), 16)
            except ValueError:
                return await inter.followup.send(
                    embed=error_embed("Cor inválida", "Use `#RRGGBB`."), ephemeral=True
                )
        if fields:
            await db.upsert_guild_config(inter.guild.id, **fields)

        cfg = await db.get_guild_config(inter.guild.id)
        msg = (cfg.get("welcome_msg") or "{mencao} Seja bem-vindo(a)!") \
            .replace("{nome}", inter.user.display_name) \
            .replace("{mencao}", inter.user.mention) \
            .replace("{servidor}", inter.guild.name) \
            .replace("{count}", str(inter.guild.member_count))

        preview = discord.Embed(
            title=cfg.get("welcome_titulo") or f"{E.RING} Novo membro chegou! {E.DECO_PINK}",
            description=msg,
            color=cfg.get("welcome_cor", Colors.MAIN),
        )
        preview.set_author(name=inter.user.display_name, icon_url=inter.user.display_avatar.url)
        preview.set_thumbnail(url=inter.user.display_avatar.url)
        preview.set_footer(text=cfg.get("welcome_rodape") or f"{inter.guild.name} • Bem-vindo(a)!")
        if cfg.get("welcome_banner"):
            preview.set_image(url=cfg["welcome_banner"])
        preview.timestamp = _now()

        await inter.followup.send(
            content=f"{E.VERIFY} **Embed atualizada!** Preview:",
            embed=preview, ephemeral=True,
        )


class BoasVindasView(discord.ui.View):
    def __init__(self, autor_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.autor_id = autor_id
        self.guild_id = guild_id

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.autor_id:
            await inter.response.send_message(
                f"{E.ARROW_RED} Apenas quem usou o comando pode interagir.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Editar Embed", style=discord.ButtonStyle.primary, emoji="✏️")
    async def editar(self, inter: discord.Interaction, _):
        cfg = await db.get_guild_config(self.guild_id)
        await inter.response.send_modal(BoasVindasModal(cfg))

    @discord.ui.button(label="Ativar DM", style=discord.ButtonStyle.secondary, emoji="📩")
    async def toggle_dm(self, inter: discord.Interaction, button: discord.ui.Button):
        cfg = await db.get_guild_config(self.guild_id)
        novo = not cfg.get("welcome_dm", False)
        await db.upsert_guild_config(self.guild_id, welcome_dm=novo)
        button.label = "Desativar DM" if novo else "Ativar DM"
        button.style = discord.ButtonStyle.success if novo else discord.ButtonStyle.secondary
        await inter.response.edit_message(view=self)
        await inter.followup.send(
            embed=success_embed("DM de boas-vindas", f"DM {'ativada' if novo else 'desativada'}."),
            ephemeral=True,
        )

    @discord.ui.button(label="Testar", style=discord.ButtonStyle.success, emoji="🚀")
    async def testar(self, inter: discord.Interaction, _):
        cog = inter.client.cogs.get("Config")
        if cog:
            await cog._send_welcome(inter.guild, inter.user)
        await inter.response.send_message(
            embed=success_embed("Teste enviado!", f"Simulou boas-vindas para {inter.user.mention}."),
            ephemeral=True,
        )

    @discord.ui.button(label="Resetar", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def resetar(self, inter: discord.Interaction, _):
        await db.upsert_guild_config(
            self.guild_id,
            welcome_msg=None, welcome_banner=None, welcome_dm=False,
            welcome_cor=Colors.MAIN, welcome_titulo=None, welcome_rodape=None,
        )
        await inter.response.send_message(
            embed=success_embed("Resetado", "Configurações de boas-vindas restauradas."),
            ephemeral=True,
        )


class Config(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Enviar boas-vindas ─────────────────────────────────────────────────
    async def _send_welcome(self, guild: discord.Guild, member: discord.Member):
        cfg = await db.get_guild_config(guild.id)
        canal_id = cfg.get("welcome_canal")
        canal    = guild.get_channel(canal_id) if canal_id else None
        if not isinstance(canal, discord.TextChannel):
            return

        msg = (cfg.get("welcome_msg") or (
            f"{E.CROWN_PINK} Seja muito bem-vindo(a), **{{nome}}**!\n\n"
            f"{E.SPARKLE} Você é o **{{count}}°** membro!\n"
            f"{E.ARROW} Leia as regras e aproveite! {E.HEARTS_S}"
        )).replace("{nome}", member.display_name) \
          .replace("{mencao}", member.mention) \
          .replace("{servidor}", guild.name) \
          .replace("{count}", str(guild.member_count))

        emb = discord.Embed(
            title=cfg.get("welcome_titulo") or f"{E.RING} Novo membro chegou! {E.DECO_PINK}",
            description=msg,
            color=cfg.get("welcome_cor", Colors.MAIN),
        )
        emb.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        emb.set_thumbnail(url=member.display_avatar.url)
        emb.set_footer(text=cfg.get("welcome_rodape") or f"{guild.name} • Bem-vindo(a)!")
        if cfg.get("welcome_banner"):
            emb.set_image(url=cfg["welcome_banner"])
        emb.timestamp = _now()

        try:
            await canal.send(content=member.mention, embed=emb)
        except discord.HTTPException:
            pass

        if cfg.get("welcome_dm"):
            try:
                dm_emb = discord.Embed(
                    title=f"{E.HEART} Olá, {member.display_name}!",
                    description=(
                        f"{E.SPARKLE} Você entrou em **{guild.name}**!\n\n"
                        f"{E.ARROW} Leia as regras para não perder nada.\n"
                        f"{E.HEARTS_S} Esperamos que você curta por aqui!"
                    ),
                    color=Colors.MAIN,
                )
                if guild.icon:
                    dm_emb.set_thumbnail(url=guild.icon.url)
                dm_emb.timestamp = _now()
                await member.send(embed=dm_emb)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._send_welcome(member.guild, member)

    # ── Grupo /config ──────────────────────────────────────────────────────
    config_group = app_commands.Group(
        name="config",
        description="Configurações do servidor",
        default_permissions=discord.Permissions(administrator=True),
    )

    @config_group.command(name="log", description="Define o canal de logs de moderação")
    @app_commands.describe(canal="Canal de texto")
    async def cfg_log(self, inter: discord.Interaction, canal: discord.TextChannel):
        await db.upsert_guild_config(inter.guild.id, log_channel=canal.id)
        await inter.response.send_message(
            embed=success_embed("Canal de log definido", f"{E.ARROW_BLUE} Logs em {canal.mention}."),
            ephemeral=True,
        )

    @config_group.command(name="boas-vindas", description="Configura o sistema de boas-vindas")
    @app_commands.describe(
        canal="Canal das mensagens de boas-vindas",
        dm="Enviar DM ao novo membro",
    )
    async def cfg_welcome(self, inter: discord.Interaction,
                           canal: discord.TextChannel,
                           dm: bool = False):
        await db.upsert_guild_config(inter.guild.id, welcome_canal=canal.id, welcome_dm=dm)
        cfg = await db.get_guild_config(inter.guild.id)
        cor = cfg.get("welcome_cor", Colors.MAIN)
        emb = success_embed("Boas-vindas configuradas!", (
            f"{E.RING} Canal: {canal.mention}\n"
            f"{E.ENVELOPE} DM: {'Ativada' if dm else 'Desativada'}\n"
            f"{E.SPARKLE} Cor: `#{cor:06X}`\n\n"
            f"{E.ARROW_BLUE} Use os botões abaixo para editar a embed completa."
        ))
        view = BoasVindasView(inter.user.id, inter.guild.id)
        await inter.response.send_message(embed=emb, view=view, ephemeral=True)

    @config_group.command(name="boas-vindas-ver", description="Exibe as configurações de boas-vindas")
    async def cfg_welcome_ver(self, inter: discord.Interaction):
        cfg   = await db.get_guild_config(inter.guild.id)
        canal = inter.guild.get_channel(cfg["welcome_canal"]) if cfg.get("welcome_canal") else None
        cor   = cfg.get("welcome_cor", Colors.MAIN)
        emb   = discord.Embed(title=f"{E.RING} Boas-vindas — Config", color=cor)
        emb.add_field(name="Canal",   value=canal.mention if canal else "Não configurado", inline=True)
        emb.add_field(name="DM",      value="Ativada" if cfg.get("welcome_dm") else "Desativada", inline=True)
        emb.add_field(name="Cor",     value=f"`#{cor:06X}`", inline=True)
        emb.add_field(name="Título",  value=cfg.get("welcome_titulo") or "Padrão", inline=True)
        emb.add_field(name="Banner",  value="Configurado" if cfg.get("welcome_banner") else "Nenhum", inline=True)
        emb.timestamp = _now()
        view = BoasVindasView(inter.user.id, inter.guild.id)
        await inter.response.send_message(embed=emb, view=view, ephemeral=True)

    @config_group.command(name="boas-vindas-testar", description="Simula a mensagem de boas-vindas")
    async def cfg_welcome_test(self, inter: discord.Interaction):
        await self._send_welcome(inter.guild, inter.user)
        await inter.response.send_message(
            embed=success_embed("Teste enviado!", f"Simulou boas-vindas para {inter.user.mention}."),
            ephemeral=True,
        )

    @config_group.command(name="automod", description="Cria regras de AutoMod automáticas no servidor")
    @app_commands.describe(canal_log="Canal para logs do AutoMod")
    async def cfg_automod(self, inter: discord.Interaction, canal_log: discord.TextChannel = None):
        await inter.response.defer(ephemeral=True)

        KEYWORDS_BLOCKS = [
            ["idiota", "imbecil", "cretino", "babaca", "otário", "fdp", "vsf", "porra", "merda", "caralho"],
            ["viado", "bicha", "sapatão", "*macaco*", "judeu", "cigano"],
            ["*vou te matar*", "*te mato*", "*explodir*", "*atirar em*"],
            ["discord.gg/*", "*discordapp.com/invite*", "bit.ly/*", "tinyurl.com/*", "*free nitro*"],
            ["*porn*", "*nude*", "*nudes*", "*pack*", "onlyfans.com/*"],
        ]

        TOKEN = os.environ.get("BOT_TOKEN", "")
        url = f"https://discord.com/api/v10/guilds/{inter.guild.id}/auto-moderation/rules"
        headers = {
            "Authorization": f"Bot {TOKEN}",
            "Content-Type": "application/json",
        }
        criadas = 0
        async with aiohttp.ClientSession() as session:
            for i, kw in enumerate(KEYWORDS_BLOCKS):
                payload = {
                    "name": f"[Bot] Palavras bloqueadas #{i+1}",
                    "event_type": 1, "trigger_type": 1,
                    "trigger_metadata": {"keyword_filter": kw},
                    "actions": [{"type": 1, "metadata": {"custom_message": "Mensagem bloqueada."}}],
                    "enabled": True,
                }
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status in (200, 201):
                        criadas += 1

        await inter.followup.send(embed=success_embed("AutoMod configurado!",
            f"{E.ARROW_GREEN} **{criadas}** regra(s) criadas.\n"
            f"{E.SYMBOL} Máximo do Discord: 10 regras por servidor."
        ), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Config(bot))
