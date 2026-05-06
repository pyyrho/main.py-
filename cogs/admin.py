"""
cogs/admin.py — Comandos exclusivos do dono do bot (Logos)
Todos os comandos são ephemeral (só o dono vê).

v2 — correções:
- datetime.utcnow() é deprecated no Python 3.12+ e será removido no 3.14.
  Substituído por datetime.now(timezone.utc) em todos os lugares.
"""

import asyncio
import datetime
from datetime import timezone
import os
import platform
import sys
import time

import discord
import psutil
from discord import app_commands
from discord.ext import commands

OWNER_ID = 695037018127990814
START_TIME = time.time()


def is_owner(inter: discord.Interaction) -> bool:
    return inter.user.id == OWNER_ID


def _now() -> datetime.datetime:
    """Retorna datetime aware UTC (substitui datetime.utcnow() deprecated)."""
    return datetime.datetime.now(tz=timezone.utc)


# ── Grupo /admin ───────────────────────────────────────────────────────────────
admin_group = app_commands.Group(
    name="admin",
    description="🔐 Comandos exclusivos do dono do bot.",
)


# ── /admin servidores ──────────────────────────────────────────────────────────
@admin_group.command(name="servidores", description="Lista todos os servidores onde o bot está.")
async def admin_servidores(inter: discord.Interaction):
    if not is_owner(inter):
        return await inter.response.send_message("❌ Sem permissão.", ephemeral=True)

    guilds = sorted(inter.client.guilds, key=lambda g: g.member_count, reverse=True)
    linhas = [
        f"`{i+1}.` **{g.name}** — ID `{g.id}` — {g.member_count} membros"
        for i, g in enumerate(guilds)
    ]

    chunks = [linhas[i:i+20] for i in range(0, len(linhas), 20)]
    embeds = []
    for idx, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=f"📋 Servidores ({len(guilds)}) — Página {idx+1}/{len(chunks)}",
            description="\n".join(chunk),
            color=0x5865F2,
            timestamp=_now(),
        )
        embed.set_footer(text=f"Total: {len(guilds)} servidores")
        embeds.append(embed)

    await inter.response.send_message(embeds=embeds[:1], ephemeral=True)
    for extra in embeds[1:]:
        await inter.followup.send(embed=extra, ephemeral=True)


# ── /admin stats ───────────────────────────────────────────────────────────────
@admin_group.command(name="stats", description="Exibe estatísticas do bot (uptime, memória, CPU).")
async def admin_stats(inter: discord.Interaction):
    if not is_owner(inter):
        return await inter.response.send_message("❌ Sem permissão.", ephemeral=True)

    uptime_s = int(time.time() - START_TIME)
    h, rem = divmod(uptime_s, 3600)
    m, s = divmod(rem, 60)

    proc = psutil.Process(os.getpid())
    mem_mb = proc.memory_info().rss / 1024 / 1024
    cpu = psutil.cpu_percent(interval=0.5)

    latency = round(inter.client.latency * 1000)
    total_members = sum(g.member_count for g in inter.client.guilds)

    embed = discord.Embed(title="📊 Stats do Bot", color=0x2ECC71, timestamp=_now())
    embed.add_field(name="⏱️ Uptime",           value=f"`{h}h {m}m {s}s`",     inline=True)
    embed.add_field(name="🏓 Latência",          value=f"`{latency}ms`",          inline=True)
    embed.add_field(name="🖥️ CPU",              value=f"`{cpu}%`",               inline=True)
    embed.add_field(name="💾 Memória RAM",       value=f"`{mem_mb:.1f} MB`",      inline=True)
    embed.add_field(name="🌐 Servidores",        value=f"`{len(inter.client.guilds)}`", inline=True)
    embed.add_field(name="👥 Total de Membros",  value=f"`{total_members}`",      inline=True)
    embed.add_field(name="🐍 Python",            value=f"`{platform.python_version()}`", inline=True)
    embed.add_field(name="📦 discord.py",        value=f"`{discord.__version__}`", inline=True)

    await inter.response.send_message(embed=embed, ephemeral=True)


# ── /admin sair ────────────────────────────────────────────────────────────────
@admin_group.command(name="sair", description="Faz o bot sair de um servidor pelo ID.")
@app_commands.describe(guild_id="ID do servidor que o bot deve sair")
async def admin_sair(inter: discord.Interaction, guild_id: str):
    if not is_owner(inter):
        return await inter.response.send_message("❌ Sem permissão.", ephemeral=True)

    try:
        gid = int(guild_id)
    except ValueError:
        return await inter.response.send_message("❌ ID inválido. Use apenas números.", ephemeral=True)

    guild = inter.client.get_guild(gid)
    if not guild:
        return await inter.response.send_message(
            f"❌ Servidor com ID `{gid}` não encontrado ou o bot não está nele.", ephemeral=True
        )

    nome = guild.name
    await guild.leave()
    await inter.response.send_message(
        f"✅ Saí do servidor **{nome}** (`{gid}`) com sucesso.", ephemeral=True
    )


