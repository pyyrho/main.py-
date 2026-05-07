"""cogs/utilidade.py — Comandos públicos, embeds, interações e ajuda."""

import discord
from discord import app_commands
from discord.ext import commands
import random
import itertools
import aiohttp
import logging
from utils.constants import Colors, E, success_embed, error_embed, _now

log = logging.getLogger("multibot.util")


def _parse_hex_color(raw: str | None, default: int = Colors.MAIN) -> int:
    if not raw:
        return default
    value = raw.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("Use uma cor no formato #RRGGBB.")
    return int(value, 16)


# ── Interações anime ──────────────────────────────────────────────────────────

_ACOES: dict[str, dict] = {
    "kiss":     {"frases": ["{a} beijou {b}!", "{a} deu um beijinho em {b}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} retribuiu o beijo de {a}!",
                 "emoji": E.HEARTS_S, "emoji2": E.HEART},
    "hug":      {"frases": ["{a} abraçou {b}!", "{b} ganhou um abraço de {a}!"],
                 "frase_solo": "{a} precisa de um abraço!", "retribuir": True,
                 "frase_ret": "{b} retribuiu o abraço!", "emoji": E.RING, "emoji2": E.HEARTS_S},
    "pat":      {"frases": ["{a} fez carinho em {b}!", "{b} ganhou um pat de {a}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} retribuiu o pat!", "emoji": E.SPARKLE, "emoji2": E.CROWN_PINK},
    "slap":     {"frases": ["{a} deu um tapa em {b}!", "{b} levou um tapa de {a}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} devolveu o tapa!", "emoji": E.WARN_IC, "emoji2": E.FLAME_ORG},
    "poke":     {"frases": ["{a} cutucou {b}!", "{b} foi cutucado por {a}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} cutucou {a} de volta!", "emoji": E.ARROW, "emoji2": E.SPARKLE},
    "bite":     {"frases": ["{a} mordeu {b}!", "{b} foi mordido por {a}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} mordeu {a} de volta!", "emoji": E.FLAME_PUR, "emoji2": E.HEART},
    "cry":      {"frases": ["{a} consolou {b}!"],
                 "frase_solo": "{a} está chorando...", "retribuir": False, "emoji": E.HEARTS_S, "emoji2": E.RING},
    "blush":    {"frases": ["{b} fez {a} corar!"],
                 "frase_solo": "{a} ficou vermelhinho(a)!", "retribuir": False, "emoji": E.HEART, "emoji2": E.SPARKLE},
    "dance":    {"frases": ["{a} chamou {b} para dançar!"],
                 "frase_solo": "{a} está dançando!", "retribuir": True,
                 "frase_ret": "{b} aceitou dançar!", "emoji": E.SPARKLE, "emoji2": E.GEM_SHINE},
    "highfive": {"frases": ["{a} deu um toca aqui em {b}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "TOCA AQUI! {b} tocou com {a}!", "emoji": E.ORB_GREEN, "emoji2": E.VERIFY},
    "wave":     {"frases": ["{a} acenou para {b}!"],
                 "frase_solo": "{a} acenou para todo mundo!", "retribuir": False, "emoji": E.ARROW_W, "emoji2": E.HEARTS_S},
    "cuddle":   {"frases": ["{a} se aconchegou com {b}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} se aconchegou com {a}!", "emoji": E.HEART, "emoji2": E.RING},
    "lick":     {"frases": ["{a} lambeu {b}!", "{b} foi lambido(a) por {a}!"],
                 "frase_solo": None, "retribuir": False, "emoji": E.FLAME_PUR, "emoji2": E.HEARTS_S},
    "yeet":     {"frases": ["{a} yeetou {b} pro espaço! YEET!"],
                 "frase_solo": None, "retribuir": False, "emoji": E.FIRE, "emoji2": E.FLAME_ORG},
    "nuzzle":   {"frases": ["{a} nuzzlou {b}!"],
                 "frase_solo": None, "retribuir": True,
                 "frase_ret": "{b} retribuiu o nuzzle!", "emoji": E.CROWN_PINK, "emoji2": E.HEART},
}

_NEKOS_MAP = {k: k for k in _ACOES}
_NEKOS_MAP["highfive"] = "highfive"


async def _get_gif(action: str) -> str:
    cat = _NEKOS_MAP.get(action, "hug")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://nekos.best/api/v2/{cat}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    return data["results"][0]["url"]
    except Exception:
        pass
    return f"https://nekos.best/api/v2/{cat}/0001.gif"


class RetribuirView(discord.ui.View):
    def __init__(self, action: str, autor: discord.Member, alvo: discord.Member):
        super().__init__(timeout=120)
        self.action = action
        self.autor  = autor
        self.alvo   = alvo

    @discord.ui.button(label="Retribuir", style=discord.ButtonStyle.primary,
                       emoji=discord.PartialEmoji.from_str("<a:1503hearts:1430339028720549908>"))
    async def retribuir(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.alvo.id:
            return await inter.response.send_message(
                f"{E.ARROW_RED} Apenas {self.alvo.mention} pode retribuir!", ephemeral=True
            )
        dados = _ACOES[self.action]
        gif   = await _get_gif(self.action)
        texto = dados.get("frase_ret", f"{self.alvo.mention} retribuiu!") \
            .format(a=self.alvo.mention, b=self.autor.mention)
        emb = discord.Embed(description=f"{dados['emoji']} {texto} {dados['emoji2']}", color=Colors.MAIN)
        emb.set_image(url=gif)
        emb.set_footer(text=f"Pedido por {self.alvo.display_name}", icon_url=self.alvo.display_avatar.url)
        emb.timestamp = _now()
        btn.disabled = True
        btn.label    = "Retribuído!"
        await inter.response.edit_message(view=self)
        await inter.followup.send(content=self.autor.mention, embed=emb)


async def _interacao(action: str, autor: discord.Member,
                      alvo: discord.Member | None = None) -> tuple[discord.Embed, discord.ui.View | None]:
    dados = _ACOES[action]
    gif   = await _get_gif(action)
    if alvo and alvo.id != autor.id:
        texto = random.choice(dados["frases"]).format(a=autor.mention, b=alvo.mention)
    else:
        texto = (dados.get("frase_solo") or dados["frases"][0]).format(a=autor.mention, b="")
    emb = discord.Embed(description=f"{dados['emoji']} {texto} {dados['emoji2']}", color=Colors.MAIN)
    emb.set_image(url=gif)
    emb.set_footer(text=f"Pedido por {autor.display_name}", icon_url=autor.display_avatar.url)
    emb.timestamp = _now()
    view = RetribuirView(action, autor, alvo) if (alvo and alvo.id != autor.id and dados.get("retribuir")) else None
    return emb, view


# ── Embeds ─────────────────────────────────────────────────────────────────────

class EmbedModal(discord.ui.Modal, title="Criar Embed"):
    titulo    = discord.ui.TextInput(label="Título", max_length=256)
    descricao = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=4000)
    cor       = discord.ui.TextInput(label="Cor hex (ex: #590CEA)", required=False, max_length=7)
    rodape    = discord.ui.TextInput(label="Rodapé", required=False, max_length=256)
    imagem    = discord.ui.TextInput(label="URL da imagem", required=False, max_length=500)

    def __init__(self, canal: discord.TextChannel):
        super().__init__()
        self.canal = canal

    async def on_submit(self, inter: discord.Interaction):
        try:
            color = _parse_hex_color(self.cor.value, Colors.MAIN)
        except ValueError:
            return await inter.response.send_message(
                embed=error_embed("Cor inválida", "Use `#RRGGBB`."), ephemeral=True
            )
        emb = discord.Embed(title=self.titulo.value, description=self.descricao.value, color=color)
        if self.rodape.value.strip():
            emb.set_footer(text=self.rodape.value.strip())
        if self.imagem.value.strip():
            emb.set_image(url=self.imagem.value.strip())
        emb.timestamp = _now()
        try:
            await self.canal.send(embed=emb)
            await inter.response.send_message(
                embed=success_embed("Enviada!", f"Embed publicada em {self.canal.mention}."), ephemeral=True
            )
        except discord.Forbidden:
            await inter.response.send_message(
                embed=error_embed("Sem permissão", f"Não posso enviar em {self.canal.mention}."), ephemeral=True
            )


class EmbedEditModal(discord.ui.Modal, title="Editar Embed"):
    novo_titulo = discord.ui.TextInput(label="Novo título (vazio = manter)", required=False, max_length=256)
    nova_desc   = discord.ui.TextInput(label="Nova descrição (vazio = manter)",
                                       style=discord.TextStyle.paragraph, required=False, max_length=4000)
    nova_cor    = discord.ui.TextInput(label="Nova cor hex", required=False, max_length=7)
    novo_rodape = discord.ui.TextInput(label="Novo rodapé", required=False, max_length=256)
    nova_imagem = discord.ui.TextInput(label="Nova URL de imagem", required=False, max_length=500)

    def __init__(self, message: discord.Message):
        super().__init__()
        self.target = message
        old = message.embeds[0] if message.embeds else None
        if old:
            if old.title:       self.novo_titulo.default = old.title
            if old.description: self.nova_desc.default   = old.description[:4000]
            if old.color:       self.nova_cor.default     = f"#{old.color.value:06X}"
            if old.footer:      self.novo_rodape.default  = old.footer.text or ""
            if old.image:       self.nova_imagem.default  = old.image.url or ""

    async def on_submit(self, inter: discord.Interaction):
        old = self.target.embeds[0] if self.target.embeds else discord.Embed()
        try:
            color = int((self.nova_cor.value or f"{old.color.value:06X}").lstrip("#"), 16)
        except Exception:
            color = Colors.MAIN
        new = discord.Embed(
            title=self.novo_titulo.value.strip() or old.title,
            description=self.nova_desc.value.strip() or old.description,
            color=color,
        )
        rodape = self.novo_rodape.value.strip()
        if rodape:
            new.set_footer(text=rodape)
        elif old.footer:
            new.set_footer(text=old.footer.text)
        imagem = self.nova_imagem.value.strip()
        if imagem:
            new.set_image(url=imagem)
        elif old.image:
            new.set_image(url=old.image.url)
        new.timestamp = _now()
        try:
            await self.target.edit(embed=new)
            await inter.response.send_message(
                embed=success_embed("Editada!", "Alterações aplicadas."), ephemeral=True
            )
        except discord.Forbidden:
            await inter.response.send_message(
                embed=error_embed("Sem permissão", "Não posso editar esta mensagem."), ephemeral=True
            )


# ── Ajuda ─────────────────────────────────────────────────────────────────────

_PAGES = [
    {
        "titulo": f"{E.MASCOT} Sobre o bot",
        "desc": (
            f"Bot multifuncional para servidores Discord.\n\n"
            f"{E.SPARKLE} **Sistemas:** tickets · moderação · XP · boas-vindas · música · interações\n\n"
            f"{E.ARROW_BLUE} Navegue pelas categorias abaixo."
        ),
    },
    {
        "titulo": "🎫 Tickets",
        "desc": f"`/ticket setup` · `/ticket painel` · `/ticket lista`",
    },
    {
        "titulo": "🛡️ Moderação",
        "desc": (
            "`/mod ban` · `/mod unban` · `/mod kick`\n"
            "`/mod mute` · `/mod unmute` · `/mod limpar`\n"
            "`/mod warn` · `/mod warns` · `/mod clearwarns` · `/mod userinfo`"
        ),
    },
    {
        "titulo": "⭐ XP & Níveis",
        "desc": (
            "`/xp rank` · `/xp top`\n"
            "`/xp config` · `/xp dar` · `/xp remover` · `/xp reset`\n"
            "`/xp cargo` · `/xp cargo-remover`"
        ),
    },
    {
        "titulo": "⚙️ Configurações",
        "desc": (
            "`/config log` · `/config boas-vindas`\n"
            "`/config boas-vindas-ver` · `/config boas-vindas-testar`\n"
            "`/config automod`"
        ),
    },
    {
        "titulo": f"{E.SPOTIFY} Música",
        "desc": (
            "`/musica tocar` · `/musica pausar` · `/musica retomar`\n"
            "`/musica pular` · `/musica parar` · `/musica sair`\n"
            "`/musica volume` · `/musica repetir` · `/musica embaralhar`\n"
            "`/musica fila` · `/musica tocando`"
        ),
    },
    {
        "titulo": "🖼️ Embeds",
        "desc": "`/embed criar` · `/embed editar` · `/embed rapido`",
    },
    {
        "titulo": "🎭 Interações",
        "desc": (
            "`/kiss` · `/hug` · `/pat` · `/slap` · `/poke` · `/bite`\n"
            "`/cry` · `/blush` · `/dance` · `/highfive` · `/wave`\n"
            "`/cuddle` · `/lick` · `/yeet` · `/nuzzle`"
        ),
    },
]
_LABELS = ["Início", "Tickets", "Moderação", "XP", "Config", "Música", "Embeds", "Interações"]


def _help_embed(page: int, guild: discord.Guild | None = None) -> discord.Embed:
    data  = _PAGES[page]
    total = len(_PAGES)
    emb   = discord.Embed(title=data["titulo"], description=data["desc"], color=Colors.MAIN)
    emb.set_footer(text=f"Página {page+1}/{total}" + (f" • {guild.name}" if guild else ""))
    emb.timestamp = _now()
    return emb


class AjudaView(discord.ui.View):
    def __init__(self, page: int = 0, autor_id: int = 0):
        super().__init__(timeout=120)
        self.page     = page
        self.autor_id = autor_id
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        btn_prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        btn_prev.callback = self._prev
        self.add_item(btn_prev)

        sel = discord.ui.Select(
            placeholder="Ir para categoria...",
            options=[discord.SelectOption(label=_LABELS[i], value=str(i), default=(i == self.page))
                     for i in range(len(_PAGES))]
        )
        sel.callback = self._select
        self.add_item(sel)

        btn_next = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=self.page == len(_PAGES)-1)
        btn_next.callback = self._next
        self.add_item(btn_next)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if self.autor_id and inter.user.id != self.autor_id:
            await inter.response.send_message(f"{E.WARN_IC} Apenas quem usou `/ajuda` pode navegar.", ephemeral=True)
            return False
        return True

    async def _prev(self, inter: discord.Interaction):
        self.page -= 1
        self._rebuild()
        await inter.response.edit_message(embed=_help_embed(self.page, inter.guild), view=self)

    async def _next(self, inter: discord.Interaction):
        self.page += 1
        self._rebuild()
        await inter.response.edit_message(embed=_help_embed(self.page, inter.guild), view=self)

    async def _select(self, inter: discord.Interaction):
        self.page = int(inter.data["values"][0])
        self._rebuild()
        await inter.response.edit_message(embed=_help_embed(self.page, inter.guild), view=self)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Utilidade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Comandos públicos ──────────────────────────────────────────────────

    @app_commands.command(name="ping", description="Latência do bot")
    async def ping(self, inter: discord.Interaction):
        lat = round(self.bot.latency * 1000)
        emb = discord.Embed(title=f"{E.DISCORD} Pong!", description=f"{E.ARROW_BLUE} `{lat}ms`", color=Colors.MAIN)
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @app_commands.command(name="serverinfo", description="Informações do servidor")
    async def serverinfo(self, inter: discord.Interaction):
        g = inter.guild
        emb = discord.Embed(title=f"{E.DISCORD} {g.name}", color=Colors.MAIN)
        if g.icon:
            emb.set_thumbnail(url=g.icon.url)
        emb.add_field(name="Dono",     value=f"<@{g.owner_id}>",       inline=True)
        emb.add_field(name="Membros",  value=f"`{g.member_count}`",     inline=True)
        emb.add_field(name="Canais",   value=f"`{len(g.channels)}`",    inline=True)
        emb.add_field(name="Cargos",   value=f"`{len(g.roles)}`",       inline=True)
        emb.add_field(name="Boosts",   value=f"`{g.premium_subscription_count}` (Nível {g.premium_tier})", inline=True)
        emb.add_field(name="Criado",   value=discord.utils.format_dt(g.created_at, "D"), inline=True)
        emb.set_footer(text=f"ID: {g.id}")
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @app_commands.command(name="avatar", description="Exibe o avatar de um membro")
    @app_commands.describe(membro="Membro (padrão: você)")
    async def avatar(self, inter: discord.Interaction, membro: discord.Member = None):
        m = membro or inter.user
        emb = discord.Embed(title=f"{E.STAR} Avatar de {m.display_name}", color=Colors.MAIN)
        emb.set_image(url=m.display_avatar.with_size(1024).url)
        emb.add_field(name="Links", value=(
            f"[PNG]({m.display_avatar.with_format('png').url}) · "
            f"[JPG]({m.display_avatar.with_format('jpg').url}) · "
            f"[WEBP]({m.display_avatar.with_format('webp').url})"
        ))
        emb.timestamp = _now()
        await inter.response.send_message(embed=emb)

    @app_commands.command(name="ajuda", description="Lista todos os comandos do bot")
    async def ajuda(self, inter: discord.Interaction):
        emb = _help_embed(0, inter.guild)
        emb.description = (emb.description or "") + f"\n\n{E.BOT_ANIME}"
        if self.bot.user:
            emb.set_thumbnail(url=self.bot.user.display_avatar.url)
        await inter.response.send_message(embed=emb, view=AjudaView(0, inter.user.id))

    # ── Embeds ─────────────────────────────────────────────────────────────

    embed_group = app_commands.Group(
        name="embed",
        description="Criar e editar embeds",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @embed_group.command(name="criar", description="Cria e envia uma embed personalizada")
    @app_commands.describe(canal="Canal de destino")
    async def embed_criar(self, inter: discord.Interaction, canal: discord.TextChannel):
        await inter.response.send_modal(EmbedModal(canal))

    @embed_group.command(name="rapido", description="Envia uma embed simples rapidamente")
    @app_commands.describe(canal="Canal", titulo="Título", descricao="Conteúdo", cor="Cor hex")
    async def embed_rapido(self, inter: discord.Interaction,
                            canal: discord.TextChannel,
                            titulo: str, descricao: str, cor: str = "#590CEA"):
        try:
            color = int(cor.lstrip("#"), 16)
        except ValueError:
            return await inter.response.send_message(embed=error_embed("Cor inválida", "Use `#RRGGBB`."), ephemeral=True)
        emb = discord.Embed(title=titulo, description=descricao, color=color)
        emb.set_footer(text=f"por {inter.user.display_name}")
        emb.timestamp = _now()
        try:
            await canal.send(embed=emb)
            await inter.response.send_message(embed=success_embed("Enviada!", f"Publicada em {canal.mention}."), ephemeral=True)
        except discord.Forbidden:
            await inter.response.send_message(embed=error_embed("Sem permissão", f"Não posso enviar em {canal.mention}."), ephemeral=True)

    @embed_group.command(name="editar", description="Edita uma embed existente pelo ID da mensagem")
    @app_commands.describe(canal="Canal", message_id="ID da mensagem")
    async def embed_editar(self, inter: discord.Interaction, canal: discord.TextChannel, message_id: str):
        try:
            msg = await canal.fetch_message(int(message_id))
        except (ValueError, discord.NotFound):
            return await inter.response.send_message(embed=error_embed("Não encontrado", "Mensagem não encontrada."), ephemeral=True)
        if msg.author.id != self.bot.user.id:
            return await inter.response.send_message(embed=error_embed("Erro", "Só posso editar embeds minhas."), ephemeral=True)
        if not msg.embeds:
            return await inter.response.send_message(embed=error_embed("Sem embed", "Essa mensagem não tem embed."), ephemeral=True)
        await inter.response.send_modal(EmbedEditModal(msg))

    # ── Interações anime ───────────────────────────────────────────────────

    async def _cmd(self, inter: discord.Interaction, action: str,
                    membro: discord.Member | None, solo_ok: bool = False):
        if not membro and not solo_ok:
            return await inter.response.send_message(embed=error_embed("Erro", "Mencione um membro."), ephemeral=True)
        if membro and membro.id == inter.user.id and not solo_ok:
            return await inter.response.send_message(embed=error_embed("Ei!", "Você não pode fazer isso consigo mesmo!"), ephemeral=True)
        await inter.response.defer()
        emb, view = await _interacao(action, inter.user, membro)
        content = membro.mention if membro and membro.id != inter.user.id else None
        kwargs: dict = {"embed": emb}
        if content is not None:
            kwargs["content"] = content
        if view is not None:
            kwargs["view"] = view
        await inter.followup.send(**kwargs)

    @app_commands.command(name="kiss",     description="Beije alguém")
    @app_commands.describe(membro="Quem você quer beijar")
    async def kiss(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "kiss", membro)

    @app_commands.command(name="hug",      description="Abrace alguém")
    @app_commands.describe(membro="Quem você quer abraçar")
    async def hug(self, inter: discord.Interaction, membro: discord.Member = None):
        await self._cmd(inter, "hug", membro, solo_ok=True)

    @app_commands.command(name="pat",      description="Faça carinho em alguém")
    @app_commands.describe(membro="Quem vai receber o pat")
    async def pat(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "pat", membro)

    @app_commands.command(name="slap",     description="Dê um tapa em alguém")
    @app_commands.describe(membro="Quem vai levar o tapa")
    async def slap(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "slap", membro)

    @app_commands.command(name="poke",     description="Cutuque alguém")
    @app_commands.describe(membro="Quem vai ser cutucado")
    async def poke(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "poke", membro)

    @app_commands.command(name="bite",     description="Morda alguém")
    @app_commands.describe(membro="Quem vai ser mordido")
    async def bite(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "bite", membro)

    @app_commands.command(name="cry",      description="Chore ou console alguém")
    @app_commands.describe(membro="Quem consolar (opcional)")
    async def cry(self, inter: discord.Interaction, membro: discord.Member = None):
        await self._cmd(inter, "cry", membro, solo_ok=True)

    @app_commands.command(name="blush",    description="Fique vermelho ou elogie alguém")
    @app_commands.describe(membro="Quem te fez corar (opcional)")
    async def blush(self, inter: discord.Interaction, membro: discord.Member = None):
        await self._cmd(inter, "blush", membro, solo_ok=True)

    @app_commands.command(name="dance",    description="Dance ou convide alguém")
    @app_commands.describe(membro="Com quem dançar (opcional)")
    async def dance(self, inter: discord.Interaction, membro: discord.Member = None):
        await self._cmd(inter, "dance", membro, solo_ok=True)

    @app_commands.command(name="highfive", description="Dê um toca aqui!")
    @app_commands.describe(membro="Com quem")
    async def highfive(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "highfive", membro)

    @app_commands.command(name="wave",     description="Acene para alguém")
    @app_commands.describe(membro="Para quem (opcional)")
    async def wave(self, inter: discord.Interaction, membro: discord.Member = None):
        await self._cmd(inter, "wave", membro, solo_ok=True)

    @app_commands.command(name="cuddle",   description="Aconchegue-se com alguém")
    @app_commands.describe(membro="Com quem")
    async def cuddle(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "cuddle", membro)

    @app_commands.command(name="lick",     description="Lamba alguém")
    @app_commands.describe(membro="Quem vai ser lambido")
    async def lick(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "lick", membro)

    @app_commands.command(name="yeet",     description="YEET alguém!")
    @app_commands.describe(membro="Quem vai ser yeetado")
    async def yeet(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "yeet", membro)

    @app_commands.command(name="nuzzle",   description="Nuzzle alguém")
    @app_commands.describe(membro="Com quem")
    async def nuzzle(self, inter: discord.Interaction, membro: discord.Member):
        await self._cmd(inter, "nuzzle", membro)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utilidade(bot))
