from fastapi import FastAPI, File, UploadFile, Form, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from fontTools.ttLib import TTFont
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import aiohttp
import asyncio
import os
import hashlib
import secrets
import time
from urllib.parse import urlparse, urlunparse
import json
import pandas as pd
import openpyxl
from io import BytesIO
import resource
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

users_db = {
    "user@example.com": {
        "email": "user@example.com",
        "hashed_password": hashlib.sha256("password123".encode()).hexdigest()
    }
}

sessions = {}

class LoginData(BaseModel):
    email: str
    password: str

def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        logger.info(f"No session found for session_id: {session_id}")
        return None
    session_data = sessions.get(session_id)
    if not session_data or time.time() - session_data["created_at"] > 3600:
        if session_id in sessions:
            logger.info(f"Session expired for session_id: {session_id}")
            del sessions[session_id]
        return None
    logger.info(f"Session valid for user: {session_data['email']}")
    return session_data["email"]

def normalize_url(url: str) -> str:
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Invalid URL: No netloc specified")
    normalized = urlunparse((
        parsed.scheme or 'https',
        parsed.netloc,
        parsed.path or '/',
        parsed.params,
        parsed.query,
        parsed.fragment
    ))
    return normalized

def extract_company_from_url(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    company = domain.split('.')[0]
    return ' '.join(word.capitalize() for word in company.split('-'))

def save_font_data(data_type: str, data: dict):
    try:
        if os.path.exists("font_data.json"):
            with open("font_data.json", "r") as f:
                existing_data = json.load(f)
        else:
            existing_data = {"uploaded_fonts": [], "fetched_fonts": [], "bulk_fetched": []}

        if data_type == "uploaded":
            existing_data["uploaded_fonts"] = [data]
        elif data_type == "fetched":
            existing_data["fetched_fonts"] = data
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

    weight = "Unknown"
    if "OS/2" in font:
        weight_class = font["OS/2"].usWeightClass
        weight = str(weight_class)

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

async def fetch_fonts_from_url(url: str, browser):
    font_details_list = []
    max_retries = 2
    retry_count = 0
    fonts = []

    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    logger.info(f"Current file descriptor limits: soft={soft_limit}, hard={hard_limit}")

    while retry_count < max_retries:
        try:
            page = await browser.new_page()
            try:
                await page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                })
                await page.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())

                async def capture_fonts(request):
                    if request.resource_type == "font":
                        font_url = request.url.lower()
                        logger.info(f"Captured font request: {font_url}")
                        if font_url.endswith(('.ttf', '.otf', '.woff', '.woff2')):
                            fonts.append(request.url)
                        else:
                            logger.info(f"Skipping unsupported font format: {font_url}")

                page.on("request", capture_fonts)

                try:
                    await page.goto(url, wait_until="networkidle", timeout=15000)  # 15 seconds timeout
                    await page.wait_for_timeout(1000)
                    if not fonts:
                        logger.warning(f"No fonts captured for {url}. Skipping download.")
                        break
                    logger.info(f"Total fonts captured: {len(fonts)}")
                    break
                except PlaywrightTimeoutError:
                    logger.warning(f"Timeout navigating to {url}. Proceeding with captured fonts.")
                    break
                except Exception as e:
                    logger.error(f"Error navigating to {url}: {str(e)}. Retrying ({retry_count + 1}/{max_retries})...")
                    retry_count += 1
            finally:
                await page.close()
        except Exception as e:
            logger.error(f"Error in fetch_fonts_from_url: {str(e)}")
            retry_count += 1
            if retry_count == max_retries:
                logger.warning(f"Max retries reached for {url}. Proceeding with captured fonts.")

    if not fonts:
        return font_details_list

    async with aiohttp.ClientSession() as session:
        for font_url in fonts:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                    "Referer": url
                }
                logger.info(f"Downloading font from: {font_url}")
                async with session.get(font_url, timeout=10, headers=headers) as response:
                    if response.status == 200:
                        content = await response.read()
                        temp_file_path = f"temp_font_{secrets.token_hex(4)}"
                        if font_url.endswith('.woff'):
                            temp_file_path += '.woff'
                        elif font_url.endswith('.woff2'):
                            temp_file_path += '.woff2'
                        else:
                            temp_file_path += '.ttf'
                        with open(temp_file_path, "wb") as f:
                            f.write(content)
                        try:
                            font = TTFont(temp_file_path)
                            font_details = extract_font_details(font)
                            font_details["url"] = font_url
                            font_details_list.append(font_details)
                            logger.info(f"Successfully processed font: {font_url}")
                        except Exception as e:
                            logger.error(f"Error processing font from {font_url}: {str(e)}")
                        finally:
                            os.remove(temp_file_path)
                    else:
                        logger.warning(f"Failed to download font from {font_url}: HTTP {response.status}. Possibly CORS-restricted.")
            except Exception as e:
                logger.error(f"Error downloading font from {font_url}: {str(e)}")

    logger.info(f"Returning {len(font_details_list)} font details")
    return font_details_list

