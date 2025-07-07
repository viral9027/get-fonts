import asyncio
import hashlib
import json
import logging
import os
import resource
import secrets
import time
from io import BytesIO
from urllib.parse import urlparse, urlunparse

import aiohttp
import openpyxl
import pandas as pd
from aiohttp import ClientSession, ClientTimeout
from fastapi import FastAPI, File, UploadFile, Form, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fontTools.ttLib import TTFont
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
from pydantic import BaseModel
import psutil

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Increase file descriptor limit
try:
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard_limit))
    logger.info(f"Updated file descriptor limit: {resource.getrlimit(resource.RLIMIT_NOFILE)}")
except Exception as e:
    logger.error(f"Failed to increase file descriptor limit: {str(e)}")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

users_db = {
    "user@example.com": {
        "email": "user@example.com",
        "hashed_password": hashlib.sha256("password123".encode()).hexdigest()
    }
}

# File-based session store
SESSION_FILE = "sessions.json"

def load_sessions():
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Error loading sessions: {str(e)}")
        return {}

def save_sessions(sessions):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(sessions, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving sessions: {str(e)}")

class LoginData(BaseModel):
    email: str
    password: str

def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    logger.info(f"Checking session_id: {session_id}")
    if not session_id:
        logger.warning("No session_id cookie found")
        return None
    sessions = load_sessions()
    if session_id not in sessions:
        logger.warning(f"Session_id {session_id} not found in sessions")
        return None
    session_data = sessions.get(session_id)
    if time.time() - session_data["created_at"] > 86400:  # 24 hours
        logger.info(f"Session expired for session_id: {session_id}")
        del sessions[session_id]
        save_sessions(sessions)
        return None
    logger.info(f"Session valid for user: {session_data['email']}")
    return session_data["email"]

def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Invalid URL: No netloc specified")
    return urlunparse((
        parsed.scheme or 'https',
        parsed.netloc,
        parsed.path or '/',
        parsed.params,
        parsed.query,
        parsed.fragment
    ))

def extract_company_from_url(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")
    return ' '.join(word.capitalize() for word in domain.split('.')[0].split('-'))

def save_font_data(data_type: str, data: dict):
    try:
        existing_data = {"uploaded_fonts": [], "fetched_fonts": [], "bulk_fetched": []}
        if os.path.exists("font_data.json"):
            with open("font_data.json", "r") as f:
                existing_data = json.load(f)

        if data_type == "uploaded":
            existing_data["uploaded_fonts"].append(data)
        elif data_type == "fetched":
            existing_data["fetched_fonts"].append(data)
        elif data_type == "bulk_fetched":
            existing_data["bulk_fetched"] = data

        with open("font_data.json", "w") as f:
            json.dump(existing_data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving font data: {str(e)}")

def extract_font_details(font: TTFont):
    name_table = font["name"]
    details = {}
    for record in name_table.names:
        if record.nameID in [0, 1, 2, 4, 6, 8, 9, 13]:
            try:
                details[record.nameID] = record.toUnicode()
            except:
                details[record.nameID] = record.string.decode("latin-1", errors="ignore")

    weight = str(font["OS/2"].usWeightClass) if "OS/2" in font else "Unknown"
    return {
        "family": details.get(1, "Unknown"),
        "subfamily": details.get(2, "Unknown"),
        "full_name": details.get(4, "Unknown"),
        "postscript_name": details.get(6, "Unknown"),
        "copyright": details.get(0, "Unknown"),
        "manufacturer": details.get(8, "Unknown"),
        "designer": details.get(9, "Unknown"),
        "license_type": details.get(13, "Unknown"),
        "weight": weight
    }

async def fetch_fonts_from_url(url: str, session: ClientSession):
    font_details_list = []
    fonts = []
    cors_blocked = False
    font_families = []
    css_fonts = []
    rendered_fonts = []

    max_retries = 3  # Increased retries
    retry_delay = 2  # Seconds between retries

    async with async_playwright() as p:
        logger.info(f"Launching Chromium for URL: {url}")
        # Log resource usage
        process = psutil.Process()
        mem = process.memory_info().rss / 1024 / 1024  # MB
        cpu = process.cpu_percent()
        logger.info(f"Resource usage before browser launch: Memory={mem:.2f}MB, CPU={cpu:.2f}%")

        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--headless=new',
                '--disable-gpu',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-background-networking',
                '--disable-software-rasterizer',
                '--disable-breakpad',
            ]
        )
        logger.info(f"Chromium launched successfully for {url}")
        try:
            for retry_count in range(max_retries):
                context = None
                page = None
                try:
                    if not browser.is_connected():
                        logger.error(f"Browser disconnected before attempt {retry_count + 1} for {url}")
                        continue

                    context = await browser.new_context(
                        viewport={'width': 1280, 'height': 720},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                        no_viewport=False,
                        bypass_csp=True,
                        ignore_https_errors=True
                    )
                    logger.info(f"Browser context created for attempt {retry_count + 1} at {url}")
                    page = await context.new_page()

                    # Log console and page errors
                    page.on("console", lambda msg: logger.info(f"Console message from {url}: {msg.text}"))
                    page.on("pageerror", lambda error: logger.error(f"Page error at {url}: {str(error)}"))

                    # Abort non-essential requests
                    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media"] else route.continue_())

                    async def capture_fonts(request):
                        if request.resource_type == "font":
                            font_url = request.url.lower()
                            logger.info(f"Captured font request: {font_url}")
                            if any(font_url.endswith(ext) for ext in ['.ttf', '.otf', '.woff', '.woff2', '.eot', '.ttc']):
                                fonts.append(request.url)
                            else:
                                logger.info(f"Skipping unsupported font format: {font_url}")

                    async def check_response(response):
                        if response.request.resource_type == "font":
                            logger.info(f"Font response: {response.url} - Status: {response.status}")
                            if response.status in [403, 401]:
                                logger.warning(f"CORS or access issue for font: {response.url}")
                                nonlocal cors_blocked
                                cors_blocked = True

                    page.on("request", capture_fonts)
                    page.on("response", check_response)

                    logger.info(f"Navigating to {url} (attempt {retry_count + 1})")
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)  # Increased timeout
                    await page.wait_for_load_state("load", timeout=10000)
                    await page.evaluate("document.fonts.ready")
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(5000)

                    font_families = await page.evaluate("""
                        Array.from(document.fonts).map(font => ({
                            family: font.family,
                            status: font.status
                        }))
                    """)
                    logger.info(f"Detected fonts via document.fonts: {font_families}")
                    css_fonts = await page.evaluate("""
                        Array.from(document.styleSheets).flatMap(sheet => {
                            try {
                                return Array.from(sheet.cssRules)
                                    .filter(rule => rule.type === CSSRule.FONT_FACE_RULE)
                                    .map(rule => ({
                                        family: rule.style.getPropertyValue('font-family'),
                                        src: rule.style.getPropertyValue('src')
                                    }));
                            } catch (e) {
                                return [];
                            }
                        })
                    """)
                    logger.info(f"Detected font-face rules: {css_fonts}")
                    rendered_fonts = await page.evaluate("""
                        Array.from(document.querySelectorAll('*'))
                            .map(el => window.getComputedStyle(el).fontFamily)
                            .filter((v, i, a) => v && a.indexOf(v) === i)
                    """)
                    logger.info(f"Detected rendered font families: {rendered_fonts}")
                    break  # Success, exit retry loop
                except PlaywrightTimeoutError:
                    logger.warning(f"Timeout navigating {url} on attempt {retry_count + 1}. Fonts may still be captured.")
                except PlaywrightError as e:
                    logger.error(f"Playwright error for {url} on attempt {retry_count + 1}: {str(e)}", exc_info=True)
                    if "Target closed" in str(e):
                        logger.error(f"Target closed error for {url}")
                except Exception as e:
                    logger.error(f"Unexpected error for {url} on attempt {retry_count + 1}: {str(e)}", exc_info=True)
                finally:
                    if page:
                        await page.close()
                    if context:
                        await context.close()
                    if retry_count < max_retries - 1:
                        logger.info(f"Waiting {retry_delay} seconds before retrying {url}")
                        await asyncio.sleep(retry_delay)
        finally:
            await browser.close()
            logger.info(f"Browser closed for {url}")
            # Log resource usage after closing browser
            mem = process.memory_info().rss / 1024 / 1024  # MB
            cpu = process.cpu_percent()
            logger.info(f"Resource usage after browser close: Memory={mem:.2f}MB, CPU={cpu:.2f}%")

    if retry_count >= max_retries:
        logger.error(f"Failed to process {url} after {max_retries} attempts")
        # Process any captured fonts even on failure
        if fonts:
            logger.info(f"Processing {len(fonts)} captured fonts despite navigation failure")
        else:
            return [], f"Failed to process URL after {max_retries} attempts"

    if not fonts:
        error_msg = "No downloadable fonts found"
        if cors_blocked:
            error_msg += " (likely due to CORS restrictions)"
        elif font_families or css_fonts or rendered_fonts:
            error_msg += " (site may use system fonts, embedded fonts, or fonts not loaded during page render)"
        else:
            error_msg += " (no fonts detected in network requests, CSS, or DOM)"
        logger.warning(f"{error_msg} for {url}")
        return [], error_msg

    for font_url in fonts:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Referer": url
            }
            logger.info(f"Downloading font: {font_url}")
            async with session.get(font_url, timeout=ClientTimeout(total=15), headers=headers) as response:
                if response.status != 200:
                    logger.warning(f"Failed to download font {font_url}: HTTP {response.status}")
                    if response.status in [403, 401]:
                        cors_blocked = True
                    continue
                content = await response.read()
                temp_file_path = f"temp_font_{secrets.token_hex(4)}{os.path.splitext(font_url)[1]}"

                with open(temp_file_path, "wb") as f:
                    f.write(content)

                try:
                    font = TTFont(temp_file_path)
                    font_details = extract_font_details(font)
                    font_details["url"] = font_url
                    font_details_list.append(font_details)
                    logger.info(f"Processed font: {font_url}")
                except Exception as e:
                    logger.error(f"Error processing font {font_url}: {str(e)}")
                finally:
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
        except Exception as e:
            logger.error(f"Error downloading font {font_url}: {str(e)}")

    error_msg = None
    if not font_details_list:
        if cors_blocked:
            error_msg = "No fonts downloaded (likely due to CORS restrictions)"
        elif font_families or css_fonts or rendered_fonts:
            error_msg = "No fonts downloaded (site may use system fonts or embedded fonts)"
        else:
            error_msg = "No fonts downloaded (no fonts detected or unsupported formats)"

    logger.info(f"Completed font fetching for {url}: {len(font_details_list)} fonts found")
    return font_details_list, error_msg

