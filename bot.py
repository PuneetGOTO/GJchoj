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
    try:
        print(f"正在连接到 Redis: {REDIS_URL}...")
        # 使用 redis.asyncio.from_url 创建异步连接池
        # decode_responses=True 让 Redis 返回字符串而不是字节
        redis_pool = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_pool.ping() # 测试连接
        print("成功连接到 Redis。")
    except Exception as e:
        print(f"致命错误: 无法连接到 Redis: {e}")
        # 如果启动时无法连接 Redis，机器人可能无法正常工作，选择关闭
        await bot.close()

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

async def save_giveaway_data(message_id: int, data: dict):
    """将抽奖数据保存到 Redis。"""
    if not redis_pool: return # 如果 Redis 未连接则跳过
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}" # 构造 Redis 键名
        # 将 datetime 对象转换为 ISO 格式字符串，以便 JSON 序列化
        if isinstance(data.get('end_time'), datetime.datetime):
            # Make datetime timezone-aware if it's naive, assuming UTC
            if data['end_time'].tzinfo is None:
                 data['end_time'] = data['end_time'].replace(tzinfo=datetime.timezone.utc)
            data['end_time_iso'] = data['end_time'].isoformat()

        # 使用 json.dumps 将字典转换为 JSON 字符串并存入 Redis
        await redis_pool.set(key, json.dumps(data))
        # 可选：设置 Redis 键的过期时间，作为自动清理的保险措施
        # ttl_seconds = int((data['end_time'] - datetime.datetime.now(datetime.timezone.utc)).total_seconds()) + 600 # Add 10 min buffer
        # if ttl_seconds > 0:
        #     await redis_pool.expire(key, ttl_seconds)
    except Exception as e:
        print(f"保存抽奖数据 {message_id} 到 Redis 时出错: {e}")

async def load_giveaway_data(message_id: int) -> dict | None:
    """从 Redis 加载抽奖数据。"""
    if not redis_pool: return None
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}"
        data_str = await redis_pool.get(key) # 从 Redis 获取 JSON 字符串
        if data_str:
            data = json.loads(data_str) # 将 JSON 字符串解析回字典
            # 将 ISO 格式字符串转换回 timezone-aware datetime 对象
            if 'end_time_iso' in data:
                data['end_time'] = datetime.datetime.fromisoformat(data['end_time_iso'])
            return data
        return None # 如果键不存在，返回 None
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
        # 使用 keys 命令查找所有以 GIVEAWAY_PREFIX 开头的键
        keys = await redis_pool.keys(f"{GIVEAWAY_PREFIX}*")
        # 从键名中提取消息 ID
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
    # 添加字段显示信息
    embed.add_field(name="<:timer:1198115585629569044> 结束于", value=f"<t:{int(end_time.timestamp())}:R>", inline=True) # Discord 相对时间戳
    embed.add_field(name="<:winner:1198115869403988039> 获奖人数", value=f"`{winners}`", inline=True)
    # 显示参与条件 (如果有)
    if required_role:
        embed.add_field(name="<:requirement:1198116280151654461> 参与条件", value=f"需要拥有 {required_role.mention} 身份组。", inline=False)
    else:
         embed.add_field(name="<:requirement:1198116280151654461> 参与条件", value="`无`", inline=False)
    # 设置页脚，显示发起人和状态
    embed.set_footer(text=f"由 {creator.display_name} 发起 | 状态: {status.upper()}", icon_url=creator.display_avatar.url if creator.display_avatar else None)
    # 设置缩略图 (可选)
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1003591315297738772/1198117400949297172/giveaway-box.png?ex=65bda71e&is=65ab321e&hm=375f317989609026891610d51d14116503d730ffb1ed1f8749f8e8215e911c18&")
    return embed

