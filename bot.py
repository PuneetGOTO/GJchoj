# --- 导入必要的库 ---
import nextcord
from nextcord.ext import commands, tasks
import datetime
import random
import asyncio
import os
import json
import redis.asyncio as redis
from urllib.parse import urlparse

# --- 配置 ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
REDIS_URL = os.environ.get('REDIS_URL')
if not BOT_TOKEN: print("错误: 未设置 BOT_TOKEN 环境变量。"); exit()
if not REDIS_URL: print("错误: 未设置 REDIS_URL 环境变量。请确保已链接 Redis 服务。"); exit()

# --- Bot Intents ---
intents = nextcord.Intents.default()
intents.guilds = True
intents.members = True
intents.reactions = True
bot = commands.Bot(intents=intents)

# --- Redis 连接 ---
redis_pool = None
async def setup_redis():
    global redis_pool
    redis_url_to_use = os.environ.get('REDIS_URL')
    if not redis_url_to_use: print("错误: 在 setup_redis 中未能获取 REDIS_URL 环境变量！"); await bot.close(); return
    try:
        print(f"正在连接到 Redis: {redis_url_to_use}...")
        redis_pool = redis.from_url(redis_url_to_use, decode_responses=True)
        await redis_pool.ping(); print("成功连接到 Redis。")
    except Exception as e: print(f"致命错误: 无法连接到 Redis: {e}"); await bot.close()

# --- 辅助函数 ---
GIVEAWAY_PREFIX = "giveaway:"

def parse_duration(duration_str: str) -> datetime.timedelta | None:
    # ... (代码不变) ...
    duration_str = duration_str.lower().strip(); value_str = ""; unit = ""
    for char in duration_str:
        if char.isdigit() or char == '.': value_str += char
        else: unit += char
    if not value_str or not unit: return None
    try:
        value = float(value_str)
        if unit == 's': return datetime.timedelta(seconds=value)
        elif unit == 'm': return datetime.timedelta(minutes=value)
        elif unit == 'h': return datetime.timedelta(hours=value)
        elif unit == 'd': return datetime.timedelta(days=value)
        else: return None
    except ValueError: return None

async def save_giveaway_data(message_id: int, data: dict):
    # ... (代码不变, 使用修正后的版本) ...
    if not redis_pool: return
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}"
        data_to_save = data.copy()
        if isinstance(data_to_save.get('end_time'), datetime.datetime):
            if data_to_save['end_time'].tzinfo is None:
                 data_to_save['end_time'] = data_to_save['end_time'].replace(tzinfo=datetime.timezone.utc)
            data_to_save['end_time'] = data_to_save['end_time'].isoformat()
        await redis_pool.set(key, json.dumps(data_to_save))
    except TypeError as e: print(f"保存抽奖数据 {message_id} 到 Redis 时出错 (序列化失败): {e}")
    except Exception as e: print(f"保存抽奖数据 {message_id} 到 Redis 时发生其他错误: {e}")


async def load_giveaway_data(message_id: int) -> dict | None:
    # ... (代码不变, 使用修正后的版本) ...
    if not redis_pool: return None
    try:
        key = f"{GIVEAWAY_PREFIX}{message_id}"
        data_str = await redis_pool.get(key)
        if data_str:
            data = json.loads(data_str)
            if isinstance(data.get('end_time'), str):
                try: data['end_time'] = datetime.datetime.fromisoformat(data['end_time'])
                except ValueError: print(f"警告: 抽奖 {message_id} 的 end_time 格式无效 (非 ISO string?)。")
            return data
        return None
    except json.JSONDecodeError: print(f"从 Redis 解码抽奖 {message_id} 的 JSON 时出错。"); return None
    except Exception as e: print(f"从 Redis 加载抽奖数据 {message_id} 时出错: {e}"); return None

async def delete_giveaway_data(message_id: int):
    # ... (代码不变) ...
    if not redis_pool: return
    try: key = f"{GIVEAWAY_PREFIX}{message_id}"; await redis_pool.delete(key)
    except Exception as e: print(f"从 Redis 删除抽奖数据 {message_id} 时出错: {e}")

