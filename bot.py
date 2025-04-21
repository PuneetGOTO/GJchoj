# --- 导入必要的库 ---
import nextcord # Discord API 库 (选用 nextcord)
from nextcord.ext import commands, tasks # 从 nextcord 导入命令扩展和后台任务功能
import datetime # 处理日期和时间
import random # 用于随机选择获胜者
import asyncio # 异步编程库 (nextcord 基于此)
import os # 用于访问环境变量 (获取配置)
import json # 用于序列化/反序列化数据 (存入 Redis)
import redis.asyncio as redis # 异步 Redis 客户端库
from urllib.parse import urlparse # 用于解析 Redis URL (虽然 redis.asyncio.from_url 会处理)

# --- 配置 ---
# 从环境变量加载配置 (Railway 会自动注入这些变量)
BOT_TOKEN = os.environ.get('BOT_TOKEN') # 获取 Discord Bot Token
REDIS_URL = os.environ.get('REDIS_URL') # 获取 Railway 提供的 Redis 连接 URL

# 检查配置是否存在
if not BOT_TOKEN:
    print("错误: 未设置 BOT_TOKEN 环境变量。")
    exit()
if not REDIS_URL:
    print("错误: 未设置 REDIS_URL 环境变量。请确保已链接 Redis 服务。")
    exit()

# --- Bot Intents (机器人意图) ---
# 确保在 Discord 开发者门户启用了 Privileged Intents (服务器成员意图, 消息内容意图)
intents = nextcord.Intents.default() # 使用默认意图
intents.guilds = True       # 需要公会 (服务器) 信息
intents.members = True      # !!关键!! 需要获取服务器成员列表，用于检查身份组
intents.message_content = False # 对于斜杠命令和反应通常不需要
intents.reactions = True    # 如果使用点击表情 (🎉) 参与，则需要此意图

# 创建机器人实例
bot = commands.Bot(intents=intents)

# --- Redis 连接 ---
redis_pool = None # 初始化 Redis 连接池变量

async def setup_redis():
    """初始化 Redis 连接池。"""
    global redis_pool
    redis_url_to_use = os.environ.get('REDIS_URL')
    if not redis_url_to_use:
         print("错误: 在 setup_redis 中未能获取 REDIS_URL 环境变量！")
         await bot.close() # 如果无法获取 URL，则关闭
         return
    try:
        print(f"正在连接到 Redis: {redis_url_to_use}...") # 打印将要连接的URL
        redis_pool = redis.from_url(redis_url_to_use, decode_responses=True)
        await redis_pool.ping() # 测试连接
        print("成功连接到 Redis。")
    except Exception as e:
        print(f"致命错误: 无法连接到 Redis: {e}")
        await bot.close() # 如果连接失败，关闭机器人

# --- 辅助函数 ---

GIVEAWAY_PREFIX = "giveaway:" # 定义 Redis 键的前缀，方便管理

def parse_duration(duration_str: str) -> datetime.timedelta | None:
    """将用户输入的时长字符串 (如 '1h', '30m', '2d') 解析为 timedelta 对象。"""
    duration_str = duration_str.lower().strip()
    value_str = ""
    unit = ""
    for char in duration_str:
        if char.isdigit() or char == '.': # Allow decimals for partial units if needed
            value_str += char
        else:
            unit += char

    if not value_str or not unit:
        return None

    try:
        value = float(value_str)
        if unit == 's':
            return datetime.timedelta(seconds=value)
        elif unit == 'm':
            return datetime.timedelta(minutes=value)
        elif unit == 'h':
            return datetime.timedelta(hours=value)
        elif unit == 'd':
            return datetime.timedelta(days=value)
        else:
            return None
    except ValueError:
        return None