@app.get("/", response_class=HTMLResponse)
async def get_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login", response_class=RedirectResponse)
async def get_login_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.post("/login", response_class=HTMLResponse)
async def login(response: Response, request: Request, email: str = Form(""), password: str = Form("")):
    logger.info(f"Login attempt with email: {email}")
    user = users_db.get(email)
    hashed_password = hashlib.sha256(password.encode()).hexdigest()
    if not user or user["hashed_password"] != hashed_password:
        logger.warning(f"Login failed for email: {email}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid credentials"
        })

    session_id = secrets.token_hex(16)
    sessions = load_sessions()
    sessions[session_id] = {
        "email": email,
        "created_at": time.time()
    }
    save_sessions(sessions)
    logger.info(f"Session created with session_id: {session_id} for email: {email}")
    response = RedirectResponse(url="/main", status_code=303)
    response.set_cookie(key="session_id", value=session_id, httponly=True, secure=False, samesite="Lax")
    return response

@app.get("/main", response_class=HTMLResponse)
async def get_main(request: Request, current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired or invalid. Please log in again."
        })
    logger.info(f"Rendering main page for user: {current_user}")
    return templates.TemplateResponse("main.html", {"request": request})

@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request, response: Response):
    session_id = request.cookies.get("session_id")
    sessions = load_sessions()
    if session_id in sessions:
        logger.info(f"Logging out session_id: {session_id}")
        del sessions[session_id]
        save_sessions(sessions)
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_id")
    return response

