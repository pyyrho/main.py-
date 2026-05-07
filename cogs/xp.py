"""cogs/xp.py — Sistema de XP por mensagem com persistência PostgreSQL."""

import discord
from discord import app_commands
from discord.ext import commands
import random
import time
import logging
from db import database as db
from utils.constants import Colors, E, success_embed, error_embed, _now

log = logging.getLogger("multibot.xp")

XP_MIN = 15
XP_MAX = 40
XP_COOLDOWN_SECONDS = 60
XP_COOLDOWN_GC_AFTER = 10_000

# Cooldown em memória (não precisa persistir — reseta ao reiniciar, sem problema)
_xp_cooldown: dict[tuple[int, int], float] = {}


def _xp_para_nivel(level: int) -> int:
    level = max(0, int(level))
    return 1000 + (level * 500)


def _level_bar(xp_atual: int, xp_necessario: int, tamanho: int = 10) -> str:
    tamanho = max(4, min(int(tamanho), 30))
    progresso = min(max(int((xp_atual / max(xp_necessario, 1)) * tamanho), 0), tamanho)
    return "█" * progresso + "░" * (tamanho - progresso)


class XP(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Listener de XP ────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not message.content or len(message.content.strip()) < 2:
            return

        cfg = await db.get_guild_config(message.guild.id)
        if not cfg.get("xp_ativo", True):
            return

        key  = (message.guild.id, message.author.id)
        agora = time.time()
        if agora - _xp_cooldown.get(key, 0) < XP_COOLDOWN_SECONDS:
            return
        _xp_cooldown[key] = agora
        if len(_xp_cooldown) > XP_COOLDOWN_GC_AFTER:
            limite = agora - (XP_COOLDOWN_SECONDS * 2)
            for old_key, last_seen in list(_xp_cooldown.items()):
                if last_seen < limite:
                    _xp_cooldown.pop(old_key, None)

        dados = await db.get_xp(message.guild.id, message.author.id)
        dados["xp"] += random.randint(XP_MIN, XP_MAX)
        max_level = cfg.get("xp_max_level", 100)

        subiu = False
        while dados["xp"] >= _xp_para_nivel(dados["level"]) and dados["level"] < max_level:
            dados["xp"]    -= _xp_para_nivel(dados["level"])
            dados["level"] += 1
            subiu = True

            # Cargo automático
            cargo_map: dict = cfg.get("xp_cargo_nivel", {})
            role_id = cargo_map.get(dados["level"])
            if role_id:
                role = message.guild.get_role(int(role_id))
                if role:
                    try:
                        if message.guild.me and role < message.guild.me.top_role:
                            await message.author.add_roles(role, reason=f"Nível {dados['level']}")
                    except discord.HTTPException:
                        pass

            # Anúncio de nível
            canal_id = cfg.get("xp_canal")
            canal    = message.guild.get_channel(canal_id) if canal_id else message.channel
            if isinstance(canal, discord.TextChannel):
                titulo  = cfg.get("xp_embed_titulo") or f"{E.TROPHY} Nível Alcançado!"
                rodape  = cfg.get("xp_embed_rodape") or f"Próximo nível: {_xp_para_nivel(dados['level']):,} XP"
                cor     = cfg.get("xp_embed_cor", Colors.MAIN)
                emb = discord.Embed(
                    title=titulo,
                    description=(
                        f"{E.CROWN_PINK} {message.author.mention} subiu para o **Nível {dados['level']}**!\n\n"
                        f"{E.STAR} Continue conversando! {E.SPARKLE}"
                    ),
                    color=cor,
                )
                emb.set_thumbnail(url=message.author.display_avatar.url)
                emb.set_footer(text=rodape)
                if cfg.get("xp_embed_banner"):
                    emb.set_image(url=cfg["xp_embed_banner"])
                emb.timestamp = _now()
                try:
                    await canal.send(embed=emb)
                except discord.HTTPException:
                    pass

        await db.upsert_xp(message.guild.id, message.author.id, dados["xp"], dados["level"])

    # ── Grupo /xp ─────────────────────────────────────────────────────────
    xp_group = app_commands.Group(
        name="xp", description="Sistema de XP e níveis",
        default_permissions=None,   # visível para todos (subcomandos têm perms próprias)
    )

    @xp_group.command(name="rank", description="Veja seu nível e XP (ou de outro membro)")
    @app_commands.describe(membro="Membro a consultar (padrão: você)")
    async def rank(self, inter: discord.Interaction, membro: discord.Member = None):
        membro = membro or inter.user
        dados  = await db.get_xp(inter.guild.id, membro.id)
        cfg    = await db.get_guild_config(inter.guild.id)
        level  = dados["level"]
        xp     = dados["xp"]
        xp_nec = _xp_para_nivel(level)
        max_lv = cfg.get("xp_max_level", 100)
        barra  = _level_bar(xp, xp_nec)
        posicao = await db.get_xp_rank_position(inter.guild.id, membro.id)

        emb = discord.Embed(title=f"{E.TROPHY} Rank de {membro.display_name}", color=Colors.MAIN)
        emb.set_thumbnail(url=membro.display_avatar.url)
        emb.add_field(name=f"{E.STAR} Nível",   value=f"`{level}` / `{max_lv}`",    inline=True)
        emb.add_field(name=f"{E.GEM} XP",        value=f"`{xp:,}` / `{xp_nec:,}`", inline=True)
        emb.add_field(name=f"{E.N1} Posição",    value=f"`#{posicao}`",              inline=True)
        emb.add_field(name=f"{E.ORB_GREEN} Progresso",
                      value=f"`{barra}` `{int(xp/max(xp_nec,1)*100)}%`", inline=False)
        if level >= max_lv:
            emb.add_field(name=f"{E.CROWN_PINK} Status", value="Nível máximo!", inline=False)
        emb.set_footer(text=f"{inter.guild.name} • XP por mensagem: 15–40 (cooldown 60s)")
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @xp_group.command(name="top", description="Ranking dos top 10 membros com mais XP")
    async def top(self, inter: discord.Interaction):
        await inter.response.defer()
        ranking = await db.get_xp_ranking(inter.guild.id, 10)
        if not ranking:
            return await inter.followup.send(
                embed=error_embed("Sem dados", "Nenhum membro tem XP registrado ainda.")
            )
        medalhas = [E.N1, E.N2, E.N3, E.N4, E.N5, E.N6, "7️⃣", "8️⃣", "9️⃣", "🔟"]
        linhas = []
        for i, row in enumerate(ranking):
            membro = inter.guild.get_member(row["user_id"])
            nome   = membro.display_name if membro else f"(ID {row['user_id']})"
            medal  = medalhas[i] if i < len(medalhas) else f"`{i+1}.`"
            linhas.append(f"{medal} **{nome}** — Nível `{row['level']}` · `{row['xp']:,}` XP")

        emb = discord.Embed(
            title=f"{E.TROPHY} Top 10 — {inter.guild.name}",
            description="\n".join(linhas),
            color=Colors.MAIN,
        )
        emb.set_footer(text="Ranking do banco de dados")
        emb.timestamp = _now()
        await inter.followup.send(embed=emb)

    @xp_group.command(name="config", description="Configura o sistema de XP do servidor")
    @app_commands.describe(
        canal_nivel="Canal de anúncio de nível (opcional)",
        nivel_maximo="Nível máximo (padrão 100, máx 1000)",
        ativo="Ativar/desativar XP neste servidor",
        cor_hex="Cor do embed de nível (ex: #590CEA)",
        banner_url="Banner do embed de nível",
    )
    @app_commands.default_permissions(administrator=True)
    async def xp_config(
        self, inter: discord.Interaction,
        canal_nivel: discord.TextChannel = None,
        nivel_maximo: app_commands.Range[int, 1, 1000] = 100,
        ativo: bool = True,
        cor_hex: str = None,
        banner_url: str = None,
    ):
        fields: dict = {
            "xp_canal":    canal_nivel.id if canal_nivel else None,
            "xp_max_level": nivel_maximo,
            "xp_ativo":    ativo,
        }
        if banner_url:
            fields["xp_embed_banner"] = banner_url
        if cor_hex:
            try:
                fields["xp_embed_cor"] = int(cor_hex.lstrip("#"), 16)
            except ValueError:
                return await inter.response.send_message(
                    embed=error_embed("Cor inválida", "Use `#RRGGBB`."), ephemeral=True
                )
        await db.upsert_guild_config(inter.guild.id, **fields)
        await inter.response.send_message(
            embed=success_embed("XP configurado!", (
                f"{E.TROPHY} Canal de nível: {canal_nivel.mention if canal_nivel else 'Canal da mensagem'}\n"
                f"{E.STAR} Nível máximo: `{nivel_maximo}`\n"
                f"{E.ORB_GREEN} Ativo: {'Sim' if ativo else 'Não'}"
            )),
            ephemeral=True,
        )

    @xp_group.command(name="dar", description="Dá XP manualmente a um membro")
    @app_commands.describe(membro="Membro", quantidade="XP a dar")
    @app_commands.default_permissions(administrator=True)
    async def xp_dar(self, inter: discord.Interaction,
                     membro: discord.Member,
                     quantidade: app_commands.Range[int, 1, 100000]):
        dados = await db.get_xp(inter.guild.id, membro.id)
        cfg   = await db.get_guild_config(inter.guild.id)
        dados["xp"] += quantidade
        while dados["xp"] >= _xp_para_nivel(dados["level"]) and dados["level"] < cfg.get("xp_max_level", 100):
            dados["xp"]    -= _xp_para_nivel(dados["level"])
            dados["level"] += 1
        await db.upsert_xp(inter.guild.id, membro.id, dados["xp"], dados["level"])
        await inter.response.send_message(
            embed=success_embed("XP adicionado!",
                f"{E.STAR} {membro.mention} recebeu `{quantidade:,}` XP.\n"
                f"{E.TROPHY} Nível: **{dados['level']}** | XP: `{dados['xp']:,}`"
            ),
            ephemeral=True,
        )

    @xp_group.command(name="remover", description="Remove XP de um membro")
    @app_commands.describe(membro="Membro", quantidade="XP a remover")
    @app_commands.default_permissions(administrator=True)
    async def xp_remover(self, inter: discord.Interaction,
                         membro: discord.Member,
                         quantidade: app_commands.Range[int, 1, 100000]):
        dados = await db.get_xp(inter.guild.id, membro.id)
        dados["xp"] = max(0, dados["xp"] - quantidade)
        await db.upsert_xp(inter.guild.id, membro.id, dados["xp"], dados["level"])
        await inter.response.send_message(
            embed=success_embed("XP removido!",
                f"{E.WARN_IC} `{quantidade:,}` XP removidos de {membro.mention}.\n"
                f"{E.TROPHY} Nível: **{dados['level']}** | XP: `{dados['xp']:,}`"
            ),
            ephemeral=True,
        )

    @xp_group.command(name="reset", description="Zera todo o XP de um membro")
    @app_commands.describe(membro="Membro")
    @app_commands.default_permissions(administrator=True)
    async def xp_reset(self, inter: discord.Interaction, membro: discord.Member):
        await db.upsert_xp(inter.guild.id, membro.id, 0, 0)
        await inter.response.send_message(
            embed=success_embed("XP zerado", f"{E.LEAF} XP de {membro.mention} foi zerado."),
            ephemeral=True,
        )

    @xp_group.command(name="cargo", description="Define um cargo automático para um nível")
    @app_commands.describe(nivel="Nível", cargo="Cargo a atribuir")
    @app_commands.default_permissions(administrator=True)
    async def xp_cargo(self, inter: discord.Interaction,
                        nivel: app_commands.Range[int, 1, 1000],
                        cargo: discord.Role):
        cfg = await db.get_guild_config(inter.guild.id)
        cargo_map: dict = cfg.get("xp_cargo_nivel", {})
        cargo_map[nivel] = cargo.id
        await db.upsert_guild_config(inter.guild.id, xp_cargo_nivel=cargo_map)
        await inter.response.send_message(
            embed=success_embed("Cargo configurado!",
                f"{E.CROWN_PINK} Ao atingir o nível **{nivel}**, o membro receberá {cargo.mention}."
            ),
            ephemeral=True,
        )

    @xp_group.command(name="cargo-remover", description="Remove o cargo automático de um nível")
    @app_commands.describe(nivel="Nível")
    @app_commands.default_permissions(administrator=True)
    async def xp_cargo_remover(self, inter: discord.Interaction,
                                nivel: app_commands.Range[int, 1, 1000]):
        cfg = await db.get_guild_config(inter.guild.id)
        cargo_map: dict = cfg.get("xp_cargo_nivel", {})
        if nivel not in cargo_map:
            return await inter.response.send_message(
                embed=error_embed("Não encontrado", f"Sem cargo configurado para o nível {nivel}."),
                ephemeral=True,
            )
        del cargo_map[nivel]
        await db.upsert_guild_config(inter.guild.id, xp_cargo_nivel=cargo_map)
        await inter.response.send_message(
            embed=success_embed("Removido", f"Cargo do nível {nivel} removido."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(XP(bot))
