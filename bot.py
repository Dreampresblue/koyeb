import discord
import json
import asyncio
import io
import datetime
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Select, View

# -----------------------------------------------------------
# 1. CARGAR CONFIGURACIÓN
# -----------------------------------------------------------
try:
    with open("config.json", "r") as f:
        config = json.load(f)
except FileNotFoundError:
    print("❌ ERROR: No encontré el archivo config.json")
    exit()

TOKEN = config["TOKEN"]
PREFIX = config.get("PREFIX", "!")
OWNER_ID = int(config.get("OWNER_ID", 0))
STAFF_ROLE_ID = int(config.get("STAFF_ROLE_ID", 0))
SERVER_ID = int(config.get("SERVER_ID", 0))
MENTION_ROLE_ID = int(config.get("MENTION_ROLE_ID", 0))
LOG_OPEN_ID = int(config.get("LOG_OPEN_ID", 0))
LOG_TRANSCRIPT_ID = int(config.get("LOG_TRANSCRIPT_ID", 0))

# Configuración de Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# -----------------------------------------------------------
# 2. SISTEMA DE RPC DINÁMICO
# -----------------------------------------------------------

@tasks.loop(seconds=30)
async def change_status():
    server_count = len(bot.guilds)
    target_guild = bot.get_guild(SERVER_ID)
    member_count = target_guild.member_count if target_guild else 0
    
    statuses = [
        f"en {server_count} servidores",
        f"a {member_count} miembros"
    ]
    current_status = statuses[change_status.current_loop % len(statuses)]
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.streaming, 
            name=current_status, 
            url="https://www.twitch.tv/kryptosmc"
        )
    )

# -----------------------------------------------------------
# 3. UTILIDADES Y SEGURIDAD
# -----------------------------------------------------------

def es_staff_o_yo(interaction: discord.Interaction) -> bool:
    if interaction.user.id == OWNER_ID: return True
    if interaction.user.guild_permissions.administrator: return True
    return False

def check_setup_perms(ctx):
    if ctx.author.id == OWNER_ID: return True
    if ctx.author.guild_permissions.administrator: return True
    return False

async def crear_archivo_transcript(channel, author_name):
    """Genera un archivo HTML simple con el historial del chat"""
    messages = [message async for message in channel.history(limit=None, oldest_first=True)]
    
    html_content = f"""
    <html>
    <head>
        <title>Transcript - {channel.name}</title>
        <style>
            body {{ background-color: #2b2d31; color: #dcddde; font-family: sans-serif; padding: 20px; }}
            .msg {{ margin-bottom: 10px; padding: 10px; border-radius: 5px; background-color: #313338; }}
            .author {{ font-weight: bold; color: #5865f2; }}
            .timestamp {{ font-size: 0.8em; color: #72767d; margin-left: 10px; }}
            .content {{ margin-top: 5px; }}
            h2 {{ color: #ffffff; border-bottom: 2px solid #5865f2; padding-bottom: 10px; }}
        </style>
    </head>
    <body>
        <h2>📄 Transcript del Ticket: {channel.name}</h2>
        <p>Generado el: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <hr>
    """
    
    for msg in messages:
        if msg.content:
            html_content += f"""
            <div class="msg">
                <span class="author">{msg.author.display_name}</span>
                <span class="timestamp">{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}</span>
                <div class="content">{msg.content}</div>
            </div>
            """
    
    html_content += "</body></html>"
    return io.BytesIO(html_content.encode('utf-8'))

# -----------------------------------------------------------
# 4. SISTEMA DE TICKETS
# -----------------------------------------------------------

