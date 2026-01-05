"""
Screenshot API with Direct USDC Payment Verification
Captures website screenshots with pay-per-use model on Base blockchain.
"""

import os
import sys
import base64
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, Field
from playwright.async_api import async_playwright, Browser

# Add shared module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from payment_verify import (
    create_payment_request,
    verify_payment_request,
    get_stored_params,
    mark_payment_used,
    get_payment_manager,
    PaymentStatus,
    Config,
    usdc_to_usd,
    ENDPOINT_PRICES,
)


# =============================================================================
# Configuration
# =============================================================================

class AppConfig:
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8000"))
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"


# =============================================================================
# Request/Response Models
# =============================================================================

class PaymentRequestBody(BaseModel):
    endpoint: str = Field(..., description="API endpoint to purchase", example="/screenshot")
    params: dict = Field(default_factory=dict, description="Parameters for the API call")

class PaymentVerifyBody(BaseModel):
    payment_id: str = Field(..., description="Payment ID from /pay/request")

class ScreenshotParams(BaseModel):
    url: HttpUrl = Field(..., description="URL to capture")
    width: int = Field(default=1280, ge=320, le=3840)
    height: int = Field(default=720, ge=240, le=2160)
    full_page: bool = Field(default=False, description="Capture full scrollable page")
    format: str = Field(default="png", pattern="^(png|jpeg|webp)$")

class ScreenshotResponse(BaseModel):
    success: bool
    url: str
    image_base64: Optional[str] = None
    format: str
    width: int
    height: int
    error: Optional[str] = None


# =============================================================================
# Browser Management
# =============================================================================

browser: Optional[Browser] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage browser lifecycle."""
    global browser
    
    # Startup
    print("[Screenshot API] Starting browser...")
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
    )
    print("[Screenshot API] Browser ready")
    
    # Start payment manager
    get_payment_manager()
    print("[Screenshot API] Payment manager ready")
    
    yield
    
    # Shutdown
    if browser:
        await browser.close()
    print("[Screenshot API] Shutdown complete")


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Screenshot API",
    description="Pay-per-use screenshot service with USDC payments on Base",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Core Screenshot Function
# =============================================================================

async def capture_screenshot(
    url: str,
    width: int = 1280,
    height: int = 720,
    full_page: bool = False,
    format: str = "png",
) -> bytes:
    """Capture screenshot using Playwright."""
    global browser
    
    if not browser:
        raise RuntimeError("Browser not initialized")
    
    context = await browser.new_context(
        viewport={"width": width, "height": height},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        
        # Small delay for any late-loading content
        await asyncio.sleep(0.5)
        
        screenshot = await page.screenshot(
            type=format if format != "jpeg" else "jpeg",
            full_page=full_page,
            quality=85 if format in ("jpeg", "webp") else None,
        )
        
        return screenshot
        
    finally:
        await context.close()


# =============================================================================
# Payment Endpoints
# =============================================================================

@app.post("/pay/request", tags=["Payment"])
async def request_payment(body: PaymentRequestBody):
    """
    Request a payment for an API endpoint.
    
    Returns payment details including the wallet address and amount.
    Client should send USDC on Base to the provided address.
    """
    # Validate endpoint exists
    valid_endpoints = ["/screenshot", "/screenshot/full"]
    if body.endpoint not in valid_endpoints:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid endpoint. Valid endpoints: {valid_endpoints}"
        )
    
    # Validate params if screenshot
    if body.endpoint.startswith("/screenshot") and body.params:
        try:
            # Validate URL is provided
            if "url" not in body.params:
                raise HTTPException(status_code=400, detail="Missing 'url' in params")
            ScreenshotParams(**body.params)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid params: {e}")
    
    # Create payment request
    payment_info = create_payment_request(body.endpoint, body.params)
    
    return {
        **payment_info,
        "instructions": f"Send {payment_info['amount_usd']} USDC to {payment_info['pay_to']} on Base network",
        "next_step": "POST /pay/verify with your payment_id after sending USDC",
    }


@app.post("/pay/verify", tags=["Payment"])
async def verify_payment(body: PaymentVerifyBody):
    """
    Verify payment and get API response if confirmed.
    
    Poll this endpoint after sending USDC. Once confirmed,
    the API response data will be included.
    """
    # Verify payment status
    result = verify_payment_request(body.payment_id)
    
    if result["status"] == PaymentStatus.CONFIRMED.value:
        # Get stored params and execute the API call
        stored = get_stored_params(body.payment_id)
        
        if stored:
            endpoint, params = stored
            
            try:
                # Execute screenshot
                url = params.get("url", "")
                width = params.get("width", 1280)
                height = params.get("height", 720)
                full_page = params.get("full_page", False) or endpoint == "/screenshot/full"
                format = params.get("format", "png")
                
                screenshot_bytes = await capture_screenshot(
                    url=str(url),
                    width=width,
                    height=height,
                    full_page=full_page,
                    format=format,
                )
                
                # Mark payment as used (replay protection)
                mark_payment_used(body.payment_id)
                
                result["data"] = {
                    "success": True,
                    "url": str(url),
                    "image_base64": base64.b64encode(screenshot_bytes).decode(),
                    "format": format,
                    "width": width,
                    "height": height,
                }
                
            except Exception as e:
                result["data"] = {
                    "success": False,
                    "error": str(e),
                }
    
    return result


# =============================================================================
# Free Test Endpoints
# =============================================================================

@app.get("/test/screenshot", tags=["Free Test"])
async def test_screenshot(url: str, width: int = 1280, height: int = 720):
    """
    Free test endpoint - limited to example.com domain.
    Use /pay/request for production usage.
    """
    # Restrict to safe test domains
    allowed_domains = ["example.com", "example.org", "httpbin.org"]
    
    from urllib.parse import urlparse
    parsed = urlparse(url)
    
    if parsed.netloc not in allowed_domains:
        raise HTTPException(
            status_code=403,
            detail=f"Free tier restricted to: {allowed_domains}. Use /pay/request for other domains."
        )
    
    try:
        screenshot_bytes = await capture_screenshot(url, width, height)
        
        return {
            "success": True,
            "url": url,
            "image_base64": base64.b64encode(screenshot_bytes).decode(),
            "format": "png",
            "width": width,
            "height": height,
            "note": "This is a free test endpoint. Use /pay/request for production.",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Info Endpoints
# =============================================================================

@app.get("/", tags=["Info"])
async def root():
    """API information and pricing."""
    return {
        "service": "Screenshot API",
        "version": "2.0.0",
        "payment": {
            "network": "Base",
            "token": "USDC",
            "wallet": Config.PAYMENT_WALLET,
        },
        "pricing": {
            endpoint: f"${usdc_to_usd(price)}"
            for endpoint, price in ENDPOINT_PRICES.items()
            if "screenshot" in endpoint
        },
        "endpoints": {
            "POST /pay/request": "Request payment for an endpoint",
            "POST /pay/verify": "Verify payment and get data",
            "GET /test/screenshot": "Free test (example.com only)",
        },
        "flow": [
            "1. POST /pay/request with endpoint and params",
            "2. Send USDC to provided wallet on Base",
            "3. POST /pay/verify with payment_id",
            "4. Receive screenshot data when payment confirms",
        ],
    }


@app.get("/health", tags=["Info"])
async def health():
    """Health check."""
    return {
        "status": "healthy",
        "browser": browser is not None and browser.is_connected(),
        "payment_wallet": Config.PAYMENT_WALLET,
    }


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=AppConfig.HOST,
        port=AppConfig.PORT,
        reload=AppConfig.DEBUG,
    )
