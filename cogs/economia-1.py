"""cogs/economia.py — Economia transacional com daily, loja e transferências.

Atualizado para discord.py 2.7+ e Components V2. As operações que movem
moedas ou estoque são atômicas para evitar saldo duplicado, compras em corrida
e transferências parcialmente concluídas.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from db.database import get_pool
from utils.constants import E
from utils.ui_components import WHITE, card, clip

log = logging.getLogger("multibot.economia")

MOEDA = "🪙"
DAILY_VALOR = 200
DAILY_HORAS = 24
MAX_TRANSFER = 1_000_000
MAX_ADMIN_AMOUNT = 10_000_000


async def _ensure_tables() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS economia (
                guild_id    BIGINT NOT NULL,
                user_id     BIGINT NOT NULL,
                saldo       BIGINT NOT NULL DEFAULT 0,
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
                estoque     INT NOT NULL DEFAULT -1,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS compras (
                id          SERIAL PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                user_id     BIGINT NOT NULL,
                item_id     INT NOT NULL,
                preco_pago  BIGINT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS economia_transacoes (
                id          BIGSERIAL PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                user_id     BIGINT NOT NULL,
                valor       BIGINT NOT NULL,
                tipo        TEXT NOT NULL,
                referencia  TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_economia_ranking
                ON economia (guild_id, saldo DESC);
            CREATE INDEX IF NOT EXISTS idx_economia_transacoes_user
                ON economia_transacoes (guild_id, user_id, created_at DESC);
            """
        )


async def _ensure_account(conn, guild_id: int, user_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO economia (guild_id, user_id, saldo)
        VALUES ($1, $2, 0)
        ON CONFLICT (guild_id, user_id) DO NOTHING
        """,
        guild_id,
        user_id,
    )


async def _get_saldo(guild_id: int, user_id: int) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT saldo FROM economia WHERE guild_id=$1 AND user_id=$2",
            guild_id,
            user_id,
        )
    return int(row["saldo"]) if row else 0


async def _change_balance(
    guild_id: int,
    user_id: int,
    amount: int,
    *,
    kind: str,
    reference: str = "",
    clamp_zero: bool = False,
) -> int:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await _ensure_account(conn, guild_id, user_id)
            row = await conn.fetchrow(
                "SELECT saldo FROM economia WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
                guild_id,
                user_id,
            )
            current = int(row["saldo"])
            new_balance = current + amount
            if clamp_zero:
                new_balance = max(0, new_balance)
            elif new_balance < 0:
                raise ValueError("Saldo insuficiente")
            await conn.execute(
                "UPDATE economia SET saldo=$3 WHERE guild_id=$1 AND user_id=$2",
                guild_id,
                user_id,
                new_balance,
            )
            actual_delta = new_balance - current
            if actual_delta:
                await conn.execute(
                    """
                    INSERT INTO economia_transacoes
                        (guild_id, user_id, valor, tipo, referencia)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    guild_id,
                    user_id,
                    actual_delta,
                    kind,
                    clip(reference, 300, fallback=""),
                )
            return new_balance