async def get_all_giveaway_ids() -> list[int]:
    # ... (代码不变) ...
     if not redis_pool: return []
     try: keys = await redis_pool.keys(f"{GIVEAWAY_PREFIX}*"); return [int(k.split(':')[-1]) for k in keys]
     except Exception as e: print(f"从 Redis 获取抽奖键时出错: {e}"); return []

# --- 新增：消息链接解析辅助函数 ---
async def parse_message_link(interaction: nextcord.Interaction, link_or_id: str) -> tuple[int | None, int | None]:
    """
    解析 Discord 消息链接。
    成功返回 (channel_id, message_id)，失败则发送错误消息并返回 (None, None)。
    """
    message_id = None
    channel_id = None
    guild_id_from_link = None

    try:
        link_parts = link_or_id.strip().split('/')
        # 检查标准链接结构: https://discord.com/channels/GUILD/CHANNEL/MESSAGE
        # 修正了这里的检查逻辑
        if len(link_parts) == 7 and link_parts[0] == 'https:' and link_parts[2] == 'discord.com' and link_parts[3] == 'channels':
            try:
                guild_id_from_link = int(link_parts[4])
                channel_id = int(link_parts[5])
                message_id = int(link_parts[6])

                # 确认链接来自当前服务器
                if guild_id_from_link != interaction.guild.id:
                    # 使用 followup 发送临时消息
                    await interaction.followup.send("错误：提供的消息链接来自另一个服务器。", ephemeral=True)
                    return None, None
            except ValueError:
                # 如果 ID 部分不是数字
                await interaction.followup.send("无效的消息链接格式 (ID部分非数字)。", ephemeral=True)
                return None, None
        else:
            # 如果不符合标准链接结构，则提示需要链接
            await interaction.followup.send("请提供格式正确的 Discord 消息链接 (例如: https://discord.com/channels/...).", ephemeral=True)
            return None, None
    except Exception as e:
        # 其他解析错误
        await interaction.followup.send(f"解析链接时发生意外错误: {e}", ephemeral=True)
        print(f"Error parsing link {link_or_id}: {e}")
        return None, None

    # 如果一切正常，返回解析出的 ID
    return channel_id, message_id

# --- 科技感 Embed 消息函数 ---
def create_giveaway_embed(prize: str, end_time: datetime.datetime, winners: int, creator: nextcord.User | nextcord.Member, required_role: nextcord.Role | None, status: str = "running"):
    # ... (代码不变) ...
    embed=nextcord.Embed(title="<a:_:1198114874891632690> **赛博抽奖进行中!** <a:_:1198114874891632690>", description=f"点击 🎉 表情参与!\n\n**奖品:** `{prize}`", color=0x00FFFF); embed.add_field(name="<:timer:1198115585629569044> 结束于", value=f"<t:{int(end_time.timestamp())}:R>", inline=True); embed.add_field(name="<:winner:1198115869403988039> 获奖人数", value=f"`{winners}`", inline=True);
    if required_role: embed.add_field(name="<:requirement:1198116280151654461> 参与条件", value=f"需要拥有 {required_role.mention} 身份组。", inline=False); else: embed.add_field(name="<:requirement:1198116280151654461> 参与条件", value="`无`", inline=False);
    embed.set_footer(text=f"由 {creator.display_name} 发起 | 状态: {status.upper()}", icon_url=creator.display_avatar.url if creator.display_avatar else None); embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1003591315297738772/1198117400949297172/giveaway-box.png?ex=65bda71e&is=65ab321e&hm=375f317989609026891610d51d14116503d730ffb1ed1f8749f8e8215e911c18&"); return embed

