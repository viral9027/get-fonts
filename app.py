from fastapi import FastAPI, File, UploadFile, Form, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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
import pandas as pd  # Added for CSV/XLSX parsing

app = FastAPI()
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
        return None
    session_data = sessions[session_id]
    if time.time() - session_data["created_at"] > 3600:
        del sessions[session_id]
        return None
    return session_data["email"]


def normalize_url(url: str) -> str:
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Invalid URL: No domain specified")
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
        print(f"Error saving font data: {str(e)}")


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
    response.set_cookie(key="session_id", value=session_id, httponly=True)
    return response


@app.get("/main", response_class=HTMLResponse)
async def get_main(request: Request, current_user: str = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/", status_code=303)
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
        return RedirectResponse(url="/", status_code=303)
    try:
        content = await file.read()
        temp_file_path = f"temp_{file.filename}"
        with open(temp_file_path, "wb") as f:
            f.write(content)

        font = TTFont(temp_file_path)
        font_details = extract_font_details(font)

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
        return RedirectResponse(url="/", status_code=303)
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

        return templates.TemplateResponse("main.html", {
            "request": request,
            "font_details_list": font_details_list,
            "website_url": normalized_url,
            "company": company,
            "total_fonts": len(font_details_list) if font_details_list else 0
        })
    except ValueError as e:
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Invalid URL: {str(e)}"
        })
    except Exception as e:
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error fetching fonts from {url}: {str(e)}"
        })


# New endpoint for uploading CSV/XLSX file
@app.get("/upload-file", response_class=RedirectResponse)
async def get_upload_file_redirect():
    return RedirectResponse(url="/", status_code=303)


@app.post("/upload-file", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/", status_code=303)

    try:
        # Validate file type
        if not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "Please upload a CSV or XLSX file."
            })

        # Read the file content
        content = await file.read()
        temp_file_path = f"temp_{file.filename}"
        with open(temp_file_path, "wb") as f:
            f.write(content)

        # Parse the file using pandas
        if file.filename.endswith('.csv'):
            df = pd.read_csv(temp_file_path)
        else:  # .xlsx
            df = pd.read_excel(temp_file_path, engine='openpyxl')

        os.remove(temp_file_path)

        # Validate the file structure
        expected_columns = ["Company", "Website"]
        if not all(col in df.columns for col in expected_columns):
            return templates.TemplateResponse("main.html", {
                "request": request,
                "error": "File must contain 'Company' and 'Website' columns."
            })

        # Process each row to fetch fonts
        bulk_results = []
        for _, row in df.iterrows():
            company = str(row["Company"]).strip()
            website = str(row["Website"]).strip()

            try:
                normalized_url = normalize_url(website)
                font_details_list = await fetch_fonts_from_url(normalized_url)
                bulk_results.append({
                    "company": company,
                    "website_url": normalized_url,
                    "total_fonts": len(font_details_list) if font_details_list else 0,
                    "font_details_list": font_details_list,
                    "error": None
                })
            except Exception as e:
                bulk_results.append({
                    "company": company,
                    "website_url": website,
                    "total_fonts": 0,
                    "font_details_list": [],
                    "error": f"Error fetching fonts from {website}: {str(e)}"
                })

        # Save the bulk results for export compatibility
        save_font_data("bulk_fetched", bulk_results)

        return templates.TemplateResponse("main.html", {
            "request": request,
            "bulk_results": bulk_results
        })
    except Exception as e:
        return templates.TemplateResponse("main.html", {
            "request": request,
            "error": f"Error processing file: {str(e)}"
        })


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

async def fetch_fonts_from_url(url: str):
    font_details_list = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()

            # Set a realistic user-agent to avoid bot detection
            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            })

            fonts = []

            async def capture_fonts(request):
                if request.resource_type == "font":
                    font_url = request.url.lower()
                    if font_url.endswith(('.ttf', '.otf', '.woff2')):  # Include WOFF2 if you want to process it
                        fonts.append(request.url)
                    else:
                        print(f"Skipping unsupported font format: {font_url}")

            page.on("request", capture_fonts)

            try:
                # Use domcontentloaded instead of networkidle to reduce load time
                await page.goto(url, wait_until="domcontentloaded", timeout=180000)
            except PlaywrightTimeoutError:
                print(f"Timeout navigating to {url}. Proceeding with captured fonts.")
            except Exception as e:
                print(f"Error navigating to {url}: {str(e)}. Proceeding with captured fonts.")

            await browser.close()

            async with aiohttp.ClientSession() as session:
                for font_url in fonts:
                    try:
                        async with session.get(font_url, timeout=30) as response:
                            if response.status == 200:
                                content = await response.read()
                                temp_file_path = f"temp_font_{secrets.token_hex(4)}"
                                # Adjust file extension based on the URL
                                if font_url.endswith('.woff2'):
                                    temp_file_path += '.woff2'
                                else:
                                    temp_file_path += '.ttf'
                                with open(temp_file_path, "wb") as f:
                                    f.write(content)
                                try:
                                    if font_url.endswith('.woff2'):
                                        # Convert WOFF2 to TTF (requires woff2 library)
                                        import woff2
                                        ttf_path = temp_file_path.replace('.woff2', '.ttf')
                                        woff2.decompress(temp_file_path, ttf_path)
                                        font爾 = TTFont(ttf_path)
                                        os.remove(ttf_path)  # Clean up the converted file
                                    else:
                                        font = TTFont(temp_file_path)
                                    font_details = extract_font_details(font)
                                    font_details["url"] = font_url
                                    font_details_list.append(font_details)
                                except Exception as e:
                                    print(f"Error processing font from {font_url}: {str(e)}")
                                finally:
                                    os.remove(temp_file_path)
                            else:
                                print(f"Failed to download font from {font_url}: HTTP {response.status}")
                    except Exception as e:
                        print(f"Error downloading font from {font_url}: {str(e)}")

    except Exception as e:
        print(f"Error in fetch_fonts_from_url: {str(e)}")

    return font_details_list