# --- 修改后的 save_giveaway_data ---
async def save_giveaway_data(message_id: int, data: dict):
    """将抽奖数据保存到 Redis，确保 datetime 可序列化。"""
    if not redis_pool: return
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}"

        # 创建一个数据的副本以避免修改原始字典
        data_to_save = data.copy()

        # 检查副本中的 'end_time' 是否是 datetime 对象
        if isinstance(data_to_save.get('end_time'), datetime.datetime):
            # 确保时区感知
            if data_to_save['end_time'].tzinfo is None:
                 data_to_save['end_time'] = data_to_save['end_time'].replace(tzinfo=datetime.timezone.utc)
            # 直接将 datetime 对象转换为 ISO 字符串，并替换掉副本中原来的值
            data_to_save['end_time'] = data_to_save['end_time'].isoformat()
            # 不再需要 'end_time_iso' 这个键了

        # 现在 data_to_save 中的 'end_time' 已经是字符串了
        await redis_pool.set(key, json.dumps(data_to_save))

    except TypeError as e: # 更具体地捕获 TypeError
        print(f"保存抽奖数据 {message_id} 到 Redis 时出错 (序列化失败): {e}")
        # 可以考虑打印 data_to_save 的内容来调试
        # print(f"Data causing serialization error: {data_to_save}")
    except Exception as e:
        print(f"保存抽奖数据 {message_id} 到 Redis 时发生其他错误: {e}")

# --- 修改后的 load_giveaway_data ---
async def load_giveaway_data(message_id: int) -> dict | None:
    """从 Redis 加载抽奖数据，并将 ISO 字符串转回 datetime。"""
    if not redis_pool: return None
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}"
        data_str = await redis_pool.get(key)
        if data_str:
            data = json.loads(data_str)

            # 检查 'end_time' 键的值是否是字符串，如果是，则转换回 datetime 对象
            if isinstance(data.get('end_time'), str):
                try:
                    data['end_time'] = datetime.datetime.fromisoformat(data['end_time'])
                except ValueError:
                    # 如果字符串不是有效的 ISO 格式，记录警告但可能保持原样或删除
                    print(f"警告: 抽奖 {message_id} 的 end_time 格式无效 (非 ISO string?)。")
                    # 根据需要决定如何处理，例如: del data['end_time'] 或保持字符串
            # else: # 如果 end_time 不是字符串，可能是旧数据或错误数据
            #    print(f"警告: 抽奖 {message_id} 的 end_time 不是字符串格式。")

            return data
        return None
    except json.JSONDecodeError:
        print(f"从 Redis 解码抽奖 {message_id} 的 JSON 时出错。")
        return None
    except Exception as e:
        print(f"从 Redis 加载抽奖数据 {message_id} 时出错: {e}")
        return None


async def delete_giveaway_data(message_id: int):
    """从 Redis 删除抽奖数据。"""
    if not redis_pool: return
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}"
        await redis_pool.delete(key) # 删除指定的键
    except Exception as e:
        print(f"从 Redis 删除抽奖数据 {message_id} 时出错: {e}")

async def get_all_giveaway_ids() -> list[int]:
    """从 Redis 获取所有活跃抽奖的消息 ID 列表。"""
    if not redis_pool: return []
    try:
        keys = await redis_pool.keys(f"{GIVEAWAY_PREFIX}*")
        return [int(key.split(':')[-1]) for key in keys]
    except Exception as e:
        print(f"从 Redis 获取抽奖键时出错: {e}")
        return []

# --- 科技感 Embed 消息函数 ---
def create_giveaway_embed(prize: str, end_time: datetime.datetime, winners: int, creator: nextcord.User | nextcord.Member, required_role: nextcord.Role | None, status: str = "running"):
    """创建用于展示抽奖信息的 Embed 对象 (运行中状态)。"""
    embed = nextcord.Embed(
        title="<a:_:1198114874891632690> **赛博抽奖进行中!** <a:_:1198114874891632690>", # 标题 (可用动态 Emoji)
        description=f"点击 🎉 表情参与!\n\n**奖品:** `{prize}`", # 描述
        color=0x00FFFF # 颜色 (青色/科技蓝)
    )
    embed.add_field(name="<:timer:1198115585629569044> 结束于", value=f"<t:{int(end_time.timestamp())}:R>", inline=True)
    embed.add_field(name="<:winner:1198115869403988039> 获奖人数", value=f"`{winners}`", inline=True)
    if required_role:
        embed.add_field(name="<:requirement:1198116280151654461> 参与条件", value=f"需要拥有 {required_role.mention} 身份组。", inline=False)
    else:
         embed.add_field(name="<:requirement:1198116280151654461> 参与条件", value="`无`", inline=False)
    embed.set_footer(text=f"由 {creator.display_name} 发起 | 状态: {status.upper()}", icon_url=creator.display_avatar.url if creator.display_avatar else None)
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1003591315297738772/1198117400949297172/giveaway-box.png?ex=65bda71e&is=65ab321e&hm=375f317989609026891610d51d14116503d730ffb1ed1f8749f8e8215e911c18&")
    return embed


