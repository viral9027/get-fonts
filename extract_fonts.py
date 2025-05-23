import asyncio
import json
import os
import aiohttp
import io
import logging
import re
from playwright.async_api import async_playwright
from fontTools.ttLib import TTFont
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from urllib.parse import urljoin, urlparse

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Thread pool for CPU-bound tasks
executor = ThreadPoolExecutor(max_workers=4)

# In-memory cache for font content and metadata
font_cache = {}

# Expose cache clearing function
def clear_font_cache():
    global font_cache
    logger.info("Clearing font cache")
    font_cache.clear()

# Cache for font metadata extraction
@lru_cache(maxsize=1000)
def _extract_font_metadata_cached(font_content, font_filename, font_url):
    logger.debug(f"Extracting metadata for font: {font_filename}")
    try:
        font_file = io.BytesIO(font_content)
        font = TTFont(font_file)
        name_table = font["name"]
        metadata = {
            "filename": font_filename,
            "family": "Unknown",
            "subfamily": "Unknown",
            "font_name": "Unknown",
            "weight": "Unknown",
            "designer": "Unknown",
            "manufacturer": "Unknown",
            "copyright": "Unknown",
            "font_url": font_url,
            "license_type": "Unknown",
            "error": None
        }

        for record in name_table.names:
            name_str = record.toUnicode()
            if record.nameID == 1:
                metadata["family"] = name_str
            elif record.nameID == 2:
                metadata["subfamily"] = name_str
            elif record.nameID == 4:
                metadata["font_name"] = name_str
            elif record.nameID == 9:
                metadata["designer"] = name_str
            elif record.nameID == 11:
                metadata["manufacturer"] = name_str
            elif record.nameID == 0:
                metadata["copyright"] = name_str
            elif record.nameID == 13:
                metadata["license_type"] = name_str if name_str else "Unknown"

        if "OS/2" in font:
            metadata["weight"] = str(font["OS/2"].usWeightClass)

        if "fonts.google.com" in font_url:
            metadata["license_type"] = "Open Source (Google Fonts)"
        elif "github.com" in font_url:
            metadata["license_type"] = "Open Source (GitHub)"

        return metadata
    except Exception as e:
        logger.error(f"Failed to extract metadata for {font_filename}: {str(e)}")
        return {
            "filename": "Unknown",
            "family": "Unknown",
            "subfamily": "Unknown",
            "font_name": "Unknown",
            "weight": "Unknown",
            "designer": "Unknown",
            "manufacturer": "Unknown",
            "copyright": "Unknown",
            "font_url": font_url,
            "license_type": "Unknown",
            "error": f"Failed to extract metadata: {str(e)}"
        }

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5),
       retry=retry_if_exception_type(Exception))
async def _download_font(aio_session, font_url, font_filename):
    logger.debug(f"Downloading font: {font_url}")
    import hashlib
    font_url_hash = hashlib.md5(font_url.encode()).hexdigest()
    if font_url_hash in font_cache:
        logger.debug(f"Cache hit for font: {font_url}")
        content, _ = font_cache[font_url_hash]
        return content, font_filename, font_url, None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "font/woff2,font/woff,font/ttf,font/otf,*/*"
        }
        async with aio_session.get(font_url, timeout=12, headers=headers, allow_redirects=True) as response:
            if response.status == 200:
                content = await response.read()
                font_cache[font_url_hash] = (content, None)
                logger.debug(f"Font downloaded: {font_url}")
                return content, font_filename, font_url, None
            else:
                logger.warning(f"Failed to download font: {font_url}, HTTP {response.status}")
                return None, font_filename, font_url, f"Failed to download font: HTTP {response.status}"
    except Exception as e:
        logger.error(f"Error downloading font: {font_url}, {str(e)}")
        return None, font_filename, font_url, f"Error downloading font: {str(e)}"

async def _download_fonts(aio_session, font_urls):
    logger.info(f"Downloading {len(font_urls)} fonts")
    tasks = []
    for font_url in font_urls:
        font_filename = f"font_{uuid.uuid4().hex[:8]}{os.path.splitext(font_url.split('?')[0])[-1] or '.woff2'}"
        tasks.append(_download_font(aio_session, font_url, font_filename))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    font_data = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Error in font download: {str(result)}")
            continue
        content, font_filename, font_url, error = result
        if content:
            font_data.append((content, font_filename, font_url))
        else:
            font_data.append((None, font_filename, font_url, error))
    logger.info(f"Completed downloading {len(font_data)} fonts")
    return font_data

def _create_error_metadata(error_message, font_url=None):
    return {
        "filename": "Unknown",
        "family": "Unknown",
        "subfamily": "Unknown",
        "font_name": "Unknown",
        "weight": "Unknown",
        "designer": "Unknown",
        "manufacturer": "Unknown",
        "copyright": "Unknown",
        "font_url": font_url if font_url else "Unknown",
        "license_type": "Unknown",
        "error": error_message
    }