def update_embed_ended(embed: nextcord.Embed, winner_mentions: str | None, prize: str, participant_count: int):
     # ... (代码不变) ...
     embed.title="<:check:1198118533916270644> **抽奖已结束** <:check:1198118533916270644>"; embed.color=0x36393F; embed.clear_fields();
     if winner_mentions: embed.description=f"**奖品:** `{prize}`\n\n恭喜以下获奖者！"; embed.add_field(name="<:winner:1198115869403988039> 获奖者", value=winner_mentions, inline=False);
     else: embed.description=f"**奖品:** `{prize}`\n\n本次抽奖没有符合条件的参与者。"; embed.add_field(name="<:cross:1198118636147118171> 获奖者", value="`无`", inline=False);
     embed.add_field(name="<:members:1198118814719295550> 参与人数", value=f"`{participant_count}`", inline=True);
     if embed.footer: original_footer_text=embed.footer.text.split('|')[0].strip(); embed.set_footer(text=f"{original_footer_text} | 状态: 已结束", icon_url=embed.footer.icon_url);
     return embed

# --- 核心开奖逻辑函数 ---
async def process_giveaway_end(message: nextcord.Message, giveaway_data: dict):
    # ... (代码不变) ...
    guild = message.guild; channel = message.channel; bot_instance = bot
    if not guild or not channel or not isinstance(channel, nextcord.TextChannel): print(f"错误: process_giveaway_end 参数无效 (消息 ID: {message.id})"); return
    print(f"正在处理抽奖结束: {message.id} (奖品: {giveaway_data.get('prize', 'N/A')})")
    reaction = nextcord.utils.get(message.reactions, emoji="🎉"); potential_participants = []
    if reaction:
        try: potential_participants = [m async for m in reaction.users() if isinstance(m, nextcord.Member)]
        except nextcord.Forbidden: print(f"无法获取消息 {message.id} 反应者 (权限不足?)。")
        except Exception as e: print(f"获取抽奖 {message.id} 反应用户出错: {e}。")
    else: print(f"消息 {message.id} 无 🎉 反应。")
    eligible_participants = []; required_role_id = giveaway_data.get('required_role_id'); required_role = None
    if required_role_id: required_role = guild.get_role(required_role_id)
    if required_role: eligible_participants = [m for m in potential_participants if required_role in m.roles]
    else: eligible_participants = potential_participants
    winners = []; winner_mentions = None; participant_count = len(eligible_participants)
    if eligible_participants:
        num_winners = min(giveaway_data['winners'], len(eligible_participants))
        if num_winners > 0: winners = random.sample(eligible_participants, num_winners); winner_mentions = ", ".join([w.mention for w in winners]); print(f"抽奖 {message.id} 获胜者: {[w.name for w in winners]}")
    result_message = f"<a:_:1198114874891632690> **抽奖结束！** <...>\n奖品: `{giveaway_data['prize']}`\n";
    if winner_mentions: result_message += f"\n恭喜 {winner_mentions}！"
    else: result_message += "\n可惜，本次抽奖没有符合条件的获奖者。"
    try: await channel.send(result_message, allowed_mentions=nextcord.AllowedMentions(users=True))
    except Exception as e: print(f"发送抽奖 {message.id} 获奖公告出错: {e}")
    if message.embeds:
        try: updated_embed = update_embed_ended(message.embeds[0], winner_mentions, giveaway_data['prize'], participant_count); await message.edit(embed=updated_embed, view=None)
        except Exception as e: print(f"编辑抽奖 {message.id} 消息出错: {e}")
    else: print(f"抽奖 {message.id} 无 Embed 可更新。")

# --- 抽奖命令 ---
@bot.slash_command(name="giveaway", description="抽奖活动管理基础命令")
async def giveaway(interaction: nextcord.Interaction): pass

