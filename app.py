import asyncio
import logging
import resource
import json
import os
from fastapi import FastAPI, File, UploadFile, Form, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from fontTools.ttLib import TTFont
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import aiohttp
import hashlib
import secrets
import time
from urllib.parse import urlparse, urlunparse
import pandas as pd
import openpyxl
from io import BytesIO
from aiohttp import ClientSession, ClientTimeout

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Increase file descriptor limit
try:
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    new_limit = min(8192, hard_limit)  # Cap at 8192 or system max
    resource.setrlimit(resource.RLIMIT_NOFILE, (new_limit, hard_limit))
    logger.info(f"Updated file descriptor limit: {resource.getrlimit(resource.RLIMIT_NOFILE)}")
except Exception as e:
    logger.warning(f"Failed to increase file descriptor limit: {str(e)}")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

users_db = {
    "user@example.com": {
        "email": "user@example.com",
        "hashed_password": hashlib.sha256("password123".encode()).hexdigest()
    }
}

# In-memory session store
sessions = {}


def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    logger.info(f"Checking session_id: {session_id}")
    if not session_id:
        logger.warning("No session_id cookie found")
        return None
    session_data = sessions.get(session_id)
    if not session_data or time.time() - session_data["created_at"] > 86400:  # 24 hours
        logger.info(f"Session expired or not found for session_id: {session_id}")
        if session_id in sessions:
            del sessions[session_id]
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
    font_data_file = os.getenv("FONT_DATA_FILE", "font_data.json")
    if not font_data_file:
        font_data_file = "font_data.json"  # Fallback if env var is empty
    try:
        os.makedirs(os.path.dirname(font_data_file), exist_ok=True)
        logger.info(f"Ensured directory exists for {font_data_file}")
        existing_data = {"uploaded_fonts": [], "fetched_fonts": [], "bulk_fetched": []}
        if os.path.exists(font_data_file):
            with open(font_data_file, "r") as f:
                existing_data = json.load(f)

        if data_type == "uploaded":
            existing_data["uploaded_fonts"].append(data)
        elif data_type == "fetched":
            existing_data["fetched_fonts"].append(data)
        elif data_type == "bulk_fetched":
            existing_data["bulk_fetched"].extend(data)

        with open(font_data_file, "w") as f:
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

    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image",
                                                                                                    "media"] else route.continue_())

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
                        nonlocal cors_blocked
                        cors_blocked = True

            page.on("request", capture_fonts)
            page.on("response", check_response)

            try:
                await page.goto(url, wait_until="load", timeout=15000)
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
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout navigating {url}. Fonts may still be captured.")
            except Exception as e:
                logger.error(f"Navigation error for {url}: {str(e)}")
        finally:
            if browser:
                await browser.close()

    if not fonts:
        error_msg = "No downloadable fonts found"
        if cors_blocked:
            error_msg += " (likely due to CORS restrictions)"
        elif font_families or css_fonts or rendered_fonts:
            error_msg += " (site may use system fonts, embedded fonts, or fonts not loaded during page render)"
        else:
            error_msg += " (no fonts detected in network requests, CSS, or DOM)"
        logger.warning(f"{error_msg} for {url}")
        return font_details_list, error_msg

    for font_url in fonts:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Referer": url
            }
            async with session.get(font_url, timeout=ClientTimeout(total=10), headers=headers) as response:
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
            "error_message": "Invalid credentials",
            "error_type": "AuthenticationError"
        })

    session_id = secrets.token_hex(16)
    sessions[session_id] = {
        "email": email,
        "created_at": time.time()
    }
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
            "error_message": "Session expired or invalid. Please log in again.",
            "error_type": "SessionError"
        })
    logger.info(f"Rendering main page for user: {current_user}")
    return templates.TemplateResponse("main.html", {"request": request})