def update_embed_ended(embed: nextcord.Embed, winner_mentions: str | None, prize: str, participant_count: int):
     """更新 Embed 对象以显示抽奖结束状态。"""
     embed.title = "<:check:1198118533916270644> **抽奖已结束** <:check:1198118533916270644>" # 修改标题
     embed.color = 0x36393F # 修改颜色 (深灰色)
     embed.clear_fields() # 清除旧的字段 (如结束时间、要求)
     # 根据是否有获胜者更新描述和字段
     if winner_mentions:
         embed.description = f"**奖品:** `{prize}`\n\n恭喜以下获奖者！"
         embed.add_field(name="<:winner:1198115869403988039> 获奖者", value=winner_mentions, inline=False)
     else:
         embed.description = f"**奖品:** `{prize}`\n\n本次抽奖没有符合条件的参与者。"
         embed.add_field(name="<:cross:1198118636147118171> 获奖者", value="`无`", inline=False)
     embed.add_field(name="<:members:1198118814719295550> 参与人数", value=f"`{participant_count}`", inline=True) # 显示最终有效参与人数
     # 更新页脚状态
     if embed.footer:
         original_footer_text = embed.footer.text.split('|')[0].strip() # 保留 "由 xxx 发起" 部分
         embed.set_footer(text=f"{original_footer_text} | 状态: 已结束", icon_url=embed.footer.icon_url)
     return embed

# --- 抽奖命令 (使用斜杠命令 /giveaway ...) ---

@bot.slash_command(name="giveaway", description="抽奖活动管理基础命令")
async def giveaway(interaction: nextcord.Interaction):
    # 这个基础命令本身不会被直接执行，用于组织子命令
    pass

@giveaway.subcommand(name="create", description="🎉 发起一个新的抽奖活动！")
async def giveaway_create(
    interaction: nextcord.Interaction,
    duration: str = nextcord.SlashOption(description="持续时间 (例如: 10s, 5m, 1h, 2d)。", required=True),
    winners: int = nextcord.SlashOption(description="获奖者数量。", required=True, min_value=1),
    prize: str = nextcord.SlashOption(description="奖品是什么？", required=True, max_length=200),
    channel: nextcord.abc.GuildChannel = nextcord.SlashOption(
        description="举办抽奖的频道 (默认当前频道)。",
        required=False,
        channel_types=[nextcord.ChannelType.text] # 限制只能选择文字频道
    ),
    required_role: nextcord.Role = nextcord.SlashOption(description="参与所需的身份组 (可选)。", required=False)
):
    """处理 /giveaway create 命令。"""
    await interaction.response.defer(ephemeral=True) # 告知 Discord 正在处理，响应仅发起者可见

    target_channel = channel or interaction.channel # 确定目标频道
    if not isinstance(target_channel, nextcord.TextChannel):
        await interaction.followup.send("错误: 所选频道不是文字频道。", ephemeral=True)
        return

    # 检查机器人权限
    bot_member = interaction.guild.me
    permissions = target_channel.permissions_for(bot_member)
    required_perms = {
        "send_messages": permissions.send_messages,
        "embed_links": permissions.embed_links,
        "add_reactions": permissions.add_reactions,
        "read_message_history": permissions.read_message_history,
        "manage_messages": permissions.manage_messages # Needed for editing later
    }
    missing_perms = [perm for perm, has in required_perms.items() if not has]
    if missing_perms:
        await interaction.followup.send(
            f"错误: 我在 {target_channel.mention} 缺少必要的权限: `{', '.join(missing_perms)}`。",
            ephemeral=True
        )
        return


    delta = parse_duration(duration) # 解析时长
    if delta is None or delta.total_seconds() <= 5: # Minimum duration, e.g., 5 seconds
        await interaction.followup.send("无效或过短的持续时间。请使用如 '10s', '5m', '1h', '2d' (至少5秒)。", ephemeral=True)
        return

    if winners <= 0:
        await interaction.followup.send("获奖者数量必须为 1 或更多。", ephemeral=True)
        return

    end_time = datetime.datetime.now(datetime.timezone.utc) + delta # 计算结束时间

    # 创建初始的抽奖 Embed 消息
    embed = create_giveaway_embed(prize, end_time, winners, interaction.user, required_role)

    try:
        giveaway_message = await target_channel.send(embed=embed) # 发送 Embed 消息
        await giveaway_message.add_reaction("🎉") # 添加参与反应 Emoji
    except nextcord.Forbidden:
        await interaction.followup.send(f"错误: 无法在 {target_channel.mention} 发送消息或添加反应。请检查权限。", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"创建抽奖时发生意外错误: {e}", ephemeral=True)
        print(f"Error creating giveaway: {e}") # Log the error server-side
        return

    # --- 将抽奖信息存入 Redis ---
    giveaway_data = {
        'guild_id': interaction.guild.id,
        'channel_id': target_channel.id,
        'message_id': giveaway_message.id,
        'end_time': end_time, # Stored as datetime object here, will be converted in save func
        'winners': winners,
        'prize': prize,
        'required_role_id': required_role.id if required_role else None,
        'creator_id': interaction.user.id,
        'creator_name': interaction.user.display_name # For display purposes
    }
    await save_giveaway_data(giveaway_message.id, giveaway_data)

    # 回复发起者，告知抽奖已成功创建
    await interaction.followup.send(f"✅ 奖品为 `{prize}` 的抽奖已在 {target_channel.mention} 创建！ 结束于: <t:{int(end_time.timestamp())}:F>", ephemeral=True)


