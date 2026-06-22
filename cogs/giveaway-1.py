"""cogs/giveaway.py — Sorteios persistentes com participação por botão.

A participação é armazenada no PostgreSQL. Isso corrige o problema clássico de
usar ``message.add_reaction`` (que adiciona a reação do bot, não a do usuário),
permite restaurar sorteios após reinícios e mantém entradas extras de forma
confiável.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from db.database import get_pool
from utils.constants import E
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.giveaway")
GIVEAWAY_EMOJI = "🎉"
MAX_DURATION_SECONDS = 30 * 24 * 3600


def _valid_url(value: Optional[str]) -> bool:
    if not value:
        return True
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_duration(raw: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", raw.lower())
    if not match:
        raise ValueError("Use formatos como 30m, 2h, 1d ou 1w.")
    value = int(match.group(1))
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    seconds = value * factor
    if seconds < 60 or seconds > MAX_DURATION_SECONDS:
        raise ValueError("A duração deve ficar entre 1 minuto e 30 dias.")
    return seconds


async def _ensure_tables() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                id               SERIAL PRIMARY KEY,
                guild_id         BIGINT NOT NULL,
                channel_id       BIGINT NOT NULL,
                message_id       BIGINT,
                host_id          BIGINT NOT NULL,
                premio           TEXT NOT NULL,
                descricao        TEXT,
                imagem           TEXT,
                thumbnail        TEXT,
                cor              INT DEFAULT 2728702,
                vencedores       INT DEFAULT 1,
                encerra_em       TIMESTAMPTZ NOT NULL,
                encerrado        BOOLEAN DEFAULT FALSE,
                roles_permitidos BIGINT[],
                roles_bloqueados BIGINT[],
                bonus_entries    JSONB DEFAULT '{}',
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                giveaway_id BIGINT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                user_id     BIGINT NOT NULL,
                entries     INT NOT NULL DEFAULT 1,
                joined_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (giveaway_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_giveaways_active
                ON giveaways (encerrado, encerra_em);
            """
        )


def _decode_json(value) -> dict[str, int]:
    if isinstance(value, dict):
        return {str(k): int(v) for k, v in value.items()}
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return {str(k): int(v) for k, v in data.items()}
        except (ValueError, TypeError):
            return {}
    return {}


def _giveaway_embed(gw: dict, guild: Optional[discord.Guild] = None) -> discord.Embed:
    ended = bool(gw.get("encerrado"))
    lines: list[str] = []
    if gw.get("descricao"):
        lines.append(clip(gw["descricao"], 800))
    lines.append(
        ("**Sorteio encerrado.**" if ended else f"{E.HEART_ANIM} **Use o botão para participar ou sair.**")
        + f"\n\n{E.LOADING} **Encerra:** {discord.utils.format_dt(gw['encerra_em'], 'R')}"
        + f"\n{E.CROWN_PINK} **Ganhadores:** `{gw['vencedores']}`"
        + f"\n{E.STAFF} **Responsável:** <@{gw['host_id']}>"
    )
    if guild and gw.get("roles_permitidos"):
        roles = [guild.get_role(rid) for rid in gw["roles_permitidos"]]
        roles = [role for role in roles if role]
        if roles:
            lines.append(f"{E.TICKET_IC} **Necessário:** " + " ".join(r.mention for r in roles))
    bonus = _decode_json(gw.get("bonus_entries"))
    if guild and bonus:
        bonus_lines = []
        for role_id, multiplier in bonus.items():
            role = guild.get_role(int(role_id))
            if role:
                bonus_lines.append(f"{role.mention} → `{multiplier}x`")
        if bonus_lines:
            lines.append(f"{E.MAGIC} **Entradas extras**\n" + "\n".join(bonus_lines))

    embed = discord.Embed(
        title=f"{GIVEAWAY_EMOJI} {clip(gw['premio'], 200)}",
        description="\n\n".join(lines),
        color=0x99AAB5 if ended else int(gw.get("cor") or 0x29A6FE),
    )
    if gw.get("thumbnail"):
        embed.set_thumbnail(url=gw["thumbnail"])
    if gw.get("imagem") and not ended:
        embed.set_image(url=gw["imagem"])
    embed.set_footer(text=f"ID: {gw.get('id', '?')} • {'Encerrado' if ended else 'Ativo'}")
    embed.timestamp = gw["encerra_em"]
    return embed