@app.get("/upload-font", response_class=RedirectResponse)
async def get_upload_font_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.post("/upload-font", response_class=HTMLResponse)
async def upload_font(request: Request, file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired or invalid. Please log in again."
        })
    try:
        logger.info(f"Uploading font file: {file.filename} by user: {current_user}")
        if not file.filename.lower().endswith(('.ttf', '.otf', '.woff', '.woff2', '.eot', '.ttc')):
            logger.warning(f"Unsupported font format: {file.filename}")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "Unsupported font format. Please upload a .ttf, .otf, .woff, .woff2, .eot, or .ttc file."
            })

        content = await file.read()
        temp_file_path = f"temp_{file.filename}_{secrets.token_hex(4)}"
        with open(temp_file_path, "wb") as f:
            f.write(content)

        font = None
        try:
            if temp_file_path.lower().endswith(('.woff', '.woff2')):
                import woff2
                ttf_path = temp_file_path.rsplit('.', 1)[0] + '.ttf'
                woff2.decompress(temp_file_path, ttf_path)
                font = TTFont(ttf_path)
                os.remove(ttf_path)
            else:
                font = TTFont(temp_file_path)

            font_details = extract_font_details(font)
        finally:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

        save_font_data("uploaded", {
            "filename": file.filename,
            "font_details": font_details
        })

        logger.info(f"Successfully uploaded font: {file.filename}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "font_details": font_details,
            "filename": file.filename
        })
    except Exception as e:
        logger.error(f"Error processing font: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error processing font: {str(e)}"
        })

