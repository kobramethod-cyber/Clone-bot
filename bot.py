import os
import io
import asyncio
import logging
from typing import Dict, Any, List
from flask import Flask
from threading import Thread

import qrcode
from PIL import Image

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from motor.motor_asyncio import AsyncIOMotorClient

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 1. FLASK KEEP-ALIVE SERVER (FOR RENDER)
# ==========================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ==========================================
# 2. CONFIG & ENVIRONMENT VARIABLES
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MONGO_URI = os.environ.get("MONGO_URI", "YOUR_MONGODB_URI_HERE")
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789"))

# ==========================================
# 3. CONVERSATION STATES (EXACTLY 16 STATES FIXED)
# ==========================================
(
    WAITING_SCREENSHOT,
    ADMIN_ADD_PRODUCT_NAME,
    ADMIN_ADD_PRODUCT_PRICE,
    ADMIN_ADD_PRODUCT_LINK,
    ADMIN_EDIT_PRODUCT_SELECT,
    ADMIN_EDIT_PRODUCT_NAME,
    ADMIN_EDIT_PRODUCT_PRICE,
    ADMIN_EDIT_PRODUCT_LINK,
    ADMIN_DELETE_PRODUCT_SELECT,
    ADMIN_SET_PRICE,
    ADMIN_SET_UPI,
    ADMIN_SET_TEXT,
    ADMIN_SET_PHOTO,
    ADMIN_SET_VIDEO,
    ADMIN_SET_SUPPORT,
    ADMIN_BROADCAST,
) = range(10, 26)  # Fixed: Exact 16 values for 16 variables (10 to 25)

# Additional states for custom payment methods & extra admin controls
(
    ADMIN_ADD_PAYMENT_NAME,
    ADMIN_ADD_PAYMENT_DETAILS,
    ADMIN_DELETE_PAYMENT_SELECT,
    ADMIN_ADD_NEW_ADMIN,
    ADMIN_REMOVE_ADMIN_SELECT,
) = range(30, 35)

# ==========================================
# 4. DATABASE HANDLER (MOTOR / MONGODB)
# ==========================================
class Database:
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client["telegram_bot_db"]
        self.users = self.db["users"]
        self.settings = self.db["settings"]
        self.products = self.db["products"]
        self.admins = self.db["admins"]
        self.payment_methods = self.db["payment_methods"]
        self.purchases = self.db["purchases"]

    async def init_defaults(self):
        settings = await self.settings.find_one({"_id": "config"})
        if not settings:
            await self.settings.insert_one({
                "_id": "config",
                "price": "199",
                "upi_id": "example@upi",
                "welcome_text": "Welcome to Premium Access Bot! Choose an option below:",
                "photo_url": None,
                "video_url": None,
                "support_username": "AdminSupport"
            })
        
        owner = await self.admins.find_one({"user_id": OWNER_ID})
        if not owner:
            await self.admins.insert_one({"user_id": OWNER_ID, "added_by": "System"})

    async def add_user(self, user_id: int, username: str):
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"username": username, "last_active": asyncio.get_event_loop().time()}},
            upsert=True
        )

    async def get_all_users(self):
        cursor = self.users.find({})
        return await cursor.to_list(length=None)

    async def get_user_count(self) -> int:
        return await self.users.count_documents({})

    async def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        admin = await self.admins.find_one({"user_id": user_id})
        return admin is not None

    async def add_admin(self, user_id: int, added_by: int):
        await self.admins.update_one(
            {"user_id": user_id},
            {"$set": {"added_by": added_by}},
            upsert=True
        )

    async def remove_admin(self, user_id: int):
        if user_id != OWNER_ID:
            await self.admins.delete_one({"user_id": user_id})

    async def get_all_admins(self):
        cursor = self.admins.find({})
        return await cursor.to_list(length=None)

    async def get_config(self) -> Dict[str, Any]:
        return await self.settings.find_one({"_id": "config"}) or {}

    async def update_config(self, key: str, value: Any):
        await self.settings.update_one(
            {"_id": "config"},
            {"$set": {key: value}},
            upsert=True
        )

    # Products DB Methods
    async def add_product(self, name: str, price: str, link: str):
        await self.products.insert_one({
            "name": name,
            "price": price,
            "link": link
        })

    async def get_all_products(self):
        cursor = self.products.find({})
        return await cursor.to_list(length=None)

    async def get_product_by_id(self, product_id: str):
        from bson.objectid import ObjectId
        try:
            return await self.products.find_one({"_id": ObjectId(product_id)})
        except Exception:
            return None

    async def update_product(self, product_id: str, name: str, price: str, link: str):
        from bson.objectid import ObjectId
        await self.products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {"name": name, "price": price, "link": link}}
        )

    async def delete_product(self, product_id: str):
        from bson.objectid import ObjectId
        await self.products.delete_one({"_id": ObjectId(product_id)})

    # Payment Methods DB
    async def add_payment_method(self, name: str, details: str):
        await self.payment_methods.insert_one({"name": name, "details": details})

    async def get_all_payment_methods(self):
        cursor = self.payment_methods.find({})
        return await cursor.to_list(length=None)

    async def delete_payment_method(self, method_id: str):
        from bson.objectid import ObjectId
        await self.payment_methods.delete_one({"_id": ObjectId(method_id)})

    # Purchase History DB
    async def record_purchase(self, user_id: int, username: str, item_name: str, amount: str):
        await self.purchases.insert_one({
            "user_id": user_id,
            "username": username,
            "item_name": item_name,
            "amount": amount,
            "timestamp": asyncio.get_event_loop().time()
        })

    async def get_total_sales_count(self) -> int:
        return await self.purchases.count_documents({})