@app.get("/", response_class=HTMLResponse)
async def get_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login", response_class=RedirectResponse)
async def get_login_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.post("/login", response_class=HTMLResponse)
async def login(response: Response, email: str = Form(...), password: str = Form(...)):
    user = users_db.get(email)
    hashed_password = hashlib.sha256(password.encode()).hexdigest()
    if not user or user["hashed_password"] != hashed_password:
        return templates.TemplateResponse("login.html", {
            "request": Request,
            "error": "Invalid credentials"
        })

    session_id = secrets.token_hex(16)
    sessions[session_id] = {
        "email": email,
        "created_at": time.time()
    }
    response = RedirectResponse(url="/main", status_code=303)
    response.set_cookie(key="session_id", value=session_id, httponly=True, secure=False)
    return response

@app.get("/main", response_class=HTMLResponse)
async def get_main(request: Request, current_user: str = Depends(get_current_user)):
    if not current_user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired. Please log in again."
        })
    return templates.TemplateResponse("main.html", {"request": request})

@app.get("/logout", response_class=RedirectResponse)
async def logout(request: Request, response: Response):
    session_id = request.cookies.get("session_id")
    if session_id in sessions:
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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired. Please log in again."
        })
    try:
        if not file.filename.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "Unsupported font format. Please upload a .ttf, .otf, .woff, or .woff2 file."
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
            os.remove(temp_file_path)

        save_font_data("uploaded", {
            "filename": file.filename,
            "font_details": font_details
        })

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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired. Please log in again."
        })
    try:
        normalized_url = normalize_url(url)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
                chromium_sandbox=False,
                timeout=60000
            )
            try:
                font_details_list = await fetch_fonts_from_url(normalized_url, browser)
            finally:
                await browser.close()

            company = extract_company_from_url(normalized_url)
            save_font_data("fetched", {
                "website_url": normalized_url,
                "company": company,
                "total_fonts": len(font_details_list) if font_details_list else 0,
                "font_details_list": font_details_list
            })

            if not font_details_list:
                return templates.TemplateResponse("main.html", {
                    "request": request,
                    "error": f"No fonts found on {normalized_url}. The website might not use downloadable fonts, or they are protected by CORS."
                })

            return templates.TemplateResponse("main.html", {
                "request": request,
                "font_details_list": font_details_list,
                "website_url": normalized_url,
                "company": company,
                "total_fonts": len(font_details_list)
            })
    except ValueError as e:
        logger.error(f"Invalid URL: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Invalid URL: {str(e)}"
        })
    except Exception as e:
        logger.error(f"Error fetching fonts from {url}: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error fetching fonts from {url}: {str(e)}"
        })