@app.get("/fetch-fonts", response_class=RedirectResponse)
async def get_fetch_fonts_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.post("/fetch-fonts", response_class=HTMLResponse)
async def fetch_fonts(request: Request, url: str = Form(...), current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired or invalid. Please log in again."
        })

    try:
        logger.info(f"Fetching fonts from {url} by user: {current_user}")
        normalized_url = normalize_url(url)
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=15)) as session:
            font_details_list, error_msg = await fetch_fonts_from_url(normalized_url, session)

        company = extract_company_from_url(normalized_url)
        result = {
            "website_url": normalized_url,
            "company": company,
            "total_fonts": len(font_details_list),
            "font_details_list": font_details_list,
            "error": error_msg
        }
        save_font_data("fetched", result)

        results = [result]

        logger.info(f"Fetch result for {normalized_url}: {len(font_details_list)} fonts, error: {error_msg}")
        if not font_details_list and error_msg:
            logger.warning(f"No fonts found on {normalized_url}: {error_msg}")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "bulk_results": results,
                "error": error_msg
            })

        logger.info(f"Successfully fetched {len(font_details_list)} fonts from {normalized_url}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "bulk_results": results,
            "message": f"Successfully fetched {len(font_details_list)} fonts from {normalized_url}"
        })
    except ValueError as e:
        logger.error(f"Invalid URL: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Invalid URL: {str(e)}"
        })
    except Exception as e:
        logger.error(f"Error fetching fonts from {url}: {str(e)}", exc_info=True)
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error fetching fonts: {str(e)}"
        })