def _member_entries(member: discord.Member, gw: dict) -> int:
    bonus = _decode_json(gw.get("bonus_entries"))
    value = 1
    role_ids = {role.id for role in member.roles}
    for role_id, multiplier in bonus.items():
        if int(role_id) in role_ids:
            value = max(value, max(1, min(int(multiplier), 100)))
    return value


def _eligible(member: discord.Member, gw: dict) -> tuple[bool, str]:
    role_ids = {role.id for role in member.roles}
    allowed = set(gw.get("roles_permitidos") or [])
    blocked = set(gw.get("roles_bloqueados") or [])
    if allowed and not role_ids.intersection(allowed):
        return False, "Você não possui um dos cargos necessários."
    if blocked and role_ids.intersection(blocked):
        return False, "Um de seus cargos bloqueia a participação."
    return True, ""


def _weighted_unique_sample(items: list[tuple[discord.Member, int]], count: int) -> list[discord.Member]:
    pool = [(member, max(1, weight)) for member, weight in items]
    winners: list[discord.Member] = []
    while pool and len(winners) < count:
        total = sum(weight for _, weight in pool)
        needle = random.uniform(0, total)
        cursor = 0.0
        selected_index = 0
        for index, (_, weight) in enumerate(pool):
            cursor += weight
            if needle <= cursor:
                selected_index = index
                break
        member, _ = pool.pop(selected_index)
        winners.append(member)
    return winners


class GiveawayJoinView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Participar / Sair",
        style=discord.ButtonStyle.success,
        emoji=GIVEAWAY_EMOJI,
        custom_id="giveaway:participar",
    )
    async def participar(self, inter: discord.Interaction, _: discord.ui.Button) -> None:
        if not inter.guild or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Este botão só funciona em servidores.", ephemeral=True)
            return
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM giveaways WHERE message_id=$1 AND encerrado=FALSE",
                inter.message.id,
            )
            if not row:
                await inter.response.send_message(view=card("Sorteio encerrado", "Este sorteio não aceita mais entradas."), ephemeral=True)
                return
            gw = dict(row)
            ok, reason = _eligible(inter.user, gw)
            if not ok:
                await inter.response.send_message(view=card("Participação indisponível", reason), ephemeral=True)
                return
            existing = await conn.fetchval(
                "SELECT 1 FROM giveaway_entries WHERE giveaway_id=$1 AND user_id=$2",
                gw["id"],
                inter.user.id,
            )
            if existing:
                await conn.execute(
                    "DELETE FROM giveaway_entries WHERE giveaway_id=$1 AND user_id=$2",
                    gw["id"],
                    inter.user.id,
                )
                await inter.response.send_message(view=card("Você saiu do sorteio", f"Sua participação em **{clip(gw['premio'], 100)}** foi removida."), ephemeral=True)
                return
            entries = _member_entries(inter.user, gw)
            await conn.execute(
                """
                INSERT INTO giveaway_entries (giveaway_id, user_id, entries)
                VALUES ($1, $2, $3)
                ON CONFLICT (giveaway_id, user_id)
                DO UPDATE SET entries=$3, joined_at=NOW()
                """,
                gw["id"],
                inter.user.id,
                entries,
            )
        await inter.response.send_message(
            view=card(
                "Participação confirmada",
                f"Você entrou no sorteio de **{clip(gw['premio'], 100)}**.",
                fields=[("Entradas", f"`{entries}x`")],
                accent=WHITE,
            ),
            ephemeral=True,
        )


