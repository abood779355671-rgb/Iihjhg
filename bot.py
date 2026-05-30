"""
GitHub ZIP Uploader Bot for Telegram
رفع ملفات ZIP إلى GitHub عبر تيليجرام مع تحسينات الأداء
"""

import os
import io
import base64
import asyncio
import zipfile
import logging
import time
from typing import Optional
from dotenv import load_dotenv

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

load_dotenv()

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ─── States ────────────────────────────────────────────────────────────────────
ASK_TOKEN, ASK_REPO, ASK_PRIVATE, WAIT_ZIP = range(4)

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
GH_API          = "https://api.github.com"
MAX_CONCURRENT  = int(os.getenv("MAX_CONCURRENT", "10"))   # رفع متوازٍ
MAX_FILE_MB     = int(os.getenv("MAX_FILE_MB", "100"))      # حد الحجم
BATCH_SIZE      = int(os.getenv("BATCH_SIZE", "50"))        # حجم الـ batch لـ Tree API

# ─── GitHub async client ───────────────────────────────────────────────────────
class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        self.client = httpx.AsyncClient(headers=self.headers, timeout=60)

    async def get(self, path: str) -> dict:
        r = await self.client.get(f"{GH_API}{path}")
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, data: dict) -> dict:
        r = await self.client.post(f"{GH_API}{path}", json=data)
        r.raise_for_status()
        return r.json()

    async def put(self, path: str, data: dict) -> dict:
        r = await self.client.put(f"{GH_API}{path}", json=data)
        if r.status_code not in (200, 201):
            raise httpx.HTTPStatusError(
                f"GitHub {r.status_code}: {r.text}", request=r.request, response=r
            )
        return r.json()

    async def get_safe(self, path: str) -> Optional[dict]:
        """Returns None on 404"""
        r = await self.client.get(f"{GH_API}{path}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def close(self):
        await self.client.aclose()


# ─── Upload engine ─────────────────────────────────────────────────────────────
class UploadEngine:
    """
    استراتيجيتان للرفع:
    1. Git Tree API  → رفع كل الملفات في commit واحد (أسرع للملفات الكثيرة)
    2. Contents API  → رفع ملف واحد في كل مرة (fallback للملفات الكبيرة جداً)
    """

    def __init__(self, client: GitHubClient, repo_full: str,
                 progress_cb=None, log_cb=None):
        self.gh = client
        self.repo = repo_full
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def _emit(self, msg: str):
        if self.log_cb:
            await self.log_cb(msg)

    async def _prog(self, done: int, total: int):
        if self.progress_cb:
            await self.progress_cb(done, total)

    # ── Blob API (upload raw content, return sha) ────────────────────────────
    async def _create_blob(self, content: bytes) -> str:
        b64 = base64.b64encode(content).decode()
        data = await self.gh.post(f"/repos/{self.repo}/git/blobs", {
            "content": b64,
            "encoding": "base64"
        })
        return data["sha"]

    async def _create_blob_safe(self, path: str, content: bytes, results: dict, idx: int, total: int):
        async with self._semaphore:
            try:
                sha = await self._create_blob(content)
                results[path] = sha
                await self._prog(len(results), total)
            except Exception as e:
                log.warning(f"Blob failed for {path}: {e}")
                results[path] = None

    # ── Git Tree strategy (fast) ─────────────────────────────────────────────
    async def upload_via_tree(self, files: dict[str, bytes]) -> str:
        """
        files: {path: content}
        Returns: commit URL
        """
        entries = list(files.items())
        total = len(entries)

        await self._emit(f"📡 رفع {total} ملف عبر Git Tree API...")

        # 1. رفع كل الملفات كـ blobs بشكل متوازٍ
        blob_shas: dict[str, Optional[str]] = {}
        tasks = [
            self._create_blob_safe(path, content, blob_shas, i, total)
            for i, (path, content) in enumerate(entries)
        ]
        await asyncio.gather(*tasks)

        failed = [p for p, s in blob_shas.items() if s is None]
        if failed:
            await self._emit(f"⚠️ فشل {len(failed)} ملف، سيتم تخطيها")

        # 2. بناء شجرة الملفات
        tree = [
            {"path": path, "mode": "100644", "type": "blob", "sha": sha}
            for path, sha in blob_shas.items() if sha
        ]

        # 3. الحصول على الـ commit الحالي (إن وجد)
        base_sha = None
        try:
            ref_data = await self.gh.get(f"/repos/{self.repo}/git/refs/heads/main")
            base_sha = ref_data["object"]["sha"]
        except Exception:
            try:
                ref_data = await self.gh.get(f"/repos/{self.repo}/git/refs/heads/master")
                base_sha = ref_data["object"]["sha"]
            except Exception:
                pass

        # 4. إنشاء Tree
        tree_payload = {"tree": tree}
        if base_sha:
            # الحصول على tree القديم
            commit_data = await self.gh.get(f"/repos/{self.repo}/git/commits/{base_sha}")
            tree_payload["base_tree"] = commit_data["tree"]["sha"]

        tree_data = await self.gh.post(f"/repos/{self.repo}/git/trees", tree_payload)

        # 5. إنشاء Commit
        commit_payload = {
            "message": f"🚀 Upload {len(tree)} files via Telegram Bot\n\n"
                       f"Files: {len(tree)} | Failed: {len(failed)}",
            "tree": tree_data["sha"]
        }
        if base_sha:
            commit_payload["parents"] = [base_sha]

        commit_data = await self.gh.post(f"/repos/{self.repo}/git/commits", commit_payload)
        commit_sha = commit_data["sha"]

        # 6. تحديث الـ ref
        try:
            await self.gh.post(f"/repos/{self.repo}/git/refs", {
                "ref": "refs/heads/main",
                "sha": commit_sha
            })
        except Exception:
            try:
                await self.gh.put(f"/repos/{self.repo}/git/refs/heads/main", {
                    "sha": commit_sha, "force": True
                })
            except Exception:
                await self.gh.put(f"/repos/{self.repo}/git/refs/heads/master", {
                    "sha": commit_sha, "force": True
                })

        await self._emit(f"✅ تم إنشاء commit يحتوي على {len(tree)} ملف")
        return commit_data.get("html_url", "")

    # ── Contents API strategy (fallback) ─────────────────────────────────────
    async def _upload_single(self, path: str, content: bytes,
                              done_ref: list, total: int, sem: asyncio.Semaphore):
        async with sem:
            b64 = base64.b64encode(content).decode()
            # فحص هل الملف موجود
            existing = await self.gh.get_safe(f"/repos/{self.repo}/contents/{path}")
            payload = {"message": f"upload {path}", "content": b64}
            if existing and isinstance(existing, dict) and "sha" in existing:
                payload["sha"] = existing["sha"]
            await self.gh.put(f"/repos/{self.repo}/contents/{path}", payload)
            done_ref[0] += 1
            await self._prog(done_ref[0], total)

    async def upload_via_contents(self, files: dict[str, bytes]) -> None:
        total = len(files)
        done_ref = [0]
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        tasks = [
            self._upload_single(path, content, done_ref, total, sem)
            for path, content in files.items()
        ]
        await asyncio.gather(*tasks)


# ─── Bot helpers ───────────────────────────────────────────────────────────────
def kb_yes_no(yes_text="✅ نعم", no_text="❌ لا", yes_cb="yes", no_cb="no"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(yes_text, callback_data=yes_cb),
        InlineKeyboardButton(no_text,  callback_data=no_cb),
    ]])