@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request, response: Response):
    session_id = request.cookies.get("session_id")
    if session_id in sessions:
        logger.info(f"Logging out session_id: {session_id}")
        del sessions[session_id]
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
            "error_message": "Session expired or invalid. Please log in again.",
            "error_type": "SessionError"
        })
    logger.info(f"Received font upload request for file: {file.filename}")
    font_details = {}
    error_type = None
    error_message = None
    try:
        logger.info(f"Reading file content: {file.filename}")
        if not file.filename.lower().endswith(('.ttf', '.otf', '.woff', '.woff2', '.eot', '.ttc')):
            error_type = "FileFormatError"
            error_message = "Unsupported font format. Please upload a .ttf, .otf, .woff, .woff2, .eot, or .ttc file."
            logger.warning(error_message)
        else:
            content = await file.read()
            temp_file_path = f"temp_{file.filename}_{secrets.token_hex(4)}"
            logger.info(f"Saving temporary file: {temp_file_path}")
            with open(temp_file_path, "wb") as f:
                f.write(content)

            font = None
            try:
                logger.info(f"Processing font file: {temp_file_path}")
                if temp_file_path.lower().endswith(('.woff', '.woff2')):
                    import woff2
                    ttf_path = temp_file_path.rsplit('.', 1)[0] + '.ttf'
                    woff2.decompress(temp_file_path, ttf_path)
                    font = TTFont(ttf_path)
                    os.remove(ttf_path)
                else:
                    font = TTFont(temp_file_path)

                font_details = extract_font_details(font)
            except Exception as e:
                error_type = type(e).__name__
                error_message = f"Failed to process font file."
                logger.error(f"Error processing font {file.filename}: {str(e)}")
            finally:
                if os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                    logger.info(f"Removed temporary file: {temp_file_path}")

            if font_details:
                save_font_data("uploaded", {
                    "filename": file.filename,
                    "font_details": font_details
                })
                logger.info(f"Successfully uploaded font: {file.filename}")
    except Exception as e:
        error_type = type(e).__name__
        error_message = "An unexpected error occurred while uploading the font."
        logger.error(f"Unexpected error processing font {file.filename}: {str(e)}")

    return templates.TemplateResponse("main.html", {
        "request": request,
        "font_details": font_details if font_details else {},
        "filename": file.filename,
        "error_type": error_type,
        "error_message": error_message
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
            "error_message": "Session expired or invalid. Please log in again.",
            "error_type": "SessionError"
        })

    logger.info(f"Fetching fonts from {url} by user: {current_user}")
    font_details_list = []
    company = ""
    error_type = None
    error_message = None
    try:
        normalized_url = normalize_url(url)
        async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as session:
            font_details_list, error_message = await fetch_fonts_from_url(normalized_url, session)

        company = extract_company_from_url(normalized_url)
        if font_details_list:
            result = {
                "website_url": normalized_url,
                "company": company,
                "total_fonts": len(font_details_list),
                "font_details_list": font_details_list
            }
            save_font_data("fetched", result)
            logger.info(f"Successfully fetched {len(font_details_list)} fonts from {normalized_url}")
        else:
            logger.warning(f"No fonts found on {normalized_url}: {error_message}")
    except ValueError as e:
        error_type = "ValueError"
        error_message = "Invalid URL format."
        logger.error(f"Invalid URL: {str(e)}")
    except Exception as e:
        error_type = type(e).__name__
        error_message = "An unexpected error occurred while fetching fonts."
        logger.error(f"Error fetching fonts from {url}: {str(e)}")

    return templates.TemplateResponse("main.html", {
        "request": request,
        "font_details_list": font_details_list if font_details_list else [],
        "website_url": normalized_url,
        "company": company,
        "total_fonts": len(font_details_list) if font_details_list else 0,
        "error_type": error_type,
        "error_message": error_message
    })


@app.get("/upload-file", response_class=RedirectResponse)
async def get_upload_file_redirect():
    return RedirectResponse(url="/", status_code=303)


