"""
db/database.py
Camada de acesso ao PostgreSQL via asyncpg.
Todas as operações são assíncronas e thread-safe.

v2 — correções:
- get_xp_rank_position: query anterior passava guild_id como 6 argumentos
  duplicados, causando erro de parâmetro no asyncpg. Reescrita de forma limpa.
- Cache leve de guild_config: evita consulta ao banco a cada mensagem
  (xp.py chama get_guild_config em todo on_message). TTL de 5 minutos.
"""

import asyncpg
import asyncio
import logging
import os
import json
import time
from typing import Any

log = logging.getLogger("logos.db")

_pool: asyncpg.Pool | None = None

# ── Cache de guild_config ──────────────────────────────────────────────────────
# Chave: guild_id → (dados, timestamp)
_config_cache: dict[int, tuple[dict, float]] = {}
_CONFIG_TTL = 300  # 5 minutos


def _invalidate_config(guild_id: int):
    """Remove guild_id do cache (chamar após upsert_guild_config)."""
    _config_cache.pop(guild_id, None)


async def init_pool() -> asyncpg.Pool:
    """Inicializa o pool de conexões e cria as tabelas se não existirem."""
    global _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("Variável DATABASE_URL não definida.")

    _pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
        statement_cache_size=0,   # necessário para algumas configs do Railway
    )
    log.info("[DB] Pool criado com sucesso.")
    await _create_tables()
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool não inicializado. Chame init_pool() primeiro.")
    return _pool


async def _create_tables():
    """Cria todas as tabelas necessárias."""
    async with get_pool().acquire() as conn:
        await conn.execute("""
            -- Configurações gerais por servidor
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id        BIGINT PRIMARY KEY,
                -- Geral
                log_channel     BIGINT,
                -- Tickets
                ticket_category BIGINT,
                ticket_log      BIGINT,
                ticket_banner   TEXT,
                staff_roles     BIGINT[],
                -- Boas-vindas
                welcome_canal   BIGINT,
                welcome_msg     TEXT,
                welcome_banner  TEXT,
                welcome_dm      BOOLEAN DEFAULT FALSE,
                welcome_cor     INT DEFAULT 5899754,
                welcome_titulo  TEXT,
                welcome_rodape  TEXT,
                -- XP
                xp_canal        BIGINT,
                xp_max_level    INT DEFAULT 100,
                xp_ativo        BOOLEAN DEFAULT TRUE,
                xp_embed_cor    INT DEFAULT 5899754,
                xp_embed_banner TEXT,
                xp_embed_titulo TEXT,
                xp_embed_rodape TEXT,
                xp_cargo_nivel  JSONB DEFAULT '{}',
                -- Timestamps
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );

            -- XP por membro por servidor
            CREATE TABLE IF NOT EXISTS xp_data (
                guild_id    BIGINT,
                user_id     BIGINT,
                xp          INT DEFAULT 0,
                level       INT DEFAULT 0,
                updated_at  TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (guild_id, user_id)
            );

            -- Avisos de moderação
            CREATE TABLE IF NOT EXISTS warns (
                id          SERIAL PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                user_id     BIGINT NOT NULL,
                motivo      TEXT NOT NULL,
                mod_id      BIGINT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS warns_guild_user ON warns(guild_id, user_id);

            -- Tickets abertos
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id  BIGINT PRIMARY KEY,
                guild_id    BIGINT NOT NULL,
                user_id     BIGINT NOT NULL,
                categoria   TEXT,
                atendente   BIGINT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS tickets_guild_user ON tickets(guild_id, user_id);
        """)
        log.info("[DB] Tabelas verificadas/criadas.")


# ═══════════════════════════════════════════════════
# GUILD CONFIG
# ═══════════════════════════════════════════════════

async def get_guild_config(guild_id: int) -> dict:
    """Retorna a config do servidor, usando cache em memória (TTL 5 min)."""
    cached = _config_cache.get(guild_id)
    if cached and (time.monotonic() - cached[1]) < _CONFIG_TTL:
        return cached[0]

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
        )

    if row:
        d = dict(row)
        raw = d.get("xp_cargo_nivel")
        if raw is None:
            d["xp_cargo_nivel"] = {}
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                d["xp_cargo_nivel"] = {int(k): v for k, v in parsed.items()}
            except Exception:
                d["xp_cargo_nivel"] = {}
        elif isinstance(raw, dict):
            d["xp_cargo_nivel"] = {int(k): v for k, v in raw.items()}
        else:
            d["xp_cargo_nivel"] = {}
        result = d
    else:
        result = _guild_defaults(guild_id)

    _config_cache[guild_id] = (result, time.monotonic())
    return result


