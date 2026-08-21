"""
API health check test to verify the server is running and optimized components are working.

This test validates:
- API server is accessible
- Optimized browser manager is initialized
- All endpoints are available
- Basic functionality works
"""

import asyncio
import aiohttp
import pytest


pytestmark = [
    pytest.mark.live,
    pytest.mark.skip(
        reason=(
            "Historischer Health-Runner; durch test_persistent_jobs_live.py ersetzt"
        )
    ),
]


async def check_api_health(base_url: str = "http://localhost:8000") -> bool:
    """Test API readiness without triggering a Kleinanzeigen scrape."""
    print("Testing API health and optimization status...")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            print("  Testing root endpoint...", end=" ")
            async with session.get(f"{base_url}/") as response:
                if response.status != 200:
                    print(f"❌ HTTP {response.status}")
                    return False
                root_data = await response.json()
                if root_data.get("browser_status") != "optimized":
                    print(
                        f"⚠️  Browser status: {root_data.get('browser_status', 'unknown')}"
                    )
                    return False
                if "/health" not in root_data.get("endpoints", []):
                    print("⚠️  /health missing from endpoint list")
                    return False
                print("✅ OK (optimized)")

            print("  Testing readiness endpoint...", end=" ")
            async with session.get(f"{base_url}/health") as response:
                if response.status != 200:
                    print(f"❌ HTTP {response.status}")
                    return False
                health_data = await response.json()
                if health_data != {
                    "status": "healthy",
                    "browser_status": "optimized",
                }:
                    print(f"⚠️  Unexpected body: {health_data}")
                    return False
                print("✅ OK")

            print("\n✅ API health check passed - All systems operational!")
            return True

        except Exception as e:
            print(f"\n❌ API health check failed: {str(e)}")
            return False


async def test_api_health():
    """Pytest-Einstiegspunkt ohne versehentlich angeforderte ``base_url``-Fixture."""
    assert await check_api_health()


async def main():
    """Run API health check"""
    print("=" * 60)
    print("API HEALTH CHECK")
    print("=" * 60)

    success = await check_api_health()

    if success:
        print("\n🎉 API is healthy and ready for integration tests!")
    else:
        print("\n❌ API health check failed. Please check:")
        print("   1. API server is running (python main.py)")
        print("   2. All dependencies are installed")
        print("   3. Browser manager is properly initialized")

    return success


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