async def _get_ranking(guild_id: int, limit: int = 10) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, saldo FROM economia
            WHERE guild_id=$1 AND saldo > 0
            ORDER BY saldo DESC, user_id ASC LIMIT $2
            """,
            guild_id,
            limit,
        )
    return [dict(row) for row in rows]


async def _get_loja(guild_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM loja WHERE guild_id=$1 ORDER BY preco ASC, id ASC",
            guild_id,
        )
    return [dict(row) for row in rows]


class Economia(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await _ensure_tables()

    eco_group = app_commands.Group(name="eco", description="Sistema de economia do servidor")

    @eco_group.command(name="saldo", description="Veja seu saldo ou o saldo de outro membro")
    @app_commands.describe(membro="Membro a consultar")
    async def saldo(self, inter: discord.Interaction, membro: Optional[discord.Member] = None) -> None:
        member = membro or inter.user
        value = await _get_saldo(inter.guild_id, member.id)
        view = card(
            f"{MOEDA} Saldo de {member.display_name}",
            f"Você possui **{value:,} moedas** neste servidor.",
            thumbnail=member.display_avatar.url,
            footer=f"{inter.guild.name} • Economia",
            accent=WHITE,
        )
        await inter.response.send_message(view=view)

    @eco_group.command(name="daily", description=f"Colete {DAILY_VALOR} moedas a cada {DAILY_HORAS} horas")
    async def daily(self, inter: discord.Interaction) -> None:
        now = datetime.now(timezone.utc)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await _ensure_account(conn, inter.guild_id, inter.user.id)
                row = await conn.fetchrow(
                    """
                    SELECT saldo, daily_last FROM economia
                    WHERE guild_id=$1 AND user_id=$2 FOR UPDATE
                    """,
                    inter.guild_id,
                    inter.user.id,
                )
                last = row["daily_last"]
                if last:
                    next_time = last + timedelta(hours=DAILY_HORAS)
                    if now < next_time:
                        view = card(
                            "Daily já coletado",
                            f"Seu próximo daily estará disponível {discord.utils.format_dt(next_time, 'R')}.",
                            accent=WHITE,
                        )
                        await inter.response.send_message(view=view, ephemeral=True)
                        return
                new_balance = int(row["saldo"]) + DAILY_VALOR
                await conn.execute(
                    """
                    UPDATE economia SET saldo=$3, daily_last=$4
                    WHERE guild_id=$1 AND user_id=$2
                    """,
                    inter.guild_id,
                    inter.user.id,
                    new_balance,
                    now,
                )
                await conn.execute(
                    """
                    INSERT INTO economia_transacoes
                        (guild_id, user_id, valor, tipo, referencia)
                    VALUES ($1, $2, $3, 'daily', 'Recompensa diária')
                    """,
                    inter.guild_id,
                    inter.user.id,
                    DAILY_VALOR,
                )

        view = card(
            f"{E.BEAR} Daily coletado",
            f"Você recebeu **{DAILY_VALOR:,}** {MOEDA}.",
            fields=[
                ("Saldo atual", f"**{new_balance:,}** {MOEDA}"),
                ("Próximo daily", discord.utils.format_dt(now + timedelta(hours=DAILY_HORAS), "R")),
            ],
            thumbnail=inter.user.display_avatar.url,
            accent=WHITE,
        )
        await inter.response.send_message(view=view)

    @eco_group.command(name="transferir", description="Transfira moedas para outro membro")
    @app_commands.describe(membro="Destinatário", valor="Valor a transferir")
    async def transferir(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        valor: app_commands.Range[int, 1, MAX_TRANSFER],
    ) -> None:
        if membro.id == inter.user.id:
            await inter.response.send_message(
                view=card("Transferência inválida", "Você não pode transferir moedas para si mesmo."),
                ephemeral=True,
            )
            return
        if membro.bot:
            await inter.response.send_message(
                view=card("Transferência inválida", "Bots não participam da economia."),
                ephemeral=True,
            )
            return

        async with get_pool().acquire() as conn:
            async with conn.transaction():
                first, second = sorted((inter.user.id, membro.id))
                await _ensure_account(conn, inter.guild_id, first)
                await _ensure_account(conn, inter.guild_id, second)
                rows = await conn.fetch(
                    """
                    SELECT user_id, saldo FROM economia
                    WHERE guild_id=$1 AND user_id = ANY($2::bigint[])
                    ORDER BY user_id FOR UPDATE
                    """,
                    inter.guild_id,
                    [first, second],
                )
                balances = {int(row["user_id"]): int(row["saldo"]) for row in rows}
                sender_balance = balances.get(inter.user.id, 0)
                if sender_balance < valor:
                    await inter.response.send_message(
                        view=card(
                            "Saldo insuficiente",
                            f"Você possui **{sender_balance:,}** {MOEDA}, mas tentou transferir **{valor:,}**.",
                        ),
                        ephemeral=True,
                    )
                    return
                receiver_balance = balances.get(membro.id, 0) + valor
                await conn.execute(
                    "UPDATE economia SET saldo=saldo-$3 WHERE guild_id=$1 AND user_id=$2",
                    inter.guild_id,
                    inter.user.id,
                    valor,
                )
                await conn.execute(
                    "UPDATE economia SET saldo=saldo+$3 WHERE guild_id=$1 AND user_id=$2",
                    inter.guild_id,
                    membro.id,
                    valor,
                )
                await conn.executemany(
                    """
                    INSERT INTO economia_transacoes
                        (guild_id, user_id, valor, tipo, referencia)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    [
                        (inter.guild_id, inter.user.id, -valor, "transferencia_enviada", f"Para {membro.id}"),
                        (inter.guild_id, membro.id, valor, "transferencia_recebida", f"De {inter.user.id}"),
                    ],
                )

        view = card(
            f"{E.HEART_ANIM} Transferência concluída",
            f"{inter.user.mention} enviou **{valor:,}** {MOEDA} para {membro.mention}.",
            fields=[("Saldo do destinatário", f"**{receiver_balance:,}** {MOEDA}")],
            accent=WHITE,
        )
        await inter.response.send_message(view=view)

    @eco_group.command(name="ranking", description="Mostra os 10 membros mais ricos do servidor")
    async def ranking(self, inter: discord.Interaction) -> None:
        await inter.response.defer()
        rows = await _get_ranking(inter.guild_id, 10)
        if not rows:
            await inter.followup.send(view=card("Ranking vazio", "Ainda não há saldos registrados."))
            return
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lines: list[str] = []
        for index, row in enumerate(rows):
            member = inter.guild.get_member(row["user_id"])
            name = member.display_name if member else f"Usuário {row['user_id']}"
            lines.append(f"{medals[index]} **{clip(name, 60)}** • `{row['saldo']:,}` {MOEDA}")
        await inter.followup.send(
            view=card(
                f"{E.GIRL_1} Ranking de riqueza",
                "\n".join(lines),
                footer=inter.guild.name,
                accent=WHITE,
            )
        )

    @eco_group.command(name="loja", description="Veja os itens disponíveis na loja")
    async def loja_ver(self, inter: discord.Interaction) -> None:
        items = await _get_loja(inter.guild_id)
        if not items:
            await inter.response.send_message(view=card("Loja vazia", "Nenhum item foi cadastrado."), ephemeral=True)
            return
        lines: list[str] = []
        for item in items[:20]:
            role_text = ""
            if item["role_id"]:
                role = inter.guild.get_role(item["role_id"])
                role_text = f" • {role.mention if role else 'cargo removido'}"
            stock = "ilimitado" if item["estoque"] < 0 else str(item["estoque"])
            lines.append(
                f"**`#{item['id']}` {clip(item['nome'], 80)}** • `{item['preco']:,}` {MOEDA}\n"
                f"{clip(item['descricao'] or 'Sem descrição', 180)}{role_text} • estoque `{stock}`"
            )
        await inter.response.send_message(
            view=card(
                f"{E.MAGIC} Loja do servidor",
                "\n\n".join(lines),
                footer="Use /eco comprar com o ID do item.",
                accent=WHITE,
            )
        )

    @eco_group.command(name="comprar", description="Compre um item da loja")
    @app_commands.describe(item_id="ID do item exibido em /eco loja")
    async def comprar(self, inter: discord.Interaction, item_id: int) -> None:
        await inter.response.defer(ephemeral=True)
        item: dict | None = None
        purchase_id: int | None = None
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM loja WHERE id=$1 AND guild_id=$2 FOR UPDATE",
                    item_id,
                    inter.guild_id,
                )
                if not row:
                    await inter.followup.send(view=card("Item não encontrado", f"Não existe item `#{item_id}` nesta loja."), ephemeral=True)
                    return
                item = dict(row)
                if item["estoque"] == 0:
                    await inter.followup.send(view=card("Item esgotado", "Este item está sem estoque."), ephemeral=True)
                    return
                role = inter.guild.get_role(item["role_id"]) if item["role_id"] else None
                if item["role_id"] and role is None:
                    await inter.followup.send(view=card("Item indisponível", "O cargo associado foi removido."), ephemeral=True)
                    return
                if role and role in inter.user.roles:
                    await inter.followup.send(view=card("Você já possui este item", f"O cargo {role.mention} já está no seu perfil."), ephemeral=True)
                    return
                if role:
                    me = inter.guild.me
                    if not me or not me.guild_permissions.manage_roles or role >= me.top_role or role.managed:
                        await inter.followup.send(view=card("Cargo não gerenciável", "A equipe precisa corrigir a hierarquia deste item."), ephemeral=True)
                        return

                await _ensure_account(conn, inter.guild_id, inter.user.id)
                account = await conn.fetchrow(
                    "SELECT saldo FROM economia WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
                    inter.guild_id,
                    inter.user.id,
                )
                balance = int(account["saldo"])
                if balance < item["preco"]:
                    await inter.followup.send(
                        view=card(
                            "Saldo insuficiente",
                            f"O item custa **{item['preco']:,}** {MOEDA}; você possui **{balance:,}**.",
                        ),
                        ephemeral=True,
                    )
                    return
                new_balance = balance - int(item["preco"])
                await conn.execute(
                    "UPDATE economia SET saldo=$3 WHERE guild_id=$1 AND user_id=$2",
                    inter.guild_id,
                    inter.user.id,
                    new_balance,
                )
                if item["estoque"] > 0:
                    await conn.execute("UPDATE loja SET estoque=estoque-1 WHERE id=$1", item_id)
                purchase = await conn.fetchrow(
                    """
                    INSERT INTO compras (guild_id, user_id, item_id, preco_pago)
                    VALUES ($1, $2, $3, $4) RETURNING id
                    """,
                    inter.guild_id,
                    inter.user.id,
                    item_id,
                    item["preco"],
                )
                purchase_id = int(purchase["id"])
                await conn.execute(
                    """
                    INSERT INTO economia_transacoes
                        (guild_id, user_id, valor, tipo, referencia)
                    VALUES ($1, $2, $3, 'compra', $4)
                    """,
                    inter.guild_id,
                    inter.user.id,
                    -int(item["preco"]),
                    f"Item #{item_id}: {item['nome']}",
                )

        role = inter.guild.get_role(item["role_id"]) if item and item["role_id"] else None
        if role:
            try:
                await inter.user.add_roles(role, reason=f"Compra na loja: {item['nome']}")
            except discord.HTTPException:
                log.exception("Falha ao entregar cargo da compra %s; iniciando estorno", purchase_id)
                async with get_pool().acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE economia SET saldo=saldo+$3 WHERE guild_id=$1 AND user_id=$2",
                            inter.guild_id,
                            inter.user.id,
                            item["preco"],
                        )
                        if item["estoque"] > 0:
                            await conn.execute("UPDATE loja SET estoque=estoque+1 WHERE id=$1", item_id)
                        if purchase_id:
                            await conn.execute("DELETE FROM compras WHERE id=$1", purchase_id)
                        await conn.execute(
                            """
                            INSERT INTO economia_transacoes
                                (guild_id, user_id, valor, tipo, referencia)
                            VALUES ($1, $2, $3, 'estorno', $4)
                            """,
                            inter.guild_id,
                            inter.user.id,
                            item["preco"],
                            f"Falha ao entregar item #{item_id}",
                        )
                await inter.followup.send(
                    view=card("Compra estornada", "Não consegui entregar o cargo. Suas moedas e o estoque foram restaurados."),
                    ephemeral=True,
                )
                return

        await inter.followup.send(
            view=card(
                f"{MOEDA} Compra realizada",
                f"Você comprou **{clip(item['nome'], 100)}**.",
                fields=[
                    ("Preço pago", f"**{item['preco']:,}** {MOEDA}"),
                    ("Saldo restante", f"**{new_balance:,}** {MOEDA}"),
                ],
                accent=WHITE,
            ),
            ephemeral=True,
        )

    @eco_group.command(name="historico", description="Mostra suas últimas transações")
    @app_commands.describe(membro="Membro a consultar; administradores podem consultar terceiros")
    async def historico(self, inter: discord.Interaction, membro: Optional[discord.Member] = None) -> None:
        target = membro or inter.user
        if target.id != inter.user.id and not inter.user.guild_permissions.administrator:
            await inter.response.send_message(view=card("Sem permissão", "Apenas administradores podem consultar terceiros."), ephemeral=True)
            return
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT valor, tipo, referencia, created_at
                FROM economia_transacoes
                WHERE guild_id=$1 AND user_id=$2
                ORDER BY created_at DESC LIMIT 12
                """,
                inter.guild_id,
                target.id,
            )
        if not rows:
            await inter.response.send_message(view=card("Sem transações", "Nenhuma movimentação registrada."), ephemeral=True)
            return
        lines = []
        for row in rows:
            sign = "+" if row["valor"] > 0 else ""
            lines.append(
                f"`{sign}{row['valor']:,}` {MOEDA} • **{clip(row['tipo'], 40)}** • "
                f"{discord.utils.format_dt(row['created_at'], 'R')}\n-# {clip(row['referencia'], 150)}"
            )
        await inter.response.send_message(
            view=card(f"Histórico de {target.display_name}", "\n\n".join(lines), thumbnail=target.display_avatar.url),
            ephemeral=True,
        )

    @eco_group.command(name="dar", description="[Admin] Adiciona moedas a um membro")
    @app_commands.default_permissions(administrator=True)
    async def eco_dar(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        valor: app_commands.Range[int, 1, MAX_ADMIN_AMOUNT],
    ) -> None:
        new_balance = await _change_balance(
            inter.guild_id,
            membro.id,
            valor,
            kind="ajuste_admin",
            reference=f"Adicionado por {inter.user.id}",
        )
        await inter.response.send_message(
            view=card("Moedas adicionadas", f"{membro.mention} recebeu **{valor:,}** {MOEDA}.\nSaldo: **{new_balance:,}**."),
            ephemeral=True,
        )

    @eco_group.command(name="remover-moedas", description="[Admin] Remove moedas de um membro")
    @app_commands.default_permissions(administrator=True)
    async def eco_remover(
        self,
        inter: discord.Interaction,
        membro: discord.Member,
        valor: app_commands.Range[int, 1, MAX_ADMIN_AMOUNT],
    ) -> None:
        new_balance = await _change_balance(
            inter.guild_id,
            membro.id,
            -valor,
            kind="ajuste_admin",
            reference=f"Removido por {inter.user.id}",
            clamp_zero=True,
        )
        await inter.response.send_message(
            view=card("Moedas removidas", f"O saldo de {membro.mention} agora é **{new_balance:,}** {MOEDA}."),
            ephemeral=True,
        )

    @eco_group.command(name="loja-adicionar", description="[Admin] Adiciona um item à loja")
    @app_commands.default_permissions(administrator=True)
    async def loja_add(
        self,
        inter: discord.Interaction,
        nome: app_commands.Range[str, 1, 100],
        preco: app_commands.Range[int, 1, 10_000_000],
        descricao: Optional[app_commands.Range[str, 1, 500]] = None,
        cargo: Optional[discord.Role] = None,
        estoque: app_commands.Range[int, -1, 1_000_000] = -1,
    ) -> None:
        if cargo:
            me = inter.guild.me
            if not me or not me.guild_permissions.manage_roles or cargo >= me.top_role or cargo.managed:
                await inter.response.send_message(view=card("Cargo inválido", "Não consigo gerenciar esse cargo pela hierarquia atual."), ephemeral=True)
                return
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO loja (guild_id, nome, descricao, preco, role_id, estoque)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
                """,
                inter.guild_id,
                nome.strip(),
                descricao.strip() if descricao else None,
                preco,
                cargo.id if cargo else None,
                estoque,
            )
        await inter.response.send_message(
            view=card(
                "Item adicionado",
                f"**`#{row['id']}` {clip(nome, 100)}** foi cadastrado por **{preco:,}** {MOEDA}.",
                fields=[
                    ("Cargo", cargo.mention if cargo else "Nenhum"),
                    ("Estoque", "Ilimitado" if estoque < 0 else estoque),
                ],
            ),
            ephemeral=True,
        )

    @eco_group.command(name="loja-remover", description="[Admin] Remove um item da loja")
    @app_commands.default_permissions(administrator=True)
    async def loja_rem(self, inter: discord.Interaction, item_id: int) -> None:
        async with get_pool().acquire() as conn:
            result = await conn.execute(
                "DELETE FROM loja WHERE id=$1 AND guild_id=$2",
                item_id,
                inter.guild_id,
            )
        if result == "DELETE 0":
            await inter.response.send_message(view=card("Item não encontrado", f"O item `#{item_id}` não existe."), ephemeral=True)
            return
        await inter.response.send_message(view=card("Item removido", f"O item `#{item_id}` foi removido."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economia(bot))