@app.get("/upload-file", response_class=RedirectResponse)
async def get_upload_file_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.post("/upload-file", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired or invalid. Please log in again."
        })

    try:
        logger.info(f"Uploading bulk file: {file.filename} by user: {current_user}")
        if not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
            logger.warning(f"Invalid file format: {file.filename}")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "Please upload a CSV or XLSX file."
            })

        content = await file.read()
        temp_file_path = f"temp_{file.filename}_{secrets.token_hex(4)}"
        with open(temp_file_path, "wb") as f:
            f.write(content)

        try:
            df = pd.read_csv(temp_file_path) if file.filename.endswith('.csv') else pd.read_excel(temp_file_path, engine='openpyxl')
        finally:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                logger.info(f"Removed temporary file: {temp_file_path}")

        possible_columns = {
            "company": ["company", "Company", "COMPANY", "name", "Name"],
            "website": ["website", "Website", "WEBSITE", "url", "URL", "site", "Site"]
        }
        company_col = next((col for col in df.columns if col in possible_columns["company"]), None)
        website_col = next((col for col in df.columns if col in possible_columns["website"]), None)

        if not company_col or not website_col:
            logger.warning("Missing required columns in uploaded file")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "File must contain columns for company and website (e.g., 'Company', 'Website', 'URL')."
            })

        queue = [(row[company_col].strip(), row[website_col].strip()) for _, row in df.iterrows()]
        logger.info(f"Total URLs to process: {len(queue)}")

        async def process_url(company: str, website: str, session: ClientSession, semaphore: asyncio.Semaphore):
            async with semaphore:
                try:
                    normalized_url = normalize_url(website)
                    font_details_list, error_msg = await asyncio.wait_for(
                        fetch_fonts_from_url(normalized_url, session), timeout=60
                    )
                    logger.info(f"Processed {normalized_url}: {len(font_details_list)} fonts found")
                    return {
                        "company": company,
                        "website_url": normalized_url,
                        "total_fonts": len(font_details_list),
                        "font_details_list": font_details_list,
                        "error": error_msg
                    }
                except asyncio.TimeoutError:
                    logger.error(f"Timeout processing {website}")
                    return {
                        "company": company,
                        "website_url": website,
                        "total_fonts": 0,
                        "font_details_list": [],
                        "error": "Timeout: Processing took too long"
                    }
                except ValueError as e:
                    logger.error(f"Invalid URL {website}: {str(e)}")
                    return {
                        "company": company,
                        "website_url": website,
                        "total_fonts": 0,
                        "font_details_list": [],
                        "error": f"Invalid URL: {str(e)}"
                    }
                except Exception as e:
                    logger.error(f"Error processing {website}: {str(e)}")
                    return {
                        "company": company,
                        "website_url": website,
                        "total_fonts": 0,
                        "font_details_list": [],
                        "error": f"Error: {str(e)}"
                    }

        max_concurrent_tasks = 2  # Reduced to prevent resource exhaustion
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=15)) as session:
            semaphore = asyncio.Semaphore(max_concurrent_tasks)
            tasks = [process_url(company, website, session, semaphore) for company, website in queue]
            bulk_results = await asyncio.gather(*tasks, return_exceptions=True)

            bulk_results = [result for result in bulk_results if isinstance(result, dict)]

        save_font_data("bulk_fetched", bulk_results)
        logger.info(f"Bulk fetch completed with {len(bulk_results)} results")

        return templates.TemplateResponse("main.html", {
            "request": request,
            "bulk_results": bulk_results,
            "message": f"Processed {len(queue)} URLs"
        })
    except Exception as e:
        logger.error(f"Error processing file {file.filename}: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error processing file: {str(e)}",
            "bulk_results": []
        })