@dataclass
class GiveawayBuilder:
    host: discord.Member
    premio: str = "Prêmio do Sorteio"
    descricao: Optional[str] = None
    imagem: Optional[str] = None
    thumbnail: Optional[str] = None
    cor: int = 0x29A6FE
    vencedores: int = 1
    duracao_secs: int = 3600
    roles_permitidos: list[int] = field(default_factory=list)
    roles_bloqueados: list[int] = field(default_factory=list)
    bonus_entries: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "host_id": self.host.id,
            "premio": self.premio,
            "descricao": self.descricao,
            "imagem": self.imagem,
            "thumbnail": self.thumbnail,
            "cor": self.cor,
            "vencedores": self.vencedores,
            "encerra_em": datetime.now(timezone.utc) + timedelta(seconds=self.duracao_secs),
            "roles_permitidos": self.roles_permitidos or None,
            "roles_bloqueados": self.roles_bloqueados or None,
            "bonus_entries": self.bonus_entries,
            "encerrado": False,
        }


class AparenciaModal(discord.ui.Modal, title="Aparência do Sorteio"):
    nome_f = discord.ui.TextInput(label="Nome do sorteio", max_length=100)
    desc_f = discord.ui.TextInput(label="Descrição", required=False, style=discord.TextStyle.paragraph, max_length=700)
    img_f = discord.ui.TextInput(label="URL da imagem", required=False, max_length=500)
    thumb_f = discord.ui.TextInput(label="URL da miniatura", required=False, max_length=500)
    cor_f = discord.ui.TextInput(label="Cor hexadecimal", required=False, max_length=7)

    def __init__(self, builder: GiveawayBuilder) -> None:
        super().__init__()
        self.builder = builder
        self.nome_f.default = builder.premio
        self.desc_f.default = builder.descricao or ""
        self.img_f.default = builder.imagem or ""
        self.thumb_f.default = builder.thumbnail or ""
        self.cor_f.default = f"#{builder.cor:06X}"

    async def on_submit(self, inter: discord.Interaction) -> None:
        image = self.img_f.value.strip() or None
        thumb = self.thumb_f.value.strip() or None
        if not _valid_url(image) or not _valid_url(thumb):
            await inter.response.send_message(view=card("URL inválida", "Use endereços começando com http:// ou https://."), ephemeral=True)
            return
        raw_color = self.cor_f.value.strip().lstrip("#")
        if raw_color and not re.fullmatch(r"[0-9a-fA-F]{6}", raw_color):
            await inter.response.send_message(view=card("Cor inválida", "Use o formato #RRGGBB."), ephemeral=True)
            return
        self.builder.premio = self.nome_f.value.strip()
        self.builder.descricao = self.desc_f.value.strip() or None
        self.builder.imagem = image
        self.builder.thumbnail = thumb
        if raw_color:
            self.builder.cor = int(raw_color, 16)
        await inter.response.send_message(view=card("Aparência atualizada", "As alterações foram salvas no rascunho."), ephemeral=True)


class GeralModal(discord.ui.Modal, title="Configurações Gerais"):
    duracao_f = discord.ui.TextInput(label="Duração: 30m, 2h, 1d, 1w", default="1h", max_length=12)
    vencedores_f = discord.ui.TextInput(label="Número de vencedores", default="1", max_length=2)

    def __init__(self, builder: GiveawayBuilder) -> None:
        super().__init__()
        self.builder = builder

    async def on_submit(self, inter: discord.Interaction) -> None:
        try:
            seconds = _parse_duration(self.duracao_f.value)
            winners = int(self.vencedores_f.value.strip())
            if not 1 <= winners <= 20:
                raise ValueError("O número de vencedores deve ficar entre 1 e 20.")
        except ValueError as exc:
            await inter.response.send_message(view=card("Configuração inválida", str(exc)), ephemeral=True)
            return
        self.builder.duracao_secs = seconds
        self.builder.vencedores = winners
        await inter.response.send_message(
            view=card("Configurações salvas", f"Duração: `{self.duracao_f.value}`\nVencedores: `{winners}`"),
            ephemeral=True,
        )