def progress_bar(done: int, total: int, width: int = 20) -> str:
    pct = done / total if total else 0
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {done}/{total} ({int(pct*100)}%)"


# ─── Conversation handlers ─────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "🤖 *GitHub ZIP Uploader Bot*\n\n"
        "يرفع ملفات ZIP إلى GitHub بشكل سريع وذكي.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 *الخطوة 1/4:* أرسل لي GitHub Token الخاص بك\n\n"
        "_يمكنك إنشاء token من:_\n"
        "Settings → Developer Settings → Personal Access Tokens\n"
        "_(تأكد من تفعيل صلاحية `repo`)_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_TOKEN


async def got_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    # حذف رسالة التوكن فوراً للأمان
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text("🔍 جاري التحقق من التوكن...")

    try:
        client = GitHubClient(token)
        user = await client.get("/user")
        await client.close()
        ctx.user_data["token"] = token
        ctx.user_data["username"] = user["login"]
        await msg.edit_text(
            f"✅ *مرحباً {user['login']}!*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📦 *الخطوة 2/4:* ما اسم المستودع الذي تريد إنشاءه؟\n\n"
            "_مثال: my-awesome-project_",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASK_REPO
    except Exception as e:
        await msg.edit_text(
            f"❌ *فشل التحقق من التوكن*\n\n`{str(e)[:200]}`\n\nحاول مرة أخرى:",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASK_TOKEN


async def got_repo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    repo_name = update.message.text.strip().replace(" ", "-")
    # تنظيف اسم المستودع
    repo_name = "".join(c for c in repo_name if c.isalnum() or c in "-_.")
    if not repo_name:
        await update.message.reply_text("❌ اسم المستودع غير صالح، حاول مرة أخرى:")
        return ASK_REPO

    ctx.user_data["repo"] = repo_name
    await update.message.reply_text(
        f"📦 *الخطوة 3/4:* خصوصية المستودع `{repo_name}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_yes_no("🔒 خاص (Private)", "🌍 عام (Public)", "private", "public")
    )
    return ASK_PRIVATE


async def got_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    is_private = q.data == "private"
    ctx.user_data["private"] = is_private

    privacy_icon = "🔒 خاص" if is_private else "🌍 عام"
    await q.message.edit_text(
        f"✅ المستودع سيكون *{privacy_icon}*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 *الخطوة 4/4:* أرسل ملف ZIP الآن\n\n"
        f"_الحد الأقصى: {MAX_FILE_MB} MB_",
        parse_mode=ParseMode.MARKDOWN
    )
    return WAIT_ZIP


async def got_zip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ أرسل ملف ZIP فقط")
        return WAIT_ZIP

    if not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("❌ الملف يجب أن يكون بصيغة `.zip`")
        return WAIT_ZIP

    if doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.message.reply_text(
            f"❌ حجم الملف كبير جداً! الحد الأقصى هو {MAX_FILE_MB} MB"
        )
        return WAIT_ZIP

    token    = ctx.user_data["token"]
    username = ctx.user_data["username"]
    repo_name = ctx.user_data["repo"]
    is_private = ctx.user_data.get("private", False)

    status_msg = await update.message.reply_text(
        "⏬ *جاري تنزيل الملف...*",
        parse_mode=ParseMode.MARKDOWN
    )

    start_time = time.time()

    try:
        # ── تنزيل الملف ──────────────────────────────────────────────────────
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()

        dl_time = time.time() - start_time
        await status_msg.edit_text(
            f"✅ تم التنزيل ({len(file_bytes)/1024/1024:.1f} MB في {dl_time:.1f}s)\n\n"
            "📂 *جاري فك ضغط الملف...*",
            parse_mode=ParseMode.MARKDOWN
        )

        # ── فك الضغط ─────────────────────────────────────────────────────────
        zip_buffer = io.BytesIO(bytes(file_bytes))
        files: dict[str, bytes] = {}
        with zipfile.ZipFile(zip_buffer) as zf:
            for name in zf.namelist():
                info = zf.getinfo(name)
                if info.is_dir():
                    continue
                files[name] = zf.read(name)

        total_files = len(files)
        await status_msg.edit_text(
            f"✅ الملف يحتوي على *{total_files}* ملف\n\n"
            "🚀 *جاري إنشاء المستودع...*",
            parse_mode=ParseMode.MARKDOWN
        )

        # ── إنشاء المستودع ────────────────────────────────────────────────────
        client = GitHubClient(token)
        repo_full = f"{username}/{repo_name}"

        try:
            repo = await client.post("/user/repos", {
                "name": repo_name,
                "private": is_private,
                "auto_init": False,
                "description": f"Uploaded via Telegram Bot 🤖"
            })
            await status_msg.edit_text(
                f"✅ تم إنشاء المستودع `{repo_full}`\n\n"
                f"📤 *رفع {total_files} ملف...*",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            if "already exists" in str(e).lower() or "name already exists" in str(e).lower():
                repo = await client.get(f"/repos/{repo_full}")
                await status_msg.edit_text(
                    f"⚠️ المستودع موجود، سيتم الرفع إليه\n\n"
                    f"📤 *رفع {total_files} ملف...*",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                raise

        repo_url = repo.get("html_url", f"https://github.com/{repo_full}")

        # ── رفع الملفات ───────────────────────────────────────────────────────
        last_edit = [0.0]

        async def on_progress(done: int, total: int):
            now = time.time()
            if now - last_edit[0] < 2 and done < total:   # تحديث كل 2 ثانية
                return
            last_edit[0] = now
            bar = progress_bar(done, total)
            try:
                await status_msg.edit_text(
                    f"📤 *جاري الرفع...*\n\n`{bar}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

        async def on_log(msg_text: str):
            log.info(msg_text)

        engine = UploadEngine(client, repo_full, on_progress, on_log)

        upload_start = time.time()
        await engine.upload_via_tree(files)
        upload_time = time.time() - upload_start

        total_time = time.time() - start_time
        size_mb = len(file_bytes) / 1024 / 1024
        speed = size_mb / upload_time if upload_time > 0 else 0

        await client.close()

        # ── رسالة النجاح ─────────────────────────────────────────────────────
        privacy_label = "🔒 خاص" if is_private else "🌍 عام"
        await status_msg.edit_text(
            f"🎉 *اكتمل الرفع بنجاح!*\n\n"
            f"📦 المستودع: `{repo_full}`\n"
            f"📁 الملفات: *{total_files}* ملف\n"
            f"🔐 الخصوصية: {privacy_label}\n"
            f"⚡ السرعة: `{speed:.1f} MB/s`\n"
            f"⏱ الوقت: `{total_time:.1f}s`\n\n"
            f"🔗 [افتح المستودع]({repo_url})",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 فتح المستودع", url=repo_url),
                InlineKeyboardButton("🔄 رفع مستودع آخر", callback_data="restart")
            ]])
        )

    except zipfile.BadZipFile:
        await status_msg.edit_text("❌ *الملف ليس ZIP صالح*", parse_mode=ParseMode.MARKDOWN)
    except httpx.HTTPStatusError as e:
        await status_msg.edit_text(
            f"❌ *خطأ في GitHub API*\n\n`{e.response.text[:300]}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        log.exception("Upload failed")
        await status_msg.edit_text(
            f"❌ *خطأ غير متوقع*\n\n`{str(e)[:300]}`",
            parse_mode=ParseMode.MARKDOWN
        )

    return ConversationHandler.END


async def restart_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await q.message.edit_text(
        "🔄 *بداية جديدة!*\n\n"
        "🔐 أرسل GitHub Token الخاص بك:",
        parse_mode=ParseMode.MARKDOWN
    )
    return ASK_TOKEN


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء. أرسل /start للبدء من جديد.")
    return ConversationHandler.END


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *مساعدة GitHub ZIP Uploader Bot*\n\n"
        "🔹 `/start` — بدء عملية رفع جديدة\n"
        "🔹 `/cancel` — إلغاء العملية الحالية\n"
        "🔹 `/help` — عرض هذه الرسالة\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *تحسينات الأداء:*\n"
        f"• رفع متوازٍ بـ {MAX_CONCURRENT} خيوط\n"
        "• Git Tree API: commit واحد لكل الملفات\n"
        f"• الحد الأقصى للحجم: {MAX_FILE_MB} MB\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 *إنشاء GitHub Token:*\n"
        "github.com → Settings → Developer Settings\n"
        "→ Personal Access Tokens → Fine-grained\n"
        "→ تفعيل صلاحية `repo`",
        parse_mode=ParseMode.MARKDOWN
    )


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN غير موجود! أضفه في ملف .env")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_TOKEN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_token)],
            ASK_REPO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_repo)],
            ASK_PRIVATE:[CallbackQueryHandler(got_private, pattern="^(private|public)$")],
            WAIT_ZIP:   [MessageHandler(filters.Document.ALL, got_zip)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(restart_cb, pattern="^restart$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(restart_cb, pattern="^restart$"))

    log.info("🤖 البوت يعمل الآن...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
