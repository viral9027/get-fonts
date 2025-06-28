import asyncio
import logging
import resource
from fastapi import FastAPI, File, UploadFile, Form, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from fontTools.ttLib import TTFont
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import aiohttp
import os
import hashlib
import secrets
import time
from urllib.parse import urlparse, urlunparse
import json
import pandas as pd
import openpyxl
from io import BytesIO

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Increase file descriptor limit
try:
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard_limit))
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

sessions = {}

class LoginData(BaseModel):
    email: str
    password: str

def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        logger.info("No session_id cookie found in request")
        return None
    if session_id not in sessions:
        logger.info(f"Session_id {session_id} not found in sessions")
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

async def fetch_fonts_from_url(url: str, max_retries: int = 2):
    font_details_list = []
    retry_count = 0
    fonts = []

    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
                chromium_sandbox=False,
                timeout=90000
            )
            context = await browser.new_context()
            while retry_count < max_retries:
                try:
                    page = await context.new_page()
                    await page.set_extra_http_headers({
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                    })
                    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media"] else route.continue_())

                    async def capture_fonts(request):
                        if request.resource_type == "font":
                            font_url = request.url.lower()
                            logger.info(f"Captured font request: {font_url}")
                            if font_url.endswith(('.ttf', '.otf', '.woff', '.woff2')):
                                fonts.append(request.url)
                            else:
                                logger.info(f"Skipping unsupported font format: {font_url}")

                    page.on("request", capture_fonts)
                    page.on("response", lambda response: logger.info(f"Response: {response.url} - {response.status}"))

                    try:
                        await page.goto(url, wait_until="load", timeout=60000)
                        logger.info(f"Page loaded: {url}")
                        break
                    except PlaywrightTimeoutError:
                        logger.warning(f"Timeout navigating to {url}. Proceeding with captured fonts.")
                        break
                    except Exception as e:
                        logger.error(f"Error navigating to {url}: {str(e)}. Retrying ({retry_count + 1}/{max_retries})...")
                        retry_count += 1
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error creating page for {url}: {str(e)}")
                    retry_count += 1
                    if retry_count == max_retries:
                        logger.warning(f"Max retries reached for {url}. Proceeding with captured fonts.")
                finally:
                    if page:
                        try:
                            await page.close()
                        except Exception as e:
                            logger.warning(f"Error closing page for {url}: {str(e)}")
        except Exception as e:
            logger.error(f"Error initializing browser for {url}: {str(e)}")
        finally:
            if context:
                try:
                    await context.close()
                except Exception as e:
                    logger.warning(f"Error closing context for {url}: {str(e)}")
            if browser:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning(f"Error closing browser for {url}: {str(e)}")

    if not fonts:
        logger.warning(f"No fonts found for {url}.")
        return font_details_list

    async with aiohttp.ClientSession() as session:
        for font_url in fonts:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                    "Referer": url
                }
                logger.info(f"Downloading font from: {font_url}")
                async with session.get(font_url, timeout=60, headers=headers) as response:
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
                            if os.path.exists(temp_file_path):
                                os.remove(temp_file_path)
                    else:
                        logger.warning(f"Failed to download font from {font_url}: HTTP {response.status}.")
            except Exception as e:
                logger.error(f"Error downloading font from {font_url}: {str(e)}")

    return font_details_list

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
            "error": "Session expired or invalid. Please log in again."
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
            "error": "Session expired or invalid. Please log in again."
        })
    try:
        if not file.filename.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
            logger.warning(f"Unsupported font format: {file.filename}")
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
        normalized_url = normalize_url(url)
        font_details_list = await fetch_fonts_from_url(normalized_url)
        company = extract_company_from_url(normalized_url)
        save_font_data("fetched", {
            "website_url": normalized_url,
            "company": company,
            "total_fonts": len(font_details_list) if font_details_list else 0,
            "font_details_list": font_details_list
        })

        if not font_details_list:
            logger.warning(f"No fonts found on {normalized_url}")
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": f"No fonts found on {normalized_url}. The website might not use downloadable fonts, or they are protected by CORS."
            })

        logger.info(f"Successfully fetched {len(font_details_list)} fonts from {normalized_url}")
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
async def upload_file(request: Request, file: UploadFile = File(...), batch_size: int = Form(20), current_user: str = Depends(get_current_user)):
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {"request": request, "error": "Session expired or invalid. Please log in again."})

    try:
        if not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
            logger.warning(f"Invalid file format: {file.filename}")
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
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

        expected_columns = ["Company", "Website"]
        if not all(col in df.columns for col in expected_columns):
            logger.warning("Missing required columns in uploaded file")
            return templates.TemplateResponse("main.html", {"request": request, "error": "File must contain 'Company' and 'Website' columns."})

        # Load existing data to avoid reprocessing
        existing_data = {}
        processed_urls = set()
        if os.path.exists("font_data.json"):
            with open("font_data.json", "r") as f:
                existing_data = json.load(f)
                for result in existing_data.get("bulk_fetched", []):
                    processed_urls.add(result.get("website_url"))

        bulk_results = existing_data.get("bulk_fetched", [])
        queue = []
        for _, row in df.iterrows():
            website = row["Website"].strip()
            if website not in processed_urls:
                queue.append((row["Company"].strip(), website))

        logger.info(f"Total URLs to process: {len(queue)}")

        for i in range(0, len(queue), batch_size):
            batch_urls = queue[i:i + batch_size]
            for company, website in batch_urls:
                try:
                    normalized_url = normalize_url(website)
                    logger.info(f"Processing URL {i + 1}/{len(queue)}: {normalized_url}")
                    font_details_list = await fetch_fonts_from_url(normalized_url)
                    result = {
                        "company": company,
                        "website_url": normalized_url,
                        "total_fonts": len(font_details_list) if font_details_list else 0,
                        "font_details_list": font_details_list,
                        "error": None
                    }
                    bulk_results.append(result)
                except ValueError as e:
                    logger.error(f"Invalid URL {website}: {str(e)}")
                    bulk_results.append({
                        "company": company,
                        "website_url": website,
                        "total_fonts": 0,
                        "font_details_list": [],
                        "error": f"Invalid URL: {str(e)}"
                    })
                except Exception as e:
                    logger.error(f"Error fetching fonts from {website}: {str(e)}")
                    bulk_results.append({
                        "company": company,
                        "website_url": website,
                        "total_fonts": 0,
                        "font_details_list": [],
                        "error": f"Error: {str(e)}"
                    })
                # Save progress after each URL
                save_font_data("bulk_fetched", bulk_results)
                logger.info(f"Saved progress after processing {website}")
                await asyncio.sleep(1)  # Brief pause to release resources

        logger.info(f"Bulk fetch completed with {len(bulk_results)} results")
        return templates.TemplateResponse("main.html", {"request": request, "bulk_results": bulk_results})

    except Exception as e:
        logger.error(f"Error processing file: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error processing file: {str(e)}",
            "bulk_results": existing_data.get("bulk_fetched", []) if os.path.exists("font_data.json") else []
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
    if not current_user:
        logger.warning("No valid user session, redirecting to login")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Session expired or invalid. Please log in again."
        })
    try:
        session_id = request.cookies.get("session_id")
        current_session = sessions.get(session_id, None)
        sessions.clear()
        if current_session:
            sessions[session_id] = current_session
        empty_data = {"uploaded_fonts": [], "fetched_fonts": [], "bulk_fetched": []}
        with open("font_data.json", "w") as f:
            json.dump(empty_data, f, indent=4)
        logger.info("Font data and sessions (except current user) cleared successfully")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "message": "All font data and sessions cleared successfully."
        })
    except Exception as e:
        logger.error(f"Error clearing font data: {str(e)}")
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error clearing font data: {str(e)}"
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)