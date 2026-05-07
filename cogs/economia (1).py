"""
cogs/economia.py — Sistema de economia com moedas, daily, loja e transferências.
Comandos públicos: /eco saldo, /eco daily, /eco transferir, /eco ranking
Comandos admin:    /eco dar, /eco remover, /eco loja-adicionar, /eco loja-remover, /eco loja-ver
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import logging
from datetime import datetime, timezone, timedelta
from db.database import get_pool
from utils.constants import Colors, E, success_embed, error_embed, _now

log = logging.getLogger("multibot.economia")

MOEDA = "🪙"
DAILY_VALOR  = 200
DAILY_HORAS  = 24


async def _ensure_tables():
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS economia (
                guild_id    BIGINT,
                user_id     BIGINT,
                saldo       BIGINT DEFAULT 0,
                daily_last  TIMESTAMPTZ,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS loja (
                id          SERIAL PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                nome        TEXT NOT NULL,
                descricao   TEXT,
                preco       BIGINT NOT NULL,
                role_id     BIGINT,
                estoque     INT DEFAULT -1,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS compras (
                id          SERIAL PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                user_id     BIGINT NOT NULL,
                item_id     INT NOT NULL,
                preco_pago  BIGINT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
        """)


async def _get_saldo(guild_id: int, user_id: int) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT saldo FROM economia WHERE guild_id=$1 AND user_id=$2", guild_id, user_id
        )
    return row["saldo"] if row else 0


async def _set_saldo(guild_id: int, user_id: int, saldo: int):
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO economia (guild_id, user_id, saldo)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET saldo=$3
        """, guild_id, user_id, max(0, saldo))


async def _add_saldo(guild_id: int, user_id: int, valor: int) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO economia (guild_id, user_id, saldo)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET saldo = economia.saldo + $3
            RETURNING saldo
        """, guild_id, user_id, valor)
    return row["saldo"]


async def _get_daily_last(guild_id: int, user_id: int) -> datetime | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT daily_last FROM economia WHERE guild_id=$1 AND user_id=$2", guild_id, user_id
        )
    return row["daily_last"] if row else None


async def _set_daily_last(guild_id: int, user_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO economia (guild_id, user_id, daily_last)
            VALUES ($1,$2,NOW())
            ON CONFLICT (guild_id, user_id) DO UPDATE SET daily_last=NOW()
        """, guild_id, user_id)


async def _get_ranking(guild_id: int, limit: int = 10) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, saldo FROM economia
            WHERE guild_id=$1 ORDER BY saldo DESC LIMIT $2
        """, guild_id, limit)
    return [dict(r) for r in rows]


async def _get_loja(guild_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM loja WHERE guild_id=$1 ORDER BY preco ASC", guild_id
        )
    return [dict(r) for r in rows]


