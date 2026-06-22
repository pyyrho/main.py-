"""cogs/selfroles.py — Painéis persistentes de cargos com Components V2."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db.database import get_pool
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.selfroles")
MAX_ROLES_PER_PANEL = 20


def _parse_hex_color(raw: Optional[str], default: int = WHITE) -> int:
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


def _safe_emoji(raw: Optional[str]) -> Optional[discord.PartialEmoji]:
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw.strip())
    except (ValueError, TypeError):
        return None


async def _ensure_table() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS selfroles_panels (
                message_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                guild_id   BIGINT NOT NULL,
                titulo     TEXT,
                descricao  TEXT,
                cor        INT DEFAULT 16777215,
                roles      JSONB DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_selfroles_guild
                ON selfroles_panels (guild_id, created_at DESC);
            """
        )


def _decode_roles(value) -> list[dict]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, list) else []
        except ValueError:
            return []
    return []


async def _get_panel(message_id: int) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM selfroles_panels WHERE message_id=$1", message_id)
    if not row:
        return None
    panel = dict(row)
    panel["roles"] = _decode_roles(panel.get("roles"))
    return panel


async def _save_panel(panel: dict) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO selfroles_panels
                (message_id, channel_id, guild_id, titulo, descricao, cor, roles)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (message_id) DO UPDATE SET
                channel_id=$2, guild_id=$3, titulo=$4,
                descricao=$5, cor=$6, roles=$7::jsonb
            """,
            panel["message_id"],
            panel["channel_id"],
            panel["guild_id"],
            panel["titulo"],
            panel["descricao"],
            panel["cor"],
            json.dumps(panel["roles"], ensure_ascii=False),
        )


async def _delete_panel(message_id: int, guild_id: int) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM selfroles_panels WHERE message_id=$1 AND guild_id=$2",
            message_id,
            guild_id,
        )
    return result != "DELETE 0"


async def _list_panels(guild_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM selfroles_panels WHERE guild_id=$1 ORDER BY created_at DESC LIMIT 20",
            guild_id,
        )
    result = []
    for row in rows:
        panel = dict(row)
        panel["roles"] = _decode_roles(panel.get("roles"))
        result.append(panel)
    return result


class SelfRoleButton(discord.ui.Button):
    def __init__(self, role_id: int, label: str, emoji: Optional[str], style: discord.ButtonStyle) -> None:
        super().__init__(
            label=clip(label, 80),
            emoji=_safe_emoji(emoji),
            style=style,
            custom_id=f"selfrole:{role_id}",
        )
        self.role_id = role_id

    async def callback(self, inter: discord.Interaction) -> None:
        if not inter.guild or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Este botão só funciona em servidores.", ephemeral=True)
            return
        role = inter.guild.get_role(self.role_id)
        if not role:
            await inter.response.send_message(view=card("Cargo removido", "Este cargo não existe mais no servidor."), ephemeral=True)
            return
        if not _role_manageable(inter.guild, role):
            await inter.response.send_message(
                view=card("Não consigo gerenciar este cargo", "Verifique a permissão **Gerenciar Cargos** e a hierarquia do bot."),
                ephemeral=True,
            )
            return
        try:
            if role in inter.user.roles:
                await inter.user.remove_roles(role, reason="Self-role removido pelo membro")
                title = "Cargo removido"
                text = f"**{role.name}** foi removido do seu perfil."
            else:
                await inter.user.add_roles(role, reason="Self-role adicionado pelo membro")
                title = "Cargo adicionado"
                text = f"**{role.name}** foi adicionado ao seu perfil."
        except discord.HTTPException:
            log.exception("Falha ao alterar self-role %s para %s", role.id, inter.user.id)
            await inter.response.send_message(view=card("Falha ao alterar cargo", "Tente novamente ou avise a equipe."), ephemeral=True)
            return
        await inter.response.send_message(view=card(title, text), ephemeral=True)


class SelfRolesLayout(discord.ui.LayoutView):
    def __init__(self, panel: dict) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=int(panel.get("cor") or WHITE))
        container.add_item(
            discord.ui.TextDisplay(
                f"## {clip(panel.get('titulo') or 'Selecione seus cargos', 250)}\n\n"
                f"{clip(panel.get('descricao') or 'Use os botões abaixo para adicionar ou remover cargos.', 1800)}"
            )
        )
        roles = _decode_roles(panel.get("roles"))[:MAX_ROLES_PER_PANEL]
        styles = [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.secondary,
        ]
        for start in range(0, len(roles), 5):
            row = discord.ui.ActionRow()
            for index, role_data in enumerate(roles[start:start + 5], start=start):
                row.add_item(
                    SelfRoleButton(
                        int(role_data["role_id"]),
                        role_data.get("label") or role_data.get("role_name") or "Cargo",
                        role_data.get("emoji"),
                        styles[index % len(styles)],
                    )
                )
            container.add_item(row)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("-# Clique novamente no mesmo botão para remover o cargo."))
        self.add_item(container)


class SelfRoles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await _ensure_table()
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT * FROM selfroles_panels")
        restored = 0
        stale: list[tuple[int, int]] = []
        for row in rows:
            panel = dict(row)
            panel["roles"] = _decode_roles(panel.get("roles"))
            guild = self.bot.get_guild(panel["guild_id"])
            channel = guild.get_channel(panel["channel_id"]) if guild else None
            if not isinstance(channel, discord.TextChannel):
                stale.append((panel["message_id"], panel["guild_id"]))
                continue
            self.bot.add_view(SelfRolesLayout(panel), message_id=panel["message_id"])
            restored += 1
        for message_id, guild_id in stale:
            await _delete_panel(message_id, guild_id)
        log.info("[SELFROLES] %s painel(is) restaurado(s); %s registro(s) obsoleto(s) removido(s).", restored, len(stale))

    sr_group = app_commands.Group(
        name="selfroles",
        description="Sistema de cargos por botão",
        default_permissions=discord.Permissions(manage_roles=True),
    )

    async def _refresh_message(self, panel: dict) -> bool:
        guild = self.bot.get_guild(panel["guild_id"])
        channel = guild.get_channel(panel["channel_id"]) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            message = await channel.fetch_message(panel["message_id"])
            layout = SelfRolesLayout(panel)
            self.bot.add_view(layout, message_id=panel["message_id"])
            await message.edit(content=None, embed=None, attachments=[], view=layout)
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Falha ao atualizar painel selfroles %s", panel["message_id"])
            return False

    @sr_group.command(name="painel", description="Cria um painel de cargos com botões")
    async def sr_painel(
        self,
        inter: discord.Interaction,
        canal: discord.TextChannel,
        titulo: app_commands.Range[str, 1, 200] = "🎭 Selecione seus cargos",
        descricao: app_commands.Range[str, 1, 1800] = "Clique nos botões abaixo para adicionar ou remover cargos do seu perfil.",
        cor: str = "#FFFFFF",
    ) -> None:
        try:
            color = _parse_hex_color(cor)
        except ValueError as exc:
            await inter.response.send_message(view=card("Cor inválida", str(exc)), ephemeral=True)
            return
        me = inter.guild.me
        permissions = canal.permissions_for(me) if me else None
        if not permissions or not permissions.send_messages:
            await inter.response.send_message(view=card("Sem permissão", f"Não consigo enviar em {canal.mention}."), ephemeral=True)
            return
        placeholder = {
            "message_id": 0,
            "channel_id": canal.id,
            "guild_id": inter.guild_id,
            "titulo": titulo,
            "descricao": descricao,
            "cor": color,
            "roles": [],
        }
        message = await canal.send(view=SelfRolesLayout(placeholder))
        placeholder["message_id"] = message.id
        await _save_panel(placeholder)
        await inter.response.send_message(
            view=card(
                "Painel criado",
                f"O painel foi enviado em {canal.mention}.",
                fields=[("ID da mensagem", f"`{message.id}`"), ("Próximo passo", "Use `/selfroles adicionar`." )],
            ),
            ephemeral=True,
        )

    @sr_group.command(name="adicionar", description="Adiciona um cargo a um painel")
    async def sr_adicionar(
        self,
        inter: discord.Interaction,
        message_id: str,
        cargo: discord.Role,
        label: Optional[app_commands.Range[str, 1, 80]] = None,
        emoji: Optional[app_commands.Range[str, 1, 100]] = None,
    ) -> None:
        try:
            mid = int(message_id)
        except ValueError:
            await inter.response.send_message(view=card("ID inválido", "Informe o ID numérico da mensagem."), ephemeral=True)
            return
        panel = await _get_panel(mid)
        if not panel or panel["guild_id"] != inter.guild_id:
            await inter.response.send_message(view=card("Painel não encontrado", "Não existe um painel com esse ID neste servidor."), ephemeral=True)
            return
        if not _role_manageable(inter.guild, cargo):
            await inter.response.send_message(view=card("Cargo inalcançável", "O cargo do bot precisa ficar acima do cargo selecionado."), ephemeral=True)
            return
        if len(panel["roles"]) >= MAX_ROLES_PER_PANEL:
            await inter.response.send_message(view=card("Limite atingido", f"Cada painel aceita até {MAX_ROLES_PER_PANEL} cargos."), ephemeral=True)
            return
        if any(int(item["role_id"]) == cargo.id for item in panel["roles"]):
            await inter.response.send_message(view=card("Cargo duplicado", f"{cargo.mention} já está neste painel."), ephemeral=True)
            return
        parsed_emoji = _safe_emoji(emoji)
        if emoji and not parsed_emoji:
            await inter.response.send_message(view=card("Emoji inválido", "Use um emoji Unicode ou um emoji personalizado válido."), ephemeral=True)
            return
        panel["roles"].append(
            {
                "role_id": cargo.id,
                "role_name": cargo.name,
                "label": label or cargo.name,
                "emoji": str(parsed_emoji) if parsed_emoji else None,
            }
        )
        await _save_panel(panel)
        if not await self._refresh_message(panel):
            await inter.response.send_message(view=card("Painel salvo, mas não atualizado", "A mensagem pode ter sido removida ou o bot perdeu acesso ao canal."), ephemeral=True)
            return
        await inter.response.send_message(view=card("Cargo adicionado", f"{cargo.mention} foi incluído no painel."), ephemeral=True)

    @sr_group.command(name="remover", description="Remove um cargo de um painel")
    async def sr_remover(self, inter: discord.Interaction, message_id: str, cargo: discord.Role) -> None:
        try:
            mid = int(message_id)
        except ValueError:
            await inter.response.send_message(view=card("ID inválido", "Informe um número."), ephemeral=True)
            return
        panel = await _get_panel(mid)
        if not panel or panel["guild_id"] != inter.guild_id:
            await inter.response.send_message(view=card("Painel não encontrado", "O painel não existe neste servidor."), ephemeral=True)
            return
        original = len(panel["roles"])
        panel["roles"] = [item for item in panel["roles"] if int(item["role_id"]) != cargo.id]
        if len(panel["roles"]) == original:
            await inter.response.send_message(view=card("Cargo não encontrado", f"{cargo.mention} não está nesse painel."), ephemeral=True)
            return
        await _save_panel(panel)
        await self._refresh_message(panel)
        await inter.response.send_message(view=card("Cargo removido", f"{cargo.mention} foi removido do painel."), ephemeral=True)

    @sr_group.command(name="apagar", description="Apaga um painel e seu registro")
    async def sr_apagar(self, inter: discord.Interaction, message_id: str, apagar_mensagem: bool = True) -> None:
        try:
            mid = int(message_id)
        except ValueError:
            await inter.response.send_message(view=card("ID inválido", "Informe um número."), ephemeral=True)
            return
        panel = await _get_panel(mid)
        if not panel or panel["guild_id"] != inter.guild_id:
            await inter.response.send_message(view=card("Painel não encontrado", "O painel não existe neste servidor."), ephemeral=True)
            return
        if apagar_mensagem:
            channel = inter.guild.get_channel(panel["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(mid)
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await _delete_panel(mid, inter.guild_id)
        await inter.response.send_message(view=card("Painel apagado", f"O painel `{mid}` foi removido."), ephemeral=True)

    @sr_group.command(name="lista", description="Lista os painéis de cargos do servidor")
    async def sr_lista(self, inter: discord.Interaction) -> None:
        panels = await _list_panels(inter.guild_id)
        if not panels:
            await inter.response.send_message(view=card("Sem painéis", "Nenhum painel foi criado."), ephemeral=True)
            return
        lines = []
        for panel in panels:
            channel = inter.guild.get_channel(panel["channel_id"])
            lines.append(
                f"**{clip(panel.get('titulo') or 'Sem título', 100)}**\n"
                f"{channel.mention if channel else 'canal removido'} • ID `{panel['message_id']}` • "
                f"`{len(panel['roles'])}` cargo(s)"
            )
        await inter.response.send_message(view=card("Painéis de self-roles", "\n\n".join(lines)), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SelfRoles(bot))