@app.get("/upload-file", response_class=RedirectResponse)
async def get_upload_file_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.post("/upload-file", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...), batch_size: int = Form(100), current_user: str = Depends(get_current_user)):
    if not current_user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Session expired. Please log in again."})

    try:
        if not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
            return templates.TemplateResponse("main.html", {"request": request, "error": "Please upload a CSV or XLSX file."})

        content = await file.read()
        temp_file_path = f"temp_{file.filename}_{secrets.token_hex(4)}"
        with open(temp_file_path, "wb") as f:
            f.write(content)

        try:
            if file.filename.endswith('.csv'):
                df = pd.read_csv(temp_file_path)
            else:
                df = pd.read_excel(temp_file_path, engine='openpyxl')
        finally:
            os.remove(temp_file_path)

        expected_columns = ["Company", "Website"]
        if not all(col in df.columns for col in expected_columns):
            return templates.TemplateResponse("main.html", {"request": request, "error": "File must contain 'Company' and 'Website' columns."})

        existing_data = {}
        if os.path.exists("font_data.json"):
            with open("font_data.json", "r") as f:
                existing_data = json.load(f).get("bulk_fetched", [])

        bulk_results = []
        queue = asyncio.Queue()
        for _, row in df.iterrows():
            queue.put_nowait((row["Company"].strip(), row["Website"].strip()))

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
                chromium_sandbox=False,
                timeout=60000
            )
            try:
                async def process_queue():
                    processed = 0
                    while not queue.empty() and processed < batch_size:
                        company, website = await queue.get()
                        try:
                            normalized_url = normalize_url(website)
                            async with asyncio.timeout(30):
                                font_details_list = await fetch_fonts_from_url(normalized_url, browser)
                            bulk_results.append({
                                "company": company,
                                "website_url": normalized_url,
                                "total_fonts": len(font_details_list) if font_details_list else 0,
                                "font_details_list": font_details_list,
                                "error": None
                            })
                            processed += 1
                        except asyncio.TimeoutError:
                            logger.error(f"Timeout fetching fonts from {website}")
                            bulk_results.append({
                                "company": company,
                                "website_url": website,
                                "total_fonts": 0,
                                "font_details_list": [],
                                "error": f"Timeout fetching fonts from {website}"
                            })
                        except Exception as e:
                            logger.error(f"Error fetching fonts from {website}: {str(e)}")
                            for data in existing_data:
                                if data.get("website_url") == website:
                                    bulk_results.append(data)
                                    break
                            else:
                                bulk_results.append({
                                    "company": company,
                                    "website_url": website,
                                    "total_fonts": 0,
                                    "font_details_list": [],
                                    "error": f"Error fetching fonts from {website}: {str(e)}"
                                })
                        finally:
                            queue.task_done()

                tasks = [process_queue() for _ in range(min(10, queue.qsize()))]  # Increased to 10 concurrent tasks
                async with asyncio.timeout(45):
                    await asyncio.gather(*tasks)

                if queue.qsize() > 0:
                    logger.warning(f"Processed {len(bulk_results)} URLs. {queue.qsize()} remaining. Submit another batch.")

            finally:
                await browser.close()

        save_font_data("bulk_fetched", bulk_results)

        return templates.TemplateResponse("main.html", {"request": request, "bulk_results": bulk_results})
    except asyncio.TimeoutError:
        logger.error("Overall request timeout exceeded")
        if os.path.exists("font_data.json"):
            with open("font_data.json", "r") as f:
                existing_bulk = json.load(f).get("bulk_fetched", [])
                return templates.TemplateResponse("main.html", {"request": request, "bulk_results": existing_bulk})
        return templates.TemplateResponse("main.html", {"request": request, "error": "Request timed out"})
    except Exception as e:
        logger.error(f"Error processing file: {str(e)}")
        if os.path.exists("font_data.json"):
            with open("font_data.json", "r") as f:
                existing_bulk = json.load(f).get("bulk_fetched", [])
                return templates.TemplateResponse("main.html", {"request": request, "bulk_results": existing_bulk})
        return templates.TemplateResponse("main.html", {"request": request, "error": f"Error processing file: {str(e)}"})

@app.get("/download-font-data", response_class=StreamingResponse)
async def download_font_data(current_user: str = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/", status_code=303)

    try:
        if not os.path.exists("font_data.json"):
            return templates.TemplateResponse("main.html", {
                "request": Request,
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
        fetched_data = font_data.get("fetched_fonts", {})
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
                    result.get("license_type", "Unknown"),
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

        return StreamingResponse(
            content=output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=font_data.xlsx"}
        )
    except Exception as e:
        logger.error(f"Error generating Excel file: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": Request,
            "error": f"Error generating Excel file: {str(e)}"
        })