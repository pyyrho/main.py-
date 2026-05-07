"""
cogs/selfroles.py — Self-roles: cargos por botão persistente.
Comandos:
  /selfroles painel  — cria painel com botões de cargo
  /selfroles adicionar — adiciona cargo a um painel existente
  /selfroles remover   — remove cargo de um painel
  /selfroles lista     — lista painéis configurados
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import logging
from db.database import get_pool
from utils.constants import Colors, E, success_embed, error_embed, _now

log = logging.getLogger("multibot.selfroles")


def _parse_hex_color(raw: str | None, default: int = Colors.MAIN) -> int:
    if not raw:
        return default
    value = raw.strip().lstrip("#")
    if len(value) != 6:
        return default
    try:
        return int(value, 16)
    except ValueError:
        return default


def _role_manageable(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles and role < me.top_role and not role.managed)


# ── Tabela ────────────────────────────────────────────────────────────────────
async def _ensure_table():
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS selfroles_panels (
                message_id  BIGINT PRIMARY KEY,
                channel_id  BIGINT NOT NULL,
                guild_id    BIGINT NOT NULL,
                titulo      TEXT,
                descricao   TEXT,
                cor         INT DEFAULT 5899754,
                roles       JSONB DEFAULT '[]',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
        """)


async def _get_panel(message_id: int) -> dict | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM selfroles_panels WHERE message_id=$1", message_id
        )
    if not row:
        return None
    d = dict(row)
    d["roles"] = json.loads(d["roles"]) if isinstance(d["roles"], str) else d["roles"]
    return d


