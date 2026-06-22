"""cogs/utilidades2.py — Contador, aniversários, lembretes, clima e tradução."""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from db.database import get_guild_config, get_pool, upsert_guild_config
from utils.ui_components import card, clip

log = logging.getLogger("multibot.util2")

BIRTHDAY_HOUR_UTC = max(0, min(int(os.getenv("BIRTHDAY_HOUR_UTC", "9")), 23))
REMINDER_BATCH = max(1, min(int(os.getenv("REMINDER_BATCH_SIZE", "50")), 200))
_DURATION_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>[smhdw])", re.IGNORECASE)


async def _ensure_tables() -> None:
    async with get_pool().acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS aniversarios (
                user_id BIGINT PRIMARY KEY,
                dia INT NOT NULL CHECK (dia BETWEEN 1 AND 31),
                mes INT NOT NULL CHECK (mes BETWEEN 1 AND 12)
            );
            CREATE TABLE IF NOT EXISTS aniversarios_envios (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                ano INT NOT NULL,
                enviado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id, ano)
            );
            CREATE TABLE IF NOT EXISTS lembretes (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                guild_id BIGINT,
                channel_id BIGINT,
                mensagem TEXT NOT NULL,
                dispara_em TIMESTAMPTZ NOT NULL,
                disparado BOOLEAN NOT NULL DEFAULT FALSE
            );
            ALTER TABLE lembretes ADD COLUMN IF NOT EXISTS processando_em TIMESTAMPTZ;
            ALTER TABLE lembretes ADD COLUMN IF NOT EXISTS tentativas INT NOT NULL DEFAULT 0;
            ALTER TABLE lembretes ADD COLUMN IF NOT EXISTS ultimo_erro TEXT;
            ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS counter_channel BIGINT;
            ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS aniv_channel BIGINT;
            ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS logs_channel BIGINT;
            CREATE INDEX IF NOT EXISTS idx_lembretes_pendentes
                ON lembretes (disparado, dispara_em)
                WHERE disparado = FALSE;
            """
        )


def _parse_duration(raw: str) -> int:
    text = raw.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("Informe uma duração.")
    matches = list(_DURATION_RE.finditer(text))
    if not matches or "".join(match.group(0) for match in matches) != text:
        raise ValueError("Use combinações como `30m`, `2h`, `1d12h` ou `1w`.")
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = sum(int(match.group("value")) * factors[match.group("unit").lower()] for match in matches)
    if seconds < 30:
        raise ValueError("O tempo mínimo é 30 segundos.")
    if seconds > 31_536_000:
        raise ValueError("O tempo máximo é 365 dias.")
    return seconds


def _valid_birthday(day: int, month: int) -> bool:
    try:
        datetime(2000, month, day)
        return True
    except ValueError:
        return False


def _weather_time(timestamp: int | None, offset: int) -> str:
    if timestamp is None:
        return "—"
    value = datetime.fromtimestamp(timestamp + offset, tz=timezone.utc)
    return value.strftime("%H:%M")


class Utilidades2(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        await _ensure_tables()
        self.session = aiohttp.ClientSession(headers={"User-Agent": "DiscordMultibot/3.0"})
        self.atualizar_contador.start()
        self.checar_aniversarios.start()
        self.checar_lembretes.start()

    async def cog_unload(self) -> None:
        self.atualizar_contador.cancel()
        self.checar_aniversarios.cancel()
        self.checar_lembretes.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    @tasks.loop(minutes=10)
    async def atualizar_contador(self) -> None:
        for guild in self.bot.guilds:
            try:
                config = await get_guild_config(guild.id)
                channel_id = config.get("counter_channel")
                if not channel_id:
                    continue
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    continue
                new_name = f"👥 Membros: {guild.member_count or len(guild.members):,}"
                if channel.name != new_name:
                    await channel.edit(name=new_name, reason="Atualização do contador de membros")
            except discord.Forbidden:
                log.warning("Sem permissão para atualizar contador no servidor %s", guild.id)
            except discord.HTTPException as exc:
                log.debug("Falha temporária no contador de %s: %s", guild.id, exc)
            except Exception:
                log.exception("Erro inesperado no contador do servidor %s", guild.id)

    @atualizar_contador.before_loop
    async def before_counter(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def checar_aniversarios(self) -> None:
        now = datetime.now(tz=timezone.utc)
        if now.hour != BIRTHDAY_HOUR_UTC:
            return
        async with get_pool().acquire() as connection:
            birthdays = await connection.fetch(
                "SELECT user_id FROM aniversarios WHERE dia=$1 AND mes=$2",
                now.day,
                now.month,
            )
        if not birthdays:
            return
        for guild in self.bot.guilds:
            try:
                config = await get_guild_config(guild.id)
                channel = guild.get_channel(config.get("aniv_channel")) if config.get("aniv_channel") else None
                if not isinstance(channel, discord.TextChannel):
                    continue
                for row in birthdays:
                    member = guild.get_member(row["user_id"])
                    if not member:
                        continue
                    async with get_pool().acquire() as connection:
                        inserted = await connection.fetchval(
                            """
                            INSERT INTO aniversarios_envios (guild_id, user_id, ano)
                            VALUES ($1,$2,$3)
                            ON CONFLICT DO NOTHING
                            RETURNING user_id
                            """,
                            guild.id,
                            member.id,
                            now.year,
                        )
                    if not inserted:
                        continue
                    try:
                        await channel.send(
                            view=card(
                                f"Feliz aniversário, {member.display_name}! 🎂",
                                f"Hoje é o dia de {member.mention}. O servidor deseja um excelente aniversário e um novo ciclo muito bom.",
                                thumbnail=member.display_avatar.url,
                                footer=f"{now.day:02d}/{now.month:02d}",
                                timeout=None,
                            ),
                            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                        )
                    except discord.HTTPException as exc:
                        async with get_pool().acquire() as connection:
                            await connection.execute(
                                "DELETE FROM aniversarios_envios WHERE guild_id=$1 AND user_id=$2 AND ano=$3",
                                guild.id,
                                member.id,
                                now.year,
                            )
                        log.warning("Falha ao anunciar aniversário em %s: %s", guild.id, exc)
            except Exception:
                log.exception("Erro ao processar aniversários no servidor %s", guild.id)

    @checar_aniversarios.before_loop
    async def before_aniv(self) -> None:
        await self.bot.wait_until_ready()

    async def _claim_reminders(self) -> list[dict]:
        async with get_pool().acquire() as connection:
            rows = await connection.fetch(
                """
                WITH pending AS (
                    SELECT id
                    FROM lembretes
                    WHERE disparado=FALSE
                      AND dispara_em <= NOW()
                      AND (processando_em IS NULL OR processando_em < NOW() - INTERVAL '5 minutes')
                    ORDER BY dispara_em
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE lembretes AS l
                SET processando_em=NOW()
                FROM pending
                WHERE l.id=pending.id
                RETURNING l.*
                """,
                REMINDER_BATCH,
            )
        return [dict(row) for row in rows]

    async def _send_reminder(self, row: dict) -> bool:
        user = self.bot.get_user(row["user_id"])
        if not user:
            try:
                user = await self.bot.fetch_user(row["user_id"])
            except (discord.NotFound, discord.HTTPException):
                return False
        reminder_view = card(
            "Lembrete",
            clip(row["mensagem"], 2500),
            footer=f"Lembrete #{row['id']} programado por você",
            timeout=None,
        )
        channel = self.bot.get_channel(row.get("channel_id")) if row.get("channel_id") else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    view=reminder_view,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
                return True
            except discord.HTTPException:
                pass
        try:
            await user.send(view=reminder_view)
            return True
        except discord.HTTPException:
            return False

    @tasks.loop(seconds=30)
    async def checar_lembretes(self) -> None:
        try:
            rows = await self._claim_reminders()
        except Exception:
            log.exception("Falha ao buscar lembretes")
            return
        for row in rows:
            sent = False
            error_text = "Não foi possível localizar um destino acessível."
            try:
                sent = await self._send_reminder(row)
            except Exception as exc:
                error_text = clip(exc, 400)
                log.exception("Falha ao enviar lembrete %s", row["id"])
            async with get_pool().acquire() as connection:
                if sent:
                    await connection.execute(
                        "UPDATE lembretes SET disparado=TRUE, processando_em=NULL, ultimo_erro=NULL WHERE id=$1",
                        row["id"],
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE lembretes
                        SET processando_em=NULL, tentativas=tentativas+1, ultimo_erro=$2
                        WHERE id=$1
                        """,
                        row["id"],
                        error_text,
                    )

    @checar_lembretes.before_loop
    async def before_lemb(self) -> None:
        await self.bot.wait_until_ready()

    counter_group = app_commands.Group(
        name="contador",
        description="Contador de membros em canal de voz",
        default_permissions=discord.Permissions(manage_channels=True),
    )

    @counter_group.command(name="setup", description="Define o canal de voz do contador")
    @app_commands.describe(canal="Canal que será renomeado automaticamente")
    async def counter_setup(self, interaction: discord.Interaction, canal: discord.VoiceChannel) -> None:
        me = interaction.guild.me
        if not canal.permissions_for(me).manage_channels:
            await interaction.response.send_message(view=card("Sem permissão", f"Não consigo renomear {canal.mention}."), ephemeral=True)
            return
        await upsert_guild_config(interaction.guild_id, counter_channel=canal.id)
        new_name = f"👥 Membros: {interaction.guild.member_count or len(interaction.guild.members):,}"
        try:
            await canal.edit(name=new_name, reason=f"Contador configurado por {interaction.user}")
        except discord.HTTPException:
            log.warning("Contador salvo, mas canal %s não pôde ser renomeado agora", canal.id)
        await interaction.response.send_message(
            view=card("Contador configurado", f"{canal.mention} será atualizado a cada 10 minutos.", fields=[("Nome atual", new_name)]),
            ephemeral=True,
        )

    @counter_group.command(name="desativar", description="Desativa o contador de membros")
    async def counter_off(self, interaction: discord.Interaction) -> None:
        await upsert_guild_config(interaction.guild_id, counter_channel=None)
        await interaction.response.send_message(view=card("Contador desativado", "O canal não será mais renomeado automaticamente."), ephemeral=True)

    aniv_group = app_commands.Group(name="aniversario", description="Sistema de aniversários")

    @aniv_group.command(name="registrar", description="Registra sua data de aniversário")
    @app_commands.describe(dia="Dia", mes="Mês")
    async def aniv_registrar(
        self,
        interaction: discord.Interaction,
        dia: app_commands.Range[int, 1, 31],
        mes: app_commands.Range[int, 1, 12],
    ) -> None:
        if not _valid_birthday(dia, mes):
            await interaction.response.send_message(view=card("Data inválida", "Esse dia não existe no mês informado."), ephemeral=True)
            return
        async with get_pool().acquire() as connection:
            await connection.execute(
                """
                INSERT INTO aniversarios (user_id, dia, mes)
                VALUES ($1,$2,$3)
                ON CONFLICT (user_id) DO UPDATE SET dia=$2, mes=$3
                """,
                interaction.user.id,
                dia,
                mes,
            )
        await interaction.response.send_message(
            view=card("Aniversário registrado", f"Sua data foi salva como **{dia:02d}/{mes:02d}**."),
            ephemeral=True,
        )

    @aniv_group.command(name="remover", description="Remove seu aniversário registrado")
    async def aniv_remover(self, interaction: discord.Interaction) -> None:
        async with get_pool().acquire() as connection:
            result = await connection.execute("DELETE FROM aniversarios WHERE user_id=$1", interaction.user.id)
        removed = result != "DELETE 0"
        await interaction.response.send_message(
            view=card("Aniversário removido" if removed else "Nada registrado", "Sua data foi apagada." if removed else "Você ainda não possui uma data salva."),
            ephemeral=True,
        )

    @aniv_group.command(name="setup", description="Define o canal de parabéns")
    @app_commands.describe(canal="Canal dos anúncios")
    @app_commands.default_permissions(manage_guild=True)
    async def aniv_setup(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        if not canal.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(view=card("Sem permissão", f"Não consigo enviar mensagens em {canal.mention}."), ephemeral=True)
            return
        await upsert_guild_config(interaction.guild_id, aniv_channel=canal.id)
        await interaction.response.send_message(
            view=card("Canal de aniversários definido", f"Os parabéns serão enviados em {canal.mention}, por volta de **{BIRTHDAY_HOUR_UTC:02d}:00 UTC**."),
            ephemeral=True,
        )

    @aniv_group.command(name="ver", description="Consulta o aniversário de um membro")
    @app_commands.describe(membro="Membro consultado; por padrão, você")
    async def aniv_ver(self, interaction: discord.Interaction, membro: discord.Member | None = None) -> None:
        member = membro or interaction.user
        async with get_pool().acquire() as connection:
            row = await connection.fetchrow("SELECT dia, mes FROM aniversarios WHERE user_id=$1", member.id)
        if not row:
            await interaction.response.send_message(view=card("Não registrado", f"{member.mention} não possui aniversário salvo."), ephemeral=True)
            return
        await interaction.response.send_message(
            view=card(f"Aniversário de {member.display_name}", f"🎂 **{row['dia']:02d}/{row['mes']:02d}**", thumbnail=member.display_avatar.url)
        )

    @app_commands.command(name="lembrar", description="Cria um lembrete pessoal")
    @app_commands.describe(quando="Ex.: 30m, 2h, 1d12h", mensagem="Texto do lembrete", canal="Canal opcional; padrão: DM")
    async def lembrar(
        self,
        interaction: discord.Interaction,
        quando: str,
        mensagem: app_commands.Range[str, 1, 1800],
        canal: discord.TextChannel | None = None,
    ) -> None:
        try:
            seconds = _parse_duration(quando)
        except ValueError as exc:
            await interaction.response.send_message(view=card("Duração inválida", str(exc)), ephemeral=True)
            return
        if canal:
            user_permissions = canal.permissions_for(interaction.user)
            bot_permissions = canal.permissions_for(interaction.guild.me)
            if not user_permissions.view_channel or not bot_permissions.send_messages:
                await interaction.response.send_message(view=card("Canal indisponível", "Você ou o bot não possui acesso suficiente ao canal escolhido."), ephemeral=True)
                return
        due_at = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)
        async with get_pool().acquire() as connection:
            reminder_id = await connection.fetchval(
                """
                INSERT INTO lembretes (user_id, guild_id, channel_id, mensagem, dispara_em)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING id
                """,
                interaction.user.id,
                interaction.guild_id,
                canal.id if canal else None,
                mensagem,
                due_at,
            )
        await interaction.response.send_message(
            view=card(
                "Lembrete criado",
                f"Vou avisar {discord.utils.format_dt(due_at, 'R')}.",
                fields=[("Destino", canal.mention if canal else "Mensagem privada"), ("Mensagem", clip(mensagem, 500)), ("ID", f"`{reminder_id}`")],
            ),
            ephemeral=True,
        )

    @app_commands.command(name="clima", description="Consulta o clima atual de uma cidade")
    @app_commands.describe(cidade="Ex.: Goiânia, BR")
    async def clima(self, interaction: discord.Interaction, cidade: app_commands.Range[str, 2, 100]) -> None:
        await interaction.response.defer(thinking=True)
        api_key = os.getenv("OPENWEATHER_API_KEY", "").strip()
        if not api_key:
            await interaction.followup.send(view=card("Clima não configurado", "Configure `OPENWEATHER_API_KEY` nas variáveis do Railway."), ephemeral=True)
            return
        if not self.session:
            await interaction.followup.send(view=card("Serviço iniciando", "Tente novamente em alguns segundos."), ephemeral=True)
            return
        query = urllib.parse.urlencode({"q": cidade.strip(), "appid": api_key, "units": "metric", "lang": "pt_br"})
        try:
            async with self.session.get(
                f"https://api.openweathermap.org/data/2.5/weather?{query}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                data = await response.json(content_type=None)
                if response.status == 404:
                    await interaction.followup.send(view=card("Cidade não encontrada", f"Não encontrei `{cidade}`."), ephemeral=True)
                    return
                if response.status != 200:
                    log.warning("OpenWeather HTTP %s: %s", response.status, clip(data, 300))
                    await interaction.followup.send(view=card("Falha no clima", "O serviço externo não respondeu corretamente."), ephemeral=True)
                    return
        except (aiohttp.ClientError, TimeoutError, ValueError):
            await interaction.followup.send(view=card("Falha de conexão", "Não foi possível consultar o serviço de clima agora."), ephemeral=True)
            return

        weather = (data.get("weather") or [{}])[0]
        main = data.get("main") or {}
        wind = data.get("wind") or {}
        system = data.get("sys") or {}
        offset = int(data.get("timezone") or 0)
        icon = weather.get("icon")
        thumbnail = f"https://openweathermap.org/img/wn/{icon}@2x.png" if icon else None
        await interaction.followup.send(
            view=card(
                f"Clima em {data.get('name', cidade)}, {system.get('country', '—')}",
                str(weather.get("description") or "Condição indisponível").capitalize(),
                fields=[
                    ("Temperatura", f"{float(main.get('temp', 0)):.1f} °C"),
                    ("Sensação", f"{float(main.get('feels_like', 0)):.1f} °C"),
                    ("Mínima / máxima", f"{float(main.get('temp_min', 0)):.1f} °C / {float(main.get('temp_max', 0)):.1f} °C"),
                    ("Umidade", f"{main.get('humidity', '—')}%"),
                    ("Vento", f"{float(wind.get('speed', 0)):.1f} m/s"),
                    ("Nascer / pôr do sol", f"{_weather_time(system.get('sunrise'), offset)} / {_weather_time(system.get('sunset'), offset)}"),
                ],
                thumbnail=thumbnail,
                footer="Fonte: OpenWeatherMap",
            )
        )

    @app_commands.command(name="traduzir", description="Traduz um texto para outro idioma")
    @app_commands.describe(texto="Texto", idioma="Código do idioma alvo: pt, en, es, fr, de, ja...")
    async def traduzir(
        self,
        interaction: discord.Interaction,
        texto: app_commands.Range[str, 1, 450],
        idioma: app_commands.Range[str, 2, 8] = "pt",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_language = idioma.lower().strip()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", target_language):
            await interaction.followup.send(view=card("Idioma inválido", "Use um código como `pt`, `en`, `es`, `fr`, `de` ou `ja`."), ephemeral=True)
            return
        if not self.session:
            await interaction.followup.send(view=card("Serviço iniciando", "Tente novamente em alguns segundos."), ephemeral=True)
            return
        query = urllib.parse.urlencode({"q": texto, "langpair": f"auto|{target_language}"})
        try:
            async with self.session.get(
                f"https://api.mymemory.translated.net/get?{query}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError):
            await interaction.followup.send(view=card("Falha na tradução", "O serviço externo não respondeu corretamente."), ephemeral=True)
            return
        translated = str((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not translated:
            await interaction.followup.send(view=card("Tradução indisponível", "Não recebi uma tradução válida."), ephemeral=True)
            return
        await interaction.followup.send(
            view=card(
                "Tradução",
                "A tradução automática pode exigir revisão em textos técnicos ou ambíguos.",
                fields=[("Original", texto), (f"Destino: {target_language.upper()}", clip(translated, 1200))],
                footer="Fonte: MyMemory Translation API",
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        log.exception("Erro em utilidades extras: %s", original)
        if interaction.response.is_done():
            await interaction.followup.send(view=card("Erro inesperado", "Não foi possível concluir esta ação."), ephemeral=True)
        else:
            await interaction.response.send_message(view=card("Erro inesperado", "Não foi possível concluir esta ação."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utilidades2(bot))