@giveaway.subcommand(name="reroll", description="<:reroll:1198121147395555328> 为指定的抽奖重新抽取获胜者。")
async def giveaway_reroll(
    interaction: nextcord.Interaction,
    message_link_or_id: str = nextcord.SlashOption(description="要重抽的抽奖的消息 ID 或消息链接。", required=True)
):
    """处理 /giveaway reroll 命令。"""
    await interaction.response.defer(ephemeral=True)

    message_id = None
    channel_id = None

    # 尝试解析消息链接
    try:
        link_parts = message_link_or_id.strip().split('/')
        if len(link_parts) >= 3 and link_parts[-3] == 'channels':
            # Ensure guild matches
            if int(link_parts[-3]) != interaction.guild.id:
                 await interaction.followup.send("错误：提供的消息链接来自另一个服务器。", ephemeral=True)
                 return
            message_id = int(link_parts[-1])
            channel_id = int(link_parts[-2])
    except ValueError:
        # 不是有效链接，尝试当作 ID 处理
        pass

    # 如果不是链接，尝试当作纯 ID
    if message_id is None:
        try:
            message_id = int(message_link_or_id.strip())
            # 要求用户必须提供链接，因为仅凭 ID 无法可靠地找到频道
            # channel_id = interaction.channel_id # 不可靠的假设
            await interaction.followup.send("请提供完整的消息链接 (右键点击消息 -> 复制消息链接) 以进行重抽。", ephemeral=True)
            return
        except ValueError:
            await interaction.followup.send("无效的消息 ID 或链接格式。", ephemeral=True)
            return

    # --- 获取原始消息和数据 ---
    target_channel = bot.get_channel(channel_id)
    if not target_channel or not isinstance(target_channel, nextcord.TextChannel) or target_channel.guild.id != interaction.guild.id:
        await interaction.followup.send("无法找到指定频道，或消息链接无效/来自其他服务器。", ephemeral=True)
        return

    # --- 修正这里的缩进 ---
    try:
        message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound:
        # 正确：比 except 多一级缩进
        await interaction.followup.send("无法找到原始的抽奖消息。", ephemeral=True)
        return
    except nextcord.Forbidden:
        # 正确：比 except 多一级缩进
        await interaction.followup.send(f"我没有权限在 {target_channel.mention} 读取消息历史记录。", ephemeral=True)
        return
    # --- 修正结束 ---
    except Exception as e:
         # 正确：比 except 多一级缩进
        await interaction.followup.send(f"获取原始消息时发生错误: {e}", ephemeral=True)
        print(f"Error fetching message for reroll {message_id}: {e}")
        return

    if not message.embeds:
        await interaction.followup.send("此消息似乎不是有效的抽奖消息（缺少 Embed）。", ephemeral=True)
        return

    original_embed = message.embeds[0]

    # --- 尝试恢复抽奖设置 (优先从 Redis) ---
    giveaway_data = await load_giveaway_data(message_id)
    prize = "未知奖品" # Default
    winners_count = 1 # Default
    required_role_id = None

    if giveaway_data:
        print(f"为 {message_id} 进行重抽时，从 Redis 中找到数据")
        winners_count = giveaway_data.get('winners', 1)
        required_role_id = giveaway_data.get('required_role_id')
        prize = giveaway_data.get('prize', prize) # Update prize if found in data
    else:
        # 如果 Redis 没有数据 (可能被清理了)，尝试从消息 Embed 中解析 (可靠性较低)
        print(f"警告: 未在 Redis 中找到抽奖 {message_id} 的数据。将尝试从消息内容解析。")
        if original_embed.description:
            prize_line = next((line for line in original_embed.description.split('\n') if line.lower().startswith('**prize:**')), None)
            if prize_line:
                 try: prize = prize_line.split('`')[1]
                 except IndexError: pass
        # 尝试从 Embed 字段解析 (需要字段名固定)
        winner_field = next((f for f in original_embed.fields if f.name and "获奖者" in f.name), None) # Example field name
        if winner_field and winner_field.value:
             try: winners_count = int(winner_field.value.strip('`'))
             except (ValueError, TypeError): pass
        req_field = next((f for f in original_embed.fields if f.name and "条件" in f.name), None) # Example field name
        if req_field and req_field.value and "<@&" in req_field.value:
             try: required_role_id = int(req_field.value.split('<@&')[1].split('>')[0])
             except (IndexError, ValueError): pass
        print(f"从 Embed 解析到的数据 - Prize: {prize}, Winners: {winners_count}, RoleID: {required_role_id}")


    # --- 执行重抽逻辑 ---
    reaction = nextcord.utils.get(message.reactions, emoji="🎉") # 获取 🎉 反应
    if reaction is None:
        await interaction.followup.send("消息上未找到 '🎉' 反应。", ephemeral=True)
        return

    # 获取所有点击了 🎉 的成员 (需要成员意图)
    try:
        potential_participants = [
            member async for member in reaction.users()
            if isinstance(member, nextcord.Member) # Ensure they are still in the server and not a bot
        ]
    except nextcord.Forbidden:
        await interaction.followup.send("错误: 我需要 '服务器成员意图' 权限来获取成员信息以进行身份组检查。", ephemeral=True)
        return
    except Exception as e:
         await interaction.followup.send(f"获取反应用户时发生错误: {e}", ephemeral=True)
         print(f"Error getting reaction users for reroll {message_id}: {e}")
         return

    # 根据身份组要求筛选参与者
    eligible_participants = []
    required_role = None
    if required_role_id:
        required_role = interaction.guild.get_role(required_role_id)

    if required_role:
        print(f"为重抽筛选身份组: {required_role.name}")
        for member in potential_participants:
            if required_role in member.roles:
                eligible_participants.append(member)
            # else: print(f"Debug Reroll: {member.name} lacks role {required_role.name}")
    else:
        eligible_participants = potential_participants

    if not eligible_participants:
        await interaction.followup.send("未找到符合条件的参与者进行重抽。", ephemeral=True)
        await target_channel.send(f"尝试为 `{prize}` 的抽奖进行重抽，但未找到符合条件的参与者 (点击了 🎉 并满足要求)。")
        return

    num_to_reroll = min(winners_count, len(eligible_participants)) # 确定重抽人数
    if num_to_reroll <= 0:
         await interaction.followup.send("无法重抽 0 位获胜者。", ephemeral=True)
         return

    new_winners = random.sample(eligible_participants, num_to_reroll) # 随机抽取新获胜者
    new_winner_mentions = ", ".join([w.mention for w in new_winners]) # 格式化提及 (@)

    # 在频道中宣布新的获胜者
    await target_channel.send(f"<:reroll:1198121147395555328> **重新抽奖！** <:reroll:1198121147395555328>\n恭喜 `{prize}` 的新获奖者: {new_winner_mentions}", allowed_mentions=nextcord.AllowedMentions(users=True))

    # 可选：再次编辑原始抽奖消息，显示新的获胜者
    try:
        updated_embed = update_embed_ended(original_embed, new_winner_mentions, prize, len(eligible_participants))
        await message.edit(embed=updated_embed)
    except nextcord.Forbidden:
        print(f"无法编辑原始消息 {message_id} (重抽后更新) (权限不足?)。")
    except Exception as e:
        print(f"重抽后编辑消息 {message_id} 时出错: {e}")


    # 回复发起者，告知重抽成功
    await interaction.followup.send(f"✅ 已在 {target_channel.mention} 为 `{prize}` 重新抽取获胜者。新获奖者: {new_winner_mentions}", ephemeral=True)


