"""
GitHub ZIP Uploader Bot — Telethon Edition
يدعم ملفات حتى 2GB عبر MTProto
"""

import os, io, base64, asyncio, zipfile, logging, time, tempfile
from dotenv import load_dotenv
import httpx
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeFilename

load_dotenv()

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    level=logging.INFO, datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
API_ID         = int(os.getenv("API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
GH_API         = "https://api.github.com"
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "10"))

# ─── State per user ────────────────────────────────────────────────────────────
# { user_id: { step, token, username, repo, private } }
STATE: dict[int, dict] = {}

STEP_TOKEN, STEP_REPO, STEP_PRIVATE, STEP_ZIP = range(4)

# ─── GitHub Client ─────────────────────────────────────────────────────────────
class GH:
    def __init__(self, token: str):
        self.h = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }
        self.c = httpx.AsyncClient(headers=self.h, timeout=120)

    async def get(self, p):
        r = await self.c.get(GH_API + p); r.raise_for_status(); return r.json()

    async def post(self, p, d):
        r = await self.c.post(GH_API + p, json=d); r.raise_for_status(); return r.json()

    async def put(self, p, d):
        r = await self.c.put(GH_API + p, json=d)
        if r.status_code not in (200, 201):
            raise Exception(f"GitHub {r.status_code}: {r.text[:200]}")
        return r.json()

    async def get_safe(self, p):
        r = await self.c.get(GH_API + p)
        return None if r.status_code == 404 else r.json()

    async def close(self): await self.c.aclose()


# ─── Upload Engine ─────────────────────────────────────────────────────────────
async def upload_tree(gh: GH, repo_full: str, files: dict[str, bytes], prog_cb=None):
    total = len(files)
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    blob_shas: dict[str, str | None] = {}

    async def make_blob(path, content):
        async with sem:
            try:
                b64 = base64.b64encode(content).decode()
                r = await gh.post(f"/repos/{repo_full}/git/blobs",
                                  {"content": b64, "encoding": "base64"})
                blob_shas[path] = r["sha"]
            except Exception as e:
                log.warning(f"blob fail {path}: {e}")
                blob_shas[path] = None
            if prog_cb:
                await prog_cb(len(blob_shas), total)

    await asyncio.gather(*[make_blob(p, c) for p, c in files.items()])

    tree = [{"path": p, "mode": "100644", "type": "blob", "sha": s}
            for p, s in blob_shas.items() if s]

    # base commit
    base_sha = None
    for branch in ("main", "master"):
        try:
            ref = await gh.get(f"/repos/{repo_full}/git/refs/heads/{branch}")
            base_sha = ref["object"]["sha"]; break
        except: pass

    tree_payload = {"tree": tree}
    if base_sha:
        cm = await gh.get(f"/repos/{repo_full}/git/commits/{base_sha}")
        tree_payload["base_tree"] = cm["tree"]["sha"]

    tree_data = await gh.post(f"/repos/{repo_full}/git/trees", tree_payload)

    cp = {"message": f"🚀 Upload {len(tree)} files via Telegram Bot", "tree": tree_data["sha"]}
    if base_sha: cp["parents"] = [base_sha]
    commit = await gh.post(f"/repos/{repo_full}/git/commits", cp)
    sha = commit["sha"]

    for method, path, body in [
        ("POST", f"/repos/{repo_full}/git/refs", {"ref": "refs/heads/main", "sha": sha}),
        ("PUT",  f"/repos/{repo_full}/git/refs/heads/main", {"sha": sha, "force": True}),
        ("PUT",  f"/repos/{repo_full}/git/refs/heads/master", {"sha": sha, "force": True}),
    ]:
        try:
            fn = gh.post if method == "POST" else gh.put
            await fn(path, body); break
        except: pass

    failed = sum(1 for s in blob_shas.values() if s is None)
    return len(tree), failed


def bar(done, total, w=18):
    p = done / total if total else 0
    f = int(p * w)
    return f"[{'█'*f}{'░'*(w-f)}] {done}/{total} ({int(p*100)}%)"


# ─── Bot ───────────────────────────────────────────────────────────────────────
bot = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)


