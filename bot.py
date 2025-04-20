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
    # ... (解析逻辑，将字符串转换为秒/分/时/天) ...
    # 返回一个 timedelta 对象表示时间差，或在格式无效时返回 None

async def save_giveaway_data(message_id: int, data: dict):
    """将抽奖数据保存到 Redis。"""
    if not redis_pool: return # 如果 Redis 未连接则跳过
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}" # 构造 Redis 键名
        # 将 datetime 对象转换为 ISO 格式字符串，以便 JSON 序列化
        if isinstance(data.get('end_time'), datetime.datetime):
            data['end_time_iso'] = data['end_time'].isoformat()
        # 使用 json.dumps 将字典转换为 JSON 字符串并存入 Redis
        await redis_pool.set(key, json.dumps(data))
        # 可选：设置 Redis 键的过期时间，作为自动清理的保险措施
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
            # 将 ISO 格式字符串转换回 datetime 对象
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
    # ... (检查频道类型和机器人权限) ...

    delta = parse_duration(duration) # 解析时长
    # ... (检查时长和获奖人数是否有效) ...

    end_time = datetime.datetime.now(datetime.timezone.utc) + delta # 计算结束时间

    # 创建初始的抽奖 Embed 消息
    embed = create_giveaway_embed(prize, end_time, winners, interaction.user, required_role)

    try:
        giveaway_message = await target_channel.send(embed=embed) # 发送 Embed 消息
        await giveaway_message.add_reaction("🎉") # 添加参与反应 Emoji
    except nextcord.Forbidden:
        await interaction.followup.send(f"错误: 无法在 {target_channel.mention} 发送消息或添加反应。请检查权限。", ephemeral=True)
        return
    # ... (其他错误处理) ...

    # --- 将抽奖信息存入 Redis ---
    giveaway_data = {
        # ... (存储各种抽奖相关信息: 服务器ID, 频道ID, 消息ID, 结束时间, 奖品, 身份组要求等) ...
        'end_time': end_time, # 注意: 实际存入时会被 save_giveaway_data 转为字符串
    }
    await save_giveaway_data(giveaway_message.id, giveaway_data)

    # 回复发起者，告知抽奖已成功创建
    await interaction.followup.send(f"✅ 奖品为 `{prize}` 的抽奖已在 {target_channel.mention} 创建！", ephemeral=True)


@giveaway.subcommand(name="reroll", description="<:reroll:1198121147395555328> 为指定的抽奖重新抽取获胜者。")
async def giveaway_reroll(
    interaction: nextcord.Interaction,
    message_link_or_id: str = nextcord.SlashOption(description="要重抽的抽奖的消息 ID 或消息链接。", required=True)
):
    """处理 /giveaway reroll 命令。"""
    await interaction.response.defer(ephemeral=True)

    # ... (解析 message_link_or_id 获取 message_id 和 channel_id) ...
    # 推荐使用消息链接，因为它包含了频道 ID，否则很难确定消息在哪

    # --- 获取原始消息和数据 ---
    target_channel = bot.get_channel(channel_id)
    # ... (检查频道是否存在和有效) ...
    try:
        message = await target_channel.fetch_message(message_id) # 获取原始抽奖消息
    except nextcord.NotFound:
        # ... (处理消息未找到的情况) ...
    # ... (其他错误处理) ...

    # --- 尝试恢复抽奖设置 (优先从 Redis) ---
    giveaway_data = await load_giveaway_data(message_id)
    if giveaway_data:
        # 如果 Redis 中还有数据 (可能刚结束还没被清理)，使用它
        winners_count = giveaway_data.get('winners', 1)
        required_role_id = giveaway_data.get('required_role_id')
        prize = giveaway_data.get('prize', "未知奖品")
    else:
        # 如果 Redis 没有数据 (可能被清理了)，尝试从消息 Embed 中解析 (可靠性较低)
        print(f"警告: 未在 Redis 中找到抽奖 {message_id} 的数据。将尝试从消息内容解析。")
        # ... (从 message.embeds[0] 中解析奖品、原获奖人数、身份组要求等信息) ...
        # 这部分比较复杂，且依赖于 Embed 的格式没有大变

    # --- 执行重抽逻辑 ---
    reaction = nextcord.utils.get(message.reactions, emoji="🎉") # 获取 🎉 反应
    # ... (检查反应是否存在) ...

    try:
        # 获取所有点击了 🎉 的成员 (需要成员意图)
        potential_participants = [member async for member in reaction.users() if isinstance(member, nextcord.Member)]
    except nextcord.Forbidden:
        # ... (处理缺少成员意图或权限的错误) ...
        return

    # 根据身份组要求筛选参与者
    eligible_participants = []
    required_role = None
    if required_role_id:
        required_role = interaction.guild.get_role(required_role_id)
    if required_role:
        # ... (筛选拥有 required_role 的成员) ...
    else:
        eligible_participants = potential_participants

    # ... (检查是否有足够的合格参与者) ...

    num_to_reroll = min(winners_count, len(eligible_participants)) # 确定重抽人数
    # ... (检查重抽人数是否大于 0) ...

    new_winners = random.sample(eligible_participants, num_to_reroll) # 随机抽取新获胜者
    new_winner_mentions = ", ".join([w.mention for w in new_winners]) # 格式化提及 (@)

    # 在频道中宣布新的获胜者
    await target_channel.send(f"<:reroll:1198121147395555328> 重新抽奖！ <:reroll:1198121147395555328>\n恭喜 `{prize}` 的新获奖者: {new_winner_mentions}")

    # 可选：再次编辑原始抽奖消息，显示新的获胜者
    if message.embeds:
         updated_embed = update_embed_ended(message.embeds[0], new_winner_mentions, prize, len(eligible_participants))
         try:
             await message.edit(embed=updated_embed)
         except Exception as e:
             print(f"重抽后编辑消息 {message_id} 时出错: {e}")

    # 回复发起者，告知重抽成功
    await interaction.followup.send(f"✅ 已在 {target_channel.mention} 为 `{prize}` 重新抽取获胜者。新获奖者: {new_winner_mentions}", ephemeral=True)