@giveaway.subcommand(name="create", description="🎉 发起一个新的抽奖活动！")
async def giveaway_create(interaction: nextcord.Interaction, duration: str = ..., winners: int = ..., prize: str = ..., channel: nextcord.abc.GuildChannel = None, required_role: nextcord.Role = None):
    # ... (代码不变) ...
    await interaction.response.defer(ephemeral=True); target_channel = channel or interaction.channel
    if not isinstance(target_channel, nextcord.TextChannel): await interaction.followup.send("错误: 非文字频道。", ephemeral=True); return
    bot_member=interaction.guild.me; permissions=target_channel.permissions_for(bot_member); required_perms={"send_messages": permissions.send_messages, "embed_links": permissions.embed_links, "add_reactions": permissions.add_reactions, "read_message_history": permissions.read_message_history, "manage_messages": permissions.manage_messages}; missing_perms=[p for p,h in required_perms.items() if not h]
    if missing_perms: await interaction.followup.send(f"错误: 缺少权限: `{', '.join(missing_perms)}`。", ephemeral=True); return
    delta=parse_duration(duration);
    if delta is None or delta.total_seconds() <= 5: await interaction.followup.send("无效时长。", ephemeral=True); return
    if winners <= 0: await interaction.followup.send("获奖人数需>=1。", ephemeral=True); return
    end_time=datetime.datetime.now(datetime.timezone.utc) + delta; embed=create_giveaway_embed(prize, end_time, winners, interaction.user, required_role)
    try: giveaway_message = await target_channel.send(embed=embed); await giveaway_message.add_reaction("🎉")
    except Exception as e: await interaction.followup.send(f"创建抽奖出错: {e}", ephemeral=True); print(f"Error creating giveaway: {e}"); return
    giveaway_data={'guild_id': interaction.guild.id, 'channel_id': target_channel.id, 'message_id': giveaway_message.id, 'end_time': end_time, 'winners': winners, 'prize': prize, 'required_role_id': required_role.id if required_role else None, 'creator_id': interaction.user.id, 'creator_name': interaction.user.display_name}
    await save_giveaway_data(giveaway_message.id, giveaway_data)
    await interaction.followup.send(f"✅ `{prize}` 抽奖已在 {target_channel.mention} 创建！结束于: <t:{int(end_time.timestamp())}:F>", ephemeral=True)

@giveaway.subcommand(name="reroll", description="<:reroll:1198121147395555328> 重新抽取获胜者。")
@commands.has_permissions(manage_guild=True)
async def giveaway_reroll(interaction: nextcord.Interaction, message_link_or_id: str = ...):
    await interaction.response.defer(ephemeral=True)
    # --- 使用辅助函数解析 ---
    channel_id, message_id = await parse_message_link(interaction, message_link_or_id)
    if channel_id is None or message_id is None: return
    # --- 获取频道和消息 ---
    target_channel = bot.get_channel(channel_id)
    if not target_channel: await interaction.followup.send("错误：无法找到链接中的频道。", ephemeral=True); return
    try: message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound: await interaction.followup.send("无法找到原始抽奖消息。", ephemeral=True); return
    except nextcord.Forbidden: await interaction.followup.send(f"无权限在 {target_channel.mention} 读取历史记录。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取消息时出错: {e}", ephemeral=True); print(f"Error fetch msg reroll {message_id}: {e}"); return
    # ... (后续 Reroll 逻辑不变) ...
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

@giveaway_reroll.error
async def reroll_error(interaction: nextcord.Interaction, error):
    # ... (代码不变) ...
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("抱歉，你没有权限执行此命令。", ephemeral=True)
    else: await interaction.response.send_message(f"执行 reroll 命令出错: {error}", ephemeral=True); print(f"Error in reroll cmd: {error}")

