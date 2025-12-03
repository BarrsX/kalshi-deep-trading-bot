"""
Perplexity API client for real-time market research.

Perplexity provides real-time web search with AI summarization,
which is ideal for prediction markets that require current data.
"""

import httpx
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from loguru import logger
from config import PerplexityConfig


class PerplexityClient:
    """Client for Perplexity API for real-time market research."""

    def __init__(self, config: PerplexityConfig):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url="https://api.perplexity.ai",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=300.0,  # 5 minutes timeout
        )

    async def research_event(self, event: Dict[str, Any], markets: List[Dict[str, Any]]) -> str:
        """
        Research an event and its markets using Perplexity's real-time search.

        Args:
            event: Event information (title, subtitle, category, etc.)
            markets: List of markets within the event (without odds)

        Returns:
            Research response as a string
        """
        try:
            # Get current date/time for the prompt
            current_datetime = datetime.now(timezone.utc)
            current_date_str = current_datetime.strftime("%B %d, %Y")
            current_time_str = current_datetime.strftime("%H:%M UTC")
            current_year = current_datetime.year

            # Format the event and markets for analysis
            event_info = f"""
Event: {event.get('title', '')}
Subtitle: {event.get('subtitle', '')}
Mutually Exclusive: {event.get('mutually_exclusive', False)}
"""

            markets_info = "Markets:\n"
            for i, market in enumerate(markets, 1):
                if market.get("volume", 0) < 1000:
                    continue
                # Emphasize human readable title over ticker
                title = market.get("title", "")
                ticker = market.get("ticker", "")
                markets_info += f"{i}. {title}"
                if ticker:
                    markets_info += f" (Ticker: {ticker})"
                markets_info += "\n"
                if market.get("subtitle"):
                    markets_info += f"   Subtitle: {market.get('subtitle', '')}\n"
                markets_info += f"   Open: {market.get('open_time', '')}\n"
                markets_info += f"   Close: {market.get('close_time', '')}\n\n"

            # Build the research prompt
            prompt = f"""TODAY'S DATE: {current_date_str} (Current time: {current_time_str}). Year: {current_year}.

You are a prediction market research analyst. Your task is to research this event and provide probability estimates for each market.

{event_info}

{markets_info}

RESEARCH REQUIREMENTS:
1. Search for the LATEST real-time information about this event
2. For financial assets: Get the CURRENT price as of today with source
3. For sports: Get CURRENT {current_year} season standings, recent game results, injury reports
4. For politics/events: Get the latest news, polls, and developments
5. ALWAYS cite your sources with dates

Please provide:
1. **Current Status** (as of {current_date_str}): State current prices, standings, or situation with specific numbers and sources
2. **Recent News & Developments**: Key news from the past week with dates
3. **Key Factors**: What will influence the outcome?
4. **For Each Market**: 
   - Probability estimate (0-100%) for YES outcome
   - Confidence level (1-10)
   - Brief reasoning with cited sources
5. **Risks & Catalysts**: What could change the outcome?

CRITICAL - MARKET SEMANTICS:
- If market asks "Will X go BELOW $Y?" → probability of going BELOW that level
- If market asks "Will X go ABOVE $Y?" → probability of going ABOVE that level
- Pay close attention to the exact wording of each market

IMPORTANT: Include the market ticker with each probability prediction.
Format: "TICKER: XX%" or "Market Name (TICKER): XX% probability"

Provide your analysis with real-time data and source citations."""

            event_ticker = event.get("event_ticker", "UNKNOWN")
            logger.info(f"Starting Perplexity research for event {event_ticker}...")

            # Make the API request to Perplexity
            payload = {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a prediction market research analyst. Provide thorough, real-time research with probability estimates. Always cite sources with dates. Be specific and data-driven.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,  # Low temperature for factual responses
                "return_citations": True,
                "search_recency_filter": "day",  # Prioritize recent results
            }

            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()

            result = response.json()

            # Extract the response content
            content = ""
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0].get("message", {}).get("content", "")

            # Add citations if available
            citations = result.get("citations", [])
            if citations:
                content += "\n\n**Sources:**\n"
                for i, citation in enumerate(citations, 1):
                    content += f"{i}. {citation}\n"

            logger.info(f"Completed Perplexity research for event {event_ticker}")

            return content

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Perplexity API error for event {event.get('event_ticker', '')}: {e.response.status_code} - {e.response.text}"
            )
            return f"Error researching event: API error {e.response.status_code}"
        except Exception as e:
            logger.error(f"Error researching event {event.get('event_ticker', '')}: {e}")
            return f"Error researching event: {str(e)}"

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# Backwards compatibility alias - allows existing code to work