@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(ev):
    uid = ev.sender_id
    STATE[uid] = {"step": STEP_TOKEN}
    await ev.respond(
        "🤖 **GitHub ZIP Uploader Bot**\n\n"
        "يرفع ملفات ZIP إلى GitHub — يدعم ملفات كبيرة بدون حد!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 **الخطوة 1/4:** أرسل GitHub Token\n\n"
        "_Settings → Developer Settings → Personal Access Tokens_\n"
        "_(تأكد من تفعيل صلاحية `repo`)_"
    )


@bot.on(events.NewMessage(pattern="/cancel"))
async def cmd_cancel(ev):
    STATE.pop(ev.sender_id, None)
    await ev.respond("❌ تم الإلغاء. أرسل /start للبدء من جديد.")


@bot.on(events.NewMessage(pattern="/help"))
async def cmd_help(ev):
    await ev.respond(
        "📖 **مساعدة**\n\n"
        "• `/start` — بدء رفع جديد\n"
        "• `/cancel` — إلغاء\n\n"
        "⚡ **الميزات:**\n"
        "• دعم ملفات حتى **2GB** (MTProto)\n"
        f"• رفع متوازٍ بـ {MAX_CONCURRENT} خيوط\n"
        "• Git Tree API — commit واحد لكل الملفات"
    )


@bot.on(events.CallbackQuery(pattern=b"(private|public)"))
async def cb_privacy(ev):
    uid = ev.sender_id
    s = STATE.get(uid, {})
    if s.get("step") != STEP_PRIVATE:
        return await ev.answer()

    is_private = ev.data == b"private"
    s["private"] = is_private
    s["step"] = STEP_ZIP
    label = "🔒 خاص (Private)" if is_private else "🌍 عام (Public)"
    await ev.edit(
        f"✅ **{label}**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📤 **الخطوة 4/4:** أرسل ملف ZIP الآن\n\n"
        "_لا يوجد حد للحجم — يدعم حتى 2GB_ 🚀"
    )
    await ev.answer()