class GiveawayRolesView(discord.ui.View):
    def __init__(self, builder: GiveawayBuilder, host_id: int) -> None:
        super().__init__(timeout=180)
        self.builder = builder
        self.host_id = host_id
        allowed = discord.ui.RoleSelect(
            placeholder="Cargos permitidos: selecione ou deixe vazio",
            min_values=0,
            max_values=10,
            row=0,
        )
        blocked = discord.ui.RoleSelect(
            placeholder="Cargos bloqueados: selecione ou deixe vazio",
            min_values=0,
            max_values=10,
            row=1,
        )
        self.allowed_select = allowed
        self.blocked_select = blocked
        allowed.callback = self._allowed
        blocked.callback = self._blocked
        self.add_item(allowed)
        self.add_item(blocked)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.host_id:
            await inter.response.send_message("Apenas o criador pode editar este rascunho.", ephemeral=True)
            return False
        return True

    async def _allowed(self, inter: discord.Interaction) -> None:
        select = self.allowed_select
        self.builder.roles_permitidos = [role.id for role in select.values]
        await inter.response.send_message(view=card("Cargos permitidos atualizados", f"Selecionados: `{len(select.values)}`."), ephemeral=True)

    async def _blocked(self, inter: discord.Interaction) -> None:
        select = self.blocked_select
        self.builder.roles_bloqueados = [role.id for role in select.values]
        await inter.response.send_message(view=card("Cargos bloqueados atualizados", f"Selecionados: `{len(select.values)}`."), ephemeral=True)


class GiveawayBonusView(discord.ui.View):
    def __init__(self, builder: GiveawayBuilder, host_id: int) -> None:
        super().__init__(timeout=180)
        self.builder = builder
        self.host_id = host_id
        self.role_id: Optional[int] = None
        self.multiplier = 2
        role_select = discord.ui.RoleSelect(placeholder="Escolha o cargo", min_values=1, max_values=1, row=0)
        multiplier = discord.ui.Select(
            placeholder="Escolha o multiplicador",
            options=[discord.SelectOption(label=f"{i} entradas", value=str(i)) for i in range(2, 11)],
            row=1,
        )
        self.role_select = role_select
        self.multiplier_select = multiplier
        role_select.callback = self._role
        multiplier.callback = self._multiplier
        self.add_item(role_select)
        self.add_item(multiplier)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.host_id:
            await inter.response.send_message("Apenas o criador pode editar este rascunho.", ephemeral=True)
            return False
        return True

    async def _role(self, inter: discord.Interaction) -> None:
        self.role_id = self.role_select.values[0].id
        await self._save_if_ready(inter)

    async def _multiplier(self, inter: discord.Interaction) -> None:
        self.multiplier = int(self.multiplier_select.values[0])
        await self._save_if_ready(inter)

    async def _save_if_ready(self, inter: discord.Interaction) -> None:
        if self.role_id is None:
            await inter.response.send_message("Agora selecione o cargo.", ephemeral=True)
            return
        self.builder.bonus_entries[str(self.role_id)] = self.multiplier
        role = inter.guild.get_role(self.role_id)
        await inter.response.send_message(
            view=card("Entrada extra configurada", f"{role.mention if role else self.role_id} terá `{self.multiplier}x` entradas."),
            ephemeral=True,
        )


