"""python-telegram-bot application wiring."""
import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.bot import handlers
from app.bot.onboarding import ASKING, cancel, handle_choice, handle_text_answer, start_onboarding
from app.config import get_settings

log = logging.getLogger(__name__)

# Set by create_application so non-handler code (e.g. AI tools spawning
# background work) can send Telegram messages.
APPLICATION: Application | None = None

BOT_COMMANDS = [
    BotCommand("start", "Başla / tanış"),
    BotCommand("plan", "Bugünün öğün planı"),
    BotCommand("kilo", "Kilo kaydet (örn: /kilo 84.5)"),
    BotCommand("su", "Su kaydet (örn: /su 500)"),
    BotCommand("hedef", "Güncel hedeflerin"),
    BotCommand("rapor", "Günlük/haftalık/aylık rapor"),
    BotCommand("grafik", "İlerleme grafikleri"),
    BotCommand("alisveris", "Ortak alışveriş listesi"),
    BotCommand("ayarlar", "Hatırlatma ayarları"),
    BotCommand("yardim", "Yardım"),
]


async def start_router(update: Update, context) -> int:
    """Entry point: new users go through onboarding, known users get a greeting."""
    user = await handlers.get_or_create_user(update)
    if user.onboarding_state == "active":
        await handlers.cmd_start_active(update, context)
        return ConversationHandler.END
    return await start_onboarding(update, context)


def create_application() -> Application:
    global APPLICATION
    settings = get_settings()
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    APPLICATION = application

    # Group -1: allowlist gate runs before everything else.
    application.add_handler(TypeHandler(Update, handlers.guard_allowlist), group=-1)

    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", start_router)],
        states={
            ASKING: [
                CallbackQueryHandler(handle_choice, pattern=r"^onbm?:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_answer),
            ]
        },
        fallbacks=[CommandHandler("iptal", cancel)],
        allow_reentry=True,
    )
    application.add_handler(onboarding)

    application.add_handler(CommandHandler("yardim", handlers.cmd_help))
    application.add_handler(CommandHandler("help", handlers.cmd_help))
    application.add_handler(CommandHandler("kilo", handlers.cmd_kilo))
    application.add_handler(CommandHandler("su", handlers.cmd_su))
    application.add_handler(CommandHandler("plan", handlers.cmd_plan))
    application.add_handler(CommandHandler("hedef", handlers.cmd_hedef))
    application.add_handler(CommandHandler("alisveris", handlers.cmd_alisveris))
    application.add_handler(CommandHandler("rapor", handlers.cmd_rapor))
    application.add_handler(CommandHandler("grafik", handlers.cmd_grafik))
    application.add_handler(CommandHandler("foto", handlers.cmd_foto))
    application.add_handler(CommandHandler("ayarlar", handlers.cmd_ayarlar))

    # Fallback for onboarding buttons pressed by someone without an active
    # conversation (e.g. the partner tapping the other's question): the
    # ConversationHandler above won't consume those, so answer the callback
    # here to avoid an endless loading spinner on their client.
    application.add_handler(CallbackQueryHandler(handlers.cb_onboarding_foreign, pattern=r"^onbm?:"))

    application.add_handler(CallbackQueryHandler(handlers.cb_su, pattern=r"^su:"))
    application.add_handler(CallbackQueryHandler(handlers.cb_shopping, pattern=r"^shop:"))
    application.add_handler(CallbackQueryHandler(handlers.cb_rapor, pattern=r"^rapor:"))
    application.add_handler(CallbackQueryHandler(handlers.cb_grafik, pattern=r"^grafik:"))
    application.add_handler(CallbackQueryHandler(handlers.cb_ayar, pattern=r"^ayar:"))

    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handlers.on_new_chat_members)
    )
    application.add_handler(MessageHandler(filters.PHOTO, handlers.photo_handler))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handlers.voice_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.chat_handler))

    return application


async def start_bot(application: Application) -> None:
    await application.initialize()
    await application.bot.set_my_commands(BOT_COMMANDS)
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot polling started")


async def stop_bot(application: Application) -> None:
    if application.updater and application.updater.running:
        await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log.info("Telegram bot stopped")