@giveaway.subcommand(name="pickwinner", description="👑 [管理员] 手动指定中奖者并结束抽奖。")
@commands.has_permissions(manage_guild=True)
async def giveaway_pickwinner(interaction: nextcord.Interaction, message_link_or_id: str = ..., winner1: nextcord.Member = ..., winner2: nextcord.Member = None, winner3: nextcord.Member = None):
    await interaction.response.defer(ephemeral=True)
    # --- 使用辅助函数解析 ---
    channel_id, message_id = await parse_message_link(interaction, message_link_or_id)
    if channel_id is None or message_id is None: return
    # --- 获取频道和消息 ---
    target_channel = bot.get_channel(channel_id)
    if not target_channel: await interaction.followup.send("错误：无法找到链接中的频道。", ephemeral=True); return
    try: message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound: await interaction.followup.send("无法找到原始抽奖消息。", ephemeral=True); return
    except nextcord.Forbidden: await interaction.followup.send(f"无权限在 {target_channel.mention} 读取历史记录。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取消息时出错: {e}", ephemeral=True); print(f"Error fetch msg pickwinner {message_id}: {e}"); return
    # ... (后续 pickwinner 逻辑不变, 使用修正后的 prize 解析) ...
    if not message.embeds: await interaction.followup.send("消息缺少 Embed。", ephemeral=True); return
    original_embed = message.embeds[0]; giveaway_data = await load_giveaway_data(message_id); prize = "未知奖品"
    if giveaway_data: prize = giveaway_data.get('prize', prize)
    else:
        print(f"无法从 Redis 加载抽奖 {message_id} 数据 (pickwinner), 尝试从 Embed 解析奖品...")
        if original_embed.description:
            prize_line = next((line for line in original_embed.description.split('\n') if line.lower().strip().startswith('**prize:**')), None)
            if prize_line:
                try: prize = prize_line.split('`')[1]; print(f"从 Embed 解析到奖品: {prize}")
                except IndexError: print("从 Embed 解析奖品失败: 格式不匹配或缺少反引号。"); pass
    specified_winners = [w for w in [winner1, winner2, winner3] if w is not None]
    if not specified_winners: await interaction.followup.send("错误：必须至少指定一位中奖者。", ephemeral=True); return
    winner_mentions = ", ".join([w.mention for w in specified_winners])
    result_message = f"👑 **抽奖结果指定！** 👑\n奖品: `{prize}`\n\n管理员指定以下用户为中奖者: {winner_mentions}"
    try: await target_channel.send(result_message, allowed_mentions=nextcord.AllowedMentions(users=True))
    except Exception as e: print(f"无法发送 pickwinner 公告 {message_id}: {e}")
    participant_count_display = len(specified_winners)
    try:
        updated_embed = update_embed_ended(original_embed, winner_mentions, prize, participant_count_display)
        updated_embed.title = "👑 **抽奖已结束 (手动指定)** 👑"
        await message.edit(embed=updated_embed, view=None)
    except Exception as e: print(f"无法编辑 pickwinner 消息 {message_id}: {e}")
    await delete_giveaway_data(message_id); print(f"已手动结束并从 Redis 移除抽奖 {message_id} (pickwinner)。")
    await interaction.followup.send(f"✅ 已成功指定 `{prize}` 中奖者为 {winner_mentions} 并结束。", ephemeral=True)

@giveaway_pickwinner.error
async def pickwinner_error(interaction: nextcord.Interaction, error):
    # ... (代码不变) ...
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("抱歉，你没有权限执行此命令。", ephemeral=True)
    else: await interaction.response.send_message(f"执行 pickwinner 命令出错: {error}", ephemeral=True); print(f"Error in pickwinner cmd: {error}")


@giveaway.subcommand(name="end", description="⏱️ [管理员] 立即结束抽奖并随机抽取获胜者。")
@commands.has_permissions(manage_guild=True)
async def giveaway_end(interaction: nextcord.Interaction, message_link_or_id: str = ...):
    await interaction.response.defer(ephemeral=True)
    # --- 使用辅助函数解析 ---
    channel_id, message_id = await parse_message_link(interaction, message_link_or_id)
    if channel_id is None or message_id is None: return
    # --- 获取频道和消息 ---
    target_channel = bot.get_channel(channel_id)
    if not target_channel: await interaction.followup.send("错误：无法找到链接中的频道。", ephemeral=True); return
    try: message = await target_channel.fetch_message(message_id)
    except nextcord.NotFound: await interaction.followup.send("无法找到原始抽奖消息。", ephemeral=True); return
    except nextcord.Forbidden: await interaction.followup.send(f"无权限在 {target_channel.mention} 读取历史记录。", ephemeral=True); return
    except Exception as e: await interaction.followup.send(f"获取消息时出错: {e}", ephemeral=True); print(f"Error fetch msg giveaway_end {message_id}: {e}"); return
    # ... (后续 end 逻辑不变) ...
    giveaway_data = await load_giveaway_data(message_id)
    if not giveaway_data:
        if message.embeds and ("结束" in message.embeds[0].title or (message.embeds[0].footer and "已结束" in message.embeds[0].footer.text)):
             await interaction.followup.send("该抽奖似乎已经结束了。", ephemeral=True)
        else: await interaction.followup.send("错误：无法从 Redis 加载此抽奖数据。", ephemeral=True)
        return
    print(f"用户 {interaction.user} 手动结束抽奖 {message_id}...")
    await process_giveaway_end(message, giveaway_data)
    await delete_giveaway_data(message_id); print(f"已手动结束并从 Redis 移除抽奖 {message_id} (end command)。")
    await interaction.followup.send(f"✅ 已手动结束 `{giveaway_data.get('prize', '未知奖品')}` 的抽奖。", ephemeral=True)

