import logging
import re
import json
import uuid
import pandas as pd
from .extract_fonts import process_urls, clear_font_cache
from flask import Blueprint
from flask import render_template, request, jsonify, send_file, redirect, url_for, session
from flask_sock import Sock

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

main = Blueprint('main', __name__)

sock = Sock(main)
# Hardcoded credentials (for demo purposes; use environment variables in production)

USERNAME = "admin"
PASSWORD = "admin"

# Store font data globally for download
global_font_data = []


# Helper to validate URLs
def is_valid_url(url):
    url = url.strip()
    if not url:
        return False
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    pattern = re.compile(
        r'^https?://(www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)$')
    return bool(pattern.match(url))


# Function to summarize license type
def summarize_license_type(license_text):
    if not license_text or license_text == "Unknown":
        return "Unknown", license_text

    # Common license names and patterns
    license_patterns = {
        "OFL": r"SIL Open Font License(?:, Version (\d+\.\d+))?",
        "Apache": r"Apache License(?:, Version (\d+\.\d+))?",
        "MIT": r"MIT License",
        "GPL": r"GNU General Public License(?:, Version (\d+))?",
        "CC": r"Creative Commons(?: (.*?))(?:, Version (\d+\.\d+))?"
    }

    # Key conditions to look for
    conditions = {
        "Free for commercial use": r"free use|commercial use allowed|free for commercial use",
        "Attribution required": r"attribution required|must give credit|copyright notice is retained",
        "No redistribution": r"no redistribution|cannot distribute|distribution not allowed",
        "Modification allowed": r"modification allowed|can be modified|free to modify"
    }

    # Initialize summary
    license_name = "Unknown"
    license_version = ""
    key_conditions = []

    # Extract license name and version
    for name, pattern in license_patterns.items():
        match = re.search(pattern, license_text, re.IGNORECASE)
        if match:
            license_name = name
            if name in ["OFL", "Apache", "GPL", "CC"] and match.group(1):
                license_version = f" {match.group(1)}"
            break

    # Extract key conditions
    for condition, pattern in conditions.items():
        if re.search(pattern, license_text, re.IGNORECASE):
            key_conditions.append(condition)

    # Build summary
    summary = f"{license_name}{license_version}"
    if key_conditions:
        summary += f", {', '.join(key_conditions)}"

    return summary, license_text


@main.route('/')
def login():
    return render_template('login.html')


@main.route('/api/login', methods=['POST'])
def login_post():
    username = request.form.get('username')
    password = request.form.get('password')
    if username == USERNAME and password == PASSWORD:
        session['logged_in'] = True
        return jsonify({"status": "success", "redirect": "/upload"})
    else:
        return jsonify({"status": "error", "message": "Invalid credentials"})


@main.route('/logout')
def logout():
    session.pop('logged_in', None)
    logger.debug("User logged out, redirecting to login page")
    return redirect(url_for('main.login'))


@main.route('/upload')
def upload():
    if not session.get('logged_in'):
        return redirect(url_for('main.login'))
    return render_template('index.html')


@main.route('/clear-cache', methods=['POST'])
def clear_cache():
    try:
        clear_font_cache()
        logger.info("Font cache cleared successfully")
        return jsonify({"status": "success", "message": "Cache cleared successfully"})
    except Exception as e:
        logger.error(f"Failed to clear cache: {str(e)}")
        return jsonify({"status": "error", "message": f"Failed to clear cache: {str(e)}"})


async def process_url(url, company, ws=None, url_index=None, total_urls=None):
    logger.debug(f"Processing URL: {url}")
    if ws and url_index is not None and total_urls is not None:
        ws.send(json.dumps({"message": f"Processing URL {url_index}/{total_urls}: {url}"}))

    # Clear font cache for each URL to avoid caching issues
    clear_font_cache()

    try:
        result = (await process_urls([url]))[0]
        local_fonts = result["fonts"]
        total_fonts = result["total_fonts"]

        font_entries = []
        for font in local_fonts:
            license_summary, full_license = summarize_license_type(font.get("license_type", "Unknown"))
            font_entry = {
                "Company": company,
                "Website URL": url,
                "Total Fonts": total_fonts,
                "Font Name(s)": font.get("font_name", "Unknown"),
                "Family": font.get("family", "Unknown"),
                "Subfamily": font.get("subfamily", "Unknown"),
                "Weight": font.get("weight", "Unknown"),
                "Designer": font.get("designer", "Unknown"),
                "Manufacturer": font.get("manufacturer", "Unknown"),
                "Copyright": font.get("copyright", "Unknown"),
                "Font URL": font.get("font_url", "Unknown"),
                "License Type": full_license,
                "License Summary": license_summary,  # Add summarized license type
                "Error": font.get("error", None)
            }
            if font.get("error"):
                font_entry["Font Name(s)"] = "-"
            font_entries.append(font_entry)

        logger.debug(f"Processed {len(font_entries)} valid font entries for {url} with total fonts: {total_fonts}")
        return {"url": url, "status": "success"}, font_entries, total_fonts
    except Exception as e:
        logger.error(f"Failed to process URL: {url}, Error: {str(e)}")
        return {"url": url, "status": "failed", "error": str(e)}, [], 0


