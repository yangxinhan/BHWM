import asyncio
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageSequence

# Discord 表情限制：檔案最大 256KB，建議尺寸 128x128，名稱 2~32 個英數字或底線
MAX_EMOJI_BYTES = 256 * 1024
EMOJI_SIZE = 128
ANIMATED_SIZES = (128, 112, 96, 80, 64, 48, 32)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def sanitize_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")
    if len(name) < 2:
        name = "emoji"
    return name[:32]


def fit_square(frame: Image.Image, size: int) -> Image.Image:
    """等比例縮放到 size x size 內，並置中貼在透明正方形畫布上。"""
    frame = frame.convert("RGBA")
    scale = size / max(frame.width, frame.height)
    new_size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
    frame = frame.resize(new_size, Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(frame, ((size - frame.width) // 2, (size - frame.height) // 2), frame)
    return canvas


def convert_to_emoji(data: bytes) -> tuple[bytes, bool]:
    """把圖片轉成 Discord 可用的表情，回傳 (圖片 bytes, 是否為動態)。"""
    img = Image.open(io.BytesIO(data))

    if not getattr(img, "is_animated", False):
        out = io.BytesIO()
        fit_square(img, EMOJI_SIZE).save(out, "PNG", optimize=True)
        return out.getvalue(), False

    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        durations.append(frame.info.get("duration", img.info.get("duration", 100)))
        frames.append(frame.copy())

    # 動圖容易超過 256KB，逐步縮小尺寸直到符合限制
    for size in ANIMATED_SIZES:
        resized = [fit_square(f, size) for f in frames]
        out = io.BytesIO()
        resized[0].save(
            out,
            "GIF",
            save_all=True,
            append_images=resized[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=True,
        )
        if out.tell() <= MAX_EMOJI_BYTES:
            return out.getvalue(), True

    raise ValueError("動圖縮到最小仍超過 256KB，請換一張幀數較少的圖")



@dataclass
class ImageSource:
    """一張待轉換的圖片：可能來自附件、嵌入（圖片連結 / Tenor）、轉發訊息或貼圖。"""

    filename: str
    url: str
    size: Optional[int] = None


def is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTS)


def filename_from_url(url: str) -> str:
    return Path(urlparse(url).path).name or "image"


def extract_images(message: discord.Message) -> list[ImageSource]:
    sources = []

    def add_from(attachments: list[discord.Attachment], embeds: list[discord.Embed]):
        for a in attachments:
            if is_image(a):
                sources.append(ImageSource(a.filename, a.url, a.size))
        for embed in embeds:
            media = embed.image if embed.image.url else embed.thumbnail
            if media.url:
                sources.append(ImageSource(filename_from_url(media.url), media.proxy_url or media.url))

    add_from(message.attachments, message.embeds)
    # 轉發的訊息，圖片放在 message_snapshots 裡而不是 attachments
    for snapshot in message.message_snapshots:
        add_from(snapshot.attachments, snapshot.embeds)
    for sticker in message.stickers:
        if sticker.format != discord.StickerFormatType.lottie:
            sources.append(ImageSource(sticker.name, sticker.url))
    return sources


async def download(session: aiohttp.ClientSession, source: ImageSource) -> bytes:
    chunks, total = [], 0
    async with session.get(source.url) as resp:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ValueError("檔案太大（超過 20MB）")
            chunks.append(chunk)
    return b"".join(chunks)


def free_slots(guild: discord.Guild, animated: bool) -> int:
    used = sum(1 for e in guild.emojis if e.animated == animated)
    return guild.emoji_limit - used


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook():
    await bot.tree.sync()


@bot.event
async def on_ready():
    print(f"已登入：{bot.user} (ID: {bot.user.id})")


async def collect_images(ctx: commands.Context, image: Optional[discord.Attachment]) -> list[ImageSource]:
    if ctx.interaction is not None:
        return [ImageSource(image.filename, image.url, image.size)] if image and is_image(image) else []

    # 文字指令：優先讀本則訊息的圖片，沒有的話讀「回覆的那則訊息」的圖片
    sources = extract_images(ctx.message)
    if not sources and ctx.message.reference and ctx.message.reference.message_id:
        ref = ctx.message.reference.resolved
        if not isinstance(ref, discord.Message):
            ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        sources = extract_images(ref)
    return sources


async def add_emojis(
    guild: discord.Guild,
    author: Union[discord.User, discord.Member],
    sources: list[ImageSource],
    name: Optional[str],
) -> list[str]:
    """把每張圖片轉換後新增為伺服器表情，回傳每張圖的結果訊息。"""
    results = []
    async with aiohttp.ClientSession() as session:
        for i, source in enumerate(sources, start=1):
            base = sanitize_name(name or Path(source.filename).stem)
            emoji_name = base if len(sources) == 1 else sanitize_name(f"{base[:29]}_{i}")

            if source.size and source.size > MAX_DOWNLOAD_BYTES:
                results.append(f"❌ `{source.filename}`：檔案太大（超過 20MB）")
                continue

            try:
                data = await download(session, source)
                emoji_bytes, animated = await asyncio.to_thread(convert_to_emoji, data)
            except Exception as e:
                results.append(f"❌ `{source.filename}`：圖片轉換失敗（{e}）")
                continue

            if free_slots(guild, animated) <= 0:
                kind = "動態" if animated else "靜態"
                results.append(f"❌ `{source.filename}`：伺服器的{kind}表情欄位已滿")
                continue

            try:
                emoji = await guild.create_custom_emoji(
                    name=emoji_name,
                    image=emoji_bytes,
                    reason=f"由 {author} 透過 addemoji 指令新增",
                )
            except discord.HTTPException as e:
                results.append(f"❌ `{source.filename}`：新增失敗（{e.text or e}）")
                continue

            results.append(f"✅ {emoji} `:{emoji.name}:`")

    return results


@bot.hybrid_command(name="addemoji", description="把訊息中的圖片轉成表情並新增到伺服器")
@app_commands.describe(name="表情名稱（英數字或底線，2~32 字）", image="要轉成表情的圖片")
@commands.guild_only()
@commands.has_guild_permissions(manage_emojis=True)
@commands.bot_has_guild_permissions(manage_emojis=True)
async def addemoji(ctx: commands.Context, name: Optional[str] = None, image: Optional[discord.Attachment] = None):
    """用法：
    !addemoji [名稱]  （附上圖片，或回覆一則有圖片的訊息）
    /addemoji name:<名稱> image:<圖片>
    """
    await ctx.defer()

    sources = await collect_images(ctx, image)
    if not sources:
        if ctx.interaction is not None:
            await ctx.send("請在 `image` 欄位附上圖片，或對訊息按右鍵 → 應用程式 → 新增為表情。")
        else:
            await ctx.send("找不到圖片！請在指令訊息附上圖片，或用指令回覆一則有圖片的訊息。")
        return

    results = await add_emojis(ctx.guild, ctx.author, sources, name)
    await ctx.send("\n".join(results))


class EmojiNameModal(discord.ui.Modal, title="新增為表情"):
    def __init__(self, sources: list[ImageSource]):
        super().__init__()
        self.sources = sources
        self.emoji_name = discord.ui.TextInput(
            label="表情名稱（英數字或底線，2~32 字）",
            default=sanitize_name(Path(sources[0].filename).stem),
            min_length=2,
            max_length=32,
        )
        self.add_item(self.emoji_name)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        results = await add_emojis(interaction.guild, interaction.user, self.sources, self.emoji_name.value)
        await interaction.followup.send("\n".join(results))


@bot.tree.context_menu(name="新增為表情")
@app_commands.guild_only()
@app_commands.default_permissions(manage_emojis=True)
async def add_emoji_from_message(interaction: discord.Interaction, message: discord.Message):
    if not interaction.permissions.manage_emojis:
        await interaction.response.send_message("你沒有「管理表情符號」權限，無法使用這個指令。", ephemeral=True)
        return
    if not interaction.app_permissions.manage_emojis:
        await interaction.response.send_message("機器人缺少「管理表情符號」權限，請先到伺服器設定開啟。", ephemeral=True)
        return

    sources = extract_images(message)
    if not sources:
        await interaction.response.send_message("這則訊息裡沒有圖片。", ephemeral=True)
        return

    await interaction.response.send_modal(EmojiNameModal(sources))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("你沒有「管理表情符號」權限，無法使用這個指令。")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("機器人缺少「管理表情符號」權限，請先到伺服器設定開啟。")
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("這個指令只能在伺服器中使用。")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        raise error


if __name__ == "__main__":
    load_dotenv()
    token = os.getenv("TOKEN")
    if not token:
        raise SystemExit("找不到 TOKEN，請在 .env 設定 TOKEN=你的機器人token")
    bot.run(token)