class Economia(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await _ensure_tables()

    eco_group = app_commands.Group(name="eco", description="Sistema de economia do servidor")

    # ── Públicos ──────────────────────────────────────────────────────────

    @eco_group.command(name="saldo", description="Veja seu saldo de moedas (ou de outro membro)")
    @app_commands.describe(membro="Membro a consultar")
    async def saldo(self, inter: discord.Interaction, membro: discord.Member = None):
        m     = membro or inter.user
        valor = await _get_saldo(inter.guild.id, m.id)
        emb   = discord.Embed(
            title=f"{MOEDA} Saldo de {m.display_name}",
            description=f"**{valor:,}** moedas",
            color=Colors.MAIN,
        )
        emb.set_thumbnail(url=m.display_avatar.url)
        emb.set_footer(text=f"{inter.guild.name} • Economia")
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @eco_group.command(name="daily", description=f"Colete suas moedas diárias ({DAILY_VALOR} moedas / {DAILY_HORAS}h)")
    async def daily(self, inter: discord.Interaction):
        last = await _get_daily_last(inter.guild.id, inter.user.id)
        agora = datetime.now(tz=timezone.utc)

        if last:
            proximo = last + timedelta(hours=DAILY_HORAS)
            if agora < proximo:
                restante = proximo - agora
                horas    = int(restante.total_seconds() // 3600)
                minutos  = int((restante.total_seconds() % 3600) // 60)
                return await inter.response.send_message(
                    embed=error_embed("Daily já coletado!",
                        f"{E.LOADING} Próximo daily disponível em **{horas}h {minutos}m**.\n"
                        f"{E.ARROW_BLUE} Volte em {discord.utils.format_dt(proximo, 'R')}."
                    ),
                    ephemeral=True,
                )

        novo_saldo = await _add_saldo(inter.guild.id, inter.user.id, DAILY_VALOR)
        await _set_daily_last(inter.guild.id, inter.user.id)

        emb = discord.Embed(
            title=f"{E.BEAR} Daily coletado! {MOEDA}",
            description=(
                f"{E.SPARKLE} Você coletou **{DAILY_VALOR:,}** moedas!\n\n"
                f"{E.STAR} Saldo atual: **{novo_saldo:,}** moedas\n"
                f"{E.LOADING} Próximo daily: {discord.utils.format_dt(agora + timedelta(hours=DAILY_HORAS), 'R')}"
            ),
            color=Colors.SUCCESS,
        )
        emb.set_thumbnail(url=inter.user.display_avatar.url)
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @eco_group.command(name="transferir", description="Transfira moedas para outro membro")
    @app_commands.describe(membro="Destinatário", valor="Valor a transferir")
    async def transferir(self, inter: discord.Interaction,
                          membro: discord.Member,
                          valor: app_commands.Range[int, 1, 1000000]):
        if membro.id == inter.user.id:
            return await inter.response.send_message(
                embed=error_embed("Erro", "Você não pode transferir para si mesmo."), ephemeral=True
            )
        if membro.bot:
            return await inter.response.send_message(
                embed=error_embed("Erro", "Não é possível transferir para bots."), ephemeral=True
            )

        saldo_origem = await _get_saldo(inter.guild.id, inter.user.id)
        if saldo_origem < valor:
            return await inter.response.send_message(
                embed=error_embed("Saldo insuficiente",
                    f"Você tem **{saldo_origem:,}** moedas e tentou transferir **{valor:,}**."
                ),
                ephemeral=True,
            )

        await _add_saldo(inter.guild.id, inter.user.id, -valor)
        novo_destino = await _add_saldo(inter.guild.id, membro.id, valor)

        emb = discord.Embed(
            title=f"{E.HEART_ANIM} Transferência realizada! {MOEDA}",
            description=(
                f"{E.ARROW_BLUE} {inter.user.mention} → {membro.mention}\n"
                f"{E.STAR} Valor: **{valor:,}** moedas\n"
                f"{E.SYMBOL} Saldo de {membro.display_name}: **{novo_destino:,}** moedas"
            ),
            color=Colors.SUCCESS,
        )
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @eco_group.command(name="ranking", description="Top 10 membros mais ricos do servidor")
    async def ranking(self, inter: discord.Interaction):
        await inter.response.defer()
        rows = await _get_ranking(inter.guild.id, 10)
        if not rows:
            return await inter.followup.send(
                embed=error_embed("Sem dados", "Nenhum membro tem moedas ainda.")
            )
        medalhas = [E.N1, E.N2, E.N3, E.N4, E.N5, E.N6, "7️⃣", "8️⃣", "9️⃣", "🔟"]
        linhas = []
        for i, row in enumerate(rows):
            m = inter.guild.get_member(row["user_id"])
            nome = m.display_name if m else f"ID {row['user_id']}"
            linhas.append(f"{medalhas[i]} **{nome}** — `{row['saldo']:,}` {MOEDA}")

        emb = discord.Embed(
            title=f"{E.GIRL_1} Top 10 — {inter.guild.name} {MOEDA}",
            description="\n".join(linhas),
            color=Colors.MAIN,
        )
        emb.timestamp = _now()
        await inter.followup.send(embed=emb)

    @eco_group.command(name="loja", description="Veja os itens disponíveis na loja do servidor")
    async def loja_ver(self, inter: discord.Interaction):
        itens = await _get_loja(inter.guild.id)
        if not itens:
            return await inter.response.send_message(
                embed=error_embed("Loja vazia", "Nenhum item disponível na loja ainda."), ephemeral=True
            )
        emb = discord.Embed(title=f"{E.MAGIC} Loja do Servidor {MOEDA}", color=Colors.MAIN)
        for item in itens[:15]:
            role_txt = ""
            if item["role_id"]:
                r = inter.guild.get_role(item["role_id"])
                role_txt = f"\n{E.ARROW_BLUE} Cargo: {r.mention if r else '`removido`'}"
            estoque = f"\n{E.SYMBOL} Estoque: `{item['estoque']}`" if item["estoque"] >= 0 else ""
            emb.add_field(
                name=f"`#{item['id']}` {item['nome']} — `{item['preco']:,}` {MOEDA}",
                value=(item["descricao"] or "Sem descrição") + role_txt + estoque,
                inline=False,
            )
        emb.set_footer(text="Use /eco comprar <id> para comprar um item.")
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @eco_group.command(name="comprar", description="Compre um item da loja")
    @app_commands.describe(item_id="ID do item (veja com /eco loja)")
    async def comprar(self, inter: discord.Interaction, item_id: int):
        async with get_pool().acquire() as conn:
            item = await conn.fetchrow(
                "SELECT * FROM loja WHERE id=$1 AND guild_id=$2", item_id, inter.guild.id
            )
        if not item:
            return await inter.response.send_message(
                embed=error_embed("Item não encontrado", f"Nenhum item com ID `{item_id}` nesta loja."),
                ephemeral=True,
            )
        item = dict(item)
        if item["estoque"] == 0:
            return await inter.response.send_message(
                embed=error_embed("Sem estoque", "Este item está esgotado."), ephemeral=True
            )

        saldo = await _get_saldo(inter.guild.id, inter.user.id)
        if saldo < item["preco"]:
            return await inter.response.send_message(
                embed=error_embed("Saldo insuficiente",
                    f"Você precisa de **{item['preco']:,}** {MOEDA} mas tem **{saldo:,}**."),
                ephemeral=True,
            )

        await _add_saldo(inter.guild.id, inter.user.id, -item["preco"])
        if item["estoque"] > 0:
            async with get_pool().acquire() as conn:
                await conn.execute("UPDATE loja SET estoque=estoque-1 WHERE id=$1", item_id)

        async with get_pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO compras (guild_id, user_id, item_id, preco_pago) VALUES ($1,$2,$3,$4)",
                inter.guild.id, inter.user.id, item_id, item["preco"]
            )

        # Dá cargo se configurado
        if item["role_id"]:
            role = inter.guild.get_role(item["role_id"])
            if role:
                try:
                    await inter.user.add_roles(role, reason=f"Compra na loja: {item['nome']}")
                except discord.HTTPException:
                    pass

        novo_saldo = await _get_saldo(inter.guild.id, inter.user.id)
        emb = discord.Embed(
            title=f"{MOEDA} Compra realizada!",
            description=(
                f"{E.VERIFY} Você comprou **{item['nome']}**!\n"
                f"{E.STAR} Preço pago: **{item['preco']:,}** {MOEDA}\n"
                f"{E.SYMBOL} Saldo restante: **{novo_saldo:,}** {MOEDA}"
            ),
            color=Colors.SUCCESS,
        )
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    # ── Admin ─────────────────────────────────────────────────────────────

    @eco_group.command(name="dar", description="[Admin] Dá moedas a um membro")
    @app_commands.describe(membro="Membro", valor="Valor")
    @app_commands.default_permissions(administrator=True)
    async def eco_dar(self, inter: discord.Interaction, membro: discord.Member,
                      valor: app_commands.Range[int, 1, 1000000]):
        novo = await _add_saldo(inter.guild.id, membro.id, valor)
        await inter.response.send_message(
            embed=success_embed("Moedas adicionadas!",
                f"{MOEDA} {membro.mention} recebeu **{valor:,}** moedas.\n"
                f"{E.STAR} Saldo: **{novo:,}**"
            ),
            ephemeral=True,
        )

    @eco_group.command(name="remover-moedas", description="[Admin] Remove moedas de um membro")
    @app_commands.describe(membro="Membro", valor="Valor")
    @app_commands.default_permissions(administrator=True)
    async def eco_remover(self, inter: discord.Interaction, membro: discord.Member,
                           valor: app_commands.Range[int, 1, 1000000]):
        saldo = await _get_saldo(inter.guild.id, membro.id)
        novo  = max(0, saldo - valor)
        await _set_saldo(inter.guild.id, membro.id, novo)
        await inter.response.send_message(
            embed=success_embed("Moedas removidas!",
                f"{MOEDA} **{valor:,}** moedas removidas de {membro.mention}.\n"
                f"{E.STAR} Saldo: **{novo:,}**"
            ),
            ephemeral=True,
        )

    @eco_group.command(name="loja-adicionar", description="[Admin] Adiciona item à loja")
    @app_commands.describe(
        nome="Nome do item", preco="Preço em moedas",
        descricao="Descrição do item",
        cargo="Cargo dado ao comprar (opcional)",
        estoque="Estoque (-1 = ilimitado)",
    )
    @app_commands.default_permissions(administrator=True)
    async def loja_add(self, inter: discord.Interaction,
                        nome: str, preco: app_commands.Range[int, 1, 10000000],
                        descricao: str = None,
                        cargo: discord.Role = None,
                        estoque: int = -1):
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO loja (guild_id, nome, descricao, preco, role_id, estoque)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
            """, inter.guild.id, nome, descricao, preco,
                cargo.id if cargo else None, estoque)
        await inter.response.send_message(
            embed=success_embed("Item adicionado!",
                f"{MOEDA} **{nome}** (ID: `{row['id']}`)\n"
                f"{E.STAR} Preço: **{preco:,}** moedas\n"
                + (f"{E.ARROW_BLUE} Cargo: {cargo.mention}\n" if cargo else "")
                + (f"{E.SYMBOL} Estoque: `{estoque}`" if estoque >= 0 else f"{E.SYMBOL} Estoque: ilimitado")
            ),
            ephemeral=True,
        )

    @eco_group.command(name="loja-remover", description="[Admin] Remove item da loja")
    @app_commands.describe(item_id="ID do item")
    @app_commands.default_permissions(administrator=True)
    async def loja_rem(self, inter: discord.Interaction, item_id: int):
        async with get_pool().acquire() as conn:
            result = await conn.execute(
                "DELETE FROM loja WHERE id=$1 AND guild_id=$2", item_id, inter.guild.id
            )
        if result == "DELETE 0":
            return await inter.response.send_message(
                embed=error_embed("Não encontrado", f"Item `{item_id}` não existe."), ephemeral=True
            )
        await inter.response.send_message(
            embed=success_embed("Removido!", f"Item `{item_id}` removido da loja."), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Economia(bot))