class GiveawayBuilderLayout(discord.ui.LayoutView):
    def __init__(self, cog: "Giveaway", builder: GiveawayBuilder, channel: discord.TextChannel) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.builder = builder
        self.channel = channel
        container = discord.ui.Container(accent_color=WHITE)
        container.add_item(discord.ui.TextDisplay(
            f"## {GIVEAWAY_EMOJI} Configurar sorteio\n\n"
            f"**Canal:** {channel.mention}\n**Prêmio:** {clip(builder.premio, 100)}\n\n"
            "Ajuste aparência, duração, regras de cargos e entradas extras. "
            "Use **Preview** antes de publicar."
        ))
        row1 = discord.ui.ActionRow()
        row2 = discord.ui.ActionRow()
        buttons = [
            ("Aparência", discord.ButtonStyle.primary, self._appearance, "🎨", row1),
            ("Geral", discord.ButtonStyle.primary, self._general, "⚙️", row1),
            ("Cargos", discord.ButtonStyle.secondary, self._roles, "🛡️", row1),
            ("Entradas extras", discord.ButtonStyle.secondary, self._bonus, "✨", row2),
            ("Preview", discord.ButtonStyle.secondary, self._preview, "👁️", row2),
            ("Iniciar", discord.ButtonStyle.success, self._start, GIVEAWAY_EMOJI, row2),
        ]
        for label, style, callback, emoji, row in buttons:
            button = discord.ui.Button(label=label, style=style, emoji=emoji)
            button.callback = callback
            row.add_item(button)
        container.add_item(row1)
        container.add_item(row2)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("-# O rascunho expira após 10 minutos sem interação."))
        self.add_item(container)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.builder.host.id:
            await inter.response.send_message("Apenas quem criou o rascunho pode configurá-lo.", ephemeral=True)
            return False
        return True

    async def _appearance(self, inter: discord.Interaction) -> None:
        await inter.response.send_modal(AparenciaModal(self.builder))

    async def _general(self, inter: discord.Interaction) -> None:
        await inter.response.send_modal(GeralModal(self.builder))

    async def _roles(self, inter: discord.Interaction) -> None:
        await inter.response.send_message(
            "Selecione os cargos permitidos e bloqueados nos menus abaixo.",
            view=GiveawayRolesView(self.builder, self.builder.host.id),
            ephemeral=True,
        )

    async def _bonus(self, inter: discord.Interaction) -> None:
        await inter.response.send_message(
            "Selecione um cargo e depois o multiplicador.",
            view=GiveawayBonusView(self.builder, self.builder.host.id),
            ephemeral=True,
        )

    async def _preview(self, inter: discord.Interaction) -> None:
        data = self.builder.to_dict()
        data["id"] = "preview"
        await inter.response.send_message(embed=_giveaway_embed(data, inter.guild), ephemeral=True)

    async def _start(self, inter: discord.Interaction) -> None:
        await inter.response.defer(ephemeral=True)
        await self.cog._publish(inter, self.builder, self.channel)
        self.stop()