@app.post("/upload-file", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...), batch_size: int = Form(20),
                      current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error_message": "Session expired or invalid. Please log in again.",
            "error_type": "SessionError"
        })
    logger.info(f"Received bulk file upload request for file: {file.filename}")
    bulk_results = []
    error_type = None
    error_message = None
    try:
        logger.info(f"Reading file content: {file.filename}")
        if not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
            error_type = "FileFormatError"
            error_message = "Please upload a CSV or XLSX file."
            logger.warning(error_message)
        else:
            content = await file.read()
            temp_file_path = f"temp_{file.filename}_{secrets.token_hex(4)}"
            logger.info(f"Saving temporary file: {temp_file_path}")
            with open(temp_file_path, "wb") as f:
                f.write(content)

            try:
                logger.info(f"Processing file: {temp_file_path}")
                df = pd.read_csv(temp_file_path) if file.filename.endswith('.csv') else pd.read_excel(temp_file_path,
                                                                                                      engine='openpyxl')
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
                error_type = "DataError"
                error_message = "File must contain columns for company and website (e.g., 'Company', 'Website', 'URL')."
                logger.warning(error_message)
            else:
                existing_data = {"bulk_fetched": []}
                processed_urls = set()
                font_data_file = os.getenv("FONT_DATA_FILE", "font_data.json")
                if not font_data_file:
                    font_data_file = "font_data.json"
                if os.path.exists(font_data_file):
                    with open(font_data_file, "r") as f:
                        existing_data = json.load(f)
                        processed_urls = {r["website_url"] for r in existing_data.get("bulk_fetched", [])}

                queue = [(row[company_col].strip(), row[website_col].strip()) for _, row in df.iterrows() if
                         row[website_col].strip() not in processed_urls]
                bulk_results = existing_data.get("bulk_fetched", [])

                logger.info(f"Total URLs to process: {len(queue)}")

                # Get maximum batch size from environment variable or default to 100
                max_batch_size = int(os.getenv("MAX_BATCH_SIZE", 100))
                if batch_size > max_batch_size:
                    batch_size = max_batch_size
                    logger.warning(f"Batch size reduced to maximum allowed: {max_batch_size}")

                async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as session:
                    tasks = []
                    for company, website in queue:
                        try:
                            normalized_url = normalize_url(website)
                            tasks.append(fetch_fonts_from_url(normalized_url, session))
                        except ValueError as e:
                            bulk_results.append({
                                "company": company,
                                "website_url": website,
                                "total_fonts": 0,
                                "font_details_list": [],
                                "error_message": "Invalid URL format.",
                                "error_type": "ValueError"
                            })

                    # Limit concurrent tasks to 5 to avoid resource exhaustion
                    semaphore = asyncio.Semaphore(5)

                    async def bounded_fetch(task):
                        async with semaphore:
                            return await task

                    results = await asyncio.gather(*(bounded_fetch(task) for task in tasks), return_exceptions=True)

                    for (company, website), result in zip(queue, results):
                        if isinstance(result, Exception):
                            bulk_results.append({
                                "company": company,
                                "website_url": website,
                                "total_fonts": 0,
                                "font_details_list": [],
                                "error_message": "An unexpected error occurred while fetching fonts.",
                                "error_type": type(result).__name__
                            })
                        else:
                            font_details_list, error_msg = result
                            bulk_results.append({
                                "company": company,
                                "website_url": normalize_url(website),
                                "total_fonts": len(font_details_list),
                                "font_details_list": font_details_list,
                                "error_message": error_msg,
                                "error_type": None if font_details_list else "NoFontsError"
                            })

                save_font_data("bulk_fetched", bulk_results)
                logger.info(f"Bulk fetch completed with {len(bulk_results)} results")
    except Exception as e:
        error_type = type(e).__name__
        error_message = "An unexpected error occurred while processing the file."
        logger.error(f"Unexpected error processing file {file.filename}: {str(e)}")

    return templates.TemplateResponse("main.html", {
        "request": request,
        "bulk_results": bulk_results if bulk_results else [],
        "message": f"Processed {len(bulk_results)} URLs" if bulk_results else "No data processed",
        "error_type": error_type,
        "error_message": error_message
    })


@app.get("/download-font-data", response_class=StreamingResponse)
async def download_font_data(request: Request, current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return RedirectResponse(url="/", status_code=303)

    try:
        font_data_file = os.getenv("FONT_DATA_FILE", "font_data.json")
        if not font_data_file:
            font_data_file = "font_data.json"
        if not os.path.exists(font_data_file):
            logger.warning("No font data available to download")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error_message": "No font data available to download.",
                "error_type": "DataNotFoundError"
            })

        with open(font_data_file, "r") as f:
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
            if result.get("error_message"):
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
                    result.get("error_message", "-")
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
            "error_message": "Error generating Excel file.",
            "error_type": type(e).__name__
        })


@app.post("/clear-font-data", response_class=HTMLResponse)
async def clear_font_data(request: Request, current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error_message": "Session expired or invalid. Please log in again.",
            "error_type": "SessionError"
        })
    try:
        font_data_file = os.getenv("FONT_DATA_FILE", "font_data.json")
        if not font_data_file:
            font_data_file = "font_data.json"
        os.makedirs(os.path.dirname(font_data_file), exist_ok=True)
        empty_data = {"uploaded_fonts": [], "fetched_fonts": [], "bulk_fetched": []}
        with open(font_data_file, "w") as f:
            json.dump(empty_data, f, indent=4)
        logger.info("Font data cleared successfully")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "message": "All font data cleared successfully."
        })
    except Exception as e:
        logger.error(f"Error clearing font data: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error_message": "Error clearing font data.",
            "error_type": type(e).__name__
        })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, workers=3)