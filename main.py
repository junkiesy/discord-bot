import os
import csv
import json
import asyncio
from pathlib import Path
from datetime import timezone

import discord
from dotenv import load_dotenv


# =========================
# Basic setup
# =========================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env file")


DATA_DIR = Path("data")
CSV_DIR = DATA_DIR / "csv"
CHECKPOINT_FILE = DATA_DIR / "checkpoints.json"

DATA_DIR.mkdir(exist_ok=True)
CSV_DIR.mkdir(exist_ok=True)


# =========================
# Discord intents
# =========================

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


# =========================
# Checkpoint helpers
# =========================

def load_checkpoints() -> dict:
    if not CHECKPOINT_FILE.exists():
        return {}

    try:
        with CHECKPOINT_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError:
        print("WARNING: checkpoints.json is broken. Starting with empty checkpoints.")
        return {}


def save_checkpoints(checkpoints: dict) -> None:
    with CHECKPOINT_FILE.open("w", encoding="utf-8") as file:
        json.dump(checkpoints, file, indent=4)


def get_channel_checkpoint_key(guild: discord.Guild, channel: discord.TextChannel) -> str:
    return f"{guild.id}:{channel.id}"


# =========================
# CSV helpers
# =========================

CSV_HEADERS = [
    "message_id",
    "guild_id",
    "guild_name",
    "channel_id",
    "channel_name",
    "author_id",
    "author_name",
    "author_display_name",
    "author_bot",
    "created_at_utc",
    "edited_at_utc",
    "content",
    "clean_content",
    "jump_url",
    "attachments",
    "embeds_count",
    "reactions",
    "reply_to_message_id",
]


def safe_filename(name: str) -> str:
    """
    Makes a safe filename from a Discord server/channel name.
    """
    keep = []
    for char in name:
        if char.isalnum() or char in ("-", "_"):
            keep.append(char)
        elif char in (" ", ".", "#"):
            keep.append("_")

    cleaned = "".join(keep).strip("_")
    return cleaned or "unknown"


def get_csv_path(guild: discord.Guild, channel: discord.TextChannel) -> Path:
    guild_part = safe_filename(guild.name)
    channel_part = safe_filename(channel.name)

    filename = f"{guild_part}_{guild.id}__{channel_part}_{channel.id}.csv"
    return CSV_DIR / filename


def ensure_csv_has_header(csv_path: Path) -> None:
    if csv_path.exists() and csv_path.stat().st_size > 0:
        return

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_HEADERS)
        writer.writeheader()


def message_to_row(
    message: discord.Message,
    guild: discord.Guild,
    channel: discord.TextChannel
) -> dict:
    attachments = [
        {
            "filename": attachment.filename,
            "url": attachment.url,
            "content_type": attachment.content_type,
            "size": attachment.size,
        }
        for attachment in message.attachments
    ]

    reactions = [
        {
            "emoji": str(reaction.emoji),
            "count": reaction.count,
        }
        for reaction in message.reactions
    ]

    reply_to_message_id = None
    if message.reference and message.reference.message_id:
        reply_to_message_id = message.reference.message_id

    return {
        "message_id": message.id,
        "guild_id": guild.id,
        "guild_name": guild.name,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "author_id": message.author.id,
        "author_name": str(message.author),
        "author_display_name": getattr(message.author, "display_name", ""),
        "author_bot": message.author.bot,
        "created_at_utc": message.created_at.astimezone(timezone.utc).isoformat(),
        "edited_at_utc": (
            message.edited_at.astimezone(timezone.utc).isoformat()
            if message.edited_at
            else ""
        ),
        "content": message.content,
        "clean_content": message.clean_content,
        "jump_url": message.jump_url,
        "attachments": json.dumps(attachments, ensure_ascii=False),
        "embeds_count": len(message.embeds),
        "reactions": json.dumps(reactions, ensure_ascii=False),
        "reply_to_message_id": reply_to_message_id or "",
    }


# =========================
# Channel archive logic
# =========================

async def archive_channel(
    guild: discord.Guild,
    channel: discord.TextChannel,
    checkpoints: dict
) -> int:
    """
    Archives one text channel.
    Returns number of new messages saved.
    """

    checkpoint_key = get_channel_checkpoint_key(guild, channel)
    last_message_id = checkpoints.get(checkpoint_key)

    csv_path = get_csv_path(guild, channel)
    ensure_csv_has_header(csv_path)

    after = None
    if last_message_id:
        after = discord.Object(id=int(last_message_id))

    print()
    print(f"Archiving #{channel.name} in {guild.name}")
    print(f"CSV: {csv_path}")

    if after:
        print(f"Continuing after checkpoint message ID: {last_message_id}")
    else:
        print("No checkpoint found. Starting from beginning.")

    saved_count = 0
    newest_message_id = int(last_message_id) if last_message_id else None

    try:
        with csv_path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_HEADERS)

            async for message in channel.history(
                limit=None,
                after=after,
                oldest_first=True
            ):
                row = message_to_row(message, guild, channel)
                writer.writerow(row)

                saved_count += 1
                newest_message_id = message.id

                if saved_count % 100 == 0:
                    print(f"  Saved {saved_count} messages from #{channel.name}...")

        if newest_message_id:
            checkpoints[checkpoint_key] = str(newest_message_id)
            save_checkpoints(checkpoints)

        print(f"Finished #{channel.name}. New messages saved: {saved_count}")

    except discord.Forbidden:
        print(f"SKIPPED #{channel.name}: missing permission.")
    except discord.HTTPException as error:
        print(f"ERROR in #{channel.name}: Discord HTTP error: {error}")
    except Exception as error:
        print(f"ERROR in #{channel.name}: {error}")

    return saved_count


async def archive_all_servers():
    checkpoints = load_checkpoints()

    total_channels_seen = 0
    total_channels_accessible = 0
    total_messages_saved = 0

    print(f"Logged in as {client.user}")
    print(f"Connected to {len(client.guilds)} server(s).")

    for guild in client.guilds:
        print()
        print("=" * 60)
        print(f"Server: {guild.name} ({guild.id})")
        print("=" * 60)

        text_channels = list(guild.text_channels)
        total_channels_seen += len(text_channels)

        print(f"Text channels found: {len(text_channels)}")

        accessible_channels = []

        for channel in text_channels:
            permissions = channel.permissions_for(guild.me)

            if permissions.view_channel and permissions.read_message_history:
                accessible_channels.append(channel)
            else:
                print(f"Not accessible: #{channel.name}")

        total_channels_accessible += len(accessible_channels)

        print(f"Accessible text channels: {len(accessible_channels)}")

        for channel in accessible_channels:
            saved = await archive_channel(guild, channel, checkpoints)
            total_messages_saved += saved

            # Small pause to be polite with Discord API.
            await asyncio.sleep(1)

    print()
    print("=" * 60)
    print("ARCHIVE COMPLETE")
    print("=" * 60)
    print(f"Total text channels seen: {total_channels_seen}")
    print(f"Total accessible channels: {total_channels_accessible}")
    print(f"Total new messages saved: {total_messages_saved}")


@client.event
async def on_ready():
    await archive_all_servers()
    await client.close()


client.run(TOKEN)