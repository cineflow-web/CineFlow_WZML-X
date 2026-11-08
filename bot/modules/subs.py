from asyncio import sleep
from os import makedirs, path as ospath
from shutil import rmtree
from time import time

from pyrogram.types import InputMediaDocument

from .. import LOGGER
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task, sync_to_async
from ..helper.ext_utils.subs_utils import SUB_EXTS, process, skipped_message
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import (
    send_message,
    edit_message,
    delete_message,
)


def _doc(m):
    d = getattr(m, "document", None)
    if d and d.file_name and d.file_name.lower().endswith((".zip",) + SUB_EXTS):
        return d
    return None


async def _gather(message):
    """Collect (msg, doc) from the command msg / replied msg / whole media group."""
    msgs = []
    for m in (message.reply_to_message, message):
        if not m:
            continue
        if m.media_group_id:
            try:
                msgs += await TgClient.bot.get_media_group(m.chat.id, m.id)
                continue
            except Exception as e:
                LOGGER.warning(f"subs: media group fetch failed: {e}")
        msgs.append(m)
    seen, out = set(), []
    for m in msgs:
        if m.id in seen:
            continue
        seen.add(m.id)
        if d := _doc(m):
            out.append((m, d))
    return out


@new_task
async def subs_cmd(_, message):
    docs = await _gather(message)
    if not docs:
        return await send_message(
            message,
            f"<b>Usage:</b> reply to a sub <code>.zip</code> (or several zips / .srt) with "
            f"<code>/{BotCommands.SubsCommand[0]}</code>\n"
            "Output: <code>Name Year si.srt</code> files, sent individually.",
        )
    status = await send_message(message, f"<i>Downloading {len(docs)} file(s)...</i>")
    work = ospath.join("subs_tmp", f"{message.chat.id}_{message.id}_{int(time())}")
    makedirs(work, exist_ok=True)
    try:
        paths = []
        for m, d in docs:
            p = await m.download(file_name=ospath.join(work, "dl", d.file_name))
            if p:
                paths.append(str(p))
        await edit_message(status, "<i>Extracting & renaming...</i>")
        skipped = []
        files = await sync_to_async(
            process, paths, work, ospath.join(work, "out"), skipped
        )
        if not files:
            return await edit_message(
                status,
                skipped_message(skipped)
                if skipped
                else "No subtitle files found in the archive(s).",
            )
        await edit_message(status, f"<i>Uploading {len(files)} subtitle(s)...</i>")
        for i in range(0, len(files), 10):
            chunk = files[i : i + 10]
            if len(chunk) == 1:
                await message.reply_document(
                    document=chunk[0], disable_notification=True
                )
            else:
                await message.reply_media_group(
                    [InputMediaDocument(f) for f in chunk],
                    disable_notification=True,
                )
            await sleep(1)
        await delete_message(status)
    except Exception as e:
        LOGGER.error(f"subs: {e}", exc_info=True)
        await edit_message(status, f"Subs failed: <code>{e}</code>")
    finally:
        await sleep(1)
        rmtree(work, ignore_errors=True)