# ── /admin broadcast ───────────────────────────────────────────────────────────
@admin_group.command(name="broadcast", description="Envia uma mensagem para todos os servidores.")
@app_commands.describe(mensagem="Mensagem a ser enviada em todos os servidores")
async def admin_broadcast(inter: discord.Interaction, mensagem: str):
    if not is_owner(inter):
        return await inter.response.send_message("❌ Sem permissão.", ephemeral=True)

    await inter.response.defer(ephemeral=True)

    enviados, falhas = 0, 0
    for guild in inter.client.guilds:
        canal = None
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                canal = ch
                break

        if canal:
            try:
                embed = discord.Embed(
                    title="📢 Aviso do desenvolvedor",
                    description=mensagem,
                    color=0xE74C3C,
                    timestamp=_now(),
                )
                embed.set_footer(text="Logos Bot")
                await canal.send(embed=embed)
                enviados += 1
            except Exception:
                falhas += 1
        else:
            falhas += 1

        await asyncio.sleep(0.5)  # evita rate limit

    await inter.followup.send(
        f"✅ Broadcast concluído!\n📨 Enviado: `{enviados}` servidores\n❌ Falhou: `{falhas}` servidores",
        ephemeral=True,
    )


# ── /admin recarregar ──────────────────────────────────────────────────────────
@admin_group.command(name="recarregar", description="Recarrega um cog sem reiniciar o bot.")
@app_commands.describe(cog="Nome do cog (ex: cogs.xp)")
async def admin_recarregar(inter: discord.Interaction, cog: str):
    if not is_owner(inter):
        return await inter.response.send_message("❌ Sem permissão.", ephemeral=True)

    try:
        await inter.client.reload_extension(cog)
        await inter.response.send_message(f"✅ Cog `{cog}` recarregado com sucesso!", ephemeral=True)
    except commands.ExtensionNotLoaded:
        await inter.response.send_message(f"❌ Cog `{cog}` não estava carregado.", ephemeral=True)
    except commands.ExtensionNotFound:
        await inter.response.send_message(f"❌ Cog `{cog}` não encontrado.", ephemeral=True)
    except Exception as e:
        await inter.response.send_message(f"❌ Erro ao recarregar: `{e}`", ephemeral=True)


# ── /admin inspecionar ─────────────────────────────────────────────────────────
@admin_group.command(name="inspecionar", description="Exibe detalhes de um servidor específico.")
@app_commands.describe(guild_id="ID do servidor para inspecionar")
async def admin_inspecionar(inter: discord.Interaction, guild_id: str):
    if not is_owner(inter):
        return await inter.response.send_message("❌ Sem permissão.", ephemeral=True)

    try:
        gid = int(guild_id)
    except ValueError:
        return await inter.response.send_message("❌ ID inválido.", ephemeral=True)

    guild = inter.client.get_guild(gid)
    if not guild:
        return await inter.response.send_message(f"❌ Servidor `{gid}` não encontrado.", ephemeral=True)

    bots    = sum(1 for m in guild.members if m.bot)
    humanos = guild.member_count - bots
    criado  = discord.utils.format_dt(guild.created_at, style="D")
    dono    = await inter.client.fetch_user(guild.owner_id)

    embed = discord.Embed(title=f"🔍 {guild.name}", color=0x9B59B6, timestamp=_now())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="🆔 ID",              value=f"`{guild.id}`",                                  inline=True)
    embed.add_field(name="👑 Dono",            value=f"{dono} (`{dono.id}`)",                          inline=True)
    embed.add_field(name="📅 Criado em",       value=criado,                                            inline=True)
    embed.add_field(name="👥 Membros",         value=f"`{humanos}` humanos + `{bots}` bots",            inline=True)
    embed.add_field(name="💬 Canais de Texto", value=f"`{len(guild.text_channels)}`",                   inline=True)
    embed.add_field(name="🔊 Canais de Voz",  value=f"`{len(guild.voice_channels)}`",                   inline=True)
    embed.add_field(name="😀 Emojis",          value=f"`{len(guild.emojis)}`",                          inline=True)
    embed.add_field(name="🚀 Boosts",          value=f"`{guild.premium_subscription_count}` (Nível {guild.premium_tier})", inline=True)

    await inter.response.send_message(embed=embed, ephemeral=True)


# ── Cog ────────────────────────────────────────────────────────────────────────
class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.tree.add_command(admin_group)

    async def cog_unload(self):
        self.bot.tree.remove_command(admin_group.name)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
