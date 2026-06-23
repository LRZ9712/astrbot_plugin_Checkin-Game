import os
import json
import time
import random
import asyncio
import aiohttp
import re
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

# 引入轻量级绘图库、裁剪工具和滤镜
from PIL import Image as PILImage, ImageDraw, ImageFont, ImageOps, ImageFilter

try:
    from pilmoji import Pilmoji
    HAS_PILMOJI = True
except ImportError:
    HAS_PILMOJI = False

from astrbot.api.all import *
from astrbot.api.event import filter
from astrbot.api.message_components import At, Plain

AUTO_RACE_TASK_ATTR = "_checkin_game_auto_race_task"

@register("checkin_game", "Author", "群签到与经济抢劫插件(修复多开与数值版)", "1.9.3")
class CheckinGamePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.plugin_dir = os.path.dirname(__file__)
        self.data_file = os.path.join(self.plugin_dir, "data.json")
        self.font_path = os.path.join(self.plugin_dir, "AlibabaPuHuiTi-2-65-Medium.ttf")
        
        
                # ========= 在 __init__ 里新增 =========
        self.pending_duels = {}          # {group_id: {target_id: duel_info}}
        self.duel_timeout_tasks = {}     # {(group_id, target_id): asyncio.Task}
        
        self.cache_dir = os.path.join("data", "plugins", "checkin_game", "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        
        
        
        
        # 接收框架传入的最新的、唯一的面板配置
        self.config = config or {}
        self.users_data = self.load_data()
        self.active_robberies = {}
        self.active_races = {}
        self.active_red_packets = {}  
        self.last_events = {}         

        # 启动定时赛马监控任务，绑定到系统事件循环上清理旧进程
        loop = asyncio.get_running_loop()
        old_task = getattr(loop, AUTO_RACE_TASK_ATTR, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        new_task = asyncio.create_task(self._auto_horse_race_loop(), name="checkin_game_auto_race")
        setattr(loop, AUTO_RACE_TASK_ATTR, new_task)





#分界线分界线分界线分界线分界线分界线分界线分界线分界线


# ========= 放到类里：通用辅助方法 =========
    def _duel_task_key(self, group_id: str, target_id: str):
        return (str(group_id), str(target_id))
    
    async def _download_bytes(self, url: str, timeout: int = 15):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except:
            return None
        return None
    
    async def _generate_meme_gif(self, api_url: str, qq_ids: list[str], file_prefix: str, texts: list[str] = None):
        form = aiohttp.FormData()
        
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            for idx, qq in enumerate(qq_ids):
                avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640"
                async with session.get(avatar_url, timeout=15) as resp:
                    if resp.status != 200:
                        raise Exception(f"下载头像失败: {qq}")
                    img_bytes = await resp.read()
                    form.add_field(
                        "images",
                        img_bytes,
                        filename=f"avatar_{idx}.png",
                        content_type="image/png"
                    )
            
            # 👇 这里就是加上昵称的地方
            if texts:
                for text in texts:
                    form.add_field("texts", text)

            form.add_field("args", "{}")
    
            async with session.post(api_url, data=form, timeout=60) as resp:
                if resp.status != 200:
                    raise Exception(f"表情接口请求失败: HTTP {resp.status}")
                result_bytes = await resp.read()
    
        ext = "gif"
        out_path = os.path.join(self.cache_dir, f"{file_prefix}_{int(time.time())}.{ext}")
        with open(out_path, "wb") as f:
            f.write(result_bytes)
        return out_path
    
    async def _duel_expire_after_40s(self, group_id: str, target_id: str):
        await asyncio.sleep(40)
    
        group_id = str(group_id)
        target_id = str(target_id)
    
        duel_map = self.pending_duels.get(group_id, {})
        duel = duel_map.get(target_id)
        if not duel:
            return
    
        challenger_name = duel["challenger_name"]
        target_name = duel["target_name"]
        points = duel["points"]
    
        duel_map.pop(target_id, None)
        self.duel_timeout_tasks.pop(self._duel_task_key(group_id, target_id), None)
    
        last_event = self.last_events.get(group_id)
        if last_event:
            try:
                yield_msg = f"⚔️ 决斗已作废：{target_name} 在 40 秒内没有回复 /接受决斗。\n本次赌注 {points} 积分已取消，{challenger_name} 可以重新发起决斗。"
                await last_event.send(yield_msg)
            except:
                pass
    
    
    # ========= 新增：统一通缉犯判定 =========
    def is_wanted(self, group_id: str, user_id: str, user_name: str = "") -> bool:
        user = self.get_user(group_id, user_id, user_name or user_id)
        return bool(user.get("is_red_name", False))
    
    
    
    
    # ========= 新增命令：/决斗 =========
    @filter.command("决斗")
    async def duel(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.last_events[group_id] = event
    
        if not group_id or group_id == "None":
            yield event.plain_result("决斗只能在群聊中发起！")
            return
    
        if not self.is_group_enabled(group_id):
            return
    
        challenger_id = event.get_sender_id()
        challenger_name = event.get_sender_name()
    
        target_id = None
        target_name = "对方"
    
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                target_id = str(comp.qq)
                target_name = getattr(comp, "name", None) or "对方"
                break
    
        if not target_id:
            yield event.plain_result("格式错误：/决斗 5@小明\n请先输入赌注积分，并 @ 目标。")
            return
    
        if target_id == challenger_id:
            yield event.plain_result("你不能和自己决斗。")
            return
    
        nums = []
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                nums.extend(re.findall(r'\d+', comp.text))
    
        if not nums:
            yield event.plain_result("请填写赌注积分，例如：/决斗 5@小明")
            return
    
        bet_points = int(nums[0])
        if bet_points <= 0:
            yield event.plain_result("赌注积分必须大于 0。")
            return
    
        challenger_data = self.get_user(group_id, challenger_id, challenger_name)
        target_data = self.get_user(group_id, target_id, target_name)
    
        if challenger_data["points"] < bet_points:
            yield event.plain_result(f"你的积分不足，当前只有 {challenger_data['points']} 分。")
            return
    
        if target_data["points"] < bet_points:
            yield event.plain_result(f"对方积分不足，当前只有 {target_data['points']} 分，无法接受这场决斗。")
            return
    
        if group_id not in self.pending_duels:
            self.pending_duels[group_id] = {}
    
        if target_id in self.pending_duels[group_id]:
            yield event.plain_result("该玩家当前已经有一场待接受的决斗了，请等这场结束或过期。")
            return
    
        self.pending_duels[group_id][target_id] = {
            "challenger_id": challenger_id,
            "challenger_name": challenger_name,
            "target_id": target_id,
            "target_name": target_name,
            "points": bet_points,
            "created_at": time.time()
        }
    
        task_key = self._duel_task_key(group_id, target_id)
        old_task = self.duel_timeout_tasks.get(task_key)
        if old_task and not old_task.done():
            old_task.cancel()
    
        self.duel_timeout_tasks[task_key] = asyncio.create_task(
            self._duel_expire_after_40s(group_id, target_id)
        )
    
        yield event.plain_result(
            f"⚔️ {challenger_name} 向 {target_name} 发起了决斗！\n"
            f"赌注：{bet_points} 积分\n"
            f"请 {target_name} 在 40 秒内发送 /接受决斗 ，超时自动作废。"
        )
    
    
    
    
    # ========= 新增命令：/接受决斗 =========
    @filter.command("接受决斗")
    async def accept_duel(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.last_events[group_id] = event
    
        if not group_id or group_id == "None":
            yield event.plain_result("决斗只能在群聊中进行！")
            return
    
        if not self.is_group_enabled(group_id):
            return
    
        target_id = event.get_sender_id()
        target_name = event.get_sender_name()
    
        duel_map = self.pending_duels.get(group_id, {})
        duel = duel_map.get(target_id)
        if not duel:
            yield event.plain_result("你当前没有待接受的决斗，或者这场决斗已经过期。")
            return
    
        challenger_id = duel["challenger_id"]
        challenger_name = duel["challenger_name"]
        bet_points = duel["points"]
    
        challenger_data = self.get_user(group_id, challenger_id, challenger_name)
        target_data = self.get_user(group_id, target_id, target_name)
    
        if challenger_data["points"] < bet_points:
            duel_map.pop(target_id, None)
            yield event.plain_result(f"决斗取消：发起人 {challenger_name} 当前积分不足 {bet_points}。")
            return
    
        if target_data["points"] < bet_points:
            duel_map.pop(target_id, None)
            yield event.plain_result(f"决斗取消：你当前积分不足 {bet_points}。")
            return
    
        task_key = self._duel_task_key(group_id, target_id)
        old_task = self.duel_timeout_tasks.pop(task_key, None)
        if old_task and not old_task.done():
            old_task.cancel()
    
        duel_map.pop(target_id, None)
    
        challenger_win = random.choice([True, False])
    
        if challenger_win:
            winner_id, winner_name = challenger_id, challenger_name
            loser_id, loser_name = target_id, target_name
            winner_data, loser_data = challenger_data, target_data
        else:
            winner_id, winner_name = target_id, target_name
            loser_id, loser_name = challenger_id, challenger_name
            winner_data, loser_data = target_data, challenger_data
    
        loser_data["points"] = max(0, loser_data["points"] - bet_points)
        winner_data["points"] += bet_points
        self.save_data()

        # 1. 独立发出激战文本
        await event.send(event.plain_result(f"⚔️ {challenger_name} 与 {target_name} 的决斗开始了！激战中..."))

        # 2. 独立画图并发图
        try:
            gif_path = await self._generate_meme_gif(
                "https://memers.tudouu.cn/api/memes/fencing/",
                [challenger_id, target_id],
                "duel"
            )
            # 恢复最原本发图的指令
            await event.send(event.image_result(str(gif_path)))
        except Exception as e:
            # 哪怕有错，也只发在文本里告诉你
            await event.send(event.plain_result(f"【图没画出来，原因：{str(e)[:50]}】"))

        # 3. 停顿 3 秒
        await asyncio.sleep(5)

        # 4. 结尾结果用 yield 结束
        msg = (
            f"💥 决斗结束！\n"
            f"胜者：{winner_name}\n"
            f"败者：{loser_name}\n"
            f"{loser_name} 输给了 {winner_name} {bet_points} 积分！"
        )
        yield event.plain_result(msg)





















#分界线分界线分界线分界线分界线分界线分界线分界线分界线

    def get_cfg(self):
        """🛡️ 核心修复 2：完全抛弃本地 config 文件，只信任 AstrBot 网页控制台传入的数据"""
        default_config = {
            "admin_qq": ["2442262339"],
            "enabled_groups": [], 
            "disabled_groups": [],
            "rob_fail_rate": 0.4, 
            "intervene_fail_rate": 0.4,
            "rob_min_points": 1, 
            "rob_max_points": 8,
            "curfew_start": "02:00",
            "curfew_end": "06:00",
            "intervene_penalty_normal": 5,
            "intervene_penalty_red_name": 10,
            "hero_reward_points": 3,
            "hr_auto_time": "00:05",
            "hr_source_group": "",
            "hr_base_ticket": 5,
            "hr_system_sponsor": 20,
            "hr_duration": 300,
            "hr_max_players": 50,
            "hr_rich_curse": 0.15,
            "hr_red_name_buff": 1.5,
            "hr_tax_rate": 0.05
        }
        
        # 只要面板里填了数据，就绝对优先覆盖默认值
        if self.config and isinstance(self.config, dict):
            for k, v in self.config.items():
                default_config[k] = v
        return default_config

    def load_data(self):
        if os.path.exists(self.data_file):
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and any("points" in v for v in data.values()):
                    return {"global_migrated_backup": data}
                return data
        return {}

    def save_data(self):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.users_data, f, ensure_ascii=False, indent=2)

    def is_group_enabled(self, group_id):
        cfg = self.get_cfg()
        enabled_groups = cfg.get("enabled_groups", [])
        disabled_groups = cfg.get("disabled_groups", [])
        group_id_str = str(group_id)

        if enabled_groups: return group_id_str in enabled_groups
        if disabled_groups: return group_id_str not in disabled_groups
        return True

    def get_user(self, group_id, user_id, user_name=""):
        group_id = str(group_id)
        user_id = str(user_id)
        
        if group_id not in self.users_data:
            self.users_data[group_id] = {}
            
        if user_id not in self.users_data[group_id]:
            self.users_data[group_id][user_id] = {
                "name": user_name, "points": 0, "last_checkin": "",
                "last_rob_date": "", "last_decay_date": "", "rob_days_count": 0, "is_red_name": False
            }
            
        user = self.users_data[group_id][user_id]
        if user_name: user["name"] = user_name
            
        if "last_decay_date" not in user:
            user["last_decay_date"] = user.get("last_rob_date", "")
            
        last_decay_str = user["last_decay_date"]
        if last_decay_str:
            try:
                last_decay_obj = datetime.strptime(last_decay_str, "%Y-%m-%d").date()
                today_obj = datetime.now().date()
                days_passed = (today_obj - last_decay_obj).days
                
                if days_passed > 1:
                    missed_days = days_passed - 1
                    user["rob_days_count"] = max(0, user.get("rob_days_count", 0) - missed_days)
                    
                    if user["rob_days_count"] < 5:
                        user["is_red_name"] = False
                        
                    new_decay_obj = last_decay_obj + timedelta(days=missed_days)
                    user["last_decay_date"] = new_decay_obj.strftime("%Y-%m-%d")
            except: pass
            
        return user

    # ================= APIs =================
    
    async def fetch_hitokoto(self):
        async with aiohttp.ClientSession() as session:
            async with session.get("https://v1.hitokoto.cn/?encode=text") as resp:
                return await resp.text() if resp.status == 200 else "我早已踏过深渊，又岂会畏惧黑夜!"

    async def fetch_image_bytes(self, url, timeout=10):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.google.com/'
            }
            timeout_obj = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout_obj) as session:
                async with session.get(url) as resp:
                    if resp.status == 200: return await resp.read()
        except: return None
        return None

    async def fetch_random_bg(self):
        bg_dir = os.path.join(self.plugin_dir, "bg_images")
        if os.path.exists(bg_dir):
            valid_exts = ('.png', '.jpg', '.jpeg', '.webp')
            imgs = [f for f in os.listdir(bg_dir) if f.lower().endswith(valid_exts)]
            if imgs:
                try:
                    with open(os.path.join(bg_dir, random.choice(imgs)), 'rb') as f: return f.read()
                except: pass

        apis = ["https://api.suyanw.cn/api/Yourname.php", "https://api.suyanw.cn/api/comic.php", "https://t.mwm.moe/pc"]
        fallback = "https://p1.ssl.qhimgs1.com/t02facf27f631c0e961.jpg"
        
        random.shuffle(apis)
        for api in apis:
            bytes_data = await self.fetch_image_bytes(api)
            if bytes_data:
                try:
                    PILImage.open(BytesIO(bytes_data)).verify()
                    return bytes_data
                except: continue
        return await self.fetch_image_bytes(fallback)

    # ================= Pillow 绘图引擎 =================
    
    def get_font(self, size):
        try: return ImageFont.truetype(self.font_path, size)
        except Exception: return ImageFont.load_default()

    def make_circle_avatar(self, img_bytes, size=(100, 100)):
        try:
            if not img_bytes: raise ValueError("No image bytes")
            img = PILImage.open(BytesIO(img_bytes)).convert("RGBA").resize(size, PILImage.Resampling.LANCZOS)
            mask = PILImage.new('L', size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0) + size, fill=255)
            output = PILImage.new('RGBA', size, (0, 0, 0, 0))
            output.paste(img, (0, 0), mask)
            return output
        except: return PILImage.new('RGBA', size, color=(200, 200, 200, 255))

    def load_custom_icon(self, filename, size=(32, 32)):
        path = os.path.join(self.plugin_dir, filename)
        if os.path.exists(path):
            try: return PILImage.open(path).convert("RGBA").resize(size, PILImage.Resampling.LANCZOS)
            except: pass
        return None

    def draw_vector_like(self, draw, x, y, color):
        draw.arc([x, y, x+12, y+12], 135, 360, fill=color, width=2)
        draw.arc([x+12, y, x+24, y+12], 180, 45, fill=color, width=2)
        draw.line([x+1.5, y+10, x+12, y+22], fill=color, width=2)
        draw.line([x+12, y+22, x+22.5, y+10], fill=color, width=2)

    def draw_vector_comment(self, draw, x, y, color):
        draw.rounded_rectangle([x, y+2, x+24, y+18], radius=4, outline=color, width=2)
        draw.polygon([(x+6, y+18), (x+6, y+25), (x+12, y+18)], fill=color)

    def draw_vector_share(self, draw, x, y, color):
        draw.polygon([(x, y+10), (x+22, y), (x+12, y+22), (x+10, y+12)], outline=color, width=2)
        draw.line([x+10, y+12, x+22, y], fill=color, width=2)

    def draw_vector_bookmark(self, draw, x, y, color):
        draw.line([x, y, x+18, y], fill=color, width=2)
        draw.line([x, y, x, y+24], fill=color, width=2)
        draw.line([x+18, y, x+18, y+24], fill=color, width=2)
        draw.line([x, y+24, x+9, y+16], fill=color, width=2)
        draw.line([x+9, y+16, x+18, y+24], fill=color, width=2)

    # ================= 卡片与排行榜生成 =================
    async def draw_checkin_image(self, user_id, name, points, quote):
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        avatar_bytes, bg_bytes = await asyncio.gather(self.fetch_image_bytes(avatar_url, timeout=3), self.fetch_random_bg())
        
        target_w = 620
        img_h = 450 
        main_img = None
        
        if bg_bytes:
            try:
                raw_img = PILImage.open(BytesIO(bg_bytes)).convert("RGB")
                ratio = target_w / float(raw_img.width)
                img_h = int(raw_img.height * ratio)
                if img_h > 1200: img_h = 1200
                main_img = raw_img.resize((target_w, img_h), PILImage.Resampling.LANCZOS)
            except: pass
                
        quote_text = f"『 {quote} 』"
        lines = [quote_text[i:i+30] for i in range(0, len(quote_text), 30)]
        
        bottom_area_h = 20 + 30 + 15 + 30 + 30 + len(lines)*25 + 40 + 20
        canvas_h = 110 + img_h + bottom_area_h + 60 
        
        canvas = PILImage.new('RGB', (680, canvas_h), color=(248, 248, 248))
        
        shadow_layer = PILImage.new('RGBA', canvas.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.rounded_rectangle([(20, 30), (660, canvas_h - 30)], radius=30, fill=(0, 0, 0, 35))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(15))
        canvas.paste(shadow_layer, (0, 0), mask=shadow_layer)
        
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle([(20, 20), (660, canvas_h - 40)], radius=30, fill=(255, 255, 255))
        
        avatar = self.make_circle_avatar(avatar_bytes, size=(50, 50))
        canvas.paste(avatar, (40, 40), mask=avatar)
        
        font_name = self.get_font(22)
        title_name = name[:15] + "..." if len(name) > 15 else name
        
        try:
            if HAS_PILMOJI:
                with Pilmoji(canvas) as pilmoji: pilmoji.text((110, 52), title_name, font=font_name, fill=(38, 38, 38))
            else: draw.text((110, 52), title_name, font=font_name, fill=(38, 38, 38))
        except: draw.text((110, 52), title_name, font=font_name, fill=(38, 38, 38))
            
        draw.text((610, 40), "...", font=self.get_font(26), fill=(38, 38, 38)) 
        
        if main_img: canvas.paste(main_img, (30, 110))
        else: draw.rectangle([(30, 110), (650, 110 + img_h)], fill=(240, 240, 240))
        
        y_actions = 110 + img_h + 20
        c = (60, 60, 60)
        
        icon = self.load_custom_icon("icon_like.png")
        if icon: canvas.paste(icon, (40, y_actions - 2), mask=icon)
        else: self.draw_vector_like(draw, 40, y_actions, c)

        icon = self.load_custom_icon("icon_comment.png")
        if icon: canvas.paste(icon, (85, y_actions - 2), mask=icon)
        else: self.draw_vector_comment(draw, 85, y_actions, c)

        icon = self.load_custom_icon("icon_share.png")
        if icon: canvas.paste(icon, (130, y_actions - 2), mask=icon)
        else: self.draw_vector_share(draw, 130, y_actions, c)

        icon = self.load_custom_icon("icon_bookmark.png")
        if icon: canvas.paste(icon, (590, y_actions - 2), mask=icon)
        else: self.draw_vector_bookmark(draw, 590, y_actions, c)

        current_y = y_actions + 45
        font_bold = self.get_font(20)
        font_quote = self.get_font(18)
        
        try:
            if HAS_PILMOJI:
                with Pilmoji(canvas) as pilmoji: pilmoji.text((40, current_y), f"{title_name} 签到成功！", font=font_bold, fill=(38, 38, 38))
            else: draw.text((40, current_y), f"{title_name} 签到成功！", font=font_bold, fill=(38, 38, 38))
        except: draw.text((40, current_y), f"{title_name} 签到成功！", font=font_bold, fill=(38, 38, 38))
            
        current_y += 30
        draw.text((40, current_y), f"财富: {points} 积分", font=font_quote, fill=(80, 80, 80))
        
        current_y += 40
        for line in lines:
            draw.text((40, current_y), line, font=font_quote, fill=(50, 50, 50))
            current_y += 25
            
        current_y += 10
        draw.text((40, current_y), datetime.now().strftime("%Y-%m-%d  %H:%M"), font=self.get_font(14), fill=(150, 150, 150))

        out_path = os.path.abspath(os.path.join(self.plugin_dir, "temp_checkin.jpg"))
        canvas.save(out_path, format='JPEG', quality=90)
        return out_path

    async def draw_profile_image(self, user_id, name, points, quote, rob_days, is_red_name=False):
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        avatar_bytes, bg_bytes = await asyncio.gather(self.fetch_image_bytes(avatar_url, timeout=3), self.fetch_random_bg())
        
        card_w, card_h = 560, 750
        canvas = PILImage.new('RGB', (card_w + 120, card_h + 120), color=(248, 248, 248))
        card_x, card_y = 60, 50 
        
        shadow_layer = PILImage.new('RGBA', canvas.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.rounded_rectangle([(card_x, card_y + 15), (card_x + card_w, card_y + card_h + 15)], radius=40, fill=(0, 0, 0, 35))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(15))
        canvas.paste(shadow_layer, (0, 0), mask=shadow_layer)

        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle([(card_x, card_y), (card_x + card_w, card_y + card_h)], radius=40, fill=(255, 255, 255))
        
        if bg_bytes:
            try:
                bg_img = PILImage.open(BytesIO(bg_bytes)).convert("RGB")
                bg_img = ImageOps.fit(bg_img, (card_w, 240), PILImage.Resampling.LANCZOS)
                mask = PILImage.new('L', (card_w, 240), 0)
                ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (card_w, 240 + 40)], radius=40, fill=255)
                canvas.paste(bg_img, (card_x, card_y), mask)
            except:
                draw.rounded_rectangle([(card_x, card_y), (card_x + card_w, card_y + 240)], radius=40, fill=(200, 210, 220))
        
        btn_w, btn_h, btn_x, btn_y = 130, 46, card_x + card_w - 150, card_y + 20
        draw.rounded_rectangle([(btn_x, btn_y), (btn_x + btn_w, btn_y + btn_h)], radius=23, fill=(250, 250, 250), outline=(200, 200, 200), width=1)
        draw.text((btn_x + 30, btn_y + 10), "Follow +", font=self.get_font(18), fill=(30, 30, 30))

        avatar = self.make_circle_avatar(avatar_bytes, size=(130, 130))
        white_bg = PILImage.new('RGBA', (142, 142), (255, 255, 255, 0))
        ImageDraw.Draw(white_bg).ellipse([(0, 0), (142, 142)], fill=(255, 255, 255, 255))
        white_bg.paste(avatar, (6, 6), mask=avatar)
        canvas.paste(white_bg, (card_x + 40, card_y + 160), mask=white_bg)

        font_name = self.get_font(36)
        title_name = name[:12] + "..." if len(name) > 12 else name
        try:
            if HAS_PILMOJI:
                with Pilmoji(canvas) as pilmoji: pilmoji.text((card_x + 40, card_y + 320), title_name, font=font_name, fill=(30, 30, 30))
            else: draw.text((card_x + 40, card_y + 320), title_name, font=font_name, fill=(30, 30, 30))
        except: draw.text((card_x + 40, card_y + 320), title_name, font=font_name, fill=(30, 30, 30))
            
        if is_red_name:
            badge_x = card_x + 400
            badge_y = card_y + 325
            draw.rounded_rectangle(
                [(badge_x, badge_y), (badge_x + 85, badge_y + 36)],
                radius=10, fill=(220, 53, 69)
            )
            draw.text((badge_x + 10, badge_y + 8), "通缉犯", font=self.get_font(18), fill=(255, 255, 255))
            
            
            
        lv = (points // 10) + 1
        font_tag = self.get_font(16)
        tag_text = f"Lv.{lv} | 初入江湖" 
        draw.rounded_rectangle([(card_x + 40, card_y + 375), (card_x + 40 + len(tag_text)*10+20, card_y + 405)], radius=8, fill=(230, 240, 255))
        draw.text((card_x + 50, card_y + 382), tag_text, font=font_tag, fill=(0, 120, 255))

        font_bio = self.get_font(20)
        lines = [quote[i:i+24] for i in range(0, len(quote), 24)]
        y_bio = card_y + 420
        for line in lines[:2]: 
            draw.text((card_x + 40, y_bio), line, font=font_bio, fill=(120, 120, 120))
            y_bio += 30

        stat_y = card_y + 520
        draw.line([(card_x, stat_y), (card_x + card_w, stat_y)], fill=(240, 240, 240), width=2)
        draw.line([(card_x, stat_y + 130), (card_x + card_w, stat_y + 130)], fill=(240, 240, 240), width=2)
        
        col_w = card_w // 3
        draw.line([(card_x + col_w, stat_y + 20), (card_x + col_w, stat_y + 110)], fill=(240, 240, 240), width=2)
        draw.line([(card_x + col_w * 2, stat_y + 20), (card_x + col_w * 2, stat_y + 110)], fill=(240, 240, 240), width=2)

        font_stat_val, font_stat_lbl = self.get_font(28), self.get_font(18)
        draw.text((card_x + 60, stat_y + 30), f"{points}", font=font_stat_val, fill=(30, 30, 30))
        draw.text((card_x + 60, stat_y + 75), "财富 (Pts)", font=font_stat_lbl, fill=(150, 150, 150))
        draw.text((card_x + col_w + 60, stat_y + 30), f"{rob_days}", font=font_stat_val, fill=(30, 30, 30))
        draw.text((card_x + col_w + 60, stat_y + 75), "罪恶 (Crimes)", font=font_stat_lbl, fill=(150, 150, 150))
        draw.text((card_x + col_w * 2 + 60, stat_y + 30), f"Lv.{lv}", font=font_stat_val, fill=(30, 30, 30))
        draw.text((card_x + col_w * 2 + 60, stat_y + 75), "等级 (Level)", font=font_stat_lbl, fill=(150, 150, 150))

        icon_y = stat_y + 155
        bx1, bx2, bx3 = card_x + 140, card_x + 280, card_x + 420
        
        def draw_centered_icon(icon_img, center_x, y, fallback_text):
            if icon_img: canvas.paste(icon_img, (center_x - 16, y), mask=icon_img)
            else:
                draw.rounded_rectangle([center_x - 12, y + 4, center_x + 12, y + 28], radius=6, outline=(180, 180, 180), width=2)
                draw.text((center_x - 6, y + 9), fallback_text, font=self.get_font(12), fill=(180, 180, 180))

        draw_centered_icon(self.load_custom_icon("icon_ins.png"), bx1, icon_y, "i")
        draw_centered_icon(self.load_custom_icon("icon_x.png"), bx2, icon_y, "X")
        draw_centered_icon(self.load_custom_icon("icon_web.png"), bx3, icon_y, "W")

        out_path = os.path.abspath(os.path.join(self.plugin_dir, f"temp_profile_{user_id}.jpg"))
        canvas.save(out_path, format='JPEG', quality=90)
        return out_path

    async def draw_leaderboard_image(self, top_users, group_id):
        row_height = 80
        total_height = 120 + (len(top_users) * row_height) + 130
        
        canvas = PILImage.new('RGB', (500, max(total_height, 200)), color=(239, 233, 217))
        draw = ImageDraw.Draw(canvas)
        
        font_title, font_name, font_score, font_badge = self.get_font(30), self.get_font(20), self.get_font(18), self.get_font(14)
        draw.text((160, 30), "- 财富排行榜 -", font=font_title, fill=(92, 84, 70))
        
        avatar_tasks = [self.fetch_image_bytes(f"https://q4.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640", timeout=2) for uid, _ in top_users]
        avatar_bytes_list = await asyncio.gather(*avatar_tasks)
        
        y_offset = 100
        for idx, ((uid, data), avatar_bytes) in enumerate(zip(top_users, avatar_bytes_list)):
            draw.rounded_rectangle([(20, y_offset), (480, y_offset + 65)], radius=10, fill=(248, 245, 238))
            draw.text((40, y_offset + 20), str(idx + 1), font=font_title, fill=(191, 165, 136))
            
            avatar = self.make_circle_avatar(avatar_bytes, size=(50, 50))
            canvas.paste(avatar, (90, y_offset + 7), mask=avatar)
                
            display_name = data.get("name") if data.get("name") else str(uid)
            display_name = display_name[:15] + "..." if len(display_name) > 15 else display_name
            
            draw.text((160, y_offset + 12), display_name, font=font_name, fill=(74, 68, 58))
            draw.text((160, y_offset + 40), f"{data['points']} 积分", font=font_score, fill=(140, 130, 113))
            
            if data.get("is_red_name"):
                draw.rounded_rectangle([(390, y_offset + 20), (460, y_offset + 45)], radius=5, fill=(255, 94, 94))
                draw.text((405, y_offset + 24), "通缉犯", font=font_badge, fill=(255, 255, 255))
            
            y_offset += row_height

        font_footer, font_tips = self.get_font(18), self.get_font(14)
        footer_color, tips_color = (140, 130, 113), (160, 150, 130)
        
        draw.line([(40, y_offset + 10), (460, y_offset + 10)], fill=(220, 210, 190), width=2)
        draw.text((40, y_offset + 25), "15积分   可兑换潇潇或小面包和你的设定关系", font=font_footer, fill=footer_color)
        draw.text((40, y_offset + 55), "30积分   拉潇潇一次", font=font_footer, fill=footer_color)
        draw.text((40, y_offset + 85), "* 仅限主群签到积分兑换", font=font_tips, fill=tips_color)

        out_path = os.path.abspath(os.path.join(self.plugin_dir, f"temp_leaderboard_{group_id}.jpg"))
        canvas.save(out_path, format='JPEG', quality=90)
        return out_path


    # ================= 签到系统指令 =================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def checkin(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        self.last_events[group_id] = event 
        msg_text = event.message_obj.message_str.strip()
        if msg_text not in ["签到", "/签到"]:
            return

        if not self.is_group_enabled(group_id): return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        user_data = self.get_user(group_id, user_id, user_name)
        
        today = datetime.now().strftime("%Y-%m-%d")
        if user_data["last_checkin"] == today:
            yield event.plain_result("今天已经签过到了哦！")
            return
            
        cfg = self.get_cfg()
        sign_in_pts = cfg.get("sign_in_points", 10)
        user_data["points"] += sign_in_pts
        user_data["last_checkin"] = today
        self.save_data()
        
        quote = await self.fetch_hitokoto()
        img_path = await self.draw_checkin_image(user_id, user_name, user_data["points"], quote)
        yield event.image_result(str(img_path))

    @filter.command("我的积分")
    async def my_points(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        self.last_events[group_id] = event
        if not self.is_group_enabled(group_id): return
            
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        user_data = self.get_user(group_id, user_id, user_name)
        
        quote = await self.fetch_hitokoto()
        rob_days = user_data.get("rob_days_count", 0)
        is_red = user_data.get("is_red_name", False)

        img_path = await self.draw_profile_image(user_id, user_name, user_data["points"], quote, rob_days, is_red)
        yield event.image_result(str(img_path))

    # ================= 抢劫与行侠系统 =================

    @filter.command("抢劫")
    async def rob(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("抢劫只能在群聊中进行！")
            return

        self.last_events[group_id] = event
        if not self.is_group_enabled(group_id): return

        cfg = self.get_cfg()

        curfew_start = cfg.get("curfew_start", "02:00")
        curfew_end = cfg.get("curfew_end", "06:00")
        try:
            now_time = datetime.now().time()
            st = datetime.strptime(curfew_start, "%H:%M").time()
            ed = datetime.strptime(curfew_end, "%H:%M").time()
            is_curfew = False
            if st < ed:
                if st <= now_time <= ed: is_curfew = True
            else:
                if now_time >= st or now_time <= ed: is_curfew = True
            
            if is_curfew:
                yield event.plain_result(f"🌙 现已进入宵禁时间 ({curfew_start}-{curfew_end})，大家都睡了，明早再来吧！")
                return
        except Exception: pass

        robber_id = event.get_sender_id()
        robber_name = event.get_sender_name()
        
        target_id = None
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                target_id = str(comp.qq)
                break
                
        if not target_id:
            yield event.plain_result("你要抢劫谁？请 @ 目标！")
            return
            
        if target_id == robber_id:
            yield event.plain_result("你不能抢劫自己！")
            return

        robber_data = self.get_user(group_id, robber_id, robber_name)
        target_data = self.get_user(group_id, target_id)
        
        if target_data["points"] <= 0:
            yield event.plain_result("他已经一无所有了，光脚的不怕穿鞋的，放过他吧！")
            return

        today_str = datetime.now().strftime("%Y-%m-%d")

        # 跨天时重置每日抢劫计数
        if robber_data.get("last_rob_date") != today_str:
            robber_data["last_rob_date"] = today_str
            robber_data["rob_days_count"] = 0

        limit = int(cfg.get("daily_rob_limit", 3))
        if robber_data["rob_days_count"] >= limit:
            yield event.plain_result(f"你今天已经作案 {limit} 次了，明天再来吧！")
            return

        robber_data["rob_days_count"] += 1
        if robber_data["rob_days_count"] >= limit:
            robber_data["is_red_name"] = True

        robber_data["last_rob_date"] = today_str
        robber_data["last_decay_date"] = today_str
        self.save_data()

        if random.random() < cfg.get("rob_fail_rate", 0.4):
            yield event.plain_result(f"【抢劫失败】{robber_name} 试图抢劫，但脚底打滑摔了一跤！")
            return
            
        raw_steal = random.randint(cfg.get("rob_min_points", 1), cfg.get("rob_max_points", 8))
        steal_points = min(raw_steal, target_data["points"])

        if raw_steal > steal_points:
            msg = f"⚠️ 警告！【{robber_name}】发起了抢劫！但目标太穷了，把兜翻底朝天也只有 {steal_points} 积分！\n⏳ 3 分钟内，输入 /行侠仗义 可阻止这场劫案！"
        else:
            msg = f"⚠️ 警告！【{robber_name}】正在抢劫目标！涉及积分：{steal_points}\n⏳ 3 分钟内，输入 /行侠仗义 可阻止这场劫案！"

        task = asyncio.create_task(self.robbery_timeout(group_id, robber_id, target_id, steal_points))
        self.active_robberies[group_id] = {
            "robber_id": robber_id,
            "target_id": target_id,
            "points": steal_points,
            "task": task
        }

        yield event.plain_result(msg)

        task = asyncio.create_task(self.robbery_timeout(group_id, robber_id, target_id, steal_points))
        self.active_robberies[group_id] = {"robber_id": robber_id, "target_id": target_id, "points": steal_points, "task": task}

    async def robbery_timeout(self, group_id, robber_id, target_id, points):
        await asyncio.sleep(180)
        if group_id in self.active_robberies:
            target_data = self.users_data[group_id][target_id]
            robber_data = self.users_data[group_id][robber_id]
            actual_steal = min(points, target_data["points"])
            robber_data["points"] += actual_steal
            target_data["points"] = max(0, target_data["points"] - actual_steal)
            self.save_data()
            del self.active_robberies[group_id]

    @filter.command("行侠仗义")
    async def intervene(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.last_events[group_id] = event
        if not self.is_group_enabled(group_id): return

        hero_id = event.get_sender_id()
        hero_name = event.get_sender_name()

        if group_id not in self.active_robberies:
            yield event.plain_result("当前没有发生抢劫案，已经被抢完啦，你来晚了！")
            return

        rob_data = self.active_robberies[group_id]
        if hero_id == rob_data["robber_id"]:
            yield event.plain_result("贼喊捉贼是吧？")
            return
        if hero_id == rob_data["target_id"]:
            yield event.plain_result("你都被五花大绑了，还想自己救自己？老实等别人来救吧！")
            return

        robber_data = self.get_user(group_id, rob_data["robber_id"])
        hero_user_data = self.get_user(group_id, hero_id, hero_name)
        cfg = self.get_cfg()
        
        if random.random() < cfg.get("intervene_fail_rate", 0.4):
            target_data = self.get_user(group_id, rob_data["target_id"])
            
            actual_steal = min(rob_data["points"], target_data["points"])
            robber_data["points"] += actual_steal
            target_data["points"] = max(0, target_data["points"] - actual_steal)
            
            hero_loss = actual_steal
            actual_hero_loss = min(hero_loss, hero_user_data["points"])
            
            robber_data["points"] += actual_hero_loss
            hero_user_data["points"] = max(0, hero_user_data["points"] - actual_hero_loss)
            
            rob_data["task"].cancel()
            del self.active_robberies[group_id]
            self.save_data()
            
            fail_msg = f"💥 哎呀！大侠 {hero_name} 技不如人被揍趴下了！\n劫案继续，受害者被抢走 {actual_steal} 积分。\n"
            if actual_hero_loss < hero_loss:
                fail_msg += f"大侠也被顺手牵羊，搜刮走了身上仅有的 {actual_hero_loss} 积分！"
            else:
                fail_msg += f"大侠连自己都没保住，也被顺手牵羊抢走了 {actual_hero_loss} 积分！"
            yield event.plain_result(fail_msg)
            return
            
        else:
            rob_data["task"].cancel()
            del self.active_robberies[group_id]
            
            if robber_data["is_red_name"]:
                penalty = cfg.get("intervene_penalty_red_name", 10)
                tag = "【红名通缉犯】"
            else:
                penalty = cfg.get("intervene_penalty_normal", 5)
                tag = "【普通劫匪】"
                
            robber_data["points"] = max(0, robber_data["points"] - penalty)
            hero_reward = cfg.get("hero_reward_points", 3)
            hero_user_data["points"] += hero_reward
            self.save_data()
            
            yield event.plain_result(f"🗡️ 大侠 {hero_name} 出手相助！受害者完好无损。\n抓获 {tag} 一名，扣除其 {penalty} 积分。\n大侠获得 {hero_reward} 积分系统奖励！")


    # ================= 赛马系统 =================

    async def _auto_horse_race_loop(self):
        while True:
            await asyncio.sleep(10)
            try:
                cfg = self.get_cfg()
                auto_time = cfg.get("hr_auto_time", "00:05")
                if not auto_time: continue
                
                now = datetime.now()
                # 精确匹配指定分钟，且控制在 15 秒内，避免多次触发
                if now.strftime("%H:%M") == auto_time and now.second < 15:
                    for group_id, ev in list(self.last_events.items()):
                        if self.is_group_enabled(group_id):
                            if group_id not in self.active_races or not self.active_races[group_id]["active"]:
                                await self._start_race_logic(group_id, ev, is_auto=True)
                    await asyncio.sleep(60) # 挂起 1 分钟，绝对阻止二次触发
            except Exception as e:
                pass

    @filter.command("开启赛马")
    async def start_horse_race(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None":
            yield event.plain_result("赛马只能在群聊中举办！")
            return
            
        self.last_events[group_id] = event
        if not self.is_group_enabled(group_id): return
        
        cfg = self.get_cfg()
        if str(event.get_sender_id()) not in cfg.get("admin_qq", []):
            yield event.plain_result("🚫 权限不足！只有系统管理员可以举办赛马。")
            return
            
        if group_id in self.active_races and self.active_races[group_id]["active"]:
            yield event.plain_result("本群已经有一场赛马正在进行了，等开奖后再开启下一场吧！")
            return
            
        await self._start_race_logic(group_id, event, is_auto=False)
        yield event.plain_result("") 

    async def _start_race_logic(self, group_id, event, is_auto=False):
        cfg = self.get_cfg()
        source_group = str(cfg.get("hr_source_group", ""))
        if not source_group: source_group = group_id
        
        group_users = self.users_data.get(source_group, {})
        for uid in list(group_users.keys()):
            self.get_user(source_group, uid)
        group_users = self.users_data.get(source_group, {})

        sorted_users = sorted(group_users.items(), key=lambda x: x[1]["points"], reverse=True)
        top_users = sorted_users[:5]
        
        if len(top_users) < 3:
            if not is_auto:
                try: await event.send(event.plain_result(f"提取群({source_group})的人数不足3人，凑不够马！"))
                except: pass
            return
            
        # 100% 绝对读取面板上的配置值
        init_pool = cfg.get("hr_system_sponsor", 20)
        duration = cfg.get("hr_duration", 300)
        min_ticket = cfg.get("hr_base_ticket", 5)
        
        self.active_races[group_id] = {
            "active": True,
            "locked": False,
            "horses": top_users,
            "bets": {},
            "pool": init_pool,
            "max_players": cfg.get("hr_max_players", 50)
        }
        
        msg = f"🏁 【积分赛马梭哈】活动{'定时' if is_auto else '正式'}开始！\n"
        msg += f"本次出战的是财富榜【前 {len(top_users)} 名】的顶级富豪：\n"
        for idx, (uid, data) in enumerate(top_users):
            badge = "💥通缉犯" if data.get("is_red_name") else ""
            msg += f"{idx+1}号马：{data['name'][:8]} {badge}\n"
        
        msg += f"\n👉 发送「/押注 马号 金额」参与（如：/押注 1 {min_ticket}）\n"
        msg += f"💰 系统已注资初始奖池：{init_pool} 积分\n"
        msg += f"⏳ 下注倒计时：{duration}秒！"
        
        try: await event.send(event.plain_result(msg))
        except: pass
        
        asyncio.create_task(self.horse_race_ticker(group_id, duration, event))

    @filter.command("押注")
    async def bet_horse(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.last_events[group_id] = event
        if not self.is_group_enabled(group_id): return
        
        if group_id not in self.active_races or not self.active_races[group_id]["active"]:
            yield event.plain_result("目前没有正在举办的赛马活动哦！快呼叫管理员 /开启赛马")
            return
            
        race = self.active_races[group_id]
        if race["locked"]:
            yield event.plain_result("❌ 比赛已经锁盘开跑，停止下注啦！")
            return
            
        uid = event.get_sender_id()
        uname = event.get_sender_name()
        
        if uid in race["bets"]:
            yield event.plain_result("你已经押过注了，买定离手，不可贪心！")
            return
            
        if len(race["bets"]) >= race["max_players"]:
            yield event.plain_result("参赛人数已达上限，场地挤爆了，下注通道提前关闭！")
            return

        nums = []
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                nums.extend(re.findall(r'\d+', comp.text))

        if len(nums) < 1:
            yield event.plain_result("格式错误，请至少输入你要押的马号！例如：/押注 1")
            return

        horse_idx = int(nums[0]) - 1
        if horse_idx < 0 or horse_idx >= len(race["horses"]):
            yield event.plain_result("没有这匹马！请看清参赛名单上的编号！")
            return

        cfg = self.get_cfg()
        min_bet = cfg.get("hr_base_ticket", 5)
        amount = min_bet
        
        if len(nums) >= 2: amount = int(nums[1])
                
        if amount < min_bet:
            yield event.plain_result(f"押注金额太寒酸了，不得低于门票 {min_bet} 积分！")
            return
            
        user_data = self.get_user(group_id, uid, uname)
        if user_data["points"] < amount:
            yield event.plain_result(f"余额不足！你只有 {user_data['points']} 积分，押不起 {amount} 分。")
            return
            
        user_data["points"] -= amount
        self.save_data()
        
        race["bets"][uid] = {"horse_idx": horse_idx, "amount": amount, "name": uname}
        race["pool"] += amount
        
        yield event.plain_result(f"✅ 押注成功！花费 {amount} 积分重仓了【{horse_idx+1}号马】。\n当前总奖池滚雪球至：{race['pool']} 积分！")

    async def horse_race_ticker(self, group_id, duration, event: AstrMessageEvent):
        end_time = time.time() + duration
        
        while True:
            remaining = end_time - time.time()
            if remaining <= 0: break
                
            sleep_time = min(60, remaining)
            await asyncio.sleep(sleep_time)
            
            if group_id not in self.active_races or not self.active_races[group_id]["active"] or self.active_races[group_id]["locked"]:
                return
                
            if end_time - time.time() > 5:
                race = self.active_races[group_id]
                pool = race["pool"]
                horses = race["horses"]
                
                h1 = random.choice(horses)[1]['name'][:8]
                h2 = random.choice(horses)[1]['name'][:8]
                while h1 == h2 and len(horses) > 1:
                    h2 = random.choice(horses)[1]['name'][:8]
                
                events_text = [
                    f"📢 实况快报：{h1} 突然猛喝一口红牛，速度飙升，冲到了第一梯队！",
                    f"📢 赛场突发：惊险！{h2} 左脚绊右脚差点摔倒，目前正在努力调整身位！",
                    f"📢 实况快报：{h1} 企图弯道超车，却被 {h2} 用一套闪电五连鞭无情挡下！",
                    f"📢 赛场突发：赛道边出现不明胡萝卜！{h1} 受到诱惑，速度明显放缓！",
                    f"📢 实况快报：当前奖池已滚至 {pool} 积分！比赛进入白热化，还没下注的搞快点！",
                    f"📢 赛场突发：{h2} 大喊一声“键来！”，身上爆发出惊人的红光，正在疯狂狂飙！"
                ]
                try: await event.send(event.plain_result(random.choice(events_text)))
                except: pass
        
        if group_id not in self.active_races or not self.active_races[group_id]["active"]: return
        race = self.active_races[group_id]
        race["locked"] = True
        
        try: await event.send(event.plain_result(f"🔒 赛马场大门已关闭，停止下注！买定离手！正在计算最终冲线结果..."))
        except: pass
        await asyncio.sleep(3) 
        
        cfg = self.get_cfg()
        top_users = race["horses"]
        
        # 🎲 真·随机系统：从面板动态读取概率与倍率
        weights = [random.randint(50, 150) for _ in top_users]
        
        rich_curse_rate = cfg.get("hr_rich_curse", 0.15)
        is_cursed = False
        if random.random() < rich_curse_rate:
            weights[0] = 1 
            is_cursed = True
            
        red_buff = cfg.get("hr_red_name_buff", 1.5)
        for idx, (uid, data) in enumerate(top_users):
            if data.get("is_red_name"):
                weights[idx] = int(weights[idx] * red_buff)
                
        winner_idx = random.choices(range(len(top_users)), weights=weights, k=1)[0]
        winner_horse = top_users[winner_idx]
        
        pool = race["pool"]
        tax_rate = cfg.get("hr_tax_rate", 0.05)
        actual_pool = int(pool * (1 - tax_rate))
        
        winners = [(u, b) for u, b in race["bets"].items() if b["horse_idx"] == winner_idx]
        
        result_msg = "🏁 比赛结束！冲线啦！！\n\n"
        if is_cursed:
            result_msg += f"💥 【赛场异闻】：作为 1 号马的首富 【{top_users[0][1]['name'][:8]}】 因为起步没站稳摔了个狗吃屎！爆了大冷门！\n\n"
            
        result_msg += f"🏆 恭喜 【{winner_idx+1}号马 - {winner_horse[1]['name'][:8]}】 夺得冠军！\n"
        
        if not winners:
            result_msg += f"😭 竟然没有一个人押中这匹马！\n💸 高达 {pool} 积分的奖池全部被系统回收入库！"
        else:
            total_winning_bets = sum(b["amount"] for u, b in winners)
            result_msg += f"扣除 {int(tax_rate*100)}% 赛场卫生打扫费后，可瓜分总奖池为 {actual_pool} 积分！\n🎉 赢家分红榜："
            for uid, bet_info in winners:
                share = int((bet_info["amount"] / total_winning_bets) * actual_pool)
                self.users_data[group_id][uid]["points"] += share
                result_msg += f"\n- 狂徒 {bet_info['name'][:8]} 分走 {share} 积分！"
                
        self.save_data()
        race["active"] = False
        
        try: await event.send(event.plain_result(result_msg))
        except: pass

        # 👑 赛后生成冠军动图并发送
        try:
            winner_id = winner_horse[0]
            winner_name = winner_horse[1]['name'][:8] # 拿到马主人的名字(防止太长截断前8个字)
            
            champion_gif_path = await self._generate_meme_gif(
                "https://memers.tudouu.cn/api/memes/champion/",
                [winner_id],
                "champion",
                texts=[winner_name]  # <--- 在这里把名字发给接口
            )
            # 单独发冠军图
            await event.send(event.image_result(str(champion_gif_path)))
        except Exception as e:
            await event.send(event.plain_result(f"【冠军专属表情生成失败，原因：{str(e)[:50]}】"))
    # ================= 社交红包系统 =================

    @filter.command("发红包")
    async def send_red_packet(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.last_events[group_id] = event
        if not group_id or group_id == "None": return
        if not self.is_group_enabled(group_id): return
        
        if group_id not in self.active_red_packets:
            self.active_red_packets[group_id] = []
            
        uid = event.get_sender_id()
        uname = event.get_sender_name()
        
        nums = []
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                nums.extend(re.findall(r'\d+', comp.text))
                
        if len(nums) < 2:
            yield event.plain_result("格式错误！请使用：/发红包 [个数] [总积分]\n示例：/发红包 5 100")
            return
            
        count = int(nums[0])
        points = int(nums[1])
        
        if count <= 0 or points < count:
            yield event.plain_result("红包参数不合理（每个红包至少需要包含1积分）！")
            return
            
        user_data = self.get_user(group_id, uid, uname)
        if user_data["points"] < points:
            yield event.plain_result(f"你的余额不足以发这么大的红包哦，当前仅有：{user_data['points']} 积分。")
            return
            
        user_data["points"] -= points
        self.save_data()
        
        packet = {
            "id": int(time.time()),
            "sender": uname,
            "rem_count": count,
            "rem_points": points,
            "grabbed": []
        }
        self.active_red_packets[group_id].append(packet)
        yield event.plain_result(f"🧧 【{uname}】包了一个 {points} 积分的大红包（共{count}个）！\n👉 快发送 /抢红包 来拼手气吧！")

    @filter.command("抢红包")
    async def grab_red_packet(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        self.last_events[group_id] = event
        if not group_id or group_id == "None": return
        if not self.is_group_enabled(group_id): return
        
        if group_id not in self.active_red_packets or not self.active_red_packets[group_id]:
            yield event.plain_result("当前群里并没有可抢的红包哦，可以求大佬 /发红包 ~")
            return
            
        uid = event.get_sender_id()
        uname = event.get_sender_name()
        
        target_idx = -1
        for i, p in enumerate(self.active_red_packets[group_id]):
            if uid not in p["grabbed"]:
                target_idx = i
                break
                
        if target_idx == -1:
            yield event.plain_result("手慢了，红包已经被抢光了，或者你已经参与过当前的红包啦！")
            return
            
        packet = self.active_red_packets[group_id][target_idx]
        
        if packet["rem_count"] == 1:
            grab = packet["rem_points"]
        else:
            max_grab = max(1, (packet["rem_points"] // packet["rem_count"]) * 2)
            max_grab = min(max_grab, packet["rem_points"] - (packet["rem_count"] - 1))
            grab = random.randint(1, max(1, max_grab))
            
        packet["rem_count"] -= 1
        packet["rem_points"] -= grab
        packet["grabbed"].append(uid)
        
        user_data = self.get_user(group_id, uid, uname)
        user_data["points"] += grab
        self.save_data()
        
        msg = f"🧧 恭喜！你抢到了 {packet['sender']} 发的红包，获得 {grab} 积分！\n"
        if packet["rem_count"] <= 0:
            msg += "💥 最后一个红包被你拿下，该红包已被抢空！"
            self.active_red_packets[group_id].pop(target_idx)
        else:
            msg += f"（此红包还剩余 {packet['rem_count']} 个待抢）"
            
        yield event.plain_result(msg)

    # ================= 管理员权限指令 =================
    
    @filter.command("排行榜")
    async def leaderboard(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        self.last_events[group_id] = event
        if not self.is_group_enabled(group_id): return

        group_users = self.users_data.get(group_id, {})
        for uid in list(group_users.keys()):
            self.get_user(group_id, uid)
        group_users = self.users_data.get(group_id, {})

        sorted_users = sorted(group_users.items(), key=lambda x: x[1]["points"], reverse=True)
        top_users = sorted_users[:10]
        
        if not top_users:
            yield event.plain_result("本群暂无排行榜数据！大家快来签到吧~")
            return

        img_path = await self.draw_leaderboard_image(top_users, group_id)
        yield event.image_result(str(img_path))

    @filter.command("发工资")
    async def pay_salary(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None": return
        if not self.is_group_enabled(group_id): return

        cfg = self.get_cfg()
        if str(event.get_sender_id()) not in cfg.get("admin_qq", []):
            yield event.plain_result("🚫 权限不足！")
            return
            
        nums = re.findall(r'\d+', event.message_obj.message_str)
        if not nums:
            yield event.plain_result("请填写发工资的数量！例如：/发工资 5")
            return
            
        amount = int(nums[0])
        if amount <= 0: return

        count = 0
        for uid in list(self.users_data[group_id].keys()):
            self.users_data[group_id][uid]["points"] += amount
            count += 1
            
        self.save_data()
        yield event.plain_result(f"💰 普天同庆，发工资啦！\n已为本群 {count} 名登记在册的群友每人发放 {amount} 积分！")

    @filter.command("财富加")
    async def add_points(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None": return
        if not self.is_group_enabled(group_id): return

        cfg = self.get_cfg()
        if str(event.get_sender_id()) not in cfg.get("admin_qq", []):
            yield event.plain_result("🚫 权限不足！")
            return
            
        target_id = None
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                target_id = str(comp.qq)
                break
                
        if not target_id:
            yield event.plain_result("请 @ 你要增加积分的群友！")
            return

        amount = 0
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                nums = re.findall(r'\d+', comp.text)
                if nums:
                    amount = int(nums[0])
                    break
                    
        if amount <= 0: return
        target_data = self.get_user(group_id, target_id)
        target_data["points"] += amount
        self.save_data()
        yield event.plain_result(f"✅ 成功增加 {amount} 积分。余额：{target_data['points']}。")

    @filter.command("财富减")
    async def sub_points(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        if not group_id or group_id == "None": return
        if not self.is_group_enabled(group_id): return

        cfg = self.get_cfg()
        if str(event.get_sender_id()) not in cfg.get("admin_qq", []):
            yield event.plain_result("🚫 权限不足！")
            return
            
        target_id = None
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                target_id = str(comp.qq)
                break
                
        if not target_id: return
        amount = 0
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                nums = re.findall(r'\d+', comp.text)
                if nums:
                    amount = int(nums[0])
                    break
        if amount <= 0: return
        target_data = self.get_user(group_id, target_id)
        target_data["points"] = max(0, target_data["points"] - amount)
        self.save_data()
        yield event.plain_result(f"✅ 成功扣除 {amount} 积分。余额：{target_data['points']}。")