@bot.on(events.NewMessage)
async def on_message(ev):
    if ev.sender_id is None or ev.via_bot_id:
        return

    uid  = ev.sender_id
    s    = STATE.get(uid)
    text = (ev.raw_text or "").strip()

    # تجاهل الأوامر
    if text.startswith("/"):
        return

    # ── لا توجد جلسة نشطة ──────────────────────────────────────────────────
    if not s:
        await ev.respond("أرسل /start للبدء.")
        return

    step = s.get("step")

    # ── STEP 1: Token ───────────────────────────────────────────────────────
    if step == STEP_TOKEN and text:
        try:
            await ev.delete()   # حذف التوكن للأمان
        except: pass
        msg = await ev.respond("🔍 جاري التحقق من التوكن...")
        try:
            gh = GH(text)
            user = await gh.get("/user")
            await gh.close()
            s["token"]    = text
            s["username"] = user["login"]
            s["step"]     = STEP_REPO
            await msg.edit(
                f"✅ **مرحباً {user['login']}!**\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📦 **الخطوة 2/4:** ما اسم المستودع؟\n\n"
                "_مثال: my-awesome-project_"
            )
        except Exception as e:
            await msg.edit(f"❌ فشل التحقق\n\n`{str(e)[:200]}`\n\nحاول مرة أخرى:")
        return

    # ── STEP 2: Repo name ───────────────────────────────────────────────────
    if step == STEP_REPO and text:
        repo = "".join(c for c in text.replace(" ", "-") if c.isalnum() or c in "-_.")
        if not repo:
            await ev.respond("❌ اسم غير صالح، حاول مرة أخرى:"); return
        s["repo"] = repo
        s["step"] = STEP_PRIVATE
        await ev.respond(
            f"📦 **الخطوة 3/4:** خصوصية `{repo}`",
            buttons=[
                [Button.inline("🔒 خاص (Private)", b"private"),
                 Button.inline("🌍 عام (Public)",   b"public")]
            ]
        )
        return

    # ── STEP 4: ZIP file ────────────────────────────────────────────────────
    if step == STEP_ZIP and ev.document:
        doc = ev.document

        # التحقق من الامتداد
        fname = ""
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                fname = attr.file_name; break
        if not fname.lower().endswith(".zip"):
            await ev.respond("❌ أرسل ملف `.zip` فقط"); return

        token     = s["token"]
        username  = s["username"]
        repo_name = s["repo"]
        is_priv   = s.get("private", False)
        size_mb   = doc.size / 1024 / 1024

        msg = await ev.respond(f"⏬ **جاري تنزيل الملف** ({size_mb:.1f} MB)...")
        t0  = time.time()

        try:
            # ── تنزيل عبر Telethon (MTProto — بدون حد) ───────────────────
            buf = io.BytesIO()

            async def dl_prog(received, total):
                pct = int(received / total * 100) if total else 0
                now = time.time()
                if not hasattr(dl_prog, "_last") or now - dl_prog._last > 2:
                    dl_prog._last = now
                    try:
                        await msg.edit(
                            f"⏬ **تنزيل...** {received/1024/1024:.1f}/{size_mb:.1f} MB "
                            f"({pct}%)\n`{bar(received, doc.size)}`"
                        )
                    except: pass

            await bot.download_media(ev.message, file=buf, progress_callback=dl_prog)
            buf.seek(0)

            dl_time = time.time() - t0
            await msg.edit(f"✅ تم التنزيل في {dl_time:.1f}s\n\n📂 **فك الضغط...**")

            # ── فك الضغط ─────────────────────────────────────────────────
            files: dict[str, bytes] = {}
            with zipfile.ZipFile(buf) as zf:
                for name in zf.namelist():
                    if not zf.getinfo(name).is_dir():
                        files[name] = zf.read(name)

            total_files = len(files)
            await msg.edit(
                f"✅ {total_files} ملف\n\n🚀 **إنشاء المستودع...**"
            )

            # ── إنشاء المستودع ────────────────────────────────────────────
            gh = GH(token)
            repo_full = f"{username}/{repo_name}"

            try:
                repo = await gh.post("/user/repos", {
                    "name": repo_name, "private": is_priv,
                    "auto_init": False,
                    "description": "Uploaded via Telegram Bot 🤖"
                })
            except Exception as e:
                if "already exists" in str(e).lower():
                    repo = await gh.get(f"/repos/{repo_full}")
                    await msg.edit(f"⚠️ المستودع موجود — سيتم الرفع إليه\n\n📤 **رفع {total_files} ملف...**")
                else:
                    raise

            repo_url = repo.get("html_url", f"https://github.com/{repo_full}")
            await msg.edit(f"📤 **رفع {total_files} ملف...**\n\n`{bar(0, total_files)}`")

            last_edit = [0.0]

            async def on_prog(done, total):
                now = time.time()
                if now - last_edit[0] < 2 and done < total: return
                last_edit[0] = now
                try:
                    await msg.edit(f"📤 **جاري الرفع...**\n\n`{bar(done, total)}`")
                except: pass

            # ── رفع عبر Git Tree API ──────────────────────────────────────
            uploaded, failed = await upload_tree(gh, repo_full, files, on_prog)
            await gh.close()

            total_time = time.time() - t0
            speed = size_mb / total_time if total_time > 0 else 0
            privacy_label = "🔒 خاص" if is_priv else "🌍 عام"

            fail_line = f"⚠️ فشل: {failed} ملف\n" if failed else ""
            await msg.edit(
                f"🎉 **اكتمل الرفع بنجاح!**\n\n"
                f"📦 `{repo_full}`\n"
                f"📁 {uploaded} ملف | {privacy_label}\n"
                f"{fail_line}"
                f"⚡ `{speed:.1f} MB/s` | ⏱ `{total_time:.1f}s`\n\n"
                f"🔗 {repo_url}",
                buttons=[[Button.url("🔗 فتح المستودع", repo_url),
                          Button.inline("🔄 رفع آخر", b"restart")]]
            )
            STATE.pop(uid, None)

        except zipfile.BadZipFile:
            await msg.edit("❌ **الملف ليس ZIP صالح**")
        except Exception as e:
            log.exception("Upload failed")
            await msg.edit(f"❌ **خطأ:** `{str(e)[:300]}`")
        return


@bot.on(events.CallbackQuery(pattern=b"restart"))
async def cb_restart(ev):
    uid = ev.sender_id
    STATE[uid] = {"step": STEP_TOKEN}
    await ev.edit(
        "🔄 **بداية جديدة!**\n\n"
        "🔐 **الخطوة 1/4:** أرسل GitHub Token:"
    )
    await ev.answer()


# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not all([BOT_TOKEN, API_ID, API_HASH]):
        log.error("❌ تأكد من إعداد BOT_TOKEN و API_ID و API_HASH في ملف .env")
    else:
        log.info("🤖 البوت يعمل (Telethon MTProto — يدعم ملفات 2GB)")
        bot.run_until_disconnected()
