import os
import logging
import asyncio
import feedparser
import trafilatura
import pandas as pd
import io
import httpx
from typing import List, Dict
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from supabase import create_client, Client
from fake_useragent import UserAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ScoutAgent")

class ScrapedArticle(BaseModel):
    source: str
    title: str
    url: str
    summary: str
    published_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

class ScoutAgent:
    def __init__(self):
        self.supabase_url = os.environ.get("SUPABASE_URL")
        self.supabase_key = os.environ.get("SUPABASE_KEY")
        self.feeds_csv_url = os.environ.get("FEEDS_CSV_URL")
        
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("❌ CRITICAL: Supabase credentials missing.")
            
        self.supabase: Client = create_client(self.supabase_url, self.supabase_key)
        self.ua = UserAgent()

    async def _load_feeds_from_remote(self) -> Dict[str, List[str]]:
        feed_config = {
            "tech_news": [],
            "reddit_sub": [],
            "security_news": [],
            "ml_ai_news": [],
            "dev_blog": [],
            "open_source": []
        }

        if not self.feeds_csv_url:
            logger.warning("⚠️ No FEEDS_CSV_URL found. Using empty config (will rely on dynamic search).")
            return feed_config

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self.feeds_csv_url)
                response.raise_for_status()
                
                df = pd.read_csv(io.StringIO(response.text))

                for _, row in df.iterrows():
                    category = str(row['Category']).strip()
                    url = str(row['URL']).strip()
                    
                    if url and url.startswith("http"):
                        if category in feed_config:
                            feed_config[category].append(url)
                        else:
                            feed_config.setdefault(category, []).append(url)
                            
            count = sum(len(v) for v in feed_config.values())
            logger.info(f"✅ Loaded {count} feeds from remote source.")
            return feed_config

        except Exception as e:
            logger.error(f"❌ Failed to load remote feeds: {e}")
            return feed_config

    def _get_source_label(self, category: str) -> str:
        """
        Maps your Google Sheet categories to a clean DB 'source' label.
        """
        if "reddit" in category:
            return "Reddit"
        elif "security" in category:
            return "Security"
        elif "ai" in category or "ml" in category:
            return "AI/ML"
        elif "blog" in category:
            return "Blog"
        elif "open_source" in category:
            return "Open Source"
        else:
            return "News"

    def _fetch_full_text(self, url: str) -> str:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                if text: return text
        except Exception:
            pass
        return ""

    def _parse_single_feed(self, feed_url: str, topic: str, source_label: str) -> List[ScrapedArticle]:
        articles = []
        headers = {'User-Agent': self.ua.random}
        
        try:
            d = feedparser.parse(feed_url, request_headers=headers)
            
            for entry in d.entries[:5]:
                title = entry.get("title", "No Title")
                link = entry.get("link", "")
                summary_raw = entry.get("summary", "")

                search_text = (title + " " + summary_raw).lower()
                if topic.lower() not in search_text:
                    continue

                summary_text = summary_raw
                if len(summary_text) < 300:
                    full_text = self._fetch_full_text(link)
                    if full_text:
                        summary_text = full_text[:3000]
                    else:
                        summary_text = f"Content unavailable. Original: {summary_text}"

                article = ScrapedArticle(
                    title=title,
                    url=link,
                    summary=summary_text,
                    source=source_label
                )
                articles.append(article)
                
        except Exception as e:
            logger.error(f"❌ Error parsing {feed_url}: {e}")
            
        return articles

    async def run_scout(self, topic: str):
        logger.info(f"🕵️ Scout starting for: {topic}")

        feed_config = await self._load_feeds_from_remote()

        try:
            session_data = {"topic": topic, "status": "scouting"}
            response = self.supabase.table("research_sessions").insert(session_data).execute()
            session_id = response.data[0]['id']
        except Exception as e:
            logger.critical(f"❌ Database Error: {e}")
            return 0

        collected_articles: List[ScrapedArticle] = []
        all_tasks = []
        loop = asyncio.get_running_loop()

        for category, urls in feed_config.items():
            label = self._get_source_label(category)
            
            for url in urls:
                all_tasks.append(
                    loop.run_in_executor(None, self._parse_single_feed, url, topic, label)
                )

        reddit_search_urls = [
            f"https://www.reddit.com/r/technology/search.rss?q={topic}&restrict_sr=1&sort=top&t=week",
            f"https://www.reddit.com/search.rss?q={topic}&sort=top&t=week"
        ]
        for url in reddit_search_urls:
            all_tasks.append(
                loop.run_in_executor(None, self._parse_single_feed, url, topic, "Reddit Search")
            )

        results = await asyncio.gather(*all_tasks)
        for res in results:
            collected_articles.extend(res)

        if collected_articles:
            logger.info(f"💾 Saving {len(collected_articles)} articles...")
            db_records = [
                {
                    "session_id": session_id,
                    "source": item.source,
                    "title": item.title,
                    "url": item.url,
                    "summary": item.summary,
                    "published_at": item.published_at
                }
                for item in collected_articles
            ]
            self.supabase.table("raw_news").insert(db_records).execute()
        else:
            logger.warning("⚠️ No articles found.")
            
        return session_id