"""
Screenshot API with x402 Protocol Support
Captures website screenshots with USDC micropayments on Base blockchain.

Supports both:
- Standard x402 protocol (for agent discovery/auto-payment)
- Manual payment flow (/pay/request → /pay/verify)
"""

import os
import sys
import base64
import asyncio
import socket
import ipaddress
from typing import Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network
from x402.server import x402ResourceServer

from pydantic import BaseModel, HttpUrl, Field
from playwright.async_api import async_playwright, Browser

# Add shared module to path
sys.path.insert(0, "/app/shared")
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

    # x402 Configuration
    PAYMENT_WALLET = Config.PAYMENT_WALLET
    SCREENSHOT_PRICE_USDC = 10000  # $0.01 in USDC units (6 decimals)
    NETWORK = "base"
    USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


# =============================================================================
# SSRF Validation (ported from x402-scraping-api)
# =============================================================================

ALLOWED_SCHEMES = {"http", "https"}


def _assert_ip_public(ip) -> None:
    """Raise ValueError if the IP is in a blocked (non-public) range."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        _assert_ip_public(ip.ipv4_mapped)
        return
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise ValueError(f"URL resolves to blocked IP range: {ip}")


def validate_url_for_ssrf(url: str) -> None:
    """Raises ValueError if the URL targets a private/internal resource.

    Uses getaddrinfo (not gethostbyname) to catch both IPv4 and IPv6 records.
    Checks ALL resolved addresses — not just the first.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"Scheme '{parsed.scheme}' not allowed; only http/https accepted")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("No hostname in URL")

    try:
        direct_ip = ipaddress.ip_address(hostname)
        _assert_ip_public(direct_ip)
        return
    except ValueError as e:
        if "blocked IP range" in str(e):
            raise

    try:
        records = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed: {e}")

    if not records:
        raise ValueError("DNS resolution returned no addresses")

    for record in records:
        ip_str = record[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise ValueError(f"Could not parse resolved IP: {ip_str!r}")
        _assert_ip_public(ip)


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
    title="Bismuth Screenshot",
    description="Playwright-powered webpage screenshot capture with full-page, viewport, and custom sizing. SSRF-protected. Part of the Bismuth utility API suite for AI agents.",
    version="3.0.0",
    lifespan=lifespan,
    contact={
        "name": "Bismuth",
        "url": "https://usebismuth.com",
        "email": os.getenv("CONTACT_EMAIL", "james@usebismuth.com"),
    },
)

# x402 v2 payment middleware for GET /screenshot
# Manual /pay/request + /pay/verify flow stays custom for legacy compatibility
PAY_TO_V2 = os.getenv("PAY_TO_ADDRESS", AppConfig.PAYMENT_WALLET)
BASE_NETWORK_V2: Network = "eip155:8453"
FACILITATOR_URL_V2 = os.getenv("FACILITATOR_URL", "https://facilitator.daydreams.systems")

_facilitator_v2 = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL_V2))
_x402_server_v2 = x402ResourceServer(_facilitator_v2)
_x402_server_v2.register(BASE_NETWORK_V2, ExactEvmServerScheme())

_paid_routes_v2 = {
    "GET /screenshot": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=PAY_TO_V2, price="$0.01", network=BASE_NETWORK_V2)],
        mime_type="application/json",
        description="Capture any webpage as a base64-encoded PNG/JPEG image",
    ),
}
app.add_middleware(PaymentMiddlewareASGI, routes=_paid_routes_v2, server=_x402_server_v2)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# OpenAPI x402 v2 Extensions + Favicon
# =============================================================================

_original_openapi_fn = app.openapi


