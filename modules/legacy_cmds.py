import asyncio
import os
import shutil
import json
import subprocess
from io import BytesIO
from typing import Union, Optional
import disnake
import wavelink
from disnake.ext import commands
from utils.client import BotCore
from utils.music.checks import check_voice, check_requester_channel
from utils.music.interactions import AskView
from utils.music.models import LavalinkPlayer, YTDLPlayer
from utils.others import sync_message, chunk_list, EmbedPaginator, CustomContext
from utils.owner_panel import panel_command, PanelView
from utils.music.errors import GenericError
from jishaku.shell import ShellReader

os_quote = "\"" if os.name == "nt" else "'"
git_format = f"--pretty=format:{os_quote}%H*****%h*****%s*****%ct{os_quote}"


def format_git_log(data_list: list):

    data = []

    for d in data_list:
        if not d:
            continue
        t = d.split("*****")
        data.append({"commit": t[0], "abbreviated_commit": t[1], "subject": t[2], "timestamp": t[3]})

    return data


def replaces(txt):

    if os.name == "nt":
        return txt.replace("\"", f"\\'").replace("'", "\"")

    return txt.replace("\"", f"\\\"").replace("'", "\"")


async def run_command(cmd):

    result = []

    with ShellReader(cmd) as reader:

        async for x in reader:
            result.append(x)

    return  "\n".join(result)


def run_command_old(cmd):
    return subprocess.check_output(cmd, shell=True).decode('utf-8').strip()


