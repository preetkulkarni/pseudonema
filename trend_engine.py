# trend.engine.py

import logging
from typing import List, Optional

from pydantic import BaseModel, TypeAdapter, ValidationError
from tavily import AsyncTavilyClient
from supabase import AsyncClient

from google import genai
from google.genai import types

from config import Trend, ParentDetails

logger = logging.getLogger(__name__)

class LLMTrend(BaseModel):
    name: str
    context: str

class TrendEngine:
    def __init__(
        self, 
        tavily_client: AsyncTavilyClient, 
        llm_client: genai.Client, 
        db_client: AsyncClient,
        model_name: str = "gemini-3.0-flash" 
    ) -> None:
        """
        Dependency Injection ensures the engine doesn't manage its own API connections.
        """
        self.tavily = tavily_client
        self.llm_client = llm_client
        self.db = db_client
        self.model_name = model_name

    async def fetch_and_generate_trends(
        self,
        num_trends: int,
        category: str,  
        subcategory: str, 
        topics: List[str], 
        urls: List[str],
        excluded_topics: Optional[List[str]] = None
    ) -> List[Trend]:
        """
        Executes the Pivot Search via Tavily, synthesizes trends via Gemini (Strict JSON), 
        and saves the structured output to Supabase.
        """
        logger.info(f"Starting Trend Engine for: {subcategory}")

        # Instantiate the exact Pydantic model for parent details
        parent_details = ParentDetails(
            category=category,
            subcategory=subcategory,
            topics=topics
        )

        # Format the topics into a readable list
        topics_str = ", ".join(topics) if topics else "emerging trends and core developments"
        
        # Format exclusions using the standard search minus operator
        exclusions_str = ""
        if excluded_topics:
            exclusions_str = " " + " ".join([f"-{ex}" for ex in excluded_topics])
            
        # Construct the comprehensive natural language directive
        tavily_query = (
            f"What are the most significant recent developments, critical news updates, "
            f"and trending discussions concerning {subcategory}? "
            f"Specifically focus on current events, breakthroughs, and challenges related to {topics_str}. "
            f"Return highly specific, factual, and recent reporting.{exclusions_str}"
        )
            
        logger.info(f"Tavily Query: {tavily_query}")
        
        # 2. Execute Async Tavily Search
        search_kwargs = {
            "query": tavily_query,
            "topic": "news", # can be kept as general by removing this or set to "web" for broader results
            "search_depth": "advanced",
            "max_results": 15,
            "time_range": "week",
            "chunks_per_source": 5
        }
        if urls:
            search_kwargs["include_domains"] = urls

        try:
            search_response = await self.tavily.search(**search_kwargs)
        except Exception as e:
            logger.error(f"Tavily Search failed: {e}")
            return []

        # 3. Prepare Context for the LLM
        raw_results = search_response.get("results", [])
        if not raw_results:
            logger.warning("No results found from Tavily.")
            return []
            
        # Sort results by relevance score, highest first
        sorted_results = sorted(raw_results, key=lambda x: x.get('score', 0), reverse=True)
        
        formatted_context = ""
        for i, res in enumerate(sorted_results, 1):
            formatted_context += f"### Source [{i}]\n"
            formatted_context += f"- **Title:** {res.get('title', 'N/A')}\n"
            formatted_context += f"- **URL:** {res.get('url', 'N/A')}\n"
            formatted_context += f"- **Relevance Score:** {res.get('score', 0):.2f}\n"
            formatted_context += f"- **Content:** {res.get('content', '')}\n\n"
            
        formatted_tavily_results = formatted_context.strip()

        # 4. Prompt Gemini to Synthesize Trends
        llm_prompt = f"""
        You are an expert tech analyst specializing in identifying and summarizing emerging trends in the {subcategory} domain.
        Based on the following recent search results, extract and synthesize the top {num_trends} most significant trends. Each trend should be highly specific, grounded in the provided evidence, and directly relevant to the core topics of {subcategory}.
        
        CONTEXT (Recent Search Results):
        =========================
        {formatted_tavily_results}
        =========================

        EXTRACTION GUIDELINES:
        1. High Specificity (`name`): Extract highly specific entities, new frameworks, or distinct methodologies. Use the "Title" and "Content" fields to formulate an accurate, professional name for the trend.
        2. Grounded Evidence (`context`): Provide a concise, 1-to-2 sentence explanation of exactly *why* this is currently trending. This MUST be strictly derived from the provided "Content". 
        3. Prioritize Relevance: Pay special attention to sources with higher "Relevance Scores". If multiple sources mention the same trend, synthesize the context to create a stronger summary.
        4. Source Quality: Use the "URL" field to silently gauge the credibility of the information, but do not include the URL in your final output.

        OUTPUT FORMAT:
        Return a strictly valid JSON array of trend objects.
        """
        
        logger.info("Sending context to Gemini for structured synthesis...")
        try:
            response = await self.llm_client.aio.models.generate_content(
                model=self.model_name,
                contents=llm_prompt,
                config=types.GenerateContentConfig(
                    system_instruction="You are an expert research assistant. Your objective is to analyze the provided search results and extract the most significant emerging trends, newly reported challenges, and active developments.",
                    response_mime_type="application/json",
                    response_schema=list[LLMTrend],
                    temperature=0.4
                )
            )
            
            # 5. Parse and map to the final Trend schema
            adapter = TypeAdapter(List[LLMTrend])
            llm_trends: List[LLMTrend] = adapter.validate_json(response.text)
            
            trends_data: List[Trend] = []
            for t in llm_trends:
                trends_data.append(
                    Trend(
                        name=t.name,
                        context=t.context,
                        parent_details=parent_details,
                        status="false" 
                    )
                )
            
        except ValidationError as ve:
            logger.error(f"Gemini returned invalid data format: {ve}")
            return []
        except Exception as e:
            logger.error(f"Gemini synthesis failed: {e}")
            return []

        # 6. Save to Supabase via BATCH Insert
        logger.info(f"Saving {len(trends_data)} trends to database...")
        try:
            db_payloads = [trend.model_dump(exclude_none=True) for trend in trends_data]
            res = await self.db.table("trends").insert(db_payloads).execute()
            
            if res.data:
                inserted_trends = [Trend.model_validate(row) for row in res.data]
                logger.info("✅ Trend generation complete.")
                return inserted_trends
            
            return []
            
        except Exception as e:
            logger.error(f"❌ Database batch insertion failed: {e}")
            return []