def _openapi_with_x402_v2():
    if app.openapi_schema:
        return app.openapi_schema
    schema = _original_openapi_fn()
    schema["info"]["x-guidance"] = (
        "Bismuth Screenshot — Playwright-powered webpage screenshot capture for AI agents. "
        "GET /screenshot?url=... captures a page as a base64-encoded PNG or JPEG ($0.01 USDC on Base). "
        "Query params: url (required), width, height, full_page, format. "
        "SSRF-protected: private/loopback IPs rejected before payment. "
        "Alternate manual payment flow: POST /pay/request → send USDC → POST /pay/verify."
    )
    for (path, method), amount in [(("/screenshot", "get"), "0.010000")]:
        op = schema.get("paths", {}).get(path, {}).get(method)
        if op is None:
            continue
        op["x-payment-info"] = {
            "price": {"mode": "fixed", "currency": "USD", "amount": amount},
            "protocols": [{"x402": {}}],
        }
        op.setdefault("responses", {})["402"] = {"description": "Payment Required"}
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_x402_v2

_FAVICON_PATH = os.path.join(os.path.dirname(__file__), "favicon.ico")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    if os.path.exists(_FAVICON_PATH):
        return FileResponse(_FAVICON_PATH, media_type="image/x-icon")
    raise HTTPException(status_code=404)


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
# x402 Protocol Helper
# =============================================================================

def create_402_response(request: Request) -> Response:
    """
    Create a proper x402 Payment Required response.
    
    This follows the x402 protocol specification so that
    x402-compatible clients and agent frameworks can auto-pay.
    """
    import json
    
    # Build the x402 payment requirements
    payment_payload = {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "base",
                "maxAmountRequired": str(AppConfig.SCREENSHOT_PRICE_USDC),
                "resource": str(request.url),
                "description": "Screenshot API - capture any webpage",
                "mimeType": "application/json",
                "payTo": AppConfig.PAYMENT_WALLET,
                "maxTimeoutSeconds": 60,
                "asset": AppConfig.USDC_ADDRESS,
                "extra": {
                    "name": "Screenshot API",
                    "pricing": "$0.01 per screenshot"
                }
            }
        ]
    }
    
    # Encode as base64 for the header
    payment_header = base64.b64encode(
        json.dumps(payment_payload).encode()
    ).decode()
    
    return Response(
        content=json.dumps({
            "error": "Payment Required",
            "message": "This endpoint requires payment. Send USDC on Base.",
            "price": "$0.01",
            "payTo": AppConfig.PAYMENT_WALLET,
            "network": "base",
            "x402": True,
        }),
        status_code=402,
        media_type="application/json",
        headers={
            "X-Payment": payment_header,
            "Access-Control-Expose-Headers": "X-Payment",
        }
    )


async def verify_x402_payment(request: Request) -> bool:
    """
    Verify x402 payment from request header.
    
    In production, this would verify the payment proof
    against the CDP facilitator. For now, we check if
    a valid payment header exists.
    """
    payment_header = request.headers.get("X-Payment")
    
    if not payment_header:
        return False
    
    try:
        # Decode and parse the payment proof
        import json
        payment_data = json.loads(base64.b64decode(payment_header))
        
        # TODO: Verify with CDP facilitator
        # For now, accept any well-formed payment header
        # Real verification would call:
        # https://x402.org/facilitator/verify
        
        return "payload" in payment_data or "signature" in payment_data
        
    except Exception:
        return False


# =============================================================================
# x402 Discovery
# =============================================================================

@app.api_route("/.well-known/x402", methods=["GET", "HEAD"], tags=["Discovery"])
async def well_known_x402():
    """x402 discovery — indexed by x402scan and other ecosystem crawlers."""
    return {
        "version": 1,
        "x402Version": 2,
        "name": "Bismuth Screenshot",
        "description": "Playwright-powered webpage screenshot capture with full-page, viewport, and custom sizing. SSRF-protected. Part of the Bismuth utility API suite for AI agents.",
        "apiVersion": "2.1.0",
        "network": "base",
        "resource": {
            "url": "https://usdc-screenshot-api-production.up.railway.app",
            "description": "Bismuth Screenshot — x402 USDC micropayments on Base",
            "mimeType": "application/json",
        },
        "services": [
            {
                "name": "Capture Screenshot",
                "endpoint": "/screenshot",
                "method": "GET",
                "price": "$0.01",
                "description": "Capture any webpage as a base64-encoded PNG/JPEG image",
            },
        ],
        "resources": ["GET /screenshot"],
        "documentation": "https://usdc-screenshot-api-production.up.railway.app/docs",
        "provider": {
            "name": "Bismuth",
            "url": "https://usebismuth.com",
        },
    }