@app.get("/download-font-data", response_class=StreamingResponse)
async def download_font_data(request: Request, current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return RedirectResponse(url="/", status_code=303)

    try:
        if not os.path.exists("font_data.json"):
            logger.warning("No font data available to download")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "No font data available to download."
            })

        with open("font_data.json", "r") as f:
            font_data = json.load(f)

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        headers = [
            "Company", "Website URL", "Total Fonts", "Font Name", "Family",
            "Subfamily", "Weight", "Designer", "Manufacturer", "Copyright",
            "Font URL", "License Type", "Error"
        ]

        ws_uploaded = wb.create_sheet("Uploaded Fonts")
        ws_uploaded.append(headers)
        for font in font_data.get("uploaded_fonts", []):
            font_details = font.get("font_details", {})
            ws_uploaded.append([
                "N/A",
                "N/A",
                1,
                font_details.get("full_name", "Unknown"),
                font_details.get("family", "Unknown"),
                font_details.get("subfamily", "Unknown"),
                font_details.get("weight", "Unknown"),
                font_details.get("designer", "Unknown"),
                font_details.get("manufacturer", "Unknown"),
                font_details.get("copyright", "Unknown"),
                "N/A",
                font_details.get("license_type", "Unknown"),
                "-"
            ])

        ws_fetched = wb.create_sheet("Fetched Fonts")
        ws_fetched.append(headers)
        for fetched_data in font_data.get("fetched_fonts", []):
            for font in fetched_data.get("font_details_list", []):
                ws_fetched.append([
                    fetched_data.get("company", "Unknown"),
                    fetched_data.get("website_url", "Unknown"),
                    fetched_data.get("total_fonts", 0),
                    font.get("full_name", "Unknown"),
                    font.get("family", "Unknown"),
                    font.get("subfamily", "Unknown"),
                    font.get("weight", "Unknown"),
                    font.get("designer", "Unknown"),
                    font.get("manufacturer", "Unknown"),
                    font.get("copyright", "Unknown"),
                    font.get("url", "N/A"),
                    font.get("license_type", "Unknown"),
                    "-"
                ])

        ws_bulk = wb.create_sheet("Bulk Fetched Fonts")
        ws_bulk.append(headers)
        for result in font_data.get("bulk_fetched", []):
            if result.get("error"):
                ws_bulk.append([
                    result.get("company", "Unknown"),
                    result.get("website_url", "Unknown"),
                    0,
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    result.get("error", "-")
                ])
            else:
                for font in result.get("font_details_list", []):
                    ws_bulk.append([
                        result.get("company", "Unknown"),
                        result.get("website_url", "Unknown"),
                        result.get("total_fonts", 0),
                        font.get("full_name", "Unknown"),
                        font.get("family", "Unknown"),
                        font.get("subfamily", "Unknown"),
                        font.get("weight", "Unknown"),
                        font.get("designer", "Unknown"),
                        font.get("manufacturer", "Unknown"),
                        font.get("copyright", "Unknown"),
                        font.get("url", "N/A"),
                        font.get("license_type", "Unknown"),
                        "-"
                    ])

        output = BytesIO()
        wb.save(output)
        output.seek(0)

        logger.info("Font data Excel file generated successfully")
        return StreamingResponse(
            content=output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=font_data.xlsx"}
        )
    except Exception as e:
        logger.error(f"Error generating Excel file: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error generating Excel file: {str(e)}"
        })

@app.post("/clear-font-data", response_class=HTMLResponse)
async def clear_font_data(request: Request, current_user: str = Depends(get_current_user)):
    try:
        font_data_file = "font_data.json"
        logger.info(f"Using font data file: {font_data_file}")

        if os.path.exists(font_data_file):
            os.remove(font_data_file)
            logger.info(f"Deleted existing file: {font_data_file}")
        else:
            logger.warning(f"File {font_data_file} does not exist, no deletion needed")
        os.makedirs(os.path.dirname(font_data_file) or '.', exist_ok=True)
        empty_data = {"uploaded_fonts": [], "fetched_fonts": [], "bulk_fetched": []}
        with open(font_data_file, "w") as f:
            json.dump(empty_data, f, indent=4)
            logger.info(f"Created new empty file: {font_data_file}")

        with open(font_data_file, "r") as f:
            verified_data = json.load(f)
            if verified_data != empty_data:
                raise Exception("Verification failed: New file does not contain expected empty structure")
            logger.info(f"Verified new file content: {verified_data}")

        return templates.TemplateResponse("main.html", {
            "request": request,
            "message": "All font data cleared and new file created successfully."
        })
    except PermissionError as e:
        logger.error(f"Permission denied when clearing font data: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error_message": f"Permission denied. Ensure the application has write access to {font_data_file}.",
            "error_type": "PermissionError"
        })
    except Exception as e:
        logger.error(f"Error clearing font data: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error_message": f"Error clearing font data: {str(e)}.",
            "error_type": type(e).__name__
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, workers=3)