def update_embed_ended(embed: nextcord.Embed, winner_mentions: str | None, prize: str, participant_count: int):
     """更新 Embed 对象以显示抽奖结束状态。"""
     embed.title = "<:check:1198118533916270644> **抽奖已结束** <:check:1198118533916270644>"
     embed.color = 0x36393F
     embed.clear_fields()
     if winner_mentions:
         embed.description = f"**奖品:** `{prize}`\n\n恭喜以下获奖者！"
         embed.add_field(name="<:winner:1198115869403988039> 获奖者", value=winner_mentions, inline=False)
     else:
         embed.description = f"**奖品:** `{prize}`\n\n本次抽奖没有符合条件的参与者。"
         embed.add_field(name="<:cross:1198118636147118171> 获奖者", value="`无`", inline=False)
     embed.add_field(name="<:members:1198118814719295550> 参与人数", value=f"`{participant_count}`", inline=True)
     if embed.footer:
         original_footer_text = embed.footer.text.split('|')[0].strip()
         embed.set_footer(text=f"{original_footer_text} | 状态: 已结束", icon_url=embed.footer.icon_url)
     return embed

# --- 核心开奖逻辑函数 (重构) ---
async def process_giveaway_end(message: nextcord.Message, giveaway_data: dict):
    """处理结束抽奖的核心逻辑：获取参与者、筛选、抽奖、宣布、更新消息。"""
    guild = message.guild
    channel = message.channel
    bot_instance = bot # Access the global bot instance

    if not guild or not channel or not isinstance(channel, nextcord.TextChannel):
         print(f"错误: 提供给 process_giveaway_end 的服务器或频道无效 (消息 ID: {message.id})")
         return # Or raise an exception

    print(f"正在处理抽奖结束: {message.id} (奖品: {giveaway_data.get('prize', 'N/A')})")

    # --- 开奖逻辑 (从 check_giveaways 移动过来) ---
    reaction = nextcord.utils.get(message.reactions, emoji="🎉")
    potential_participants = []
    if reaction:
        try:
            potential_participants = [
                member async for member in reaction.users()
                if isinstance(member, nextcord.Member) # Must be member to check roles and not bot
            ]
        except nextcord.Forbidden:
            print(f"无法获取消息 {message.id} 的反应者成员列表 (缺少成员意图/权限?)。假设无参与者。")
        except Exception as e:
            print(f"获取抽奖 {message.id} 的反应用户时发生错误: {e}。假设无参与者。")
    else:
        print(f"消息 {message.id} 上无 🎉 反应。")

    # 根据身份组要求筛选参与者
    eligible_participants = []
    required_role_id = giveaway_data.get('required_role_id')
    required_role = None
    if required_role_id:
        required_role = guild.get_role(required_role_id)

    if required_role:
        for member in potential_participants:
            if required_role in member.roles:
                eligible_participants.append(member)
    else:
        eligible_participants = potential_participants

    # --- 宣布获胜者 ---
    winners = []
    winner_mentions = None
    participant_count = len(eligible_participants) # 统计有效参与人数

    if eligible_participants:
        num_winners = min(giveaway_data['winners'], len(eligible_participants))
        if num_winners > 0:
            winners = random.sample(eligible_participants, num_winners) # 随机抽取
            winner_mentions = ", ".join([w.mention for w in winners])
            print(f"抽奖 {message.id} 选出的获胜者: {[w.name for w in winners]}")

    # 准备结果消息
    result_message = f"<a:_:1198114874891632690> **抽奖结束！** <a:_:1198114874891632690>\n奖品: `{giveaway_data['prize']}`\n"
    if winner_mentions:
        result_message += f"\n恭喜 {winner_mentions}！"
    else:
        result_message += "\n可惜，本次抽奖没有符合条件的获奖者。"

    try:
        # 发送结果消息，允许提及用户 (@)
        allowed_mentions = nextcord.AllowedMentions(users=True, roles=False, everyone=False)
        await channel.send(result_message, allowed_mentions=allowed_mentions)
    except nextcord.Forbidden:
        print(f"无法在频道 {channel.id} 发送获奖公告 (权限不足?)。")
    except Exception as e:
        print(f"发送抽奖 {message.id} 获奖公告时发生错误: {e}")

    # --- 更新原始抽奖消息的 Embed ---
    if message.embeds:
        try:
            updated_embed = update_embed_ended(
                message.embeds[0],
                winner_mentions,
                giveaway_data['prize'],
                participant_count
            )
            await message.edit(embed=updated_embed, view=None)
        except nextcord.Forbidden:
            print(f"无法编辑原始抽奖消息 {message.id} (权限不足?)。")
        except nextcord.NotFound:
            print(f"原始抽奖消息 {message.id} 在编辑前消失。")
        except Exception as e:
            print(f"编辑原始抽奖消息 {message.id} 时出错: {e}")
    else:
        print(f"原始抽奖消息 {message.id} 没有 Embed 可更新。")

