# ========================
# 📦 IMPORTS
# ========================

import io
import re
import logging
import base64
import os
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF
import pandas as pd
import subprocess

# ========================
# ⚙️ CONFIGURATION DE L'APP
# ========================

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 Mo
CORS(app)
logging.basicConfig(level=logging.INFO)

# Nettoyage automatique des fichiers vieux de plus de 24h dans "static"
@app.before_request
def cleanup_old_files():
    folder = "static"
    now = time.time()
    cutoff = now - 24 * 3600  # 24h

    for filename in os.listdir(folder):
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            if os.path.getmtime(path) < cutoff:
                try:
                    os.remove(path)
                    logging.info(f"Fichier supprimé : {path}")
                except Exception as e:
                    logging.warning(f"Erreur suppression {path} : {e}")

MAX_CHARS = 10000

# ========================
# 📄 ROUTE : Extraction de texte PDF
# ========================

@app.route("/extract", methods=["POST"])
def extract_pdf():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier envoyé."}), 400

    file = request.files["file"]

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Le fichier doit être un PDF."}), 400

    try:
        doc = fitz.open(stream=file.read(), filetype="pdf")
        full_text = "\n".join(page.get_text() for page in doc)

        is_partial = False
        if len(full_text) > MAX_CHARS:
            is_partial = True
            full_text = full_text[:MAX_CHARS]

        logging.info(f"Texte extrait ({len(full_text)} caractères)")
        return jsonify({ "text": full_text, "partial": is_partial, "charCount": len(full_text) })
    except Exception as e:
        logging.error(f"Erreur lors de l’extraction : {str(e)}")
        return jsonify({"error": "Erreur lors de l’analyse du fichier."}), 500

# ========================
# 🧱 GESTION DES ERREURS
# ========================

@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return jsonify({"error": "Fichier trop lourd. Limite à 20 Mo."}), 413

# ========================
# 📊 ROUTE : Nettoyage de fichier Excel
# ========================

@app.route("/excel-cleaner", methods=["POST"])
def excel_cleaner():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier fourni."}), 400

    file = request.files["file"]
    filename = file.filename.lower()

    remove_duplicates = request.form.get("removeDuplicates", "true") == "true"
    clean_emails = request.form.get("cleanEmails", "true") == "true"
    sanitize_characters = request.form.get("sanitizeCharacters", "true") == "true"

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(file, on_bad_lines='skip')
        elif filename.endswith(".xlsx"):
            try:
                df = pd.read_excel(file, engine='openpyxl')
            except Exception as e:
                logging.warning(f"Erreur lecture Excel .xlsx : {e}")
                return jsonify({"error": "Erreur de lecture du fichier Excel. Vérifiez qu'il est bien formaté."}), 400
        else:
            return jsonify({"error": "Format non pris en charge. Utilisez .csv ou .xlsx"}), 400

        def clean_cell(value):
            if pd.isna(value):
                return None
            value = str(value).strip()

            if sanitize_characters:
                value = re.sub(r"[^\w@.\sÀ-ÿ-]", "", value)

            if clean_emails and "@" in value:
                if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
                    return None
                return value.lower()

            return " ".join(w.capitalize() for w in value.split())

        df = df.applymap(clean_cell)

        if remove_duplicates:
            df.drop_duplicates(inplace=True)

        cleaned_text = df.to_csv(index=False)
        return jsonify({ "output": cleaned_text })

    except Exception as e:
        logging.error(f"Erreur Excel : {str(e)}")
        return jsonify({"error": "Erreur de traitement Excel."}), 500

# ========================
# 🗜️ ROUTE : Compression de PDF
# ========================

@app.route("/pdf-compress", methods=["POST"])
def pdf_compress():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier PDF fourni."}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Le fichier doit être un PDF."}), 400

    original_filename = secure_filename(file.filename)
    import os
    import uuid
    import platform

    basename = os.path.splitext(original_filename)[0]

    mode = request.form.get("mode", "lossless")

    try:
        os.makedirs("static", exist_ok=True)

        # Sauvegarde temporaire du PDF original
        temp_input = f"static/{basename}_input_{uuid.uuid4().hex}.pdf"
        file.save(temp_input)
        original_size = os.path.getsize(temp_input)

        # Chemin de sortie du fichier compressé
        filename = f"{basename}_compressed_{uuid.uuid4().hex}.pdf"
        output_path = os.path.join("static", filename)

        # Mapping Ghostscript settings
        gs_quality_map = {
            "lossless": "/prepress",
            "moderate": "/ebook",
            "extreme": "/screen"
        }

        gs_quality = gs_quality_map.get(mode, "/ebook")

        gs_binary = "/opt/homebrew/bin/gs" if platform.system() == "Darwin" else "gs"

        # Commande Ghostscript
        command = [
            gs_binary,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={gs_quality}",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            f"-sOutputFile={output_path}",
            temp_input
        ]

        # Parametrage dynamique de la résolution
        resolution = request.form.get("resolution")
        if resolution:
            command.extend(["-r" + resolution])

        subprocess.run(command, check=True)
        compressed_size = os.path.getsize(output_path)
        logging.info(f"Taille originale : {original_size} octets")
        logging.info(f"Taille compressée : {compressed_size} octets")

        # Nettoyage de l’original temporaire
        os.remove(temp_input)

        gain_percent = round(100 * (1 - compressed_size / original_size), 2)
        alert_message = None

        if compressed_size >= original_size:
            if mode == "lossless":
                alert_message = "Compression inefficace. Essayez le mode 'moderate' pour de meilleurs résultats."
            else:
                alert_message = "Compression inefficace : le fichier est plus lourd après compression."

        # Création d'un fichier de log des compressions
        with open("compressions.log", "a") as logf:
            logf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | IP: {request.remote_addr} | Mode: {mode} | Gain: {gain_percent}% | {original_size} -> {compressed_size} octets\n")

        return jsonify({
            "url": f"http://localhost:8000/static/{filename}",
            "originalSize": original_size,
            "compressedSize": compressed_size,
            "alert": alert_message,
            "gainPercent": gain_percent,
        })

    except subprocess.CalledProcessError as e:
        logging.error(f"Erreur Ghostscript : {e}")
        return jsonify({"error": "Erreur lors de la compression Ghostscript."}), 500
    except Exception as e:
        logging.error(f"Erreur compression PDF : {e}")
        return jsonify({"error": "Erreur lors de la compression du PDF."}), 500

# ========================
# 🚀 LANCEMENT DU SERVEUR
# ========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)