class Owner(commands.Cog):

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.git_init_cmds = [
            "git init",
            f'git remote add origin {self.bot.config["SOURCE_REPO"]}',
            'git fetch origin',
            'git checkout -b main -f --track origin/main'
        ]
        self.owner_view: Optional[PanelView] = None


    def format_log(self, data: list):
        return "\n".join( f"[`{c['abbreviated_commit']}`]({self.bot.remote_git_url}/commit/{c['commit']}) `- "
                          f"{(c['subject'][:40].replace('`', '') + '...') if len(c['subject']) > 39 else c['subject']}` "
                          f"(<t:{c['timestamp']}:R>)" for c in data)


    @commands.is_owner()
    @panel_command(aliases=["rd", "recarregar"], description="Recarregar os módulos.", emoji="🔄",
                   alt_name="Carregar/Recarregar módulos.")
    async def reload(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        data = self.bot.load_modules()

        txt = ""

        if data["loaded"]:
            txt += f'**Módulos carregados:** ```ansi\n[0;34m{" [0;37m| [0;34m".join(data["loaded"])}```\n'

        if data["reloaded"]:
            txt += f'**Módulos recarregados:** ```ansi\n[0;32m{" [0;37m| [0;32m".join(data["reloaded"])}```\n'

        if data["error"]:
            txt += f'**Módulos que falharam:** ```ansi\n[0;31m{" [0;37m| [0;31m".join(data["error"])}```\n'

        if not txt:
            txt = "**Nenhum módulo encontrado...**"

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(colour=self.bot.get_color(ctx.me), description=txt)
            await ctx.send(embed=embed, view=self.owner_view)
        else:
            return txt


    @commands.is_owner()
    @commands.max_concurrency(1, commands.BucketType.default)
    @panel_command(aliases=["up", "atualizar"], description="Atualizar meu code usando o git.",
                   emoji="<:git:944873798166020116>", alt_name="Atualizar Bot")
    async def update(self, ctx: Union[CustomContext, disnake.MessageInteraction], *,
                     opts: str = ""): #TODO: Rever se há alguma forma de usar commands.Flag sem um argumento obrigatório, ex: --pip.

        out_git = ""

        git_log = []

        force = "--force" in opts

        requirements_old = ""
        try:
            with open("./requirements.txt") as f:
                requirements_old = f.read()
        except:
            pass

        if not os.path.isdir("./.git") or force:

            out_git += await self.cleanup_git(force=force)

        else:

            try:
                await ctx.response.defer()
            except:
                pass

            try:
                run_command_old("git reset --hard")
            except:
                pass

            try:
                pull_log = run_command_old("git pull --allow-unrelated-histories -X theirs")
                if "Already up to date" in pull_log:
                    raise GenericError("Já estou com os ultimos updates instalados...")
                out_git += pull_log

            except GenericError as e:
                return str(e)

            except Exception as e:

                if "Already up to date" in str(e):
                    raise GenericError("Já estou com os ultimos updates instalados...")

                elif not "Fast-forward" in str(e):
                    out_git += await self.cleanup_git(force=True)

            commit = ""

            for l in out_git.split("\n"):
                if l.startswith("Updating"):
                    commit = l.replace("Updating ", "").replace("..", "...")
                    break

            data = (run_command_old(f"git log {commit} {git_format}")).split("\n")

            git_log += format_git_log(data)

        text = "`Será necessário me reiniciar após as alterações.`"

        txt = f"`✅` **[Atualização realizada com sucesso!]({self.bot.remote_git_url}/commits/main)**"

        if git_log:
            txt += f"\n\n{self.format_log(git_log[:10])}"

        txt += f"\n\n`📄` **Log:** ```py\n{out_git[:1000]}```\n{text}"

        if isinstance(ctx, CustomContext):
            embed = disnake.Embed(
                description=txt,
                color=self.bot.get_color(ctx.guild.me)
            )
            await ctx.send(embed=embed, view=self.owner_view)

            self.bot.loop.create_task(self.update_deps(ctx, requirements_old, opts))

        else:
            self.bot.loop.create_task(self.update_deps(ctx, requirements_old, opts))
            return txt


    async def update_deps(self, ctx, original_reqs, opts):

        if "--pip" in opts:
            await run_command("pip3 install -U -r requirements.txt")

        else:

            with open("./requirements.txt") as f:
                requirements_new = f.read()

            if original_reqs != requirements_new:

                view = AskView(timeout=45, ctx=ctx)

                embed = disnake.Embed(
                    description="**Será necessário atualizar as dependências, escolha sim para instalar.**\n\n"
                                "Nota: Caso não tenha no mínimo 150mb de ram livre, escolha **Não**, mas dependendo "
                                "da hospedagem você deverá usar o comando abaixo: ```sh\npip3 install -U -r requirements.txt``` "
                                "(ou apenas upar o arquivo requirements.txt)",
                    color=self.bot.get_color(ctx.guild.me)
                )

                try:
                    await ctx.edit_original_message(embed=embed, view=view)
                except AttributeError:
                    await ctx.send(embed=embed, view=view)

                await view.wait()

                if view.selected:
                    embed.description = "**Instalando dependências...**"
                    await view.interaction_resp.response.edit_message(embed=embed, view=None)
                    await run_command("pip3 install -U -r requirements.txt")

                try:
                    await (await view.interaction_resp.original_message()).delete()
                except:
                    pass


    async def cleanup_git(self, force=False):

        if force:
            try:
                shutil.rmtree("./.git")
            except FileNotFoundError:
                pass

        out_git = ""

        for c in self.git_init_cmds:
            try:
                out_git += run_command_old(c) + "\n"
            except Exception as e:
                out_git += f"{e}\n"

        self.bot.commit = run_command_old("git rev-parse --short HEAD")
        self.bot.remote_git_url = self.bot.config["SOURCE_REPO"][:-4]

        return out_git


    @commands.is_owner()
    @panel_command(aliases=["latest", "lastupdate"], description="Ver minhas atualizações mais recentes.", emoji="📈",
                   alt_name="Ultimas atualizações")
    async def updatelog(self, ctx: Union[CustomContext, disnake.MessageInteraction], amount: int = 10):

        if not os.path.isdir("./.git"):
            raise GenericError("Não há repositorio iniciado no diretório do bot...\nNota: Use o comando update.")

        if not self.bot.remote_git_url:
            self.bot.remote_git_url = self.bot.config["SOURCE_REPO"][:-4]

        git_log = []

        data = (run_command_old(f"git log -{amount or 10} {git_format}")).split("\n")

        git_log += format_git_log(data)

        txt = f"🔰 ** | [Atualizações recentes:]({self.bot.remote_git_url}/commits/main)**\n\n" + self.format_log(git_log)

        if isinstance(ctx, CustomContext):

            embed = disnake.Embed(
                description=txt,
                color=self.bot.get_color(ctx.guild.me)
            )

            await ctx.send(embed=embed, view=self.owner_view)

        else:
            return txt


    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(description="Sincronizar/Registrar os comandos de barra no servidor.", hidden=True)
    async def syncguild(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        embed = disnake.Embed(
            color=self.bot.get_color(ctx.guild.me),
            description="**Esse comando não é mais necessário ser usado (A sincronização dos comandos agora "
                        f"é automática).**\n\n{sync_message(self.bot)}"
        )

        await ctx.send(embed=embed)


    @commands.is_owner()
    @panel_command(aliases=["sync"], description="Sincronizar os comandos de barra manualmente.", emoji="<:slash:944875586839527444>",
                   alt_name="Sincronizar comandos manualmente.")
    async def synccmds(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        if self.bot.config["AUTO_SYNC_COMMANDS"] is True:
            raise GenericError(f"**Isso não pode ser usado com a sincronização automática ativada...**\n\n{sync_message(self.bot)}")

        await self.bot._sync_application_commands()

        txt = f"**Os comandos de barra foram sincronizados com sucesso!**\n\n{sync_message(self.bot)}"

        if isinstance(ctx, CustomContext):

            embed = disnake.Embed(
                color=self.bot.get_color(ctx.guild.me),
                description=txt
            )

            await ctx.send(embed=embed, view=self.owner_view)

        else:
            return txt


    @commands.command(name="help", aliases=["ajuda"], hidden=True)
    @commands.cooldown(1, 3, commands.BucketType.member)
    async def help_(self, ctx: CustomContext, cmd_name: str = None):

        if cmd_name:

            cmd = self.bot.get_command(cmd_name)

            if not cmd:
                raise GenericError(f"O comando **{cmd_name}** não existe.")

            embed = disnake.Embed(
                color=self.bot.get_color(ctx.guild.me),
                title=f"**Informações do comando: {cmd.name}**"
            )

            if cmd.description:
                embed.add_field(name="Descrição:", value=f"```ldif\n{cmd.description}```", inline=False)

            if cmd.aliases:
                embed.add_field(name="Aliases/Sinônimos:", value="```ldif\n{}```".format(" | ".join(f"{ctx.clean_prefix}{a}" for a in cmd.aliases)), inline=False)

            if cmd.usage:
                embed.add_field(name="Como usar:", value=f" ```ldif\n{ctx.clean_prefix}{cmd.name} {cmd.usage}```", inline=False)

            await ctx.send(embed=embed)
            return

        cmds = [c for c in self.bot.commands if not c.hidden]

        cmds_final = []

        for cmd in cmds:

            txt = ""

            prefix = ctx.prefix if ctx.guild.me.mention != ctx.prefix else ""

            cmd_name = f"**{prefix}{cmd.name}**"

            if cmd.aliases:
                cmd_name += f" ({', '.join(a for a in cmd.aliases)})"

            txt += f" ```ldif\n{cmd.description or 'Sem descrição...'}```"

            txt += "\n"

            cmds_final.append([cmd_name, txt])

        embeds = []

        slash_msg = "`Veja meus comandos de barra usando:` **/**\n\n" if self.bot.slash_commands else ""

        txt_chunked = chunk_list(cmds_final, 9)

        for c, txt_pages in enumerate(txt_chunked):

            embed = disnake.Embed(color=self.bot.get_color(ctx.me), title=f"Meus comandos ({len(cmds)}):",
                                  description=slash_msg)

            embed.set_footer(text=f"Página: {c+1}/{len(txt_chunked)} | para ver informações detalhadas de um comando especifico use: {ctx.clean_prefix}{ctx.invoked_with} comando")

            for cmd_name, cmd_desc in txt_pages:
                embed.add_field(name=cmd_name, value=cmd_desc)

            embeds.append(embed)

        view = EmbedPaginator(ctx, embeds, timeout=60)

        view.message = await ctx.reply(embed=embeds[0], view=view)

        await view.wait()


    @commands.has_guild_permissions(administrator=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.command(
        aliases=["mudarprefixo", "prefix", "changeprefix"],
        description="Alterar o prefixo do servidor",
        usage="prefixo"
    )
    async def setprefix(self, ctx: CustomContext, prefix: str = None):

        if not prefix:
            raise GenericError("**Você não informou um novo prefixo.**")

        if " " in prefix or len(prefix) > 5:
            raise GenericError("**O prefixo não pode conter espaços ou ter acima de 5 caracteres.**")


        data = await self.bot.db.get_data(ctx.guild.id, db_name="guilds")

        data["prefix"] = prefix
        await self.bot.db.update_data(ctx.guild.id, data, db_name="guilds")

        embed = disnake.Embed(
            description=f"**O prefixo deste servidor agora é:** {disnake.utils.escape_markdown(prefix)}",
            color=self.bot.get_color(ctx.guild.me)
        )

        await ctx.send(embed=embed)


    @commands.is_owner()
    @panel_command(aliases=["export"], description="Exportar minhas configs/secrets/env para um arquivo.", emoji="🔐",
                   alt_name="Exportar env/config")
    async def exportenv(self, ctx: Union[CustomContext, disnake.MessageInteraction]):

        fp = BytesIO(bytes(json.dumps(self.bot.config, indent=4), 'utf-8'))
        try:
            embed=disnake.Embed(
                    description="**Não divulge/mostre esse arquivo pra ninguém e muito cuidado ao postar print's "
                                "do conteudo dele e não adicione esse arquivo em locais públicos como github, repl.it, "
                                "glitch.com, etc!**",
                    color=self.bot.get_color(ctx.guild.me))
            embed.set_footer(text="Por medida de segurança, esta mensagem será deletada em 60 segundos.")
            await ctx.author.send(embed=embed,
                file=disnake.File(fp=fp, filename="config.json"), delete_after=60)

        except disnake.Forbidden:
            raise GenericError("Seu DM está desativado!")

        if isinstance(ctx, CustomContext):
            await ctx.message.add_reaction("👍")
        else:
            return "Arquivo de configuração enviado com sucesso no seu DM."


    @check_voice()
    @commands.command(description='inicializar um player no servidor.', aliases=["spawn", "sp", "spw", "smn"])
    async def summon(self, ctx: CustomContext):

        try:
            self.bot.music.players[ctx.guild.id] #type ignore
            raise GenericError("**Já há um player iniciado no servidor.**")
        except KeyError:
            pass

        node: wavelink.Node = self.bot.music.get_best_node()

        if not node:
            raise GenericError("**Não há servidores de música disponível!**")

        guild_data = await self.bot.db.get_data(ctx.guild.id, db_name="guilds")

        static_player = guild_data['player_controller']

        try:
            channel = ctx.guild.get_channel(int(static_player['channel'])) or ctx.channel
            message = await channel.fetch_message(int(static_player.get('message_id')))
        except (KeyError, TypeError):
            channel = ctx.channel
            message = None

        player: Union[LavalinkPlayer, YTDLPlayer] = self.bot.music.get_player(
            node_id=node.identifier,
            guild_id=ctx.guild.id,
            cls=LavalinkPlayer,
            requester=ctx.author,
            guild=ctx.guild,
            channel=channel,
            message=message,
            static=bool(static_player['channel'])
        )

        channel = ctx.author.voice.channel

        await player.connect(channel.id)

        self.bot.loop.create_task(ctx.message.add_reaction("👍"))

        while not ctx.guild.me.voice:
            await asyncio.sleep(1)

        if isinstance(channel, disnake.StageChannel):

            stage_perms =  channel.permissions_for(ctx.guild.me)
            if stage_perms.manage_permissions:
                await ctx.guild.me.edit(suppress=False)
            elif stage_perms.request_to_speak:
                await ctx.guild.me.request_to_speak()

            await asyncio.sleep(1.5)

        await player.process_next()

    async def cog_check(self, ctx: CustomContext) -> bool:
        return await check_requester_channel(ctx)

    async def cog_load(self) -> None:
        self.owner_view =  PanelView(self.bot)


def setup(bot: BotCore):
    bot.remove_command("help")
    bot.add_cog(Owner(bot))