# --- 抽奖命令 ---

@bot.slash_command(name="giveaway", description="抽奖活动管理基础命令")
async def giveaway(interaction: nextcord.Interaction):
    pass

@giveaway.subcommand(name="create", description="🎉 发起一个新的抽奖活动！")
async def giveaway_create(interaction: nextcord.Interaction, duration: str = ..., winners: int = ..., prize: str = ..., channel: nextcord.abc.GuildChannel = None, required_role: nextcord.Role = None):
    """处理 /giveaway create 命令。"""
    await interaction.response.defer(ephemeral=True)
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, nextcord.TextChannel):
        await interaction.followup.send("错误: 所选频道不是文字频道。", ephemeral=True)
        return
    bot_member = interaction.guild.me
    permissions = target_channel.permissions_for(bot_member)
    required_perms = {
        "send_messages": permissions.send_messages, "embed_links": permissions.embed_links,
        "add_reactions": permissions.add_reactions, "read_message_history": permissions.read_message_history,
        "manage_messages": permissions.manage_messages
    }
    missing_perms = [perm for perm, has in required_perms.items() if not has]
    if missing_perms:
        await interaction.followup.send(f"错误: 我在 {target_channel.mention} 缺少必要权限: `{', '.join(missing_perms)}`。", ephemeral=True); return
    delta = parse_duration(duration)
    if delta is None or delta.total_seconds() <= 5:
        await interaction.followup.send("无效或过短的持续时间。请使用如 '10s', '5m', '1h', '2d' (至少5秒)。", ephemeral=True); return
    if winners <= 0:
        await interaction.followup.send("获奖者数量必须为 1 或更多。", ephemeral=True); return
    end_time = datetime.datetime.now(datetime.timezone.utc) + delta
    embed = create_giveaway_embed(prize, end_time, winners, interaction.user, required_role)
    try:
        giveaway_message = await target_channel.send(embed=embed); await giveaway_message.add_reaction("🎉")
    except nextcord.Forbidden: await interaction.followup.send(f"错误: 无法在 {target_channel.mention} 发送消息或添加反应。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"创建抽奖时发生意外错误: {e}", ephemeral=True); print(f"Error creating giveaway: {e}"); return
    giveaway_data = {
        'guild_id': interaction.guild.id, 'channel_id': target_channel.id, 'message_id': giveaway_message.id,
        'end_time': end_time, # 在 save_giveaway_data 中会被转为字符串
        'winners': winners, 'prize': prize,
        'required_role_id': required_role.id if required_role else None,
        'creator_id': interaction.user.id, 'creator_name': interaction.user.display_name
    }
    # 在调用 save 之前，end_time 还是 datetime 对象
    await save_giveaway_data(giveaway_message.id, giveaway_data)
    await interaction.followup.send(f"✅ 奖品为 `{prize}` 的抽奖已在 {target_channel.mention} 创建！ 结束于: <t:{int(end_time.timestamp())}:F>", ephemeral=True)


@giveaway.subcommand(name="reroll", description="<:reroll:1198121147395555328> 为指定的抽奖重新抽取获胜者。")
@commands.has_permissions(manage_guild=True) # 添加权限检查
async def giveaway_reroll(interaction: nextcord.Interaction, message_link_or_id: str = ...):
    """处理 /giveaway reroll 命令。"""
    await interaction.response.defer(ephemeral=True)
    message_id = None; channel_id = None
    try:
        link_parts = message_link_or_id.strip().split('/')
        if len(link_parts) >= 3 and link_parts[-3] == 'channels':
            if int(link_parts[-3]) != interaction.guild.id: await interaction.followup.send("错误：链接来自其他服务器。", ephemeral=True); return
            message_id = int(link_parts[-1]); channel_id = int(link_parts[-2])
        else: await interaction.followup.send("请提供完整的消息链接。", ephemeral=True); return
    except ValueError: await interaction.followup.send("无效的消息链接格式。", ephemeral=True); return
    target_channel = bot.get_channel(channel_id)
    if not target_channel or not isinstance(target_channel, nextcord.TextChannel) or target_channel.guild.id != interaction.guild.id:
        await interaction.followup.send("无法找到指定频道或链接无效。", ephemeral=True); return
    try: message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound: await interaction.followup.send("无法找到原始抽奖消息。", ephemeral=True); return
    except nextcord.Forbidden: await interaction.followup.send(f"无权限在 {target_channel.mention} 读取历史记录。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取消息时出错: {e}", ephemeral=True); print(f"Error fetch msg reroll {message_id}: {e}"); return
    if not message.embeds: await interaction.followup.send("消息缺少 Embed。", ephemeral=True); return
    original_embed = message.embeds[0]
    giveaway_data = await load_giveaway_data(message_id); prize = "未知奖品"; winners_count = 1; required_role_id = None
    if giveaway_data: print(f"Reroll {message_id} using Redis data"); winners_count=giveaway_data.get('winners',1); required_role_id=giveaway_data.get('required_role_id'); prize=giveaway_data.get('prize', prize)
    else: print(f"Warn: No Redis data for {message_id}, parsing embed for reroll."); # ... (Fallback embed parsing logic as before) ...
    reaction = nextcord.utils.get(message.reactions, emoji="🎉")
    if reaction is None: await interaction.followup.send("消息上无 🎉 反应。", ephemeral=True); return
    try: potential_participants = [m async for m in reaction.users() if isinstance(m, nextcord.Member)]
    except nextcord.Forbidden: await interaction.followup.send("错误: 需要成员意图权限。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取反应用户出错: {e}", ephemeral=True); print(f"Error react users reroll {message_id}: {e}"); return
    eligible_participants = []; required_role = None
    if required_role_id: required_role = interaction.guild.get_role(required_role_id)
    if required_role: print(f"Reroll filtering for role: {required_role.name}"); eligible_participants = [m for m in potential_participants if required_role in m.roles]
    else: eligible_participants = potential_participants
    if not eligible_participants: await interaction.followup.send("无符合条件的参与者可重抽。", ephemeral=True); await target_channel.send(f"尝试为 `{prize}` 重抽，但无合格参与者。"); return
    num_to_reroll = min(winners_count, len(eligible_participants))
    if num_to_reroll <= 0: await interaction.followup.send("无法重抽0位。", ephemeral=True); return
    new_winners = random.sample(eligible_participants, num_to_reroll); new_winner_mentions = ", ".join([w.mention for w in new_winners])
    await target_channel.send(f"<:reroll:1198121147395555328> **重新抽奖！** <...>\n恭喜 `{prize}` 的新获奖者: {new_winner_mentions}", allowed_mentions=nextcord.AllowedMentions(users=True))
    try: updated_embed = update_embed_ended(original_embed, new_winner_mentions, prize, len(eligible_participants)); await message.edit(embed=updated_embed)
    except Exception as e: print(f"Error edit msg after reroll {message_id}: {e}")
    await interaction.followup.send(f"✅ 已为 `{prize}` 重抽。新获奖者: {new_winner_mentions}", ephemeral=True)

@giveaway_reroll.error # 添加 reroll 的错误处理
async def reroll_error(interaction: nextcord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("抱歉，你没有权限执行此命令。", ephemeral=True)
    else: await interaction.response.send_message(f"执行 reroll 命令出错: {error}", ephemeral=True); print(f"Error in reroll cmd: {error}")


@giveaway.subcommand(name="pickwinner", description="👑 [管理员] 手动指定中奖者并结束抽奖。")
@commands.has_permissions(manage_guild=True) # 限制权限
async def giveaway_pickwinner(
    interaction: nextcord.Interaction,
    message_link_or_id: str = nextcord.SlashOption(description="要指定中奖者的抽奖的消息 ID 或链接。", required=True),
    winner1: nextcord.Member = nextcord.SlashOption(description="指定的第一位中奖者。", required=True),
    winner2: nextcord.Member = nextcord.SlashOption(description="指定的第二位中奖者 (可选)。", required=False, default=None),
    winner3: nextcord.Member = nextcord.SlashOption(description="指定的第三位中奖者 (可选)。", required=False, default=None),
):
    """[管理员] 手动选择获胜者并结束抽奖。"""
    await interaction.response.defer(ephemeral=True)

    # --- 解析消息 ID 和频道 ID ---
    message_id = None; channel_id = None
    try:
        link_parts = message_link_or_id.strip().split('/')
        if len(link_parts) >= 3 and link_parts[-3] == 'channels':
            if int(link_parts[-3]) != interaction.guild.id: await interaction.followup.send("错误：链接来自其他服务器。", ephemeral=True); return
            message_id = int(link_parts[-1]); channel_id = int(link_parts[-2])
        else: await interaction.followup.send("请提供完整的消息链接。", ephemeral=True); return
    except ValueError: await interaction.followup.send("无效的消息链接格式。", ephemeral=True); return

    # --- 获取频道和消息 ---
    target_channel = bot.get_channel(channel_id)
    if not target_channel or not isinstance(target_channel, nextcord.TextChannel) or target_channel.guild.id != interaction.guild.id:
        await interaction.followup.send("无法找到指定频道或链接无效。", ephemeral=True); return
    try: message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound: await interaction.followup.send("无法找到原始抽奖消息。", ephemeral=True); return
    except nextcord.Forbidden: await interaction.followup.send(f"无权限在 {target_channel.mention} 读取历史记录。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取消息时出错: {e}", ephemeral=True); print(f"Error fetch msg pickwinner {message_id}: {e}"); return
    if not message.embeds: await interaction.followup.send("消息缺少 Embed。", ephemeral=True); return
    original_embed = message.embeds[0]

    # --- 获取奖品名称 (包含 SyntaxError 修正) ---
    giveaway_data = await load_giveaway_data(message_id)
    prize = "未知奖品" # 设置默认值

    if giveaway_data:
        prize = giveaway_data.get('prize', prize) # 优先从 Redis 数据获取
    else:
        # 如果 Redis 没有数据，尝试从 Embed 解析
        print(f"无法从 Redis 加载抽奖 {message_id} 数据 (pickwinner), 尝试从 Embed 解析奖品...")
        if original_embed.description:
            # 查找包含 "**prize:**" 的行 (修正: 添加 strip() 去除可能的前后空格)
            prize_line = next((line for line in original_embed.description.split('\n') if line.lower().strip().startswith('**prize:**')), None)
            # --- 这里是修正后的代码块 ---
            if prize_line:
                # 尝试从 `**Prize:** \`Prize Name\`` 格式中提取
                try:
                    prize = prize_line.split('`')[1]
                    print(f"从 Embed 解析到奖品: {prize}")
                except IndexError:
                    # 如果格式不匹配（例如没有反引号或只有一个），则忽略错误，保持默认值
                    print("从 Embed 解析奖品失败: 格式不匹配或缺少反引号。")
                    pass # 保持 prize 为 "未知奖品"
            # --- 修正后的代码块结束 ---

    # --- 收集指定的中奖者 ---
    specified_winners = [w for w in [winner1, winner2, winner3] if w is not None]
    if not specified_winners: await interaction.followup.send("错误：必须至少指定一位中奖者。", ephemeral=True); return
    winner_mentions = ", ".join([w.mention for w in specified_winners])

    # --- 宣布指定结果 ---
    result_message = f"👑 **抽奖结果指定！** 👑\n奖品: `{prize}`\n\n管理员指定以下用户为中奖者: {winner_mentions}"
    try: await target_channel.send(result_message, allowed_mentions=nextcord.AllowedMentions(users=True))
    except Exception as e: print(f"无法发送 pickwinner 公告 {message_id}: {e}")

    # --- 更新原始消息 ---
    participant_count_display = len(specified_winners)
    try:
        updated_embed = update_embed_ended(original_embed, winner_mentions, prize, participant_count_display)
        updated_embed.title = "👑 **抽奖已结束 (手动指定)** 👑" # 标记为手动
        await message.edit(embed=updated_embed, view=None)
    except Exception as e: print(f"无法编辑 pickwinner 消息 {message_id}: {e}")

    # --- 清理 Redis 数据 ---
    await delete_giveaway_data(message_id)
    print(f"已手动结束并从 Redis 移除抽奖 {message_id} (pickwinner)。")
    await interaction.followup.send(f"✅ 已成功指定 `{prize}` 中奖者为 {winner_mentions} 并结束。", ephemeral=True)

@giveaway_pickwinner.error
async def pickwinner_error(interaction: nextcord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("抱歉，你没有权限执行此命令。", ephemeral=True)
    else: await interaction.response.send_message(f"执行 pickwinner 命令出错: {error}", ephemeral=True); print(f"Error in pickwinner cmd: {error}")


# --- 新增：手动结束并随机抽奖命令 ---
@giveaway.subcommand(name="end", description="⏱️ [管理员] 立即结束抽奖并随机抽取获胜者。")
@commands.has_permissions(manage_guild=True) # 限制权限
async def giveaway_end(
    interaction: nextcord.Interaction,
    message_link_or_id: str = nextcord.SlashOption(description="要立即结束的抽奖的消息 ID 或链接。", required=True)
):
    """[管理员] 立即结束抽奖并从当前参与者中随机抽取。"""
    await interaction.response.defer(ephemeral=True)

    # --- 解析消息 ID 和频道 ID ---
    message_id = None; channel_id = None
    try:
        link_parts = message_link_or_id.strip().split('/')
        if len(link_parts) >= 3 and link_parts[-3] == 'channels':
            if int(link_parts[-3]) != interaction.guild.id: await interaction.followup.send("错误：链接来自其他服务器。", ephemeral=True); return
            message_id = int(link_parts[-1]); channel_id = int(link_parts[-2])
        else: await interaction.followup.send("请提供完整的消息链接。", ephemeral=True); return
    except ValueError: await interaction.followup.send("无效的消息链接格式。", ephemeral=True); return

    # --- 获取频道和消息 ---
    target_channel = bot.get_channel(channel_id)
    if not target_channel or not isinstance(target_channel, nextcord.TextChannel) or target_channel.guild.id != interaction.guild.id:
        await interaction.followup.send("无法找到指定频道或链接无效。", ephemeral=True); return
    try: message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound: await interaction.followup.send("无法找到原始抽奖消息。", ephemeral=True); return
    except nextcord.Forbidden: await interaction.followup.send(f"无权限在 {target_channel.mention} 读取历史记录。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取消息时出错: {e}", ephemeral=True); print(f"Error fetch msg giveaway_end {message_id}: {e}"); return

    # --- 加载抽奖数据 ---
    giveaway_data = await load_giveaway_data(message_id)
    if not giveaway_data:
        if message.embeds and ("结束" in message.embeds[0].title or (message.embeds[0].footer and "已结束" in message.embeds[0].footer.text)): # 更可靠地检查是否结束
             await interaction.followup.send("该抽奖似乎已经结束了。", ephemeral=True)
        else:
             await interaction.followup.send("错误：无法从 Redis 加载此抽奖的数据，可能已被处理或数据丢失。", ephemeral=True)
        return

    # --- 调用核心开奖逻辑 ---
    print(f"用户 {interaction.user} 手动结束抽奖 {message_id}...")
    await process_giveaway_end(message, giveaway_data) # <--- 调用重构的函数

    # --- 清理 Redis 数据 ---
    await delete_giveaway_data(message_id)
    print(f"已手动结束并从 Redis 移除抽奖 {message_id} (end command)。")

    await interaction.followup.send(f"✅ 已手动结束 `{giveaway_data.get('prize', '未知奖品')}` 的抽奖并抽取了获胜者。", ephemeral=True)

@giveaway_end.error
async def end_error(interaction: nextcord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("抱歉，你没有权限执行此命令。", ephemeral=True)
    else: await interaction.response.send_message(f"执行 end 命令出错: {error}", ephemeral=True); print(f"Error in end cmd: {error}")


# --- 后台任务：检查并结束到期的抽奖 (现在调用核心逻辑) ---
@tasks.loop(seconds=15)
async def check_giveaways():
    """定期检查 Redis 中是否有抽奖到期，并进行处理。"""
    if not redis_pool: return

    current_time = datetime.datetime.now(datetime.timezone.utc)
    ended_giveaway_ids = []
    giveaway_ids = await get_all_giveaway_ids()
    if not giveaway_ids: return

    for message_id in giveaway_ids:
        giveaway_data = await load_giveaway_data(message_id)
        if not giveaway_data:
            continue
        # 检查 end_time 是否还是 datetime 对象 (理论上 load_giveaway_data 会处理, 但加一层保险)
        if not isinstance(giveaway_data.get('end_time'), datetime.datetime):
            print(f"警告: 抽奖 {message_id} 的 end_time 格式无效 (非 datetime 对象)。可能数据已损坏或加载失败。跳过。")
            # 可以考虑删除: await delete_giveaway_data(message_id)
            continue

        if giveaway_data['end_time'] <= current_time:
            print(f"抽奖 {message_id} 到期，准备处理...")
            guild = bot.get_guild(giveaway_data['guild_id'])
            if not guild: print(f"未找到服务器 {giveaway_data['guild_id']}。跳过。"); continue
            channel = guild.get_channel(giveaway_data['channel_id'])
            if not channel or not isinstance(channel, nextcord.TextChannel): print(f"未找到频道 {giveaway_data['channel_id']}。跳过。"); continue
            try:
                message = await channel.fetch_message(message_id)
                # --- 调用核心开奖逻辑 ---
                await process_giveaway_end(message, giveaway_data) # <--- 调用重构的函数
                ended_giveaway_ids.append(message_id) # 标记为待删除
            except nextcord.NotFound: print(f"原始消息 {message_id} 未找到 (check_giveaways)。"); ended_giveaway_ids.append(message_id) # 消息没了也要清理数据
            except nextcord.Forbidden: print(f"无法获取消息 {message_id} (check_giveaways 权限不足?)。") # 不清理，可能下次能获取
            except Exception as e: print(f"处理到期抽奖 {message_id} 时出错: {e}") # 暂时不清理，等待下次重试

    if ended_giveaway_ids:
        print(f"正在从 Redis 清理已处理或过期的抽奖: {ended_giveaway_ids}")
        for msg_id in ended_giveaway_ids:
            await delete_giveaway_data(msg_id)

@check_giveaways.before_loop
async def before_check_giveaways():
    """在后台任务循环开始前执行。"""
    await bot.wait_until_ready()
    # setup_redis 现在主要由 on_ready 处理
    print("检查抽奖任务已准备就绪。")

# --- 机器人事件 ---
@bot.event
async def on_ready():
    """当机器人成功连接到 Discord 并准备好时调用。"""
    print("-" * 30)
    print(f'已登录为: {bot.user.name} ({bot.user.id})')
    print(f'Nextcord 版本: {nextcord.__version__}')
    print(f'运行于: {len(bot.guilds)} 个服务器')
    # 确保 Redis 连接在启动任务前完成
    if not redis_pool:
        await setup_redis()

    redis_status = "未知"
    if redis_pool:
        try:
            await redis_pool.ping()
            redis_status = "已连接"
        except Exception as e:
            redis_status = f"连接失败 ({e})" # 显示具体错误
    print(f'Redis 连接池状态: {redis_status}')
    print("-" * 30)

    # 只有在 Redis 确认连接成功后才启动任务
    if redis_status == "已连接":
        if not check_giveaways.is_running():
            check_giveaways.start()
            print("已启动后台检查抽奖任务。")
    else:
        print("警告: 由于 Redis 连接失败，后台检查抽奖任务未启动。")


# --- 运行机器人 ---
if __name__ == "__main__":
    print("正在启动机器人...")
    bot.run(BOT_TOKEN)