# --- 后台任务：检查并结束到期的抽奖 ---
@tasks.loop(seconds=15) # 设置任务循环间隔 (例如每 15 秒检查一次)
async def check_giveaways():
    """定期检查 Redis 中是否有抽奖到期，并进行处理。"""
    if not redis_pool:
        # print("Redis pool not available, skipping giveaway check.") # Less noisy
        return

    # print("Checking for ended giveaways...") # Debug logging
    current_time = datetime.datetime.now(datetime.timezone.utc) # 获取当前 UTC 时间
    ended_giveaway_ids = [] # 存储本轮需要处理的已结束抽奖 ID

    giveaway_ids = await get_all_giveaway_ids() # 获取 Redis 中所有抽奖的 ID
    if not giveaway_ids: return # No giveaways active, exit early

    # print(f"Found {len(giveaway_ids)} potential giveaways in Redis.") # Debug

    # 遍历所有 ID
    for message_id in giveaway_ids:
        giveaway_data = await load_giveaway_data(message_id) # 加载抽奖数据

        if not giveaway_data:
            print(f"抽奖 {message_id} 的数据在处理前从 Redis 消失。")
            await delete_giveaway_data(message_id) # 清理可能损坏的键
            continue

        # 确保 end_time 是 datetime 对象
        if not isinstance(giveaway_data.get('end_time'), datetime.datetime):
             print(f"警告: 抽奖 {message_id} 的结束时间格式无效。跳过。")
             # Consider deleting if data is consistently bad
             await delete_giveaway_data(message_id) # Clean up bad data
             continue

        # 检查是否到期
        if giveaway_data['end_time'] <= current_time:
            print(f"抽奖 {message_id} (奖品: {giveaway_data.get('prize', 'N/A')}) 已到期。正在处理...")
            ended_giveaway_ids.append(message_id) # 加入待处理列表

            # --- 获取必要的 Discord 对象 ---
            guild = bot.get_guild(giveaway_data['guild_id'])
            if not guild:
                print(f"未找到抽奖 {message_id} 的服务器 {giveaway_data['guild_id']}。跳过。")
                continue # Cannot process without guild

            channel = guild.get_channel(giveaway_data['channel_id'])
            if not channel or not isinstance(channel, nextcord.TextChannel):
                print(f"未找到抽奖 {message_id} 的频道 {giveaway_data['channel_id']} 或不是文字频道。跳过。")
                continue

            try:
                message = await channel.fetch_message(message_id) # 获取原始抽奖消息
            except nextcord.NotFound:
                print(f"抽奖的原始消息 {message_id} 未找到。无法处理。")
                continue # 消息被删了，跳过
            except nextcord.Forbidden:
                print(f"无法获取频道 {channel.id} 中的消息 {message_id} (权限不足?)。跳过。")
                continue
            except Exception as e:
                 print(f"获取消息 {message_id} 时发生错误: {e}。跳过。")
                 continue


            # --- 开奖逻辑 ---
            reaction = nextcord.utils.get(message.reactions, emoji="🎉") # 获取 🎉 反应
            potential_participants = []
            if reaction:
                try:
                    # 获取所有点击了 🎉 的成员 (需要成员意图)
                    potential_participants = [
                        member async for member in reaction.users()
                        if isinstance(member, nextcord.Member) # Must be member to check roles
                    ]
                except nextcord.Forbidden:
                     print(f"无法获取消息 {message_id} 的反应者成员列表 (缺少成员意图/权限?)。假设无参与者。")
                except Exception as e:
                     print(f"获取抽奖 {message_id} 的反应用户时发生错误: {e}。假设无参与者。")
            else:
                 print(f"消息 {message_id} 上无 🎉 反应。")


            # 根据身份组要求筛选参与者
            eligible_participants = []
            required_role_id = giveaway_data.get('required_role_id')
            required_role = None
            if required_role_id:
                required_role = guild.get_role(required_role_id)

            if required_role:
                 # print(f"筛选参与者，需要身份组 {required_role.name} (ID: {required_role.id})")
                 for member in potential_participants:
                     if required_role in member.roles:
                         eligible_participants.append(member)
                     # else: print(f"Debug End: {member.name} lacks role {required_role.name}")
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
                    print(f"抽奖 {message_id} 选出的获胜者: {[w.name for w in winners]}")

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
                 print(f"发送抽奖 {message_id} 获奖公告时发生错误: {e}")


            # --- 更新原始抽奖消息的 Embed ---
            if message.embeds:
                try:
                    # 使用 update_embed_ended 函数更新 Embed
                    updated_embed = update_embed_ended(
                        message.embeds[0],
                        winner_mentions,
                        giveaway_data['prize'],
                        participant_count # Pass the count of eligible participants
                    )
                    # 编辑原始消息，移除组件 (如果未来添加按钮的话)
                    await message.edit(embed=updated_embed, view=None)
                except nextcord.Forbidden:
                     print(f"无法编辑原始抽奖消息 {message_id} (权限不足?)。")
                except nextcord.NotFound:
                     print(f"原始抽奖消息 {message_id} 在编辑前消失。")
                except Exception as e:
                     print(f"编辑原始抽奖消息 {message_id} 时出错: {e}")
            else:
                 print(f"原始抽奖消息 {message_id} 没有 Embed 可更新。")


    # --- 清理 Redis ---
    # 在处理完所有检查后，统一删除本轮已结束的抽奖数据
    if ended_giveaway_ids:
        print(f"正在从 Redis 清理已结束的抽奖: {ended_giveaway_ids}")
        for msg_id in ended_giveaway_ids:
            await delete_giveaway_data(msg_id)
            # print(f"已从 Redis 移除结束的抽奖 {msg_id}。") # Less noisy


@check_giveaways.before_loop
async def before_check_giveaways():
    """在后台任务循环开始前执行。"""
    await bot.wait_until_ready() # 等待机器人连接成功
    await setup_redis() # 确保 Redis 已连接
    print("检查抽奖任务已准备就绪。")

# --- 机器人事件 ---
@bot.event
async def on_ready():
    """当机器人成功连接到 Discord 并准备好时调用。"""
    print("-" * 30)
    print(f'已登录为: {bot.user.name} ({bot.user.id})')
    print(f'Nextcord 版本: {nextcord.__version__}')
    print(f'运行于: {len(bot.guilds)} 个服务器')
    print(f'Redis 连接池状态: {"已连接" if redis_pool and redis_pool.connection else "未连接"}')
    print("-" * 30)
    # 启动后台检查任务 (如果尚未运行)
    if not check_giveaways.is_running():
        check_giveaways.start()
        print("已启动后台检查抽奖任务。")

# --- 运行机器人 ---
if __name__ == "__main__":
    print("正在启动机器人...")
    # 使用你的 Bot Token 运行机器人
    bot.run(BOT_TOKEN)