def _guild_defaults(guild_id: int) -> dict:
    return {
        "guild_id": guild_id,
        "log_channel": None, "ticket_category": None,
        "ticket_log": None, "ticket_banner": None, "staff_roles": [],
        "welcome_canal": None, "welcome_msg": None, "welcome_banner": None,
        "welcome_dm": False, "welcome_cor": 5899754,
        "welcome_titulo": None, "welcome_rodape": None,
        "xp_canal": None, "xp_max_level": 100, "xp_ativo": True,
        "xp_embed_cor": 5899754, "xp_embed_banner": None,
        "xp_embed_titulo": None, "xp_embed_rodape": None,
        "xp_cargo_nivel": {},
    }


async def upsert_guild_config(guild_id: int, **fields):
    """Atualiza campos específicos da config do servidor e invalida o cache."""
    if not fields:
        return

    # Serializa xp_cargo_nivel para JSONB se presente
    if "xp_cargo_nivel" in fields:
        fields["xp_cargo_nivel"] = json.dumps(
            {str(k): v for k, v in fields["xp_cargo_nivel"].items()}
        )

    cols = list(fields.keys())
    vals = list(fields.values())

    set_clause = ", ".join(f"{c} = ${i+2}" for i, c in enumerate(cols))
    set_clause += ", updated_at = NOW()"

    query = f"""
        INSERT INTO guild_config (guild_id, {', '.join(cols)})
        VALUES ($1, {', '.join(f'${i+2}' for i in range(len(cols)))})
        ON CONFLICT (guild_id) DO UPDATE SET {set_clause}
    """
    async with get_pool().acquire() as conn:
        await conn.execute(query, guild_id, *vals)

    # Invalida cache para forçar releitura do banco
    _invalidate_config(guild_id)


# ═══════════════════════════════════════════════════
# XP DATA
# ═══════════════════════════════════════════════════

async def get_xp(guild_id: int, user_id: int) -> dict:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xp, level FROM xp_data WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
    return {"xp": row["xp"], "level": row["level"]} if row else {"xp": 0, "level": 0}


async def upsert_xp(guild_id: int, user_id: int, xp: int, level: int):
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO xp_data (guild_id, user_id, xp, level, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET xp=$3, level=$4, updated_at=NOW()
        """, guild_id, user_id, xp, level)


async def get_xp_ranking(guild_id: int, limit: int = 10) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, xp, level
            FROM xp_data
            WHERE guild_id = $1
            ORDER BY level DESC, xp DESC
            LIMIT $2
        """, guild_id, limit)
    return [dict(r) for r in rows]


async def get_xp_rank_position(guild_id: int, user_id: int) -> int:
    """Retorna a posição do usuário no ranking (1-indexed).

    Correção v2: a query anterior duplicava o parâmetro guild_id 6 vezes
    em subqueries aninhadas, causando erro de binding no asyncpg.
    A nova versão usa uma CTE mais limpa com apenas 2 parâmetros.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("""
            WITH meu AS (
                SELECT level, xp
                FROM xp_data
                WHERE guild_id = $1 AND user_id = $2
            )
            SELECT COUNT(*) + 1 AS pos
            FROM xp_data, meu
            WHERE xp_data.guild_id = $1
              AND (
                    xp_data.level > meu.level
                    OR (xp_data.level = meu.level AND xp_data.xp > meu.xp)
              )
        """, guild_id, user_id)
    return int(row["pos"]) if row else 1


# ═══════════════════════════════════════════════════
# WARNS
# ═══════════════════════════════════════════════════

async def add_warn(guild_id: int, user_id: int, motivo: str, mod_id: int) -> int:
    """Adiciona um warn e retorna o total de warns do membro."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO warns (guild_id, user_id, motivo, mod_id) VALUES ($1,$2,$3,$4)",
            guild_id, user_id, motivo, mod_id,
        )
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM warns WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
    return row["total"]


async def get_warns(guild_id: int, user_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT motivo, mod_id, created_at FROM warns WHERE guild_id=$1 AND user_id=$2 ORDER BY created_at",
            guild_id, user_id,
        )
    return [dict(r) for r in rows]


async def clear_warns(guild_id: int, user_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM warns WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )


# ═══════════════════════════════════════════════════
# TICKETS
# ═══════════════════════════════════════════════════

async def open_ticket(guild_id: int, user_id: int, channel_id: int, categoria: str):
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO tickets (channel_id, guild_id, user_id, categoria)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (channel_id) DO NOTHING
        """, channel_id, guild_id, user_id, categoria)


async def get_ticket_by_user(guild_id: int, user_id: int) -> dict | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tickets WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
    return dict(row) if row else None


async def get_ticket_by_channel(channel_id: int) -> dict | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tickets WHERE channel_id=$1", channel_id
        )
    return dict(row) if row else None


async def set_ticket_atendente(channel_id: int, user_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE tickets SET atendente=$1 WHERE channel_id=$2",
            user_id, channel_id,
        )


async def close_ticket(channel_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM tickets WHERE channel_id=$1", channel_id
        )


async def list_open_tickets(guild_id: int) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tickets WHERE guild_id=$1 ORDER BY created_at",
            guild_id,
        )
    return [dict(r) for r in rows]
