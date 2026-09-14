import logging
import motor.motor_asyncio
from datetime import datetime, timezone
from config import DB_URL, DB_NAME  # Explicit imports are usually safer

logging.basicConfig(level=logging.INFO)

class NewsDB:
    def __init__(self, db_url: str, db_name: str):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(db_url)
        self.database = self.client[db_name]

        # Collections
        self.posted_news = self.database["posted_news"]
        self.rss_feeds = self.database["rss_feeds"]
        self.channels = self.database["channels"]

    # --- News Tracking ---

    async def is_posted(self, link: str) -> bool:
        try:
            result = await self.posted_news.find_one({"link": link})
            return result is not None
        except Exception as e:
            logging.error(f"Error checking if posted ({link}): {e}")
            return False

    async def mark_posted(self, link: str):
        try:
            await self.posted_news.update_one(
                {"link": link},
                {"$setOnInsert": {"link": link}},
                upsert=True
            )
        except Exception as e:
            logging.error(f"Error marking as posted ({link}): {e}")

    async def claim_post(self, link: str, key: str = None) -> bool:
        """
        Atomically claim a news item before broadcasting.
        Returns True if THIS call was the first to claim it (safe to broadcast),
        False if another call/source already claimed it.
        Uses MongoDB upsert — atomic, prevents cross-source duplicate posts.
        """
        dedup_key = key or link
        try:
            result = await self.posted_news.update_one(
                {"$or": [{"link": link}, {"key": dedup_key}]},
                {"$setOnInsert": {"link": link, "key": dedup_key}},
                upsert=True
            )
            # upserted_id is non-null only when a new document was inserted
            return result.upserted_id is not None
        except Exception as e:
            logging.error(f"Error claiming post ({link}): {e}")
            # On DB failure, fall back to check-based dedup (non-blocking)
            try:
                existing = await self.posted_news.find_one({"link": link})
                return existing is None
            except Exception:
                return True

    async def get_total_posted(self) -> int:
        """Gets the total number of articles ever posted."""
        try:
            return await self.posted_news.count_documents({})
        except Exception as e:
            logging.error(f"Error getting total posted count: {e}")
            return 0

    # --- RSS Feed Management ---

    async def add_rss_db(self, url: str):
        try:
            await self.rss_feeds.update_one(
                {"url": url},
                {"$setOnInsert": {"url": url}},
                upsert=True
            )
        except Exception as e:
            logging.error(f"Error adding RSS feed ({url}): {e}")

    async def rem_rss_db(self, url: str):
        try:
            await self.rss_feeds.delete_one({"url": url})
        except Exception as e:
            logging.error(f"Error removing RSS feed ({url}): {e}")

    async def get_all_rss(self) -> list:
        try:
            feeds = await self.rss_feeds.find({}).to_list(None)
            return [feed["url"] for feed in feeds if "url" in feed]
        except Exception as e:
            logging.error(f"Error fetching RSS feeds: {e}")

    # --- Enhanced Source Management (RSS + Scraper) ---

    async def add_source(self, source: dict):
        """
        Add a new RSS/scraper source with full metadata.
        Uses url as unique key to prevent duplicates.
        """
        try:
            await self.rss_feeds.update_one(
                {"url": source["url"]},
                {"$set": source},
                upsert=True
            )
        except Exception as e:
            logging.error(f"Error adding source ({source.get('url')}): {e}")

    async def get_all_sources(self) -> list:
        """
        Get all enabled sources (both RSS and scraper).
        Returns list of source documents.
        Compatible with old documents that have no 'enabled' field.
        """
        try:
            # Include documents where enabled is True OR enabled is missing (old format)
            sources = await self.rss_feeds.find({
                "$or": [
                    {"enabled": True},
                    {"enabled": {"$exists": False}}
                ]
            }).to_list(None)
            # Normalize: ensure every source has a 'type' field
            for source in sources:
                if "type" not in source:
                    source["type"] = "rss"
                    source["feed_url"] = source.get("url")
            return sources
        except Exception as e:
            logging.error(f"Error fetching sources: {e}")
            return []

    async def update_source(self, url: str, update: dict):
        """
        Update a source document by URL.
        """
        try:
            await self.rss_feeds.update_one(
                {"url": url},
                {"$set": update}
            )
        except Exception as e:
            logging.error(f"Error updating source ({url}): {e}")

    async def migrate_old_sources(self):
        """
        Migrate old RSS documents to new format.
        Adds 'enabled', 'type', and 'feed_url' fields to documents missing them.
        """
        try:
            # Update documents that don't have a 'type' field
            cursor = self.rss_feeds.find({"type": {"$exists": False}})
            docs = await cursor.to_list(None)
            for doc in docs:
                await self.rss_feeds.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "enabled": True,
                        "type": "rss",
                        "feed_url": doc.get("url"),
                        "created_at": datetime.now(timezone.utc),
                        "last_item": None
                    }}
                )
            if docs:
                logging.info(f"Migrated {len(docs)} old RSS documents to new format")
        except Exception as e:
            logging.error(f"Error migrating old sources: {e}")

    # --- Channel Management ---

    async def add_channel_db(self, chat_id: int):
        try:
            await self.channels.update_one(
                {"chat_id": int(chat_id)},
                {"$setOnInsert": {"chat_id": int(chat_id)}},
                upsert=True
            )
        except Exception as e:
            logging.error(f"Error adding channel ({chat_id}): {e}")

    async def rem_channel_db(self, chat_id: int):
        try:
            await self.channels.delete_one({"chat_id": int(chat_id)})
        except Exception as e:
            logging.error(f"Error removing channel ({chat_id}): {e}")

    async def get_all_channels(self) -> list:
        try:
            channels = await self.channels.find({}).to_list(None)
            return [ch["chat_id"] for ch in channels if "chat_id" in ch]
        except Exception as e:
            logging.error(f"Error fetching channels: {e}")
            return []

# Initialize the database instance
db = NewsDB(DB_URL, DB_NAME)