@main.route('/upload', methods=['POST'])
async def upload_file():
    if not session.get('logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized access. Please login."})

    global global_font_data

    # Reset global_font_data at the start of each new process
    logger.debug("Resetting global font data before new process")
    global_font_data = []

    url = request.form.get('url', '').strip()
    file = request.files.get('file')

    urls = []
    companies = []

    if url:
        if not is_valid_url(url):
            return jsonify({
                "status": "error",
                "message": "Invalid URL format. Please provide a valid URL starting with http:// or https://"
            })
        urls.append(url)
        companies.append(url.split('.')[1] if '.' in url else url)

    if file:
        mime_type = file.content_type
        logger.debug(f"File MIME type: {mime_type}")

        if mime_type not in [
            'text/csv',
            'application/vnd.ms-excel',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        ]:
            return jsonify({
                "status": "error",
                "message": "Unsupported file type. Please upload a CSV or Excel file."
            })

        try:
            if mime_type == 'text/csv':
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)

            if 'Website' not in df.columns or 'Company' not in df.columns:
                return jsonify({
                    "status": "error",
                    "message": "File must contain 'Company' and 'Website' columns."
                })

            for _, row in df.iterrows():
                website = str(row['Website']).strip()
                company = str(row['Company']).strip()
                if website and company:
                    if not website.startswith(('http://', 'https://')):
                        website = 'https://' + website
                    if is_valid_url(website):
                        if website not in urls:  # Avoid duplicates
                            urls.append(website)
                            companies.append(company)
                    else:
                        logger.warning(f"Skipping invalid URL: {website}")
        except Exception as e:
            logger.error(f"Error reading file: {str(e)}")
            return jsonify({
                "status": "error",
                "message": f"Error reading file: {str(e)}"
            })

    if not urls:
        return jsonify({
            "status": "error",
            "message": "No valid URLs provided. Please provide a URL or upload a file with valid URLs."
        })

    logger.debug(f"Starting processing for {len(urls)} URLs")
    results = []
    font_data = []

    for idx, (url, company) in enumerate(zip(urls, companies), 1):
        result, local_fonts, total_fonts = await process_url(
            url, company, request.environ.get('wsgi.websocket'), idx, len(urls)
        )
        results.append(result)

        if not local_fonts:
            font_entry = {
                "Company": company,
                "Website URL": url,
                "Total Fonts": total_fonts,
                "Font Name(s)": "-",
                "Family": "Unknown",
                "Subfamily": "Unknown",
                "Weight": "Unknown",
                "Designer": "Unknown",
                "Manufacturer": "Unknown",
                "Copyright": "Unknown",
                "Font URL": "Unknown",
                "License Type": "Unknown",
                "License Summary": "Unknown",  # Add summarized license type
                "Error": result.get("error", "No fonts found")
            }
            font_data.append(font_entry)
            logger.debug(f"Added no-fonts entry for {url} with Total Fonts: {total_fonts}")
        else:
            font_data.extend(local_fonts)
            logger.debug(f"Added {len(local_fonts)} font entries for {url} with total fonts: {total_fonts}")

        # Ensure Total Fonts consistency across all entries for this URL
        for entry in font_data:
            if entry["Website URL"] == url:
                entry["Total Fonts"] = total_fonts
        logger.debug(f"Set Total Fonts to {total_fonts} for {url}")

    global_font_data = font_data  # Update global_font_data atomically
    logger.debug(f"Completed processing {len(urls)} URLs")
    logger.info(f"Upload processing completed. Font data entries: {len(font_data)}")

    return jsonify({
        "urls": urls,
        "results": results,
        "font_data": font_data
    })


@main.route('/download-excel', methods=['POST'])
async def download_excel():
    if not session.get('logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized access. Please login."})

    if not global_font_data:
        return jsonify({"status": "error", "message": "No font data available to download"})

    try:
        df = pd.DataFrame(global_font_data)
        excel_file = f"font_metadata_{uuid.uuid4().hex[:8]}.xlsx"
        df.to_excel(excel_file, index=False)
        logger.debug(f"Excel file generated: {excel_file}")
        return send_file(excel_file, as_attachment=True, download_name="font_metadata.xlsx")
    except Exception as e:
        logger.error(f"Error generating Excel file: {str(e)}")
        return jsonify({"status": "error", "message": f"Error generating Excel file: {str(e)}"})


@sock.route('/ws')
async def websocket(ws):
    try:
        data = await ws.receive()
        if data:
            message = json.loads(data)
            if message.get("type") == "progress":
                await ws.send(json.dumps({"message": message["message"]}))
    except Exception as e:
        logger.debug(f"WebSocket closed: {str(e)}")
    finally:
        ws.close()
