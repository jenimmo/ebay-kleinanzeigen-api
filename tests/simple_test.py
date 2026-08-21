"""
Simple test to check individual endpoints
"""

import asyncio
import aiohttp
import pytest


pytestmark = [
    pytest.mark.live,
    pytest.mark.skip(
        reason=(
            "Historische synchrone Smoke-Suite; durch "
            "test_persistent_jobs_live.py ersetzt"
        )
    ),
]


async def test_endpoints():
    """Test each endpoint individually"""
    base_url = "http://localhost:8000"

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120)
    ) as session:
        # Test inserate endpoint
        print("Testing /inserate...")
        async with session.get(
            f"{base_url}/inserate", params={"query": "test", "page_count": 1}
        ) as response:
            assert response.status == 200
            data = await response.json()
            assert isinstance(data.get("success"), bool)
            assert isinstance(data.get("data"), list)
            print(f"Status: {response.status}")
            print(f"Success: {data['success']}")
            print(f"Results: {data.get('unique_results', 0)}")

        print("\nTesting /inserate-detailed...")
        async with session.get(
            f"{base_url}/inserate-detailed",
            params={"query": "test", "page_count": 1, "max_concurrent_details": 2},
        ) as response:
            assert response.status == 200
            data = await response.json()
            assert isinstance(data.get("success"), bool)
            assert isinstance(data.get("data"), list)
            print(f"Status: {response.status}")
            print(f"Success: {data['success']}")


if __name__ == "__main__":
    asyncio.run(test_endpoints())