@giveaway_end.error
async def end_error(interaction: nextcord.Interaction, error):
    # ... (代码不变) ...
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("抱歉，你没有权限执行此命令。", ephemeral=True)
    else: await interaction.response.send_message(f"执行 end 命令出错: {error}", ephemeral=True); print(f"Error in end cmd: {error}")


# --- 后台任务 ---
@tasks.loop(seconds=15)
async def check_giveaways():
    # ... (代码不变, 调用 process_giveaway_end) ...
    if not redis_pool: return
    current_time = datetime.datetime.now(datetime.timezone.utc); ended_giveaway_ids = []; giveaway_ids = await get_all_giveaway_ids()
    if not giveaway_ids: return
    for message_id in giveaway_ids:
        giveaway_data = await load_giveaway_data(message_id)
        if not giveaway_data: continue
        if not isinstance(giveaway_data.get('end_time'), datetime.datetime): print(f"警告: 抽奖 {message_id} end_time 格式无效。"); await delete_giveaway_data(message_id); continue
        if giveaway_data['end_time'] <= current_time:
            print(f"抽奖 {message_id} 到期，处理...")
            guild = bot.get_guild(giveaway_data['guild_id']); channel = guild.get_channel(giveaway_data['channel_id']) if guild else None
            if not guild or not channel or not isinstance(channel, nextcord.TextChannel): print(f"无法获取服务器/频道 {giveaway_data['guild_id']}/{giveaway_data['channel_id']}。"); continue
            try: message = await channel.fetch_message(message_id); await process_giveaway_end(message, giveaway_data); ended_giveaway_ids.append(message_id)
            except nextcord.NotFound: print(f"消息 {message_id} 未找到。"); ended_giveaway_ids.append(message_id)
            except nextcord.Forbidden: print(f"无法获取消息 {message_id} (权限不足?)。")
            except Exception as e: print(f"处理到期抽奖 {message_id} 出错: {e}")
    if ended_giveaway_ids: print(f"清理 Redis: {ended_giveaway_ids}"); [await delete_giveaway_data(msg_id) for msg_id in ended_giveaway_ids]

@check_giveaways.before_loop
async def before_check_giveaways():
    # ... (代码不变) ...
    await bot.wait_until_ready(); print("检查抽奖任务已准备就绪。")

# --- 机器人事件 ---
@bot.event
async def on_ready():
    # ... (代码不变) ...
    print("-" * 30); print(f'已登录为: {bot.user.name} ({bot.user.id})'); print(f'Nextcord 版本: {nextcord.__version__}'); print(f'运行于: {len(bot.guilds)} 个服务器')
    if not redis_pool: await setup_redis()
    redis_status = "未知"
    if redis_pool:
        try: await redis_pool.ping(); redis_status = "已连接"
        except Exception as e: redis_status = f"连接失败 ({e})"
    print(f'Redis 连接池状态: {redis_status}'); print("-" * 30)
    if redis_status == "已连接":
        if not check_giveaways.is_running(): check_giveaways.start(); print("已启动后台检查抽奖任务。")
    else: print("警告: Redis 连接失败，后台任务未启动。")

# --- 运行机器人 ---
if __name__ == "__main__":
    print("正在启动机器人...")
    bot.run(BOT_TOKEN)