"""
cogs/utilidades2.py — Funcionalidades extras:
  - Contador de membros em canal de voz (atualiza a cada 10 min)
  - Sistema de aniversário (registra data, bot parabeniza)
  - Lembretes pessoais (/lembrar)
  - Clima (/clima)
  - Tradução (/traduzir)
"""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
import aiohttp
import asyncio
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
from db.database import get_pool, get_guild_config, upsert_guild_config
from utils.constants import Colors, E, success_embed, error_embed, _now

log = logging.getLogger("multibot.util2")


async def _ensure_tables():
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS aniversarios (
                user_id   BIGINT PRIMARY KEY,
                dia       INT NOT NULL,
                mes       INT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lembretes (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                guild_id    BIGINT,
                channel_id  BIGINT,
                mensagem    TEXT NOT NULL,
                dispara_em  TIMESTAMPTZ NOT NULL,
                disparado   BOOLEAN DEFAULT FALSE
            );
            -- Coluna para canal de membro counter e aniversário
            ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS counter_channel BIGINT;
            ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS aniv_channel    BIGINT;
            ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS logs_channel    BIGINT;
        """)


class Utilidades2(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await _ensure_tables()
        self.atualizar_contador.start()
        self.checar_aniversarios.start()
        self.checar_lembretes.start()

    def cog_unload(self):
        self.atualizar_contador.cancel()
        self.checar_aniversarios.cancel()
        self.checar_lembretes.cancel()

    # ── Contador de membros ────────────────────────────────────────────────

    @tasks.loop(minutes=10)
    async def atualizar_contador(self):
        for guild in self.bot.guilds:
            cfg = await get_guild_config(guild.id)
            ch_id = cfg.get("counter_channel")
            if not ch_id:
                continue
            ch = guild.get_channel(ch_id)
            if not isinstance(ch, discord.VoiceChannel):
                continue
            nome_novo = f"👥 Membros: {guild.member_count:,}"
            if ch.name != nome_novo:
                try:
                    await ch.edit(name=nome_novo, reason="Contador de membros")
                except discord.HTTPException:
                    pass

    @atualizar_contador.before_loop
    async def before_counter(self):
        await self.bot.wait_until_ready()

    # ── Checar aniversários (diário às 9h UTC) ─────────────────────────────

    @tasks.loop(hours=1)
    async def checar_aniversarios(self):
        agora = datetime.now(tz=timezone.utc)
        if agora.hour != 9:
            return
        for guild in self.bot.guilds:
            cfg = await get_guild_config(guild.id)
            ch_id = cfg.get("aniv_channel")
            if not ch_id:
                continue
            ch = guild.get_channel(ch_id)
            if not isinstance(ch, discord.TextChannel):
                continue
            async with get_pool().acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id FROM aniversarios WHERE dia=$1 AND mes=$2",
                    agora.day, agora.month,
                )
            for row in rows:
                member = guild.get_member(row["user_id"])
                if not member:
                    continue
                emb = discord.Embed(
                    title=f"{E.BEAR} Feliz Aniversário, {member.display_name}! 🎂",
                    description=(
                        f"{E.HEARTS_S} Hoje é o grande dia de {member.mention}!\n\n"
                        f"{E.SPARKLE} O servidor inteiro deseja a você um ótimo dia!\n"
                        f"{E.CROWN_PINK} Parabéns! 🎉🎊"
                    ),
                    color=0xFF69B4,
                )
                emb.set_thumbnail(url=member.display_avatar.url)
                emb.timestamp = _now()
                try:
                    await ch.send(content=member.mention, embed=emb)
                except discord.HTTPException:
                    pass

    @checar_aniversarios.before_loop
    async def before_aniv(self):
        await self.bot.wait_until_ready()

    # ── Checar lembretes ───────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def checar_lembretes(self):
        agora = datetime.now(tz=timezone.utc)
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM lembretes WHERE disparado=FALSE AND dispara_em <= $1", agora
            )
            if rows:
                await conn.execute(
                    "UPDATE lembretes SET disparado=TRUE WHERE id = ANY($1::int[])",
                    [r["id"] for r in rows],
                )
        for row in rows:
            user = self.bot.get_user(row["user_id"])
            if not user:
                continue
            emb = discord.Embed(
                title=f"{E.GHOST} Lembrete! {E.BULB}",
                description=row["mensagem"],
                color=Colors.MAIN,
            )
            emb.set_footer(text="Lembrete programado por você")
            emb.timestamp = _now()
            # Tenta enviar no canal original, senão DM
            if row["channel_id"]:
                ch = self.bot.get_channel(row["channel_id"])
                if isinstance(ch, discord.TextChannel):
                    try:
                        await ch.send(content=f"{user.mention} 🔔", embed=emb)
                        continue
                    except Exception:
                        pass
            try:
                await user.send(embed=emb)
            except Exception:
                pass

    @checar_lembretes.before_loop
    async def before_lemb(self):
        await self.bot.wait_until_ready()

    # ── Slash commands ─────────────────────────────────────────────────────

    # Contador
    counter_group = app_commands.Group(
        name="contador",
        description="Contador de membros em canal de voz",
        default_permissions=discord.Permissions(manage_channels=True),
    )

    @counter_group.command(name="setup", description="Define o canal de voz para contagem de membros")
    @app_commands.describe(canal="Canal de voz (será renomeado automaticamente)")
    async def counter_setup(self, inter: discord.Interaction, canal: discord.VoiceChannel):
        await upsert_guild_config(inter.guild.id, counter_channel=canal.id)
        nome = f"👥 Membros: {inter.guild.member_count:,}"
        try:
            await canal.edit(name=nome)
        except discord.HTTPException:
            pass
        await inter.response.send_message(
            embed=success_embed("Contador configurado!",
                f"{E.ARROW_BLUE} {canal.mention} mostrará a contagem de membros.\n"
                f"{E.SYMBOL} Atualiza a cada 10 minutos."
            ),
            ephemeral=True,
        )

    @counter_group.command(name="desativar", description="Desativa o contador de membros")
    async def counter_off(self, inter: discord.Interaction):
        await upsert_guild_config(inter.guild.id, counter_channel=None)
        await inter.response.send_message(
            embed=success_embed("Contador desativado", "O contador de membros foi desativado."),
            ephemeral=True,
        )

    # Aniversário
    aniv_group = app_commands.Group(
        name="aniversario",
        description="Sistema de aniversários",
    )

    @aniv_group.command(name="registrar", description="Registre sua data de aniversário")
    @app_commands.describe(dia="Dia do aniversário", mes="Mês do aniversário")
    async def aniv_registrar(self, inter: discord.Interaction,
                              dia: app_commands.Range[int, 1, 31],
                              mes: app_commands.Range[int, 1, 12]):
        async with get_pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO aniversarios (user_id, dia, mes)
                VALUES ($1,$2,$3)
                ON CONFLICT (user_id) DO UPDATE SET dia=$2, mes=$3
            """, inter.user.id, dia, mes)
        await inter.response.send_message(
            embed=success_embed("Aniversário registrado!",
                f"🎂 Seu aniversário foi registrado: **{dia:02d}/{mes:02d}**\n"
                f"{E.SPARKLE} O servidor vai te parabenizar neste dia!"
            ),
            ephemeral=True,
        )

    @aniv_group.command(name="setup", description="[Admin] Define o canal de parabéns")
    @app_commands.describe(canal="Canal onde o bot parabenizará os aniversariantes")
    @app_commands.default_permissions(manage_guild=True)
    async def aniv_setup(self, inter: discord.Interaction, canal: discord.TextChannel):
        await upsert_guild_config(inter.guild.id, aniv_channel=canal.id)
        await inter.response.send_message(
            embed=success_embed("Canal de aniversário definido!",
                f"{E.ARROW_BLUE} Parabéns serão enviados em {canal.mention} às 9h UTC."
            ),
            ephemeral=True,
        )

    @aniv_group.command(name="ver", description="Veja o aniversário de um membro")
    @app_commands.describe(membro="Membro (padrão: você)")
    async def aniv_ver(self, inter: discord.Interaction, membro: discord.Member = None):
        m = membro or inter.user
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow("SELECT dia, mes FROM aniversarios WHERE user_id=$1", m.id)
        if not row:
            return await inter.response.send_message(
                embed=error_embed("Não registrado",
                    f"{m.mention} não tem aniversário registrado.\n"
                    f"Use `/aniversario registrar` para registrar."
                ),
                ephemeral=True,
            )
        await inter.response.send_message(
            embed=discord.Embed(
                title=f"🎂 Aniversário de {m.display_name}",
                description=f"**{row['dia']:02d}/{row['mes']:02d}**",
                color=0xFF69B4,
            )
        )

    # Lembrete
    @app_commands.command(name="lembrar", description="Crie um lembrete pessoal")
    @app_commands.describe(
        quando="Quando lembrar: ex. 30m, 2h, 1d",
        mensagem="O que lembrar",
        canal="Enviar neste canal (padrão: DM)",
    )
    async def lembrar(self, inter: discord.Interaction,
                       quando: str, mensagem: str,
                       canal: discord.TextChannel = None):
        unidades = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        try:
            secs = int(quando[:-1]) * unidades[quando[-1].lower()]
        except (ValueError, KeyError, IndexError):
            return await inter.response.send_message(
                embed=error_embed("Formato inválido", "Use: `30m`, `2h`, `1d`."), ephemeral=True
            )
        if secs < 30:
            return await inter.response.send_message(
                embed=error_embed("Muito curto", "Mínimo: 30 segundos."), ephemeral=True
            )
        dispara_em = datetime.now(tz=timezone.utc) + timedelta(seconds=secs)
        async with get_pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO lembretes (user_id, guild_id, channel_id, mensagem, dispara_em)
                VALUES ($1,$2,$3,$4,$5)
            """,
                inter.user.id,
                inter.guild.id if inter.guild else None,
                canal.id if canal else None,
                mensagem,
                dispara_em,
            )
        destino = canal.mention if canal else "sua DM"
        await inter.response.send_message(
            embed=success_embed("Lembrete criado!",
                f"{E.BULB} Vou te lembrar {discord.utils.format_dt(dispara_em, 'R')}.\n"
                f"{E.ARROW_BLUE} Destino: {destino}\n"
                f"{E.SYMBOL} Mensagem: *{mensagem[:100]}*"
            ),
            ephemeral=True,
        )

    # Clima
    @app_commands.command(name="clima", description="Consulta o clima de uma cidade")
    @app_commands.describe(cidade="Nome da cidade (ex: São Paulo, BR)")
    async def clima(self, inter: discord.Interaction, cidade: str):
        await inter.response.defer()
        import os
        api_key = os.getenv("OPENWEATHER_API_KEY", "")
        if not api_key:
            return await inter.followup.send(
                embed=error_embed("Sem chave de API", "Configure a variável `OPENWEATHER_API_KEY` no Railway."),
                ephemeral=True,
            )
        query = urllib.parse.quote(cidade.strip())
        url = f"https://api.openweathermap.org/data/2.5/weather?q={query}&appid={api_key}&units=metric&lang=pt_br"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 404:
                        return await inter.followup.send(
                            embed=error_embed("Cidade não encontrada", f"Não encontrei `{cidade}`."), ephemeral=True
                        )
                    if resp.status != 200:
                        raise ValueError(f"HTTP {resp.status}")
                    data = await resp.json()
        except Exception as exc:
            return await inter.followup.send(
                embed=error_embed("Erro", f"Não foi possível consultar o clima.\n`{exc}`"), ephemeral=True
            )

        nome    = data["name"]
        pais    = data["sys"]["country"]
        temp    = data["main"]["temp"]
        sens    = data["main"]["feels_like"]
        umid    = data["main"]["humidity"]
        desc    = data["weather"][0]["description"].capitalize()
        vento   = data["wind"]["speed"]
        icon    = data["weather"][0]["icon"]
        icon_url = f"https://openweathermap.org/img/wn/{icon}@2x.png"

        emb = discord.Embed(
            title=f"🌤️ Clima em {nome}, {pais}",
            description=f"**{desc}**",
            color=0x87CEEB,
        )
        emb.set_thumbnail(url=icon_url)
        emb.add_field(name="🌡️ Temperatura", value=f"`{temp:.1f}°C`",     inline=True)
        emb.add_field(name="🤔 Sensação",    value=f"`{sens:.1f}°C`",     inline=True)
        emb.add_field(name="💧 Umidade",     value=f"`{umid}%`",          inline=True)
        emb.add_field(name="💨 Vento",       value=f"`{vento:.1f} m/s`",  inline=True)
        emb.set_footer(text="Fonte: OpenWeatherMap")
        emb.timestamp = _now()
        await inter.followup.send(embed=emb)

    # Tradução
    @app_commands.command(name="traduzir", description="Traduz um texto para outro idioma")
    @app_commands.describe(
        texto="Texto a traduzir",
        idioma="Idioma alvo (ex: pt, en, es, fr, de, ja, ko)",
    )
    async def traduzir(self, inter: discord.Interaction, texto: str, idioma: str = "pt"):
        await inter.response.defer(ephemeral=True)
        # Usa a API MyMemory (gratuita, sem chave)
        query = urllib.parse.urlencode({"q": texto[:450], "langpair": f"auto|{idioma.lower().strip()[:8]}"})
        url = f"https://api.mymemory.translated.net/get?{query}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
        except Exception as exc:
            return await inter.followup.send(
                embed=error_embed("Erro de conexão", str(exc)), ephemeral=True
            )

        traducao = data.get("responseData", {}).get("translatedText", "")
        if not traducao:
            return await inter.followup.send(
                embed=error_embed("Erro", "Não foi possível traduzir o texto."), ephemeral=True
            )

        emb = discord.Embed(title=f"{E.WAND} Tradução", color=Colors.MAIN)
        emb.add_field(name="Original",   value=texto[:500],    inline=False)
        emb.add_field(name=f"→ `{idioma.upper()}`", value=traducao[:500], inline=False)
        emb.set_footer(text="Fonte: MyMemory Translation API")
        emb.timestamp = _now()
        await inter.followup.send(embed=emb, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utilidades2(bot))
            