class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._tasks: dict[int, asyncio.Task] = {}
        self._closing: set[int] = set()

    async def cog_load(self) -> None:
        await _ensure_tables()
        self.bot.add_view(GiveawayJoinView())
        await self._restore()

    def cog_unload(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _restore(self) -> None:
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT * FROM giveaways WHERE encerrado=FALSE")
        for row in rows:
            gw = dict(row)
            self._schedule(gw)
        log.info("[GIVEAWAY] %s sorteio(s) restaurado(s).", len(rows))

    def _schedule(self, gw: dict) -> None:
        giveaway_id = int(gw["id"])
        old = self._tasks.pop(giveaway_id, None)
        if old:
            old.cancel()
        self._tasks[giveaway_id] = asyncio.create_task(self._wait_and_end(giveaway_id, gw["encerra_em"]))

    async def _wait_and_end(self, giveaway_id: int, end_at: datetime) -> None:
        try:
            delay = max(0.0, (end_at - datetime.now(timezone.utc)).total_seconds())
            await asyncio.sleep(delay)
            await self._end(giveaway_id)
        except asyncio.CancelledError:
            raise
        finally:
            self._tasks.pop(giveaway_id, None)

    async def _publish(self, inter: discord.Interaction, builder: GiveawayBuilder, channel: discord.TextChannel) -> None:
        me = inter.guild.me
        permissions = channel.permissions_for(me) if me else None
        if not permissions or not permissions.send_messages or not permissions.embed_links:
            await inter.followup.send(view=card("Sem permissão", f"Não consigo publicar corretamente em {channel.mention}."), ephemeral=True)
            return
        data = builder.to_dict()
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO giveaways
                    (guild_id, channel_id, host_id, premio, descricao, imagem, thumbnail,
                     cor, vencedores, encerra_em, roles_permitidos, roles_bloqueados, bonus_entries)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
                RETURNING id
                """,
                inter.guild_id,
                channel.id,
                builder.host.id,
                builder.premio,
                builder.descricao,
                builder.imagem,
                builder.thumbnail,
                builder.cor,
                builder.vencedores,
                data["encerra_em"],
                builder.roles_permitidos or None,
                builder.roles_bloqueados or None,
                json.dumps(builder.bonus_entries),
            )
        data["id"] = int(row["id"])
        try:
            message = await channel.send(embed=_giveaway_embed(data, inter.guild), view=GiveawayJoinView())
        except discord.HTTPException:
            async with get_pool().acquire() as conn:
                await conn.execute("DELETE FROM giveaways WHERE id=$1", data["id"])
            await inter.followup.send(
                view=card("Falha ao publicar", "O sorteio foi revertido porque a mensagem não pôde ser enviada."),
                ephemeral=True,
            )
            return
        async with get_pool().acquire() as conn:
            await conn.execute("UPDATE giveaways SET message_id=$1 WHERE id=$2", message.id, data["id"])
        data["message_id"] = message.id
        self._schedule(data)
        await inter.followup.send(
            view=card(
                "Sorteio iniciado",
                f"**{clip(builder.premio, 100)}** foi publicado em {channel.mention}.",
                fields=[
                    ("Encerra", discord.utils.format_dt(data["encerra_em"], "R")),
                    ("ID", f"`{data['id']}`"),
                ],
            ),
            ephemeral=True,
        )

    async def _end(self, giveaway_id: int) -> None:
        if giveaway_id in self._closing:
            return
        self._closing.add(giveaway_id)
        try:
            async with get_pool().acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("SELECT * FROM giveaways WHERE id=$1 FOR UPDATE", giveaway_id)
                    if not row or row["encerrado"]:
                        return
                    await conn.execute("UPDATE giveaways SET encerrado=TRUE WHERE id=$1", giveaway_id)
                    entry_rows = await conn.fetch(
                        "SELECT user_id, entries FROM giveaway_entries WHERE giveaway_id=$1",
                        giveaway_id,
                    )
            gw = dict(row)
            guild = self.bot.get_guild(gw["guild_id"])
            if not guild:
                return
            channel = guild.get_channel(gw["channel_id"])
            if not isinstance(channel, discord.TextChannel) or not gw.get("message_id"):
                return
            try:
                message = await channel.fetch_message(gw["message_id"])
            except discord.HTTPException:
                return

            candidates: list[tuple[discord.Member, int]] = []
            for entry in entry_rows:
                member = guild.get_member(entry["user_id"])
                if not member or member.bot or member.id == gw["host_id"]:
                    continue
                ok, _ = _eligible(member, gw)
                if ok:
                    candidates.append((member, max(1, int(entry["entries"]))))
            winners = _weighted_unique_sample(candidates, min(int(gw["vencedores"]), len(candidates)))
            mentions = " ".join(member.mention for member in winners)
            if winners:
                description = (
                    f"{E.HEART_ANIM} **Vencedores:** {mentions}\n\n"
                    f"{E.STAR} **Prêmio:** {clip(gw['premio'], 200)}\n"
                    f"{E.STAFF} **Responsável:** <@{gw['host_id']}>"
                )
            else:
                description = f"{E.GHOST} Nenhum participante elegível.\n\n{E.STAR} **Prêmio:** {clip(gw['premio'], 200)}"
            gw["encerrado"] = True
            final_embed = _giveaway_embed(gw, guild)
            final_embed.title = f"{GIVEAWAY_EMOJI} SORTEIO ENCERRADO • {clip(gw['premio'], 150)}"
            final_embed.description = description
            try:
                await message.edit(embed=final_embed, view=None)
                if winners:
                    await channel.send(
                        f"{GIVEAWAY_EMOJI} {mentions} ganharam **{clip(gw['premio'], 150)}**!",
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
            except discord.HTTPException:
                log.exception("Falha ao finalizar mensagem do sorteio %s", giveaway_id)
        finally:
            self._closing.discard(giveaway_id)

    gv_group = app_commands.Group(
        name="giveaway",
        description="Sistema de sorteios",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @gv_group.command(name="criar", description="Abre o configurador de sorteio")
    async def gv_criar(self, inter: discord.Interaction, canal: discord.TextChannel, premio: app_commands.Range[str, 1, 100]) -> None:
        builder = GiveawayBuilder(host=inter.user, premio=premio.strip())
        await inter.response.send_message(view=GiveawayBuilderLayout(self, builder, canal), ephemeral=True)

    @gv_group.command(name="encerrar", description="Encerra um sorteio antecipadamente")
    async def gv_encerrar(self, inter: discord.Interaction, giveaway_id: int) -> None:
        await inter.response.defer(ephemeral=True)
        async with get_pool().acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM giveaways WHERE id=$1 AND guild_id=$2 AND encerrado=FALSE",
                giveaway_id,
                inter.guild_id,
            )
        if not exists:
            await inter.followup.send(view=card("Sorteio não encontrado", "O ID não existe ou já foi encerrado."), ephemeral=True)
            return
        task = self._tasks.pop(giveaway_id, None)
        if task:
            task.cancel()
        await self._end(giveaway_id)
        await inter.followup.send(view=card("Sorteio encerrado", f"O sorteio `#{giveaway_id}` foi finalizado."), ephemeral=True)

    @gv_group.command(name="resorteio", description="Seleciona novos vencedores de um sorteio encerrado")
    async def gv_resorteio(self, inter: discord.Interaction, giveaway_id: int) -> None:
        await inter.response.defer()
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM giveaways WHERE id=$1 AND guild_id=$2 AND encerrado=TRUE",
                giveaway_id,
                inter.guild_id,
            )
            entries = await conn.fetch(
                "SELECT user_id, entries FROM giveaway_entries WHERE giveaway_id=$1",
                giveaway_id,
            ) if row else []
        if not row:
            await inter.followup.send(view=card("Sorteio não encontrado", "O sorteio precisa existir e estar encerrado."), ephemeral=True)
            return
        gw = dict(row)
        candidates = []
        for entry in entries:
            member = inter.guild.get_member(entry["user_id"])
            if member and not member.bot and member.id != gw["host_id"] and _eligible(member, gw)[0]:
                candidates.append((member, int(entry["entries"])))
        winners = _weighted_unique_sample(candidates, min(int(gw["vencedores"]), len(candidates)))
        if not winners:
            await inter.followup.send(view=card("Sem participantes", "Nenhum participante elegível foi encontrado."), ephemeral=True)
            return
        mentions = " ".join(member.mention for member in winners)
        await inter.followup.send(
            view=card(
                f"{GIVEAWAY_EMOJI} Resorteio",
                f"**Novos vencedores:** {mentions}\n\n**Prêmio:** {clip(gw['premio'], 150)}",
                footer=f"Resorteado por {inter.user}",
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    @gv_group.command(name="lista", description="Lista os sorteios ativos")
    async def gv_lista(self, inter: discord.Interaction) -> None:
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT g.*, COUNT(e.user_id) AS participantes
                FROM giveaways g
                LEFT JOIN giveaway_entries e ON e.giveaway_id=g.id
                WHERE g.guild_id=$1 AND g.encerrado=FALSE
                GROUP BY g.id ORDER BY g.encerra_em LIMIT 20
                """,
                inter.guild_id,
            )
        if not rows:
            await inter.response.send_message(view=card("Sem sorteios ativos", "Nenhum sorteio está em andamento."), ephemeral=True)
            return
        lines = []
        for row in rows:
            channel = inter.guild.get_channel(row["channel_id"])
            lines.append(
                f"**`#{row['id']}` {clip(row['premio'], 80)}**\n"
                f"{channel.mention if channel else 'canal removido'} • "
                f"{discord.utils.format_dt(row['encerra_em'], 'R')} • "
                f"`{row['participantes']}` participante(s)"
            )
        await inter.response.send_message(view=card("Sorteios ativos", "\n\n".join(lines)), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaway(bot))