def normalize_url(url, base_url):
    full_url = urljoin(base_url, url.strip('\'"'))
    parsed = urlparse(full_url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

async def extract_fonts_from_page(page, url, font_urls, font_regex):
    async def handle_request(request):
        normalized_url = normalize_url(request.url, url)
        if font_regex.search(request.url) or "font" in request.resource_type or "font" in request.headers.get("content-type", "").lower():
            font_urls.add(normalized_url)

    page.on("request", handle_request)

    try:
        await page.goto(url, wait_until="networkidle", timeout=20000)
    except Exception as e:
        logger.warning(f"Failed to load {url}: {str(e)}")
        return False

    try:
        font_face_urls = await page.evaluate("""
            () => {
                const fontUrls = [];
                for (const sheet of document.styleSheets) {
                    try {
                        for (const rule of sheet.cssRules) {
                            if (rule instanceof CSSFontFaceRule) {
                                const src = rule.style.getPropertyValue('src');
                                const urlMatch = src.match(/url\\(["']?([^"']+)["']?\\)/i);
                                if (urlMatch && urlMatch[1]) fontUrls.push(urlMatch[1]);
                            }
                        }
                    } catch (e) {}
                }
                return fontUrls;
            }
        """)
        for font_url in font_face_urls:
            normalized_url = normalize_url(font_url, url)
            font_urls.add(normalized_url)
    except Exception as e:
        logger.warning(f"Failed to extract @font-face rules for {url}: {str(e)}")

    try:
        await page.evaluate("""
            async () => {
                for (let i = 0; i < 5; i++) {
                    window.scrollTo(0, document.body.scrollHeight);
                    await new Promise(resolve => setTimeout(resolve, 1500));
                    window.scrollTo(0, 0);
                    await new Promise(resolve => setTimeout(resolve, 1500));
                }
            }
        """)
    except Exception as e:
        logger.warning(f"Error during scrolling for {url}: {str(e)}")

    await asyncio.sleep(5)

    return True

async def extract_fonts(url, aio_session=None):
    logger.info(f"Starting font extraction for {url} (first page only)")
    if aio_session is None:
        async with aiohttp.ClientSession() as temp_session:
            return await extract_fonts(url, temp_session)

    font_urls = set()
    font_regex = re.compile(r'\.(woff2?|ttf|otf|eot|sfnt)(\?.*)?$', re.IGNORECASE)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--blink-settings=imagesEnabled=false",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ])
            context = await browser.new_context()
            page = await context.new_page()

            success = await extract_fonts_from_page(page, url, font_urls, font_regex)
            if not success:
                logger.warning(f"Failed to load {url}")
                await browser.close()
                return {"total_fonts": 0, "fonts": [_create_error_metadata(f"Failed to load page: {url}")]}

            await browser.close()
            logger.info(f"Found {len(font_urls)} unique font URLs")

    except Exception as e:
        logger.error(f"Playwright error for {url}: {str(e)}")
        return {"total_fonts": 0, "fonts": [_create_error_metadata(f"Playwright error: {str(e)}")]}

    if not font_urls:
        logger.info(f"No font URLs found for {url}")
        return {"total_fonts": 0, "fonts": [_create_error_metadata("No font URLs detected")]}

    font_data = await _download_fonts(aio_session, font_urls)

    font_metadata = []
    for item in font_data:
        if len(item) == 4:
            content, font_filename, font_url, error = item
        else:
            content, font_filename, font_url = item
            error = None

        if error:
            continue

        if content:
            import hashlib
            font_url_hash = hashlib.md5(font_url.encode()).hexdigest()
            if font_url_hash in font_cache and font_cache[font_url_hash][1]:
                metadata = font_cache[font_url_hash][1]
            else:
                metadata = await asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: _extract_font_metadata_cached(content, font_filename, font_url)
                )
                font_cache[font_url_hash] = (content, metadata)

            if metadata["error"]:
                continue

            if metadata["family"] == "Unknown" and metadata["subfamily"] == "Unknown" and metadata["weight"] == "Unknown":
                continue

            font_metadata.append(metadata)

    total_fonts = len([meta for meta in font_metadata if meta["error"] is None])
    logger.info(f"Total valid fonts for {url}: {total_fonts}")
    return {
        "total_fonts": total_fonts,
        "fonts": font_metadata if font_metadata else [_create_error_metadata("No fonts found")]
    }

async def process_urls(urls, max_concurrency=10):
    logger.info(f"Starting batch processing for {len(urls)} URLs")
    async with aiohttp.ClientSession() as aio_session:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def process_with_semaphore(url):
            async with semaphore:
                try:
                    return await extract_fonts(url, aio_session)
                except Exception as e:
                    logger.error(f"Error processing URL {url}: {str(e)}")
                    return {"total_fonts": 0, "fonts": [_create_error_metadata(f"Error processing URL: {str(e)}")]}

        tasks = [process_with_semaphore(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results

if __name__ == "__main__":
    test_urls = ["https://www.customersbank.com"]
    try:
        results = asyncio.run(process_urls(test_urls))
        for url, result in zip(test_urls, results):
            logger.info(f"Fonts for {url}:")
            print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Error in main execution: {str(e)}")