async def _save_panel(panel: dict):
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO selfroles_panels (message_id, channel_id, guild_id, titulo, descricao, cor, roles)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (message_id) DO UPDATE SET
                titulo=$4, descricao=$5, cor=$6, roles=$7
        """,
            panel["message_id"], panel["channel_id"], panel["guild_id"],
            panel["titulo"], panel["descricao"], panel["cor"],
            json.dumps(panel["roles"]),
        )


async def _list_panels(guild_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM selfroles_panels WHERE guild_id=$1 ORDER BY created_at DESC LIMIT 20",
            guild_id,
        )
    result = []
    for row in rows:
        d = dict(row)
        d["roles"] = json.loads(d["roles"]) if isinstance(d["roles"], str) else d["roles"]
        result.append(d)
    return result


# ── View de botões ────────────────────────────────────────────────────────────

class SelfRoleButton(discord.ui.Button):
    def __init__(self, role_id: int, label: str, emoji_str: str | None, style: discord.ButtonStyle):
        super().__init__(
            label=label,
            emoji=discord.PartialEmoji.from_str(emoji_str) if emoji_str else None,
            style=style,
            custom_id=f"selfrole:{role_id}",
        )
        self.role_id = role_id

    async def callback(self, inter: discord.Interaction):
        role = inter.guild.get_role(self.role_id)
        if not role:
            return await inter.response.send_message(
                embed=error_embed("Cargo não encontrado", "Este cargo não existe mais."), ephemeral=True
            )
        if not _role_manageable(inter.guild, role):
            return await inter.response.send_message(
                embed=error_embed("Sem permissão", "Não consigo gerenciar este cargo. Verifique a hierarquia do meu cargo."), ephemeral=True
            )
        if role in inter.user.roles:
            await inter.user.remove_roles(role, reason="Self-role removida")
            await inter.response.send_message(
                embed=discord.Embed(
                    description=f"{E.LEAF} Cargo **{role.name}** removido do seu perfil.",
                    color=0x99AAB5,
                ),
                ephemeral=True,
            )
        else:
            await inter.user.add_roles(role, reason="Self-role adicionada")
            await inter.response.send_message(
                embed=discord.Embed(
                    description=f"{E.VERIFY} Cargo **{role.name}** adicionado ao seu perfil!",
                    color=Colors.SUCCESS,
                ),
                ephemeral=True,
            )


def _build_view(roles: list[dict]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    styles = [
        discord.ButtonStyle.primary,
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.success,
        discord.ButtonStyle.danger,
    ]
    for i, r in enumerate(roles[:20]):  # max 20 botões (4 linhas × 5)
        view.add_item(SelfRoleButton(
            role_id=r["role_id"],
            label=r.get("label") or r["role_name"],
            emoji_str=r.get("emoji"),
            style=styles[i % len(styles)],
        ))
    return view


def _restore_view(panel: dict) -> discord.ui.View:
    return _build_view(panel["roles"])


# ── Cog ───────────────────────────────────────────────────────────────────────

class SelfRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await _ensure_table()
        # Restaura views de todos os painéis após reinício
        panels = []
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT * FROM selfroles_panels")
        for row in rows:
            d = dict(row)
            d["roles"] = json.loads(d["roles"]) if isinstance(d["roles"], str) else d["roles"]
            panels.append(d)
        for panel in panels:
            self.bot.add_view(_restore_view(panel), message_id=panel["message_id"])
        log.info(f"[SELFROLES] {len(panels)} painel(is) restaurado(s).")

    sr_group = app_commands.Group(
        name="selfroles",
        description="Sistema de cargos por botão",
        default_permissions=discord.Permissions(manage_roles=True),
    )

    @sr_group.command(name="painel", description="Cria um painel de self-roles com botões")
    @app_commands.describe(
        canal="Canal onde enviar o painel",
        titulo="Título do painel",
        descricao="Descrição do painel",
        cor="Cor hex (ex: #590CEA)",
    )
    async def sr_painel(self, inter: discord.Interaction,
                         canal: discord.TextChannel,
                         titulo: str = "🎭 Selecione seus cargos",
                         descricao: str = "Clique nos botões abaixo para adicionar ou remover cargos do seu perfil.",
                         cor: str = "#590CEA"):
        try:
            color = _parse_hex_color(cor)
        except Exception:
            color = Colors.MAIN

        emb = discord.Embed(title=titulo, description=descricao, color=color)
        emb.set_footer(text=f"{inter.guild.name} • Self-roles")
        emb.timestamp = _now()

        msg = await canal.send(embed=emb, view=discord.ui.View())  # view vazia até adicionar cargos

        panel = {
            "message_id": msg.id,
            "channel_id": canal.id,
            "guild_id":   inter.guild.id,
            "titulo":     titulo,
            "descricao":  descricao,
            "cor":        color,
            "roles":      [],
        }
        await _save_panel(panel)

        await inter.response.send_message(
            embed=success_embed("Painel criado!",
                f"{E.ARROW_BLUE} Painel enviado em {canal.mention}.\n"
                f"{E.SYMBOL} ID da mensagem: `{msg.id}`\n\n"
                f"Use `/selfroles adicionar id:{msg.id} cargo:@Cargo` para adicionar cargos."
            ),
            ephemeral=True,
        )

    @sr_group.command(name="adicionar", description="Adiciona um cargo a um painel existente")
    @app_commands.describe(
        message_id="ID da mensagem do painel",
        cargo="Cargo a adicionar",
        label="Nome do botão (padrão: nome do cargo)",
        emoji="Emoji do botão (opcional, ex: 🎮)",
    )
    async def sr_adicionar(self, inter: discord.Interaction,
                            message_id: str, cargo: discord.Role,
                            label: str = None, emoji: str = None):
        try:
            mid = int(message_id)
        except ValueError:
            return await inter.response.send_message(
                embed=error_embed("ID inválido", "Digite o ID numérico da mensagem."), ephemeral=True
            )

        panel = await _get_panel(mid)
        if not panel or panel["guild_id"] != inter.guild.id:
            return await inter.response.send_message(
                embed=error_embed("Painel não encontrado", f"Nenhum painel com ID `{mid}` neste servidor."),
                ephemeral=True,
            )

        if not _role_manageable(inter.guild, cargo):
            return await inter.response.send_message(
                embed=error_embed("Cargo inalcançável", "Meu cargo precisa ficar acima desse cargo e ter permissão de gerenciar cargos."), ephemeral=True
            )

        if len(panel["roles"]) >= 20:
            return await inter.response.send_message(
                embed=error_embed("Limite atingido", "Máximo de 20 cargos por painel."), ephemeral=True
            )

        if any(r["role_id"] == cargo.id for r in panel["roles"]):
            return await inter.response.send_message(
                embed=error_embed("Já adicionado", f"{cargo.mention} já está neste painel."), ephemeral=True
            )

        panel["roles"].append({
            "role_id":   cargo.id,
            "role_name": cargo.name,
            "label":     label or cargo.name,
            "emoji":     emoji.strip() if emoji else None,
        })
        await _save_panel(panel)

        # Atualiza a mensagem
        ch  = inter.guild.get_channel(panel["channel_id"])
        if isinstance(ch, discord.TextChannel):
            try:
                msg = await ch.fetch_message(mid)
                view = _build_view(panel["roles"])
                self.bot.add_view(view, message_id=mid)
                await msg.edit(view=view)
            except discord.HTTPException:
                pass

        await inter.response.send_message(
            embed=success_embed("Cargo adicionado!", f"{cargo.mention} adicionado ao painel."),
            ephemeral=True,
        )

    @sr_group.command(name="remover", description="Remove um cargo de um painel")
    @app_commands.describe(message_id="ID da mensagem do painel", cargo="Cargo a remover")
    async def sr_remover(self, inter: discord.Interaction, message_id: str, cargo: discord.Role):
        try:
            mid = int(message_id)
        except ValueError:
            return await inter.response.send_message(embed=error_embed("ID inválido", ""), ephemeral=True)

        panel = await _get_panel(mid)
        if not panel or panel["guild_id"] != inter.guild.id:
            return await inter.response.send_message(embed=error_embed("Não encontrado", ""), ephemeral=True)

        antes = len(panel["roles"])
        panel["roles"] = [r for r in panel["roles"] if r["role_id"] != cargo.id]
        if len(panel["roles"]) == antes:
            return await inter.response.send_message(
                embed=error_embed("Não encontrado", f"{cargo.mention} não está neste painel."), ephemeral=True
            )

        await _save_panel(panel)

        ch = inter.guild.get_channel(panel["channel_id"])
        if isinstance(ch, discord.TextChannel):
            try:
                msg = await ch.fetch_message(mid)
                view = _build_view(panel["roles"])
                await msg.edit(view=view)
            except discord.HTTPException:
                pass

        await inter.response.send_message(
            embed=success_embed("Removido!", f"{cargo.mention} removido do painel."), ephemeral=True
        )

    @sr_group.command(name="lista", description="Lista todos os painéis de self-roles do servidor")
    async def sr_lista(self, inter: discord.Interaction):
        panels = await _list_panels(inter.guild.id)
        if not panels:
            return await inter.response.send_message(
                embed=error_embed("Sem painéis", "Nenhum painel criado ainda."), ephemeral=True
            )
        emb = discord.Embed(title=f"{E.CHIBI_2} Painéis de Self-roles", color=Colors.MAIN)
        for p in panels:
            ch = inter.guild.get_channel(p["channel_id"])
            emb.add_field(
                name=p["titulo"] or "Sem título",
                value=(
                    f"{E.ARROW_BLUE} Canal: {ch.mention if ch else ('`' + str(p['channel_id']) + '`')}\n"
                    f"{E.SYMBOL} ID: `{p['message_id']}`\n"
                    f"{E.STAR} Cargos: `{len(p['roles'])}`"
                ),
                inline=False,
            )
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SelfRoles(bot))