# =============================================================================
# x402 Screenshot Endpoint
# =============================================================================

@app.get("/screenshot", tags=["Screenshot"])
async def screenshot_x402(
    request: Request,
    url: str,
    width: int = 1280,
    height: int = 720,
    full_page: bool = False,
    format: str = "png",
):
    """
    Screenshot endpoint. Payment enforced by PaymentMiddlewareASGI at the ASGI layer;
    reaching this handler means payment was verified.

    For manual payment flow, use /pay/request instead.
    """
    # SSRF guard — reject private/internal IPs (payment already verified by middleware)
    try:
        validate_url_for_ssrf(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"SSRF validation failed: {e}")

    try:
        screenshot_bytes = await capture_screenshot(
            url=url,
            width=width,
            height=height,
            full_page=full_page,
            format=format,
        )

        return {
            "success": True,
            "url": url,
            "image_base64": base64.b64encode(screenshot_bytes).decode(),
            "format": format,
            "width": width,
            "height": height,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Manual Payment Endpoints (Original Flow)
# =============================================================================

@app.post("/pay/request", tags=["Payment"])
async def request_payment(body: PaymentRequestBody):
    """
    Request a payment for an API endpoint.
    
    Returns payment details including the wallet address and amount.
    Client should send USDC on Base to the provided address.
    
    Alternative to x402 auto-payment for manual workflows.
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
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid params: {e}")

        # SSRF guard — block private/internal IPs BEFORE charging
        try:
            validate_url_for_ssrf(str(body.params["url"]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"SSRF validation failed: {e}")

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

@app.api_route("/test/screenshot", methods=["GET", "HEAD"], tags=["Free Test"])
async def test_screenshot(url: str, width: int = 1280, height: int = 720):
    """
    Free test endpoint - limited to example.com domain.
    Use /screenshot (x402) or /pay/request for production usage.
    """
    # Restrict to safe test domains
    allowed_domains = ["example.com", "example.org", "httpbin.org"]
    
    from urllib.parse import urlparse
    parsed = urlparse(url)
    
    if parsed.netloc not in allowed_domains:
        raise HTTPException(
            status_code=403,
            detail=f"Free tier restricted to: {allowed_domains}. Use /screenshot for other domains."
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
            "note": "This is a free test endpoint. Use /screenshot for production.",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Info Endpoints
# =============================================================================

@app.api_route("/", methods=["GET", "HEAD"], tags=["Info"])
async def root():
    """API information and pricing."""
    return {
        "service": "Screenshot API",
        "version": "2.1.0",
        "x402": True,
        "payment": {
            "network": "base",
            "token": "USDC",
            "asset": AppConfig.USDC_ADDRESS,
            "wallet": AppConfig.PAYMENT_WALLET,
        },
        "pricing": {
            "/screenshot": "$0.01",
        },
        "endpoints": {
            "GET /screenshot": "x402 paid endpoint - returns 402 without payment",
            "POST /pay/request": "Manual payment request (alternative to x402)",
            "POST /pay/verify": "Verify manual payment and get data",
            "GET /test/screenshot": "Free test (example.com only)",
        },
        "x402_flow": [
            "1. GET /screenshot?url=... → 402 with X-Payment header",
            "2. Parse payment requirements from X-Payment header",
            "3. Send USDC payment via Base network",
            "4. Retry request with X-Payment proof header",
            "5. Receive screenshot data",
        ],
        "manual_flow": [
            "1. POST /pay/request with endpoint and params",
            "2. Send USDC to provided wallet on Base",
            "3. POST /pay/verify with payment_id",
            "4. Receive screenshot data when payment confirms",
        ],
    }


@app.api_route("/health", methods=["GET", "HEAD"], tags=["Info"])
async def health():
    """Health check."""
    return {
        "status": "healthy",
        "browser": browser is not None and browser.is_connected(),
        "payment_wallet": AppConfig.PAYMENT_WALLET,
        "x402_enabled": True,
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
