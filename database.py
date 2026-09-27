from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI

client = AsyncIOMotorClient(MONGO_URI)

db = client["premium_bot"]

settings = db["settings"]
products = db["products"]
payments = db["payments"]
admins = db["admins"]