class TicketControlView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reclamar Ticket", style=discord.ButtonStyle.primary, custom_id="reclamar_btn", emoji="🙋‍♂️")
    async def reclamar_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        guild = interaction.guild
        es_staff = user.get_role(STAFF_ROLE_ID) is not None
        es_admin = user.guild_permissions.administrator
        es_dueno = user.id == OWNER_ID

        if not (es_staff or es_admin or es_dueno):
            await interaction.response.send_message("❌ Solo el Staff puede reclamar.", ephemeral=True)
            return

        # 1. Permisos: Aislar el ticket
        staff_role = guild.get_role(STAFF_ROLE_ID)
        owner_member = guild.get_member(OWNER_ID)

        if staff_role: await interaction.channel.set_permissions(staff_role, read_messages=False)
        await interaction.channel.set_permissions(user, read_messages=True, send_messages=True)
        if owner_member: await interaction.channel.set_permissions(owner_member, read_messages=True, send_messages=True)

        # 2. Actualizar botón en el ticket
        button.disabled = True
        button.label = f"Reclamado por {user.name}"
        button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)
        
        await interaction.channel.send(embed=discord.Embed(
            description=f"👑 **Ticket reclamado por:** {user.mention}\n🔒 _Acceso restringido._", 
            color=discord.Color.gold()
        ))

        # 3. ACTUALIZAR LOG EN EL CANAL DE ABIERTOS
        try:
            topic = interaction.channel.topic or ""
            if "Log:" in topic:
                log_id = int(topic.split("Log:")[1])
                log_channel = guild.get_channel(LOG_OPEN_ID)
                if log_channel:
                    log_msg = await log_channel.fetch_message(log_id)
                    embed_log = log_msg.embeds[0]
                    embed_log.color = discord.Color.gold()
                    embed_log.add_field(name="👷 Reclamado por", value=f"{user.mention} (`{user.id}`)", inline=False)
                    embed_log.set_footer(text=f"Reclamado el {datetime.datetime.now().strftime('%H:%M')}")
                    await log_msg.edit(embed=embed_log)
        except Exception as e:
            print(f"No se pudo actualizar el log de apertura: {e}")


    @discord.ui.button(label="Cerrar Ticket", style=discord.ButtonStyle.red, custom_id="cerrar_btn", emoji="🔒")
    async def cerrar_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        guild = interaction.guild
        closer = interaction.user
        
        await interaction.response.send_message("⚠️ Generando transcript y cerrando...", ephemeral=True)

        # 1. Obtener datos del Topic (Dueño y Log original)
        ticket_owner = "Desconocido"
        owner_id = 0 # <--- CORRECCIÓN AQUÍ: Inicializamos la variable para evitar el error
        
        try:
            topic = channel.topic or ""
            if "Owner:" in topic:
                owner_id = int(topic.split("Owner:")[1].split("|")[0])
                member = guild.get_member(owner_id)
                ticket_owner = f"{member.name} ({member.id})" if member else f"ID: {owner_id}"
        except:
            pass

        # 2. Generar Transcript
        transcript_file = await crear_archivo_transcript(channel, ticket_owner)
        file_discord = discord.File(transcript_file, filename=f"transcript-{channel.name}.html")

        # 3. Enviar al canal de LOGS DE CIERRE
        log_channel = guild.get_channel(LOG_TRANSCRIPT_ID)
        if log_channel:
            embed = discord.Embed(title="📕 Ticket Cerrado", color=discord.Color.red(), timestamp=datetime.datetime.now())
            embed.add_field(name="📂 Canal", value=channel.name, inline=True)
            embed.add_field(name="👤 Dueño del Ticket", value=ticket_owner, inline=True)
            embed.add_field(name="🔒 Cerrado por", value=f"{closer.name} (`{closer.id}`)", inline=False)
            
            # Intentamos ver quién reclamó mirando los permisos del canal
            claimer_name = "Nadie (Staff General)"
            for member, overwrite in channel.overwrites.items():
                # Verificamos que sea miembro, que tenga permiso, no sea bot y NO SEA EL DUEÑO DEL TICKET
                if isinstance(member, discord.Member) and overwrite.read_messages and not member.bot and member.id != owner_id:
                     claimer_name = f"{member.name} ({member.id})"
                     break
            
            embed.add_field(name="🙋‍♂️ Atendido por", value=claimer_name, inline=True)
            embed.set_footer(text="KryptosMC Logs", icon_url=guild.icon.url if guild.icon else None)

            await log_channel.send(embed=embed, file=file_discord)

        await asyncio.sleep(2)
        await channel.delete()

class TicketSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Soporte General", description="Problemas de conexión o dudas", emoji="🎟️", value="general"),
            discord.SelectOption(label="Reportar Usuario", description="Comportamiento tóxico o hacks", emoji="⚔️", value="reporte"),
            discord.SelectOption(label="Reporte de Bugs", description="Informar fallos encontrados", emoji="🐛", value="bug"),
            discord.SelectOption(label="Alianzas", description="Crecer mutuamente", emoji="🤝", value="alianzas"),
            discord.SelectOption(label="Apelaciones", description="Solicita revisión de sanción", emoji="⚖️", value="apelacion"),
            discord.SelectOption(label="Tienda", description="Soporte sobre compras", emoji="💰", value="tienda"),
            discord.SelectOption(label="Cuenta y conexión", description="Problemas de acceso", emoji="👑", value="cuenta"),
        ]
        super().__init__(placeholder="Elige una categoría para el ticket", min_values=1, max_values=1, options=options, custom_id="ticket_menu_select")

    async def callback(self, interaction: discord.Interaction):
        categoria = self.values[0]
        guild = interaction.guild
        user = interaction.user
        nombre_canal = f"{categoria}-{user.name.lower().replace(' ', '-')}"

        if discord.utils.get(guild.text_channels, name=nombre_canal):
            if not interaction.response.is_done(): await interaction.response.send_message("❌ Ya tienes un ticket abierto.", ephemeral=True)
            return

        cat_obj = discord.utils.get(guild.categories, name="Tickets")
        staff_role = guild.get_role(STAFF_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        if staff_role: overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            channel = await guild.create_text_channel(nombre_canal, overwrites=overwrites, category=cat_obj)
            
            if MENTION_ROLE_ID != 0: await channel.send(f"<@&{MENTION_ROLE_ID}>")

            # --- LOG AL CANAL DE ABIERTOS ---
            log_open_channel = guild.get_channel(LOG_OPEN_ID)
            log_msg_id = 0
            if log_open_channel:
                embed_log = discord.Embed(title="🟢 Ticket Abierto", color=discord.Color.green(), timestamp=datetime.datetime.now())
                embed_log.add_field(name="👤 Usuario", value=f"{user.mention} (`{user.id}`)", inline=True)
                embed_log.add_field(name="📂 Categoría", value=categoria.capitalize(), inline=True)
                embed_log.add_field(name="#️⃣ Canal", value=channel.mention, inline=False)
                embed_log.set_footer(text="Estado: Esperando Staff")
                msg = await log_open_channel.send(embed=embed_log)
                log_msg_id = msg.id 

            # GUARDAMOS LA ID DEL DUEÑO Y DEL LOG EN EL TOPIC
            await channel.edit(topic=f"Owner:{user.id}|Log:{log_msg_id}")

            # Mensaje dentro del ticket
            embed = discord.Embed(
                title=f"Ticket: {categoria.capitalize().replace('_', ' ')}",
                description=f"Hola {user.mention}.\nHas abierto un ticket de **{categoria}**.\n\nEspera a que un miembro del Staff reclame este ticket.",
                color=discord.Color.from_rgb(43, 45, 49)
            )
            await channel.send(embed=embed, view=TicketControlView())
            await interaction.followup.send(f"✅ Ticket creado: {channel.mention}", ephemeral=True)

        except Exception as e:
            if not interaction.response.is_done(): await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

# -----------------------------------------------------------
# 5. SLASH COMMANDS
# -----------------------------------------------------------

@bot.tree.command(name="nuke", description="💣 Borra este canal y crea uno nuevo")
@app_commands.check(es_staff_o_yo)
async def nuke(interaction: discord.Interaction):
    channel = interaction.channel
    await interaction.response.send_message("💣 Preparando Nuke...", ephemeral=True)
    try:
        new_channel = await channel.clone(reason="Nuke ejecutado por Staff/Dueño")
        await new_channel.edit(position=channel.position)
        await channel.delete()
        embed = discord.Embed(title="💥 Canal Nukeado", description="Reiniciado correctamente.", color=discord.Color.orange())
        embed.set_image(url="https://media.giphy.com/media/HhTXt43pk1I1W/giphy.gif")
        await new_channel.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

@bot.tree.command(name="kick", description="👢 Expulsa a un usuario")
@app_commands.check(es_staff_o_yo)
async def kick(interaction: discord.Interaction, usuario: discord.Member, razon: str = "No especificada"):
    if usuario.top_role >= interaction.user.top_role and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Jerarquía insuficiente.", ephemeral=True)
        return
    await usuario.kick(reason=razon)
    await interaction.response.send_message(f"👢 **{usuario.name}** expulsado. Razón: {razon}")

@bot.tree.command(name="ban", description="🔨 Banea a un usuario")
@app_commands.check(es_staff_o_yo)
async def ban(interaction: discord.Interaction, usuario: discord.Member, razon: str = "No especificada"):
    if usuario.top_role >= interaction.user.top_role and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Jerarquía insuficiente.", ephemeral=True)
        return
    await usuario.ban(reason=razon)
    await interaction.response.send_message(f"🔨 **{usuario.name}** baneado. Razón: {razon}")

@bot.tree.command(name="clear", description="🧹 Borra mensajes")
@app_commands.check(es_staff_o_yo)
async def clear(interaction: discord.Interaction, cantidad: int):
    if cantidad > 100:
        await interaction.response.send_message("❌ Máximo 100 mensajes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=cantidad)
    await interaction.followup.send(f"🧹 {len(deleted)} mensajes borrados.", ephemeral=True)

@bot.tree.command(name="addrole", description="➕ Añade un rol")
@app_commands.check(es_staff_o_yo)
async def addrole(interaction: discord.Interaction, usuario: discord.Member, rol: discord.Role):
    if interaction.user.id != OWNER_ID and interaction.user.top_role <= rol:
        await interaction.response.send_message("❌ Rol superior o igual al tuyo.", ephemeral=True)
        return
    await usuario.add_roles(rol)
    await interaction.response.send_message(f"✅ Rol **{rol.name}** añadido a **{usuario.name}**.")

@bot.tree.command(name="removerole", description="➖ Quita un rol")
@app_commands.check(es_staff_o_yo)
async def removerole(interaction: discord.Interaction, usuario: discord.Member, rol: discord.Role):
    if interaction.user.id != OWNER_ID and interaction.user.top_role <= rol:
        await interaction.response.send_message("❌ Rol superior o igual al tuyo.", ephemeral=True)
        return
    await usuario.remove_roles(rol)
    await interaction.response.send_message(f"➖ Rol **{rol.name}** quitado a **{usuario.name}**.")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = f"❌ Error: {error}"
    if isinstance(error, app_commands.CheckFailure): msg = "⛔ **Acceso denegado.**"
    if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
    else: await interaction.response.send_message(msg, ephemeral=True)

# -----------------------------------------------------------
# 6. SETUP
# -----------------------------------------------------------

@bot.event
async def on_ready():
    print("⏳ Sincronizando comandos...")
    try: await bot.tree.sync()
    except Exception as e: print(f"Error sync: {e}")
    
    bot.add_view(TicketView())
    bot.add_view(TicketControlView())
    change_status.start()
    print(f'✅ Bot listo como: {bot.user}')

@bot.command(name="panel", aliases=["setup"])
@commands.check(check_setup_perms)
async def panel(ctx):
    try: await ctx.message.delete()
    except: pass 

    desc = (
        "『⚡』**Centro de Atención:** KryptosMC\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "¡Saludos, Guerrero! Has llegado al área de soporte técnico.\n"
        "Nuestro equipo de administración está listo para asistirte en tu travesía. No importa la hora, los dioses de KryptosMC están disponibles 24/7 para garantizar que tu experiencia sea legendaria.\n\n"
        "**¿Qué necesitas resolver hoy?**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎟️ | **Soporte General**\n"
        "Problemas de conexión, dudas del servidor o ayuda general.\n"
        "⚔️ | **Reportar Usuario**\n"
        "Reporta comportamientos tóxicos, hacks o faltas al reglamento.\n"
        "🐛 | **Reporte de Bugs**\n"
        "Ayúdanos a mejorar informando fallos o bugs encontrados.\n"
        "🤝 | **Alianzas**\n"
        "Para poder crecer mutuamente y obtener beneficios.\n"
        "⚖️ | **Apelaciones**\n"
        "Solicita la revisión de una sanción aplicada a tu cuenta.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 _**Recuerda no abrir ticket por que si o para molestar, ya que trabajamos muy duro para traer la mejor experiencia**_"
    )

    embed = discord.Embed(
        title="Soporte de KryptosMC",
        description=desc,
        color=0x2b2d31
    )
    embed.set_footer(text="KryptosMC")
    if ctx.guild.icon: embed.set_thumbnail(url=ctx.guild.icon.url)
    await ctx.send(embed=embed, view=TicketView())

@panel.error
async def panel_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("⛔ No tienes permisos para usar este comando.", delete_after=5)

bot.run(TOKEN)