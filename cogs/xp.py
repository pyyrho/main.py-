"""cogs/xp.py — Sistema de XP e níveis com persistência PostgreSQL.

Atualizações principais:
- respostas e anúncios em Discord Components V2;
- bloqueio por usuário para evitar atualizações concorrentes;
- cargos de nível compatíveis com chaves JSON em texto ou inteiro;
- configuração parcial sem redefinir opções omitidas;
- validação de cor, banner, hierarquia e permissões;
- atribuição de todos os cargos atravessados ao subir vários níveis;
- tratamento de erros para o listener não interromper outros cogs.

Requer ``utils/ui_components.py`` e discord.py >= 2.7.1.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import defaultdict
from typing import Any, Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from db import database as db
from utils.constants import E
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.xp")

XP_MIN = 15
XP_MAX = 40
XP_COOLDOWN_SECONDS = 60
XP_COOLDOWN_GC_AFTER = 10_000
MAX_LEVEL_LIMIT = 1000

_HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def _xp_para_nivel(level: int) -> int:
    """XP necessário para sair de ``level`` e alcançar o próximo nível."""
    level = max(0, int(level))
    return 1000 + (level * 500)


def _total_xp(level: int, current_xp: int) -> int:
    """Converte nível + progresso atual para XP acumulado aproximado."""
    level = max(0, int(level))
    current_xp = max(0, int(current_xp))
    # Soma aritmética: 1000 + 1500 + ... para todos os níveis concluídos.
    completed = (level * (2_000 + ((level - 1) * 500))) // 2 if level else 0
    return completed + current_xp


def _level_bar(xp_atual: int, xp_necessario: int, tamanho: int = 12) -> str:
    tamanho = max(4, min(int(tamanho), 30))
    ratio = min(max(xp_atual / max(xp_necessario, 1), 0.0), 1.0)
    preenchido = int(ratio * tamanho)
    return "█" * preenchido + "░" * (tamanho - preenchido)


def _parse_hex_color(raw: Optional[str], *, default: int = WHITE) -> int:
    value = (raw or "").strip().lstrip("#")
    if not value:
        return default
    if not _HEX_RE.fullmatch(value):
        raise ValueError("Use uma cor no formato `#RRGGBB`.")
    return int(value, 16)


def _is_http_url(raw: Optional[str]) -> bool:
    if not raw:
        return False
    parsed = urlparse(raw.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)




def _safe_max_level(raw: Any, default: int = 100) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_LEVEL_LIMIT))


def _safe_accent(raw: Any, default: int = WHITE) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(0, min(value, 0xFFFFFF))


def _normalize_role_map(raw: Any) -> dict[int, int]:
    """Normaliza mapas vindos de JSONB, onde as chaves costumam virar strings."""
    if not isinstance(raw, dict):
        return {}
    result: dict[int, int] = {}
    for level, role_id in raw.items():
        try:
            parsed_level = int(level)
            parsed_role = int(role_id)
        except (TypeError, ValueError):
            continue
        if parsed_level >= 1 and parsed_role > 0:
            result[parsed_level] = parsed_role
    return result


def _role_manageable(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(
        me
        and me.guild_permissions.manage_roles
        and role != guild.default_role
        and not role.managed
        and role < me.top_role
    )


def _sanitize_xp_state(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {"xp": 0, "level": 0}
    try:
        xp = max(0, int(raw.get("xp", 0)))
    except (TypeError, ValueError):
        xp = 0
    try:
        level = max(0, int(raw.get("level", 0)))
    except (TypeError, ValueError):
        level = 0
    return {"xp": xp, "level": level}


def _apply_xp_amount(state: dict[str, int], amount: int, max_level: int) -> tuple[int, int]:
    """Adiciona XP e retorna ``(nível_antigo, nível_novo)``."""
    max_level = max(1, min(int(max_level), MAX_LEVEL_LIMIT))
    old_level = state["level"]

    if state["level"] >= max_level:
        state["level"] = max_level
        state["xp"] = 0
        return old_level, state["level"]

    state["xp"] = max(0, state["xp"] + int(amount))
    while state["level"] < max_level:
        required = _xp_para_nivel(state["level"])
        if state["xp"] < required:
            break
        state["xp"] -= required
        state["level"] += 1

    if state["level"] >= max_level:
        state["level"] = max_level
        state["xp"] = 0

    return old_level, state["level"]


class XP(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _get_state(self, guild_id: int, user_id: int) -> dict[str, int]:
        return _sanitize_xp_state(await db.get_xp(guild_id, user_id))

    async def _save_state(self, guild_id: int, user_id: int, state: dict[str, int]) -> None:
        await db.upsert_xp(guild_id, user_id, max(0, state["xp"]), max(0, state["level"]))

    async def _grant_crossed_roles(
        self,
        member: discord.Member,
        old_level: int,
        new_level: int,
        role_map_raw: Any,
    ) -> list[discord.Role]:
        """Entrega os cargos configurados nos níveis atravessados."""
        if new_level <= old_level:
            return []

        role_map = _normalize_role_map(role_map_raw)
        granted: list[discord.Role] = []
        for level in range(old_level + 1, new_level + 1):
            role_id = role_map.get(level)
            if not role_id:
                continue
            role = member.guild.get_role(role_id)
            if not role:
                log.warning("Cargo de XP %s do nível %s não existe mais em %s.", role_id, level, member.guild.id)
                continue
            if role in member.roles:
                continue
            if not _role_manageable(member.guild, role):
                log.warning("Cargo de XP %s não é gerenciável no servidor %s.", role.id, member.guild.id)
                continue
            try:
                await member.add_roles(role, reason=f"Recompensa automática de XP: nível {level}")
                granted.append(role)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Falha ao entregar cargo de XP %s para %s.", role.id, member.id)
        return granted

    async def _announce_level_up(
        self,
        message: discord.Message,
        cfg: dict,
        old_level: int,
        new_level: int,
        roles: list[discord.Role],
    ) -> None:
        channel_id = cfg.get("xp_canal")
        channel = message.guild.get_channel(channel_id) if channel_id else message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        title = clip(cfg.get("xp_embed_titulo") or f"{E.TROPHY} Nível alcançado!", 200)
        if new_level - old_level > 1:
            level_line = f"{message.author.mention} avançou do nível **{old_level}** para o **{new_level}**."
        else:
            level_line = f"{message.author.mention} subiu para o **nível {new_level}**."

        description = f"{E.CROWN_PINK} {level_line}\n\n{E.STAR} Continue participando da comunidade! {E.SPARKLE}"
        if roles:
            description += "\n\n**Recompensas recebidas**\n" + " ".join(role.mention for role in roles)

        footer = clip(
            cfg.get("xp_embed_rodape")
            or (
                "Nível máximo alcançado"
                if new_level >= _safe_max_level(cfg.get("xp_max_level"))
                else f"Próximo nível: {_xp_para_nivel(new_level):,} XP"
            ),
            280,
        )
        accent = _safe_accent(cfg.get("xp_embed_cor"))
        banner = cfg.get("xp_embed_banner") if _is_http_url(cfg.get("xp_embed_banner")) else None

        try:
            await channel.send(
                view=card(
                    title,
                    description,
                    thumbnail=message.author.display_avatar.url,
                    image=banner,
                    footer=footer,
                    accent=accent,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Falha ao anunciar nível no servidor %s.", message.guild.id)

    def _cleanup_cooldowns(self, now: float) -> None:
        if len(self._cooldowns) <= XP_COOLDOWN_GC_AFTER:
            return
        threshold = now - (XP_COOLDOWN_SECONDS * 2)
        for key, last_seen in list(self._cooldowns.items()):
            if last_seen < threshold:
                self._cooldowns.pop(key, None)
                # O lock só é descartado quando não está em uso.
                lock = self._locks.get(key)
                if lock is not None and not lock.locked():
                    self._locks.pop(key, None)

    # ── Listener de XP ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        content = (message.content or "").strip()
        if len(content) < 2:
            return

        try:
            cfg = await db.get_guild_config(message.guild.id)
        except Exception:
            log.exception("Falha ao carregar configuração de XP do servidor %s.", message.guild.id)
            return

        if not cfg.get("xp_ativo", True):
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._cooldowns.get(key, 0.0) < XP_COOLDOWN_SECONDS:
            return
        self._cooldowns[key] = now
        self._cleanup_cooldowns(now)

        max_level = _safe_max_level(cfg.get("xp_max_level"))

        async with self._locks[key]:
            try:
                state = await self._get_state(message.guild.id, message.author.id)
                if state["level"] >= max_level:
                    if state["level"] != max_level or state["xp"] != 0:
                        state["level"] = max_level
                        state["xp"] = 0
                        await self._save_state(message.guild.id, message.author.id, state)
                    return

                amount = random.randint(XP_MIN, XP_MAX)
                old_level, new_level = _apply_xp_amount(state, amount, max_level)
                await self._save_state(message.guild.id, message.author.id, state)

                if new_level > old_level:
                    roles = await self._grant_crossed_roles(
                        message.author,
                        old_level,
                        new_level,
                        cfg.get("xp_cargo_nivel"),
                    )
                    await self._announce_level_up(message, cfg, old_level, new_level, roles)
            except Exception:
                log.exception(
                    "Falha ao processar XP de %s no servidor %s.",
                    message.author.id,
                    message.guild.id,
                )

    # ── Grupo /xp ─────────────────────────────────────────────────────────

    xp_group = app_commands.Group(
        name="xp",
        description="Sistema de XP e níveis",
        default_permissions=None,
    )

    @xp_group.command(name="rank", description="Veja seu nível e XP, ou o de outro membro")
    @app_commands.describe(membro="Membro a consultar (padrão: você)")
    @app_commands.guild_only()
    async def rank(self, inter: discord.Interaction, membro: Optional[discord.Member] = None) -> None:
        target = membro or inter.user
        if not isinstance(target, discord.Member) or not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return

        await inter.response.defer()
        try:
            state = await self._get_state(inter.guild.id, target.id)
            cfg = await db.get_guild_config(inter.guild.id)
            position = await db.get_xp_rank_position(inter.guild.id, target.id)
        except Exception:
            log.exception("Falha ao consultar rank de XP.")
            await inter.followup.send(view=card("Falha ao consultar XP", "Tente novamente em instantes."), ephemeral=True)
            return

        level = state["level"]
        xp = state["xp"]
        max_level = _safe_max_level(cfg.get("xp_max_level"))
        at_max = level >= max_level
        required = _xp_para_nivel(level)
        percent = 100 if at_max else min(100, int((xp / max(required, 1)) * 100))
        bar = "█" * 12 if at_max else _level_bar(xp, required)
        position_text = f"#{position}" if position else "Sem posição"

        fields = [
            (f"{E.STAR} Nível", f"`{min(level, max_level)}` / `{max_level}`"),
            (f"{E.GEM} XP", "Nível máximo" if at_max else f"`{xp:,}` / `{required:,}`"),
            (f"{E.N1} Posição", f"`{position_text}`"),
            (f"{E.ORB_GREEN} Progresso", f"`{bar}` **{percent}%**"),
            ("XP acumulado", f"`{_total_xp(level, xp):,}`"),
        ]
        if at_max:
            fields.append((f"{E.CROWN_PINK} Status", "Nível máximo alcançado"))

        await inter.followup.send(
            view=card(
                f"{E.TROPHY} Rank de {target.display_name}",
                "Seu progresso dentro do sistema de níveis do servidor.",
                fields=fields,
                thumbnail=target.display_avatar.url,
                footer=f"{inter.guild.name} • {XP_MIN}–{XP_MAX} XP por mensagem • intervalo de {XP_COOLDOWN_SECONDS}s",
            )
        )

    @xp_group.command(name="top", description="Mostra os 10 membros com mais XP")
    @app_commands.guild_only()
    async def top(self, inter: discord.Interaction) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        await inter.response.defer()
        try:
            ranking = await db.get_xp_ranking(inter.guild.id, 10)
        except Exception:
            log.exception("Falha ao consultar ranking de XP.")
            await inter.followup.send(view=card("Falha ao consultar ranking", "Tente novamente em instantes."), ephemeral=True)
            return

        if not ranking:
            await inter.followup.send(view=card("Ranking vazio", "Nenhum membro possui XP registrado ainda."))
            return

        medals = [E.N1, E.N2, E.N3, E.N4, E.N5, E.N6, "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lines: list[str] = []
        for index, row in enumerate(ranking[:10]):
            member = inter.guild.get_member(int(row["user_id"]))
            name = discord.utils.escape_markdown(member.display_name) if member else f"ID {row['user_id']}"
            medal = medals[index] if index < len(medals) else f"`{index + 1}.`"
            row_data = dict(row)
            level = max(0, int(row_data.get("level", 0)))
            xp = max(0, int(row_data.get("xp", 0)))
            lines.append(f"{medal} **{clip(name, 80)}**\n-# Nível `{level}` • `{xp:,}` XP no nível")

        await inter.followup.send(
            view=card(
                f"{E.TROPHY} Top 10 • {inter.guild.name}",
                "\n\n".join(lines),
                footer="Ranking calculado a partir dos dados salvos no servidor.",
            )
        )

    @xp_group.command(name="config", description="Configura ou consulta o sistema de XP")
    @app_commands.describe(
        canal_nivel="Canal de anúncios de nível",
        nivel_maximo="Nível máximo, entre 1 e 1000",
        ativo="Ativar ou desativar o ganho de XP",
        cor_hex="Cor lateral do anúncio, como #FFFFFF; use 'padrão' para redefinir",
        banner_url="Banner do anúncio; use 'remover' para apagar",
        limpar_canal="Voltar a anunciar no mesmo canal da mensagem",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_config(
        self,
        inter: discord.Interaction,
        canal_nivel: Optional[discord.TextChannel] = None,
        nivel_maximo: Optional[app_commands.Range[int, 1, MAX_LEVEL_LIMIT]] = None,
        ativo: Optional[bool] = None,
        cor_hex: Optional[str] = None,
        banner_url: Optional[str] = None,
        limpar_canal: bool = False,
    ) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return

        updates: dict[str, Any] = {}
        if limpar_canal:
            updates["xp_canal"] = None
        elif canal_nivel is not None:
            updates["xp_canal"] = canal_nivel.id
        if nivel_maximo is not None:
            updates["xp_max_level"] = int(nivel_maximo)
        if ativo is not None:
            updates["xp_ativo"] = ativo

        if cor_hex is not None:
            if cor_hex.strip().lower() in {"padrao", "padrão", "resetar", "remover"}:
                updates["xp_embed_cor"] = WHITE
            else:
                try:
                    updates["xp_embed_cor"] = _parse_hex_color(cor_hex)
                except ValueError as exc:
                    await inter.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
                    return

        if banner_url is not None:
            normalized_banner = banner_url.strip()
            if normalized_banner.lower() in {"remover", "nenhum", "none", "resetar"}:
                updates["xp_embed_banner"] = None
            elif not _is_http_url(normalized_banner):
                await inter.response.send_message(
                    view=card("Banner inválido", "Use uma URL começando com `https://` ou `http://`, ou escreva `remover`."),
                    ephemeral=True,
                )
                return
            else:
                updates["xp_embed_banner"] = normalized_banner

        try:
            if updates:
                await db.upsert_guild_config(inter.guild.id, **updates)
            cfg = await db.get_guild_config(inter.guild.id)
        except Exception:
            log.exception("Falha ao salvar configuração de XP.")
            await inter.response.send_message(view=card("Falha ao salvar", "Tente novamente em instantes."), ephemeral=True)
            return

        channel_id = cfg.get("xp_canal")
        configured_channel = inter.guild.get_channel(channel_id) if channel_id else None
        role_map = _normalize_role_map(cfg.get("xp_cargo_nivel"))
        accent = _safe_accent(cfg.get("xp_embed_cor"))
        banner_status = "Configurado" if _is_http_url(cfg.get("xp_embed_banner")) else "Não configurado"

        await inter.response.send_message(
            view=card(
                "Configuração de XP",
                "As opções não informadas permanecem como estavam.",
                fields=[
                    ("Sistema", "Ativo" if cfg.get("xp_ativo", True) else "Desativado"),
                    ("Canal de anúncios", configured_channel.mention if isinstance(configured_channel, discord.TextChannel) else "Canal onde o XP foi ganho"),
                    ("Nível máximo", f"`{_safe_max_level(cfg.get('xp_max_level'))}`"),
                    ("Cargos automáticos", f"`{len(role_map)}` configurado(s)"),
                    ("Cor lateral", f"`#{accent:06X}`"),
                    ("Banner", banner_status),
                ],
                accent=accent,
                footer="Use novamente /xp config para alterar apenas os campos desejados.",
            ),
            ephemeral=True,
        )

    @xp_group.command(name="dar", description="Adiciona XP manualmente a um membro")
    @app_commands.describe(membro="Membro", quantidade="Quantidade de XP")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_dar(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        quantidade: app_commands.Range[int, 1, 100_000],
    ) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        key = (inter.guild.id, membro.id)
        try:
            async with self._locks[key]:
                state = await self._get_state(inter.guild.id, membro.id)
                cfg = await db.get_guild_config(inter.guild.id)
                max_level = _safe_max_level(cfg.get("xp_max_level"))
                old_level, new_level = _apply_xp_amount(state, int(quantidade), max_level)
                await self._save_state(inter.guild.id, membro.id, state)
                roles = await self._grant_crossed_roles(membro, old_level, new_level, cfg.get("xp_cargo_nivel"))
        except Exception:
            log.exception("Falha ao adicionar XP manualmente.")
            await inter.followup.send(view=card("Falha ao adicionar XP", "Tente novamente em instantes."), ephemeral=True)
            return

        description = (
            f"{E.STAR} {membro.mention} recebeu **{int(quantidade):,} XP**.\n"
            f"{E.TROPHY} Nível atual: **{state['level']}** • progresso: **{state['xp']:,} XP**"
        )
        if roles:
            description += "\n\n**Cargos entregues**\n" + " ".join(role.mention for role in roles)
        await inter.followup.send(view=card("XP adicionado", description, thumbnail=membro.display_avatar.url), ephemeral=True)

    @xp_group.command(name="remover", description="Remove XP do progresso atual de um membro")
    @app_commands.describe(membro="Membro", quantidade="Quantidade de XP a remover")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_remover(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        quantidade: app_commands.Range[int, 1, 100_000],
    ) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        key = (inter.guild.id, membro.id)
        try:
            async with self._locks[key]:
                state = await self._get_state(inter.guild.id, membro.id)
                removed = min(state["xp"], int(quantidade))
                state["xp"] = max(0, state["xp"] - int(quantidade))
                await self._save_state(inter.guild.id, membro.id, state)
        except Exception:
            log.exception("Falha ao remover XP manualmente.")
            await inter.response.send_message(view=card("Falha ao remover XP", "Tente novamente em instantes."), ephemeral=True)
            return

        await inter.response.send_message(
            view=card(
                "XP removido",
                f"{E.WARN_IC} Foram removidos **{removed:,} XP** do progresso atual de {membro.mention}.\n"
                f"{E.TROPHY} Nível: **{state['level']}** • progresso: **{state['xp']:,} XP**",
                thumbnail=membro.display_avatar.url,
            ),
            ephemeral=True,
        )

    @xp_group.command(name="reset", description="Zera o nível e o XP de um membro")
    @app_commands.describe(
        membro="Membro",
        remover_cargos="Também remove os cargos configurados como recompensa de XP",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_reset(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        remover_cargos: bool = False,
    ) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        removed_roles: list[discord.Role] = []
        try:
            key = (inter.guild.id, membro.id)
            async with self._locks[key]:
                await db.upsert_xp(inter.guild.id, membro.id, 0, 0)

            if remover_cargos:
                cfg = await db.get_guild_config(inter.guild.id)
                role_ids = set(_normalize_role_map(cfg.get("xp_cargo_nivel")).values())
                roles = [role for role in membro.roles if role.id in role_ids and _role_manageable(inter.guild, role)]
                if roles:
                    await membro.remove_roles(*roles, reason=f"XP resetado por {inter.user}")
                    removed_roles = roles
        except (discord.Forbidden, discord.HTTPException):
            log.exception("XP foi resetado, mas houve falha ao remover cargos de %s.", membro.id)
        except Exception:
            log.exception("Falha ao resetar XP.")
            await inter.followup.send(view=card("Falha ao resetar XP", "Tente novamente em instantes."), ephemeral=True)
            return

        text = f"{E.LEAF} O nível e o XP de {membro.mention} foram zerados."
        if remover_cargos:
            text += f"\nCargos de XP removidos: **{len(removed_roles)}**."
        await inter.followup.send(view=card("XP zerado", text, thumbnail=membro.display_avatar.url), ephemeral=True)

    @xp_group.command(name="cargo", description="Define um cargo automático para determinado nível")
    @app_commands.describe(nivel="Nível", cargo="Cargo a atribuir")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_cargo(
        self,
        inter: discord.Interaction,
        nivel: app_commands.Range[int, 1, MAX_LEVEL_LIMIT],
        cargo: discord.Role,
    ) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        if not _role_manageable(inter.guild, cargo):
            await inter.response.send_message(
                view=card(
                    "Cargo não gerenciável",
                    "Escolha um cargo comum que esteja abaixo do cargo do bot e verifique a permissão **Gerenciar Cargos**.",
                ),
                ephemeral=True,
            )
            return

        try:
            cfg = await db.get_guild_config(inter.guild.id)
            role_map = _normalize_role_map(cfg.get("xp_cargo_nivel"))
            role_map[int(nivel)] = cargo.id
            # Chaves em texto são estáveis ao salvar em JSON/JSONB.
            serialized = {str(level): role_id for level, role_id in sorted(role_map.items())}
            await db.upsert_guild_config(inter.guild.id, xp_cargo_nivel=serialized)
        except Exception:
            log.exception("Falha ao configurar cargo de XP.")
            await inter.response.send_message(view=card("Falha ao configurar cargo", "Tente novamente em instantes."), ephemeral=True)
            return

        await inter.response.send_message(
            view=card(
                "Cargo de nível configurado",
                f"{E.CROWN_PINK} Ao alcançar o nível **{int(nivel)}**, o membro receberá {cargo.mention}.",
            ),
            ephemeral=True,
        )

    @xp_group.command(name="cargo-remover", description="Remove o cargo automático de determinado nível")
    @app_commands.describe(nivel="Nível")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_cargo_remover(
        self,
        inter: discord.Interaction,
        nivel: app_commands.Range[int, 1, MAX_LEVEL_LIMIT],
    ) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        try:
            cfg = await db.get_guild_config(inter.guild.id)
            role_map = _normalize_role_map(cfg.get("xp_cargo_nivel"))
            removed_role_id = role_map.pop(int(nivel), None)
            if removed_role_id is None:
                await inter.response.send_message(
                    view=card("Configuração não encontrada", f"Não existe cargo automático no nível **{int(nivel)}**."),
                    ephemeral=True,
                )
                return
            serialized = {str(level): role_id for level, role_id in sorted(role_map.items())}
            await db.upsert_guild_config(inter.guild.id, xp_cargo_nivel=serialized)
        except Exception:
            log.exception("Falha ao remover cargo de XP.")
            await inter.response.send_message(view=card("Falha ao remover configuração", "Tente novamente em instantes."), ephemeral=True)
            return

        role = inter.guild.get_role(removed_role_id)
        await inter.response.send_message(
            view=card(
                "Cargo de nível removido",
                f"A recompensa do nível **{int(nivel)}** foi removida"
                + (f": {role.mention}." if role else "."),
            ),
            ephemeral=True,
        )

    @xp_group.command(name="cargos", description="Lista os cargos automáticos configurados por nível")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def xp_cargos(self, inter: discord.Interaction) -> None:
        if not inter.guild:
            await inter.response.send_message("Este comando só funciona em servidores.", ephemeral=True)
            return
        try:
            cfg = await db.get_guild_config(inter.guild.id)
            role_map = _normalize_role_map(cfg.get("xp_cargo_nivel"))
        except Exception:
            log.exception("Falha ao listar cargos de XP.")
            await inter.response.send_message(view=card("Falha ao consultar cargos", "Tente novamente em instantes."), ephemeral=True)
            return

        if not role_map:
            await inter.response.send_message(
                view=card("Nenhum cargo configurado", "Use `/xp cargo` para criar recompensas automáticas."),
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for level, role_id in sorted(role_map.items()):
            role = inter.guild.get_role(role_id)
            status = role.mention if role else f"Cargo removido (`{role_id}`)"
            lines.append(f"**Nível {level}** → {status}")

        await inter.response.send_message(
            view=card("Cargos automáticos de XP", "\n".join(lines), footer=f"Total: {len(lines)} configuração(ões)."),
            ephemeral=True,
        )

    async def cog_app_command_error(self, inter: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        log.error(
            "Erro em comando de XP: %s",
            original,
            exc_info=(type(original), original, original.__traceback__),
        )
        view = card("Erro no sistema de XP", "Não consegui concluir essa operação. Verifique minhas permissões e tente novamente.")
        try:
            if inter.response.is_done():
                await inter.followup.send(view=view, ephemeral=True)
            else:
                await inter.response.send_message(view=view, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(XP(bot))