db = Database(MONGO_URI)

# ==========================================
# 5. HELPER FUNCTIONS
# ==========================================
def generate_upi_qr(upi_id: str, amount: str, name: str = "Premium Access") -> io.BytesIO:
    upi_url = f"upi://pay?pa={upi_id}&pn={name}&am={amount}&cu=INR"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    bio = io.BytesIO()
    bio.name = 'qr.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

# ==========================================
# 6. USER HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.add_user(user.id, user.username or "NoUsername")
    
    config = await db.get_config()
    text = config.get("welcome_text", "Welcome to Premium Bot!")
    photo = config.get("photo_url")
    video = config.get("video_url")
    
    keyboard = [
        [InlineKeyboardButton("💳 Buy Premium Access", callback_data="buy_main")],
        [InlineKeyboardButton("📦 Products Catalog", callback_data="products_catalog")],
        [InlineKeyboardButton("🌐 Other Payment Methods", callback_data="custom_payments_user")],
        [InlineKeyboardButton("💬 Support", url=f"https://t.me/{config.get('support_username', '')}")]
    ]
    
    if await db.is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_panel")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if video:
        await update.message.reply_video(video=video, caption=text, reply_markup=reply_markup)
    elif photo:
        await update.message.reply_photo(photo=photo, caption=text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=text, reply_markup=reply_markup)

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    config = await db.get_config()
    text = config.get("welcome_text", "Welcome to Premium Bot!")
    
    keyboard = [
        [InlineKeyboardButton("💳 Buy Premium Access", callback_data="buy_main")],
        [InlineKeyboardButton("📦 Products Catalog", callback_data="products_catalog")],
        [InlineKeyboardButton("🌐 Other Payment Methods", callback_data="custom_payments_user")],
        [InlineKeyboardButton("💬 Support", url=f"https://t.me/{config.get('support_username', '')}")]
    ]
    
    if await db.is_admin(query.from_user.id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_panel")])
        
    await query.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    config = await db.get_config()
    price = config.get("price", "199")
    upi_id = config.get("upi_id", "example@upi")
    
    context.user_data["buying_item"] = "Main Premium Access"
    context.user_data["buying_price"] = price
    
    qr_img = generate_upi_qr(upi_id, price)
    caption = (
        f"🛍 **Item:** Premium Access\n"
        f"💰 **Amount:** ₹{price}\n"
        f"📌 **UPI ID:** `{upi_id}`\n\n"
        f"1️⃣ Above QR code ko scan karke payment karein.\n"
        f"2️⃣ Payment complete karne ke baad **Screenshot** yahan send karein."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]]
    
    await query.message.reply_photo(
        photo=qr_img,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SCREENSHOT

async def products_catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    products = await db.get_all_products()
    
    if not products:
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.message.reply_text("📂 Abhi koi alag product available nahi hai.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
        
    keyboard = []
    for prod in products:
        prod_id = str(prod["_id"])
        keyboard.append([InlineKeyboardButton(f"{prod['name']} - ₹{prod['price']}", callback_data=f"buy_prod_{prod_id}")])
        
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
    await query.message.reply_text("📦 **Available Products:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def buy_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    prod_id = query.data.replace("buy_prod_", "")
    product = await db.get_product_by_id(prod_id)
    
    if not product:
        await query.message.reply_text("❌ Product nahi mila.")
        return
        
    config = await db.get_config()
    upi_id = config.get("upi_id", "example@upi")
    
    context.user_data["buying_item"] = product["name"]
    context.user_data["buying_price"] = product["price"]
    context.user_data["product_link"] = product["link"]
    
    qr_img = generate_upi_qr(upi_id, product["price"])
    caption = (
        f"🛍 **Item:** {product['name']}\n"
        f"💰 **Amount:** ₹{product['price']}\n"
        f"📌 **UPI ID:** `{upi_id}`\n\n"
        f"1️⃣ Above QR scan karke payment karein.\n"
        f"2️⃣ Payment ke baad **Screenshot** yahan bheinjiye."
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Catalog", callback_data="products_catalog")]]
    
    await query.message.reply_photo(
        photo=qr_img,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SCREENSHOT

async def custom_payments_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    methods = await db.get_all_payment_methods()
    if not methods:
        text = "ℹ️ Currently only UPI payment is active."
    else:
        text = "🌐 **Other Payment Options:**\n\n"
        for m in methods:
            text += f"🔹 **{m['name']}**:\n`{m['details']}`\n\n"
        text += "Payment karne ke baad screenshot yahan send karein."
        
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo_file = update.message.photo[-1]
    
    item_name = context.user_data.get("buying_item", "Premium Access")
    price = context.user_data.get("buying_price", "N/A")
    prod_link = context.user_data.get("product_link", "Main Premium Group")
    
    caption = (
        f"📩 **New Payment Screenshot**\n\n"
        f"👤 **User:** @{user.username or 'N/A'} (`{user.id}`)\n"
        f"🛍 **Item:** {item_name}\n"
        f"💰 **Amount:** ₹{price}\n"
        f"🔗 **Target Content:** `{prod_link}`"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")
        ]
    ]
    
    # Save target link temporarily for approval process
    context.bot_data[f"pending_link_{user.id}"] = prod_link
    context.bot_data[f"pending_item_{user.id}"] = item_name
    context.bot_data[f"pending_amount_{user.id}"] = price
    
    await context.bot.send_photo(
        chat_id=OWNER_ID,
        photo=photo_file.file_id,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    await update.message.reply_text(
        "✅ Aapka screenshot mil gaya hai! Admin verify karke jald hi aapko access link bhej denge."
    )
    return ConversationHandler.END

# ==========================================
# 7. ADMIN HANDLERS & CONTROL PANEL
# ==========================================
async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not await db.is_admin(query.from_user.id):
        await query.message.reply_text("❌ Aap Admin nahi hain.")
        return
        
    keyboard = [
        [InlineKeyboardButton("💵 Change Price", callback_data="admin_set_price"), InlineKeyboardButton("💳 Change UPI ID", callback_data="admin_set_upi")],
        [InlineKeyboardButton("📝 Edit Welcome Text", callback_data="admin_set_text"), InlineKeyboardButton("🖼 Edit Start Photo", callback_data="admin_set_photo")],
        [InlineKeyboardButton("🎥 Edit Video Tutorial", callback_data="admin_set_video"), InlineKeyboardButton("💬 Support Username", callback_data="admin_set_support")],
        [InlineKeyboardButton("📦 Product Management", callback_data="admin_prod_mgmt")],
        [InlineKeyboardButton("🌐 Custom Payment Methods", callback_data="admin_pay_mgmt")],
        [InlineKeyboardButton("👥 Manage Admins", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📊 View Stats", callback_data="admin_stats"), InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    
    await query.message.reply_text("⚙️ **Admin Control Panel**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_count = await db.get_user_count()
    sales_count = await db.get_total_sales_count()
    admins = await db.get_all_admins()
    
    text = (
        f"📊 **Bot Statistics:**\n\n"
        f"👤 **Total Users:** {user_count}\n"
        f"🎉 **Total Successful Sales:** {sales_count}\n"
        f"👑 **Total Admins:** {len(admins)}\n"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# Product Management Callbacks
async def admin_prod_mgmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add_prod")],
        [InlineKeyboardButton("✏️ Edit Product", callback_data="admin_edit_prod")],
        [InlineKeyboardButton("🗑 Delete Product", callback_data="admin_del_prod")],
        [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
    ]
    
    await query.message.reply_text("📦 **Product Management:**", reply_markup=InlineKeyboardMarkup(keyboard))

# Add Product Steps
async def admin_add_prod_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Product ka Naam enter karein:")
    return ADMIN_ADD_PRODUCT_NAME

async def admin_add_prod_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_prod_name"] = update.message.text.strip()
    await update.message.reply_text("Product ka Price enter karein (e.g. 299):")
    return ADMIN_ADD_PRODUCT_PRICE

async def admin_add_prod_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_prod_price"] = update.message.text.strip()
    await update.message.reply_text("Product ka Content/Invite Link enter karein:")
    return ADMIN_ADD_PRODUCT_LINK

async def admin_add_prod_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    name = context.user_data.get("new_prod_name")
    price = context.user_data.get("new_prod_price")
    
    await db.add_product(name, price, link)
    await update.message.reply_text(f"✅ Product **{name}** (₹{price}) successfully add ho gaya!", parse_mode="Markdown")
    return ConversationHandler.END

# Price Change Steps
async def admin_set_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Main Access ka Naya Price enter karein (e.g. 199):")
    return ADMIN_SET_PRICE

async def admin_set_price_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_price = update.message.text.strip()
    await db.update_config("price", new_price)
    await update.message.reply_text(f"✅ Price updated to ₹{new_price}")
    return ConversationHandler.END

# UPI Change Steps
async def admin_set_upi_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Naya UPI ID enter karein:")
    return ADMIN_SET_UPI

async def admin_set_upi_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_upi = update.message.text.strip()
    await db.update_config("upi_id", new_upi)
    await update.message.reply_text(f"✅ UPI ID updated to `{new_upi}`", parse_mode="Markdown")
    return ConversationHandler.END

# Text Change Steps
async def admin_set_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Naya Welcome Text enter karein:")
    return ADMIN_SET_TEXT

async def admin_set_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_text = update.message.text.strip()
    await db.update_config("welcome_text", new_text)
    await update.message.reply_text("✅ Welcome Text update ho gaya hai!")
    return ConversationHandler.END

# Photo Change Steps
async def admin_set_photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Start Photo bheinjiye:")
    return ADMIN_SET_PHOTO

async def admin_set_photo_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file = update.message.photo[-1]
    await db.update_config("photo_url", photo_file.file_id)
    await update.message.reply_text("✅ Start Photo update ho gayi!")
    return ConversationHandler.END

# Video Change Steps
async def admin_set_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Tutorial Video bheinjiye:")
    return ADMIN_SET_VIDEO

async def admin_set_video_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_file = update.message.video
    await db.update_config("video_url", video_file.file_id)
    await update.message.reply_text("✅ Video Tutorial update ho gaya!")
    return ConversationHandler.END

# Support Change Steps
async def admin_set_support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Support Username enter karein (without @):")
    return ADMIN_SET_SUPPORT

async def admin_set_support_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sup_user = update.message.text.strip().replace("@", "")
    await db.update_config("support_username", sup_user)
    await update.message.reply_text(f"✅ Support Username set to @{sup_user}")
    return ConversationHandler.END

# Broadcast Message Steps
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Broadcast ke liye Message enter karein:")
    return ADMIN_BROADCAST

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    users = await db.get_all_users()
    
    count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=message_text)
            count += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
            
    await update.message.reply_text(f"✅ Broadcast complete! Sent to {count} users.")
    return ConversationHandler.END

# Payment Approval System
async def payment_approval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    
    action, target_user_id = data.split("_")
    target_user_id = int(target_user_id)
    
    prod_link = context.bot_data.get(f"pending_link_{target_user_id}", "Contact Admin")
    item_name = context.bot_data.get(f"pending_item_{target_user_id}", "Premium Access")
    amount = context.bot_data.get(f"pending_amount_{target_user_id}", "199")
    
    if action == "approve":
        msg = (
            f"🎉 **Payment Approved!**\n\n"
            f"Item: {item_name}\n"
            f"Aapka link yeh raha: {prod_link}"
        )
        await context.bot.send_message(chat_id=target_user_id, text=msg, parse_mode="Markdown")
        await query.message.edit_caption(caption=f"{query.message.caption}\n\nSTATUS: ✅ **Approved**")
        
        user_obj = await context.bot.get_chat(target_user_id)
        await db.record_purchase(target_user_id, user_obj.username or "N/A", item_name, amount)
        
    elif action == "reject":
        await context.bot.send_message(
            chat_id=target_user_id,
            text="❌ **Payment Verification Failed!**\nPlease contact support if this was a mistake."
        )
        await query.message.edit_caption(caption=f"{query.message.caption}\n\nSTATUS: ❌ **Rejected**")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled operation.")
    return ConversationHandler.END

# ==========================================
# 8. MAIN BOT RUNNER
# ==========================================
def main():
    # Start Keep Alive Web Server
    keep_alive()
    
    # Initialize DB Async Loop
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.init_defaults())
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # User Conversation Handler
    user_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(buy_main_callback, pattern="^buy_main$"),
            CallbackQueryHandler(buy_product_callback, pattern="^buy_prod_"),
        ],
        states={
            WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, handle_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    # Admin Settings Conversation Handler
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_set_price_start, pattern="^admin_set_price$"),
            CallbackQueryHandler(admin_set_upi_start, pattern="^admin_set_upi$"),
            CallbackQueryHandler(admin_set_text_start, pattern="^admin_set_text$"),
            CallbackQueryHandler(admin_set_photo_start, pattern="^admin_set_photo$"),
            CallbackQueryHandler(admin_set_video_start, pattern="^admin_set_video$"),
            CallbackQueryHandler(admin_set_support_start, pattern="^admin_set_support$"),
            CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast$"),
            CallbackQueryHandler(admin_add_prod_start, pattern="^admin_add_prod$"),
        ],
        states={
            ADMIN_SET_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_price_save)],
            ADMIN_SET_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_upi_save)],
            ADMIN_SET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_text_save)],
            ADMIN_SET_PHOTO: [MessageHandler(filters.PHOTO, admin_set_photo_save)],
            ADMIN_SET_VIDEO: [MessageHandler(filters.VIDEO, admin_set_video_save)],
            ADMIN_SET_SUPPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_support_save)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)],
            ADMIN_ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_name)],
            ADMIN_ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_price)],
            ADMIN_ADD_PRODUCT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_prod_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(user_conv)
    application.add_handler(admin_conv)
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(products_catalog_callback, pattern="^products_catalog$"))
    application.add_handler(CallbackQueryHandler(custom_payments_user_callback, pattern="^custom_payments_user$"))
    application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_stats_callback, pattern="^admin_stats$"))
    application.add_handler(CallbackQueryHandler(admin_prod_mgmt_callback, pattern="^admin_prod_mgmt$"))
    application.add_handler(CallbackQueryHandler(payment_approval_handler, pattern="^(approve|reject)_"))
    
    print("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
