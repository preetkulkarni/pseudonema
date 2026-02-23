import os
import yaml
import random
import logging
from typing import List, Optional, Tuple

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

class ParentDetails(BaseModel):
    category: str
    subcategory: str
    topics: List[str] = Field(default_factory=list)

class Trend(BaseModel):
    id: Optional[str] = None
    name: str
    context: str
    parent_details: ParentDetails
    status: str

class RemoteSubcategory(BaseModel):
    name: str
    topics: List[str] = Field(default_factory=list)
    urls: List[str] = Field(default_factory=list)

class RemoteCategory(BaseModel):
    name: str
    subcategories: List[RemoteSubcategory] = Field(default_factory=list)

class RemoteDataSchema(BaseModel):
    categories: List[RemoteCategory] = Field(default_factory=list)

class ConfigManager:
    def __init__(self, yaml_path: str = "policy.yaml"):
        self.yaml_path = yaml_path
        self.active_category: str = "technology"
        self.num_topics: int = 2
        self.num_trends: int = 6
        self.remote_data: List[RemoteCategory] = []

    async def initialize(self) -> None:
        """Asynchronously loads all configuration data."""
        self._load_policy()
        await self._load_remote_data()

    def _load_policy(self) -> None:
        """Loads local YAML policy. """
        try:
            with open(self.yaml_path, 'r') as f:
                data = yaml.safe_load(f) or {}
                
            self.active_category = data.get("active_category", "technology")
            self.num_topics = data.get("num_topics", 2)
            self.num_trends = data.get("num_trends", 6)
            logger.info(f"Policy loaded: category='{self.active_category}', num_topics={self.num_topics}, num_trends={self.num_trends}")
            
        except FileNotFoundError:
            logger.warning(f"Policy file '{self.yaml_path}' not found. Using defaults.")
        except yaml.YAMLError as e:
            logger.error(f"Malformed YAML in '{self.yaml_path}': {e}. Using defaults.")

    async def _load_remote_data(self) -> None:
        """Fetches and validates remote JSON data asynchronously."""
        url = os.getenv("REMOTE_DATA_URL")
        if not url:
            logger.warning("REMOTE_DATA_URL not set in environment variables. Remote config will be empty.")
            return

        try:
            logger.info(f"Fetching remote data from {url}...")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                raw_data = response.json()
                
            validated_data = RemoteDataSchema.model_validate(raw_data)
            self.remote_data = validated_data.categories
            logger.info(f"✅ Remote data loaded and validated ({len(self.remote_data)} categories found).")
            
        except httpx.HTTPError as e:
            logger.error(f"Network error fetching remote data: {e}")
        except ValidationError as e:
            logger.error(f"Data validation error (JSON structure mismatch): {e}")
        except Exception as e:
            logger.error(f"Unexpected error loading remote data: {e}", exc_info=True)

    def get_trends(self, excluded_topics: Optional[List[str]] = None) -> Tuple[int, str, str, List[str], List[str]]:
        """
        Selects a random subcategory and topics based on the active policy.
        """
        if not self.remote_data:
            raise ValueError("ConfigManager has no remote data loaded. Did you call `await initialize()`?")

        # Find the active category data
        active_cat_data = next((cat for cat in self.remote_data if cat.name == self.active_category), None)
        
        if not active_cat_data or not active_cat_data.subcategories:
            raise ValueError(f"No data or subcategories found for active category '{self.active_category}'.")

        # Select a random subcategory
        selected_subcat = random.choice(active_cat_data.subcategories)
        subcat_name = selected_subcat.name
        available_topics = selected_subcat.topics
        urls = selected_subcat.urls

        # Filter excluded topics
        if excluded_topics:
            available_topics = [topic for topic in available_topics if topic not in excluded_topics]
            
        if not available_topics:
            raise ValueError(f"No topics available in '{subcat_name}' after excluding: {excluded_topics}")

        # Sample topics
        sample_size = min(self.num_topics, len(available_topics))
        selected_topics = random.sample(available_topics, sample_size)

        return self.num_trends, self.active_category, subcat_name, selected_topics, urls