# --- 后台任务：检查并结束到期的抽奖 ---
@tasks.loop(seconds=15) # 设置任务循环间隔 (例如每 15 秒检查一次)
async def check_giveaways():
    """定期检查 Redis 中是否有抽奖到期，并进行处理。"""
    if not redis_pool: return # Redis 未连接则跳过

    current_time = datetime.datetime.now(datetime.timezone.utc) # 获取当前 UTC 时间
    ended_giveaway_ids = [] # 存储本轮需要处理的已结束抽奖 ID

    giveaway_ids = await get_all_giveaway_ids() # 获取 Redis 中所有抽奖的 ID

    # 遍历所有 ID
    for message_id in giveaway_ids:
        giveaway_data = await load_giveaway_data(message_id) # 加载抽奖数据
        if not giveaway_data: continue # 数据丢失则跳过

        # 确保 end_time 是 datetime 对象
        if not isinstance(giveaway_data.get('end_time'), datetime.datetime):
            print(f"警告: 抽奖 {message_id} 的结束时间格式无效。跳过。")
            continue

        # 检查是否到期
        if giveaway_data['end_time'] <= current_time:
            print(f"抽奖 {message_id} (奖品: {giveaway_data.get('prize', 'N/A')}) 已到期。正在处理...")
            ended_giveaway_ids.append(message_id) # 加入待处理列表

            # --- 获取必要的 Discord 对象 ---
            guild = bot.get_guild(giveaway_data['guild_id'])
            # ... (检查服务器是否存在) ...
            channel = guild.get_channel(giveaway_data['channel_id'])
            # ... (检查频道是否存在且为文字频道) ...
            try:
                message = await channel.fetch_message(message_id) # 获取原始抽奖消息
            except nextcord.NotFound:
                print(f"抽奖的原始消息 {message_id} 未找到。无法处理。")
                continue # 消息被删了，跳过
            # ... (处理权限错误) ...

            # --- 开奖逻辑 ---
            reaction = nextcord.utils.get(message.reactions, emoji="🎉") # 获取 🎉 反应
            potential_participants = []
            if reaction:
                try:
                    # 获取所有点击了 🎉 的成员 (需要成员意图)
                    potential_participants = [member async for member in reaction.users() if isinstance(member, nextcord.Member)]
                except nextcord.Forbidden:
                     print(f"无法获取消息 {message_id} 的反应者成员列表 (缺少成员意图/权限?)。假设无参与者。")
                # ... (其他错误处理) ...

            # 根据身份组要求筛选参与者
            eligible_participants = []
            required_role_id = giveaway_data.get('required_role_id')
            # ... (筛选逻辑，同 reroll 部分) ...

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
            # ... (其他错误处理) ...

            # --- 更新原始抽奖消息的 Embed ---
            if message.embeds:
                try:
                    # 使用 update_embed_ended 函数更新 Embed
                    updated_embed = update_embed_ended(
                        message.embeds[0],
                        winner_mentions,
                        giveaway_data['prize'],
                        participant_count
                    )
                    # 编辑原始消息，移除组件 (如果未来添加按钮的话)
                    await message.edit(embed=updated_embed, view=None)
                except Exception as e:
                     print(f"编辑原始抽奖消息 {message_id} 时出错: {e}")

    # --- 清理 Redis ---
    # 在处理完所有检查后，统一删除本轮已结束的抽奖数据
    for msg_id in ended_giveaway_ids:
        await delete_giveaway_data(msg_id)
        print(f"已从 Redis 移除结束的抽奖 {msg_id}。")

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