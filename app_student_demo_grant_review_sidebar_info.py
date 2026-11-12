"""Grant-reviewer demonstration. Fictional documents only; no user authentication."""
import sqlite3
import re
import hashlib
import json
import secrets
import os
import tempfile
import shutil
import time
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
# A high-entropy bearer token in the URL reconnects a reviewer to their own
# disposable database after refresh. This is NOT authentication or secure storage.
# Never upload real records, and never share a URL containing demo_session.
DEMO_ROOT = Path(tempfile.gettempdir()) / "atlas_grant_review_sessions"
DEMO_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
DEMO_LIFETIME_SECONDS = 24 * 60 * 60


def _demo_token():
    token = st.query_params.get("demo_session", "")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        token = secrets.token_hex(32)
        st.query_params["demo_session"] = token
    return token


def _demo_database_path():
    token = _demo_token()
    # Clean expired demo data opportunistically. Do not delete active sessions.
    now = time.time()
    for folder in DEMO_ROOT.iterdir():
        if (folder.is_dir() and re.fullmatch(r"[0-9a-f]{64}", folder.name)
                and now - folder.stat().st_mtime > DEMO_LIFETIME_SECONDS):
            shutil.rmtree(folder, ignore_errors=True)
    directory = DEMO_ROOT / token
    directory.mkdir(mode=0o700, exist_ok=True)
    os.utime(directory, None)
    return directory / "demo_records.db"


DB_PATH = _demo_database_path()
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"pdf", "png", "jpg", "jpeg"}


def initialize_database():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_type TEXT NOT NULL,
                school TEXT NOT NULL,
                year TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_data BLOB NOT NULL
            )
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        if "grade_level" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN grade_level TEXT")
        if "country" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN country TEXT")
        if "updated_at" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN updated_at TEXT")
            # Historical records have no known upload time. Do not invent one.


def initialize_profile_database():
    """Separate self-reported school history from uploaded documents."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS education_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school TEXT NOT NULL,
            country TEXT NOT NULL,
            start_year INTEGER,
            end_year INTEGER,
            grade_level TEXT NOT NULL,
            curriculum TEXT NOT NULL DEFAULT '',
            instruction_language TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT ''
        )""")
        history_cols = {row[1] for row in connection.execute("PRAGMA table_info(education_history)")}
        if "document_type" not in history_cols:
            connection.execute("ALTER TABLE education_history ADD COLUMN document_type TEXT NOT NULL DEFAULT 'Other'")
        if "linked_record_id" not in history_cols:
            connection.execute("ALTER TABLE education_history ADD COLUMN linked_record_id INTEGER")
        connection.execute("""CREATE TABLE IF NOT EXISTS transition_details (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            learner_languages TEXT NOT NULL DEFAULT '',
            receiving_country TEXT NOT NULL DEFAULT '',
            receiving_school TEXT NOT NULL DEFAULT '',
            receiving_curriculum TEXT NOT NULL DEFAULT '',
            teacher_notes TEXT NOT NULL DEFAULT '',
            expected_grade TEXT NOT NULL DEFAULT 'Unknown'
        )""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(transition_details)")}
        if "expected_grade" not in columns:
            connection.execute("ALTER TABLE transition_details ADD COLUMN expected_grade TEXT NOT NULL DEFAULT 'Unknown'")
        connection.execute("""CREATE TABLE IF NOT EXISTS subject_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            mark TEXT NOT NULL,
            maximum_mark REAL,
            minimum_mark REAL,
            passing_mark REAL,
            grading_notes TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL,
            school TEXT NOT NULL DEFAULT '',
            academic_year TEXT NOT NULL DEFAULT '',
            qualification TEXT NOT NULL DEFAULT 'Not established'
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS extracted_grades (
            record_id INTEGER PRIMARY KEY,
            document_hash TEXT NOT NULL,
            confirmed_json TEXT NOT NULL,
            confirmed_at TEXT NOT NULL
        )""")
        mark_columns = {row[1] for row in connection.execute("PRAGMA table_info(subject_marks)")}
        if "qualification" not in mark_columns:
            connection.execute("ALTER TABLE subject_marks ADD COLUMN qualification TEXT NOT NULL DEFAULT 'Not established'")



def grade_document_candidates():
    """Most recent eligible upload; no automatic reading of images or scanned PDFs."""
    records = load_records_from_database()
    eligible = [r for r in records if str(r["type"]).strip().lower() in
                {"report card", "transcript"}]
    return sorted(eligible, key=lambda r: (r.get("updated_at") or "", r["id"]), reverse=True)


def extract_grades_from_pdf(record):
    if not record["filename"].lower().endswith(".pdf"):
        return [], "Image files require a document-vision extraction step."
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(record["file_data"]))
        if len(reader.pages) > 15:
            return [], "This prototype supports up to 15 pages for text extraction."
        text = "\\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return [], "This PDF could not be read."
    if not text.strip():
        return [], "This PDF has no extractable text (it may be scanned)."
    # Multilingual subject aliases. Text extraction only; not OCR or translation of arbitrary prose.
    subject_aliases = {
        "Mathematics": ["math", "mathematics", "maths", "algebra", "geometry",
            "mathématiques", "mathématique", "matemáticas", "matemática",
            "رياضيات", "الرياضيات", "数学", "數學"],
        "Language / Reading / Literature": ["english", "english language", "reading",
            "literature", "language arts", "literacy", "french", "arabic", "spanish",
            "français", "lecture", "littérature", "langue", "español",
            "lengua", "lectura", "literatura", "inglés", "اللغة العربية",
            "عربي", "العربية", "قراءة", "القراءة", "أدب", "الإنجليزية",
            "语文", "語文", "英语", "英語", "阅读", "閱讀", "文学", "文學"],
        "Science": ["science", "biology", "chemistry", "physics", "sciences",
            "biologie", "chimie", "physique", "ciencias", "biología",
            "química", "física", "علوم", "العلوم", "فيزياء", "كيمياء",
            "أحياء", "科学", "科學", "物理", "化学", "化學", "生物"],
        "Social Studies": ["social studies", "history", "geography", "civics",
            "social science", "histoire", "géographie", "éducation civique",
            "historia", "geografía", "ciencias sociales", "estudios sociales",
            "تاريخ", "التاريخ", "جغرافيا", "الجغرافيا", "دراسات اجتماعية",
            "التربية الوطنية", "历史", "歷史", "地理", "社会", "社會"]
    }
    def classify_subject(name):
        norm = re.sub(r"\\s+", " ", name.casefold()).strip(" :-—")
        for category, aliases in subject_aliases.items():
            if norm in aliases:
                return category
        return None

    candidates = []
    for line in text.splitlines():
        line = line.strip()
        # Match subject : mark, subject mark/max, or subject <whitespace> mark.
        match = re.fullmatch(
            r"([^\\d:]{2,80}?)\\s*(?:[:：\\-–—]|\\s{2,}|\\t)\\s*"
            r"(\\d{1,3}(?:[.,]\\d{1,2})?)\\s*(?:/\\s*(\\d{1,3}))?\\s*%?\\s*",
            line)
        if not match:
            continue
        subject, mark, maximum = match.groups()
        category = classify_subject(subject)
        if category is None:
            continue
        number = float(mark.replace(",", "."))
        if number > 100 or (maximum and number > float(maximum)):
            continue
        candidates.append({"subject": subject.strip(), "category": category,
                           "mark": mark.replace(",", "."), "maximum": maximum or "",
                           "include": True})
    return candidates[:40], None


def confirmed_extraction(record):
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT document_hash, confirmed_json FROM extracted_grades WHERE record_id=?",
            (record["id"],)).fetchone()
    if not row or row[0] != hashlib.sha256(record["file_data"]).hexdigest():
        return None
    import json
    return json.loads(row[1])


def show_document_grade_review():
    st.caption("Finds the latest uploaded report card or transcript with recognizable grades "
               "in mathematics, language/reading/literature, science, or social studies. "
               "Supports selected subject names in English, French, Spanish, Arabic, and Chinese "
               "in text-based PDFs. Scans and images are not yet supported.")
    records = grade_document_candidates()
    if not records:
        st.info("Upload a Report Card or Transcript in Add a Record first.")
        return
    matching = [(r, *extract_grades_from_pdf(r)) for r in records]
    match = next(((r, grades) for r, grades, error in matching if grades and not error), None)
    if match is None:
        st.warning("No eligible report card or transcript contains recognizable subject grades. "
                   "Scanned documents, photos, tables, and other languages may need more advanced extraction.")
        return
    record, candidates = match
    st.write(f'Latest grade-bearing upload: **{record["filename"]}**')
    st.caption(f'Academic year: {record["year"]} · Origin country: '
               f'{record.get("country") or "Not provided"}')
    saved = confirmed_extraction(record)
    if saved is not None:
        st.success(f'{len(saved)} subject grade(s) confirmed for this file.')
    st.caption("Review every proposed mark against the original document. "
               "Extraction can misread numbers or labels.")
    with st.form(f'confirm_doc_grades_{record["id"]}'):
        chosen = []
        for i, candidate in enumerate(candidates):
            a, b, c = st.columns([1, 3, 2])
            include = a.checkbox("Use", value=True, key=f'use_{record["id"]}_{i}')
            subject = b.text_input("Subject", candidate["subject"],
                                   key=f'subject_{record["id"]}_{i}')
            mark = c.text_input("Mark", candidate["mark"] +
                                ("/" + candidate["maximum"] if candidate["maximum"] else ""),
                                key=f'mark_{record["id"]}_{i}')
            if include:
                chosen.append({"subject": subject.strip()[:100], "category": candidate["category"],
                               "mark": mark.strip()[:40]})
        if st.form_submit_button("Confirm extracted grades"):
            if not chosen or any(not v["subject"] or not v["mark"] for v in chosen):
                st.error("Select at least one complete subject and mark.")
            else:
                import json
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute(
                        """INSERT OR REPLACE INTO extracted_grades
                           (record_id, document_hash, confirmed_json, confirmed_at)
                           VALUES (?, ?, ?, ?)""",
                        (record["id"], hashlib.sha256(record["file_data"]).hexdigest(),
                         json.dumps(chosen), now_utc()))
                st.success("Grades confirmed for teacher review.")
                st.rerun()


def show_latest_document_comparison(destination):
    records = grade_document_candidates()
    if not records:
        st.info("No report card or transcript uploaded.")
        return
    match = next((r for r in records if extract_grades_from_pdf(r)[0]), None)
    if match is None:
        st.info("No recognizable grades in uploaded report cards or transcripts.")
        return
    record = match
    st.write(f'Latest grade-bearing report card/transcript: **{record["filename"]}**')
    grades = confirmed_extraction(record)
    if grades is None:
        st.info("Grades from the latest eligible document have not been confirmed "
                "by the learner. No comparison is available yet.")
        return
    if not grades:
        st.info("No confirmed subject grades in this document.")
        return
    st.table([{"Subject area": g.get("category", "Unclassified"),
               "Original subject": g["subject"], "Mark": g["mark"]} for g in grades])
    st.write(f'Origin country: {record.get("country") or "Not specified"}')
    st.write(f'Receiving country: {destination or "Not selected"}')
    st.caption("Grades are transcribed from the uploaded file and confirmed by the learner, "
               "not independently verified. No national grade equivalency has been calculated.")


def load_subject_marks():
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT * FROM subject_marks ORDER BY id DESC")]


def add_subject_mark(subject, mark, minimum, maximum, passing, notes, country, school, year, qualification):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """INSERT INTO subject_marks
            (subject, mark, minimum_mark, maximum_mark, passing_mark,
             grading_notes, country, school, academic_year, qualification)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject, mark, minimum, maximum, passing, notes, country, school, year, qualification))


def delete_subject_mark(mark_id):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("DELETE FROM subject_marks WHERE id=?", (mark_id,))


# Source-backed reference data; never assume these apply to all schools in a country.
GRADING_REFERENCES = {
    "Syrian Arab Republic": {
        "source": "Nuffic — Syria: Grades and study results",
        "url": "https://www.nuffic.nl/en/education-systems/syria/grades-and-study-results",
        "systems": {
            "Secondary vocational (intermediate institutes)": [
                (85, 100, "Excellent"), (75, 84.999, "Very good"),
                (65, 74.999, "Good"), (50, 64.999, "Pass"), (0, 49.999, "Fail")
            ],
            "Bachelor's degree": [
                (90, 100, "Honours"), (80, 89.999, "Excellent"),
                (70, 79.999, "Very good"), (60, 69.999, "Good"),
                (50, 59.999, "Pass"), (0, 49.999, "Fail")
            ],
            "Master's / PhD": [
                (95, 100, "Honours"), (85, 94.999, "Excellent"),
                (75, 84.999, "Very good"), (65, 74.999, "Good"),
                (60, 64.999, "Pass"), (0, 59.999, "Fail")
            ],
        },
        "note": "These are qualification-specific scales, not a general grading key "
                "for Syrian primary or lower-secondary school marks. Syrian general "
                "upper-secondary certificates use specialisation-dependent point totals."
    }
}


def show_verified_grading_reference(item, destination):
    country = item.get("country", "")
    reference = GRADING_REFERENCES.get(country)
    if reference:
        st.markdown("**Published grading-system reference (qualification-specific)**")
        st.caption(reference["note"])
        qualification = item.get("qualification", "Not established")
        st.write("Qualification / grading system recorded by learner: " + qualification)
        if qualification not in reference["systems"]:
            qualification = "Not established"
        if qualification != "Not established":
            bands = reference["systems"][qualification]
            st.table([{"Mark range": f"{lo:g}–{hi if hi == 100 else int(hi):g}%",
                       "Meaning": meaning} for lo, hi, meaning in bands])
            try:
                number = float(item["mark"])
            except (ValueError, TypeError):
                number = None
            if number is not None and 0 <= number <= 100:
                meaning = next((label for lo, hi, label in bands if lo <= number <= hi), None)
                if meaning:
                    st.success(f"Under the selected {qualification} scale: {meaning}")
            st.caption("This is an interpretation of the learner-selected qualification, "
                       "not independent verification of the uploaded record.")
        st.markdown(f'[Reference: {reference["source"]}]({reference["url"]})')
    else:
        st.caption("No verified original-country grading key installed for this country.")

    if destination == "Germany":
        st.markdown("**German receiving-system context**")
        st.write("German university grade converters often use the modified Bavarian "
                 "formula, with 1.0 as the highest grade and 4.0 as a bare pass.")
        st.caption("This method is used for foreign qualification/admission orientation. "
                   "It is not automatically valid for converting a child's individual "
                   "school subject mark or determining placement.")
        st.markdown("[University of Hamburg — grade converter]"
                    "(https://www.uni-hamburg.de/en/campuscenter/bewerbung/"
                    "international/studium-mit-abschluss/"
                    "anerkennung-auslaendischer-schulbildung/notenumrechner.html)")
    else:
        st.caption("No validated host-country grade conversion has been installed "
                   "for this destination.")
    st.info("Individual host-country equivalent: not established. "
            "A matching qualification, applicable scale, and accepted conversion "
            "policy must be confirmed first.")


def show_subject_comparison(marks, destination):
    st.caption("Marks are learner-entered and have not been verified against original documents.")
    if not marks:
        st.info("No subject marks have been entered yet. Add them in Learner View.")
        return
    for item in marks:
        with st.container(border=True):
            st.markdown("**" + item["subject"] + "**")
            st.write(f'Original mark: {item["mark"]} · Country: {item["country"]} · '
                     f'School: {item["school"] or "Not specified"}')
            minimum, maximum, passing = (item.get("minimum_mark"),
                                          item.get("maximum_mark"),
                                          item.get("passing_mark"))
            try:
                value = float(item["mark"])
            except (TypeError, ValueError):
                value = None
            if value is not None and minimum is not None and maximum is not None and maximum > minimum:
                if minimum <= value <= maximum:
                    proportion = (value - minimum) / (maximum - minimum) * 100
                    st.write(f"Position within the entered numerical scale: {proportion:.1f}%")
                    st.caption("This is a scale position, not a percentile, national average, or grade equivalence.")
                else:
                    st.warning("The mark falls outside the entered grading scale.")
            if passing is not None and value is not None:
                st.write("Entered passing threshold: " + str(passing))
                st.write("Relative to threshold: " + ("At or above" if value >= passing else "Below"))
            if item["grading_notes"]:
                st.write("Learner-provided grading explanation: " + item["grading_notes"])
            st.write("Receiving country: " + (destination or "Not selected"))
            show_verified_grading_reference(item, destination)
    st.caption("Country selections adapt automatically. Reference coverage is currently limited; "
               "no national student performance averages or individual percentiles are calculated.")


UNVERIFIED_HELP = (
    "This school history was entered without an accompanying academic document. "
    "To support verification, upload a transcript, report card, enrollment letter, "
    "or other school-issued evidence under Add a Record, using the same school and country. "
    "Matching an uploaded document does not automatically verify the claim; "
    "verification requires review by an authorized school or organization."
)


def unverified_history_label():
    st.markdown("**Verification status: Unverified**")
    st.caption(UNVERIFIED_HELP)


def load_history():
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT * FROM education_history ORDER BY COALESCE(end_year, start_year, 0) DESC, id DESC")]


def save_history(school, country, start_year, end_year, grade, curriculum, language, notes, document_type="Other", linked_record_id=None):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""INSERT INTO education_history
            (school, country, start_year, end_year, grade_level, curriculum, instruction_language, notes, document_type, linked_record_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (school, country, start_year, end_year, grade, curriculum, language, notes, document_type, linked_record_id))


def update_history(entry_id, school, country, start_year, end_year, grade, curriculum, language, notes, document_type=None):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""UPDATE education_history SET school=?, country=?, start_year=?, end_year=?,
            grade_level=?, curriculum=?, instruction_language=?, notes=?, document_type=COALESCE(?, document_type) WHERE id=?""",
            (school, country, start_year, end_year, grade, curriculum, language, notes, document_type, entry_id))


def delete_history(entry_id):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("DELETE FROM education_history WHERE id=?", (entry_id,))


def load_transition():
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM transition_details WHERE id=1").fetchone()
    return dict(row) if row else dict(learner_languages='', receiving_country='', receiving_school='', receiving_curriculum='', teacher_notes='', expected_grade='Unknown')


def save_transition(languages, country, school, curriculum, notes, expected_grade):
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""INSERT INTO transition_details
            (id, learner_languages, receiving_country, receiving_school, receiving_curriculum, teacher_notes, expected_grade)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET learner_languages=excluded.learner_languages,
            receiving_country=excluded.receiving_country, receiving_school=excluded.receiving_school,
            receiving_curriculum=excluded.receiving_curriculum, teacher_notes=excluded.teacher_notes,
            expected_grade=excluded.expected_grade""",
            (languages, country, school, curriculum, notes, expected_grade))


def related_documents(entry, records):
    """Suggest related documents; do not imply they verify attendance."""
    return [r for r in records if r['school'].strip().casefold() == entry['school'].strip().casefold()
            and (r['country'] or 'Not specified') == entry['country']]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def format_date(timestamp):
    if not timestamp:
        return t("Date unavailable (older record)")
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%m/%d/%Y")
    except ValueError:
        return t("Date unavailable")


def save_record_to_database(document_type, school, year, filename, file_data, grade_level=None, country=None):
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute(
            """INSERT INTO records (document_type, school, year, filename, file_data, updated_at, grade_level, country)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_type, school, year, filename, file_data, now_utc(), grade_level, country),
        )
        return cursor.lastrowid


def find_record_by_filename(filename):
    """Look up a filename without assuming filenames are unique in old test data."""
    with sqlite3.connect(DB_PATH) as connection:
        return connection.execute(
            "SELECT id, filename FROM records WHERE filename = ? COLLATE NOCASE ORDER BY id DESC LIMIT 1",
            (filename,),
        ).fetchone()


def replace_record_in_database(record_id, document_type, school, year, filename, file_data, grade_level=None, country=None):
    """Update one selected record, retaining its database ID."""
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute(
            """UPDATE records SET document_type=?, school=?, year=?, filename=?, file_data=?, updated_at=?, grade_level=?, country=?
               WHERE id=?""",
            (document_type, school, year, filename, file_data, now_utc(), grade_level, country, record_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("The record to replace no longer exists.")
        # Replacing the document invalidates all previously confirmed extraction.
        connection.execute("DELETE FROM extracted_grades WHERE record_id=?", (record_id,))


def safe_filename(filename):
    """Use a simple filename, not a directory path."""
    return Path(filename.replace("\\", "/")).name


def rename_with_original_extension(original_filename, new_stem):
    """Keep the file's original extension and validate the new name."""
    stem = new_stem.strip()
    if not stem or stem in {".", ".."} or len(stem) > 150:
        return None
    if any(char in stem for char in ('/', '\\', ':', '\0')):
        return None
    return stem + Path(original_filename).suffix


def edit_record_metadata(record_id, document_type, school, year, grade_level=None, country=None):
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute(
            "UPDATE records SET document_type=?, school=?, year=?, updated_at=?, grade_level=?, country=? WHERE id=?",
            (document_type, school, year, now_utc(), grade_level, country, record_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Record not found")
        # A metadata change can invalidate the context of a confirmed grade.
        connection.execute("DELETE FROM extracted_grades WHERE record_id=?", (record_id,))


def delete_record(record_id):
    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.execute("DELETE FROM records WHERE id=?", (record_id,))
        if cursor.rowcount == 1:
            # Confirmed extracted grades are derived from this upload.
            connection.execute("DELETE FROM extracted_grades WHERE record_id=?", (record_id,))
        if cursor.rowcount != 1:
            raise ValueError("Record not found")


def load_records_from_database():
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            """SELECT id, document_type, school, year, filename, file_data, updated_at, grade_level, country
               FROM records"""
        ).fetchall()
    return [
        {"id": row[0], "type": row[1], "school": row[2], "year": row[3],
         "filename": row[4], "file_data": row[5], "updated_at": row[6], "grade_level": row[7], "country": row[8]}
        for row in rows
    ]


def validate_document(filename, data):
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_TYPES:
        return "Please upload a PDF, PNG, or JPEG file."
    if not data:
        return "The uploaded file is empty."
    if len(data) > MAX_FILE_BYTES:
        return "The file exceeds the 5 MB prototype limit."
    if extension == "pdf" and not data.startswith(b"%PDF-"):
        return "The file does not appear to be a PDF."
    if extension == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "The file does not appear to be a PNG image."
    if extension in {"jpg", "jpeg"} and not data.startswith(b"\xff\xd8\xff"):
        return "The file does not appear to be a JPEG image."
    return None


# A single dictionary makes it easy for a developer to add or revise translations.
LANGUAGES = {"English": "en", "العربية": "ar", "Français": "fr", "Español": "es", "简体中文": "zh"}
TRANSLATIONS = {
 "en": {},
 "ar": {"Preschool":"مرحلة ما قبل المدرسة", "Kindergarten":"روضة الأطفال", "Add a Record":"إضافة سجل", "My Records":"سجلاتي", "Resume and Letters":"السيرة الذاتية والرسائل", "Resources":"الموارد", "Employment":"التوظيف", "Privacy and Consent":"الخصوصية والموافقة", "Document Type":"نوع المستند", "School or Institution":"المدرسة أو المؤسسة", "Academic Year":"السنة الدراسية", "Grade Level":"الصف الدراسي", "Save Record":"حفظ السجل", "Sort records":"ترتيب السجلات", "Total saved records":"إجمالي السجلات المحفوظة", "Download":"تنزيل", "Edit record details":"تعديل تفاصيل السجل", "Delete record":"حذف السجل", "Save changes":"حفظ التغييرات", "Delete permanently":"حذف نهائي", "Language":"اللغة", "Contact / Help Center":"اتصل بنا / مركز المساعدة", "Navigation":"التنقل", "Add an Educational Record":"إضافة سجل تعليمي", "Recently updated":"الأحدث تحديثًا", "Oldest updated":"الأقدم تحديثًا", "Report Card":"كشف درجات", "Transcript":"السجل الأكاديمي", "Diploma":"شهادة تخرج", "Certificate":"شهادة", "Other":"أخرى", "Unknown":"غير معروف", "Primary school":"المرحلة الابتدائية", "Secondary school":"المرحلة الثانوية", "University":"الجامعة", "Vocational / Technical":"مهني / تقني", "Keep duplicate":"الاحتفاظ بنسخة مكررة", "Replace existing record":"استبدال السجل الحالي", "Rename new file":"إعادة تسمية الملف الجديد", "Cancel upload":"إلغاء الرفع"},
 "fr": {"Preschool":"Préscolaire", "Kindergarten":"Maternelle", "Add a Record":"Ajouter un document", "My Records":"Mes documents", "Resume and Letters":"CV et lettres", "Resources":"Ressources", "Employment":"Emploi", "Privacy and Consent":"Confidentialité et consentement", "Document Type":"Type de document", "School or Institution":"École ou établissement", "Academic Year":"Année scolaire", "Grade Level":"Niveau scolaire", "Save Record":"Enregistrer", "Sort records":"Trier les documents", "Total saved records":"Nombre de documents", "Download":"Télécharger", "Edit record details":"Modifier les informations", "Delete record":"Supprimer le document", "Save changes":"Enregistrer les modifications", "Delete permanently":"Supprimer définitivement", "Language":"Langue", "Contact / Help Center":"Contact / Aide", "Navigation":"Navigation", "Add an Educational Record":"Ajouter un document scolaire", "Recently updated":"Modifiés récemment", "Oldest updated":"Plus anciens", "Report Card":"Bulletin scolaire", "Transcript":"Relevé de notes", "Diploma":"Diplôme", "Certificate":"Certificat", "Other":"Autre", "Unknown":"Inconnu", "Primary school":"École primaire", "Secondary school":"École secondaire", "University":"Université", "Vocational / Technical":"Formation professionnelle", "Keep duplicate":"Conserver le doublon", "Replace existing record":"Remplacer le document", "Rename new file":"Renommer le nouveau fichier", "Cancel upload":"Annuler l'importation"},
 "es": {"Preschool":"Preescolar", "Kindergarten":"Jardín de infancia", "Add a Record":"Agregar documento", "My Records":"Mis documentos", "Resume and Letters":"Currículum y cartas", "Resources":"Recursos", "Employment":"Empleo", "Privacy and Consent":"Privacidad y consentimiento", "Document Type":"Tipo de documento", "School or Institution":"Escuela o institución", "Academic Year":"Año académico", "Grade Level":"Grado escolar", "Save Record":"Guardar documento", "Sort records":"Ordenar documentos", "Total saved records":"Total de documentos", "Download":"Descargar", "Edit record details":"Editar detalles", "Delete record":"Eliminar documento", "Save changes":"Guardar cambios", "Delete permanently":"Eliminar definitivamente", "Language":"Idioma", "Contact / Help Center":"Contacto / Ayuda", "Navigation":"Navegación", "Add an Educational Record":"Agregar documento educativo", "Recently updated":"Actualizados recientemente", "Oldest updated":"Más antiguos", "Report Card":"Boletín de notas", "Transcript":"Certificado de notas", "Diploma":"Diploma", "Certificate":"Certificado", "Other":"Otro", "Unknown":"Desconocido", "Primary school":"Primaria", "Secondary school":"Secundaria", "University":"Universidad", "Vocational / Technical":"Formación profesional", "Keep duplicate":"Conservar duplicado", "Replace existing record":"Reemplazar documento", "Rename new file":"Cambiar nombre del archivo", "Cancel upload":"Cancelar carga"},
 "zh": {"Preschool":"学前班", "Kindergarten":"幼儿园", "Add a Record":"添加记录", "My Records":"我的记录", "Resume and Letters":"简历与信件", "Resources":"资源", "Employment":"就业", "Privacy and Consent":"隐私与同意", "Document Type":"文件类型", "School or Institution":"学校或机构", "Academic Year":"学年", "Grade Level":"年级", "Save Record":"保存记录", "Sort records":"记录排序", "Total saved records":"已保存记录总数", "Download":"下载", "Edit record details":"编辑记录详情", "Delete record":"删除记录", "Save changes":"保存更改", "Delete permanently":"永久删除", "Language":"语言", "Contact / Help Center":"联系／帮助中心", "Navigation":"导航", "Add an Educational Record":"添加教育记录", "Recently updated":"最近更新", "Oldest updated":"最早更新", "Report Card":"成绩报告", "Transcript":"成绩单", "Diploma":"毕业证书", "Certificate":"证书", "Other":"其他", "Unknown":"未知", "Primary school":"小学", "Secondary school":"中学", "University":"大学", "Vocational / Technical":"职业／技术教育", "Keep duplicate":"保留重复文件", "Replace existing record":"替换现有记录", "Rename new file":"重命名新文件", "Cancel upload":"取消上传"}
}


# Descriptive text translations (the original interface only translated short labels).
DESCRIPTION_TRANSLATIONS = {'fr': {'This prototype demonstrates how learners could organize educational records across schools and countries. It is currently a local demonstration using fictional documents only, without user accounts or secure storage.': 'Ce '
                                                                                                                                                                                                                                    'prototype '
                                                                                                                                                                                                                                    'montre '
                                                                                                                                                                                                                                    'comment '
                                                                                                                                                                                                                                    'les '
                                                                                                                                                                                                                                    'apprenants '
                                                                                                                                                                                                                                    'peuvent '
                                                                                                                                                                                                                                    'organiser '
                                                                                                                                                                                                                                    'leurs '
                                                                                                                                                                                                                                    'documents '
                                                                                                                                                                                                                                    'scolaires '
                                                                                                                                                                                                                                    'provenant '
                                                                                                                                                                                                                                    'de '
                                                                                                                                                                                                                                    'différents '
                                                                                                                                                                                                                                    'établissements '
                                                                                                                                                                                                                                    'et '
                                                                                                                                                                                                                                    'pays. '
                                                                                                                                                                                                                                    'Il '
                                                                                                                                                                                                                                    "s'agit "
                                                                                                                                                                                                                                    'actuellement '
                                                                                                                                                                                                                                    "d'une "
                                                                                                                                                                                                                                    'démonstration '
                                                                                                                                                                                                                                    'locale '
                                                                                                                                                                                                                                    'utilisant '
                                                                                                                                                                                                                                    'uniquement '
                                                                                                                                                                                                                                    'des '
                                                                                                                                                                                                                                    'documents '
                                                                                                                                                                                                                                    'fictifs, '
                                                                                                                                                                                                                                    'sans '
                                                                                                                                                                                                                                    'comptes '
                                                                                                                                                                                                                                    'utilisateurs '
                                                                                                                                                                                                                                    'ni '
                                                                                                                                                                                                                                    'stockage '
                                                                                                                                                                                                                                    'sécurisé.',
        'Educational records can help learners document their achievements and continue their education when moving between schools, regions, or countries. This page lets you organize copies of transcripts, report cards, diplomas, and certificates together with details about the institution, country, academic year, and grade level. Select the most accurate information available; if you do not know the grade level, choose Unknown. Hover over the question-mark icon beside each field label to see a definition. This is a local prototype: upload fictional test documents only, never real personal records.': 'Les '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'documents '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'scolaires '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'peuvent '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'aider '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'les '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'apprenants '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'à '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'attester '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'leurs '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'acquis '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'et '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'à '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'poursuivre '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'leurs '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'études '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "lorsqu'ils "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'changent '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "d'établissement, "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'région '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ou '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'pays. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Cette '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'page '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'permet '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'regrouper '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'des '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'copies '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'relevés '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'notes, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'bulletins, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'diplômes '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'et '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'certificats, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ainsi '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'que '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'des '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'renseignements '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sur '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "l'établissement, "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'le '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'pays, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "l'année "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'scolaire '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'et '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'le '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'niveau '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "d'études. "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Choisissez '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'les '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'informations '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'les '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'plus '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'exactes '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'possibles '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '; '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'si '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'vous '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ne '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'connaissez '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'pas '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'le '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'niveau, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sélectionnez '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '« '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Inconnu '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '». '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Survolez '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "l'icône "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '« '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '? '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '» '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'à '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'côté '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'chaque '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'champ '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'pour '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'voir '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sa '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'définition. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Ce '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'prototype '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'local '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'est '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'réservé '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'aux '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'documents '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'fictifs '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 ': '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 "n'importez "
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'jamais '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'véritables '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'documents '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'personnels.',
        'Drag and drop a file into the upload area, or click Browse files. PDF, PNG, JPG or JPEG; maximum 5 MB.': 'Glissez-déposez '
                                                                                                                  'un '
                                                                                                                  'fichier '
                                                                                                                  'dans '
                                                                                                                  'la '
                                                                                                                  'zone '
                                                                                                                  'de '
                                                                                                                  'dépôt '
                                                                                                                  'ou '
                                                                                                                  'cliquez '
                                                                                                                  'sur '
                                                                                                                  '« '
                                                                                                                  'Parcourir '
                                                                                                                  'les '
                                                                                                                  'fichiers '
                                                                                                                  '». '
                                                                                                                  'PDF, '
                                                                                                                  'PNG, '
                                                                                                                  'JPG '
                                                                                                                  'ou '
                                                                                                                  'JPEG '
                                                                                                                  '; '
                                                                                                                  '5 '
                                                                                                                  'Mo '
                                                                                                                  'maximum.',
        'Hover over the ? icon next to each field label for a definition. No dropdown is needed.': 'Survolez '
                                                                                                   "l'icône « ? » "
                                                                                                   'à côté de '
                                                                                                   'chaque champ '
                                                                                                   'pour voir sa '
                                                                                                   'définition.',
        'Total saved records': 'Nombre de documents enregistrés',
        'Country': 'Pays',
        'Not specified': 'Non précisé',
        'File': 'Fichier',
        'Last updated': 'Dernière mise à jour',
        'No saved records to display.': 'Aucun document enregistré à afficher.',
        'Editing changes record details only, not the attached file.': 'La modification concerne uniquement les '
                                                                       'informations du document, pas le fichier '
                                                                       'joint.',
        'Date unavailable (older record)': 'Date indisponible (ancien document)',
        'Date unavailable': 'Date indisponible',
        'Recently Updated': 'Récemment modifié',
        'Just Added': "Ajouté à l'instant",
        'Duplicate filename': 'Nom de fichier en double'},
 'es': {'This prototype demonstrates how learners could organize educational records across schools and countries. It is currently a local demonstration using fictional documents only, without user accounts or secure storage.': 'Este '
                                                                                                                                                                                                                                    'prototipo '
                                                                                                                                                                                                                                    'muestra '
                                                                                                                                                                                                                                    'cómo '
                                                                                                                                                                                                                                    'los '
                                                                                                                                                                                                                                    'estudiantes '
                                                                                                                                                                                                                                    'pueden '
                                                                                                                                                                                                                                    'organizar '
                                                                                                                                                                                                                                    'sus '
                                                                                                                                                                                                                                    'documentos '
                                                                                                                                                                                                                                    'académicos '
                                                                                                                                                                                                                                    'de '
                                                                                                                                                                                                                                    'distintas '
                                                                                                                                                                                                                                    'escuelas '
                                                                                                                                                                                                                                    'y '
                                                                                                                                                                                                                                    'países. '
                                                                                                                                                                                                                                    'Por '
                                                                                                                                                                                                                                    'ahora '
                                                                                                                                                                                                                                    'es '
                                                                                                                                                                                                                                    'una '
                                                                                                                                                                                                                                    'demostración '
                                                                                                                                                                                                                                    'local '
                                                                                                                                                                                                                                    'que '
                                                                                                                                                                                                                                    'utiliza '
                                                                                                                                                                                                                                    'únicamente '
                                                                                                                                                                                                                                    'documentos '
                                                                                                                                                                                                                                    'ficticios, '
                                                                                                                                                                                                                                    'sin '
                                                                                                                                                                                                                                    'cuentas '
                                                                                                                                                                                                                                    'de '
                                                                                                                                                                                                                                    'usuario '
                                                                                                                                                                                                                                    'ni '
                                                                                                                                                                                                                                    'almacenamiento '
                                                                                                                                                                                                                                    'seguro.',
        'Educational records can help learners document their achievements and continue their education when moving between schools, regions, or countries. This page lets you organize copies of transcripts, report cards, diplomas, and certificates together with details about the institution, country, academic year, and grade level. Select the most accurate information available; if you do not know the grade level, choose Unknown. Hover over the question-mark icon beside each field label to see a definition. This is a local prototype: upload fictional test documents only, never real personal records.': 'Los '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'documentos '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'académicos '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ayudan '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'a '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'los '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'estudiantes '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'a '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'acreditar '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sus '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'logros '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'y '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'continuar '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sus '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'estudios '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'al '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'cambiar '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'escuela, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'región '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'o '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'país. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Esta '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'página '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'permite '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'organizar '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'copias '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'expedientes, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'boletines, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'diplomas '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'y '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'certificados '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'junto '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'con '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'datos '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'de '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'la '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'institución, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'el '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'país, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'el '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'año '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'académico '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'y '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'el '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'nivel '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'educativo. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Selecciona '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'la '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'información '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'más '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'precisa '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'disponible; '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'si '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'desconoces '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'el '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'nivel, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'elige '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '« '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Desconocido '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '». '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Pasa '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'el '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'cursor '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sobre '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'el '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'icono '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '« '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '? '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '» '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'junto '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'a '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'cada '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'campo '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'para '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ver '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'su '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'definición. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'Este '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'es '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'un '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'prototipo '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'local: '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'sube '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'solo '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'documentos '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ficticios, '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'nunca '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'documentos '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'personales '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'reales.',
        'Drag and drop a file into the upload area, or click Browse files. PDF, PNG, JPG or JPEG; maximum 5 MB.': 'Arrastra '
                                                                                                                  'un '
                                                                                                                  'archivo '
                                                                                                                  'al '
                                                                                                                  'área '
                                                                                                                  'de '
                                                                                                                  'carga '
                                                                                                                  'o '
                                                                                                                  'haz '
                                                                                                                  'clic '
                                                                                                                  'en '
                                                                                                                  '« '
                                                                                                                  'Buscar '
                                                                                                                  'archivos '
                                                                                                                  '». '
                                                                                                                  'PDF, '
                                                                                                                  'PNG, '
                                                                                                                  'JPG '
                                                                                                                  'o '
                                                                                                                  'JPEG; '
                                                                                                                  'máximo '
                                                                                                                  '5 '
                                                                                                                  'MB.',
        'Hover over the ? icon next to each field label for a definition. No dropdown is needed.': 'Pasa el '
                                                                                                   'cursor sobre '
                                                                                                   'el icono « ? '
                                                                                                   '» junto a '
                                                                                                   'cada campo '
                                                                                                   'para ver su '
                                                                                                   'definición.',
        'Country': 'País',
        'Not specified': 'No especificado',
        'File': 'Archivo',
        'Last updated': 'Última actualización',
        'No saved records to display.': 'No hay documentos guardados.',
        'Editing changes record details only, not the attached file.': 'La edición solo cambia los datos del '
                                                                       'registro, no el archivo adjunto.',
        'Date unavailable (older record)': 'Fecha no disponible (registro anterior)',
        'Date unavailable': 'Fecha no disponible',
        'Recently Updated': 'Actualizado recientemente',
        'Just Added': 'Recién agregado',
        'Duplicate filename': 'Nombre de archivo duplicado'},
 'zh': {'This prototype demonstrates how learners could organize educational records across schools and countries. It is currently a local demonstration using fictional documents only, without user accounts or secure storage.': '此原型展示学习者如何整理来自不同学校和国家的教育记录。目前仅供本地演示，使用虚构文件，不提供用户账户或安全存储。',
        'Educational records can help learners document their achievements and continue their education when moving between schools, regions, or countries. This page lets you organize copies of transcripts, report cards, diplomas, and certificates together with details about the institution, country, academic year, and grade level. Select the most accurate information available; if you do not know the grade level, choose Unknown. Hover over the question-mark icon beside each field label to see a definition. This is a local prototype: upload fictional test documents only, never real personal records.': '教育记录可以帮助学习者证明学习成果，并在跨学校、地区或国家转学时继续学业。此页面可整理成绩单、成绩报告、毕业证书和其他证书的副本，并记录学校或机构、国家、学年和年级等信息。请选择最准确的信息；如不清楚年级，请选择“未知”。将鼠标悬停在字段旁的问号图标上可查看说明。这只是本地原型：请仅上传虚构测试文件，切勿上传真实个人资料。',
        'Drag and drop a file into the upload area, or click Browse files. PDF, PNG, JPG or JPEG; maximum 5 MB.': '将文件拖放至上传区域，或点击“浏览文件”。支持 '
                                                                                                                  'PDF、PNG、JPG、JPEG；最大 '
                                                                                                                  '5 '
                                                                                                                  'MB。',
        'Hover over the ? icon next to each field label for a definition. No dropdown is needed.': '将鼠标悬停在字段旁的“?”图标上可查看说明。',
        'Country': '国家',
        'Not specified': '未指定',
        'File': '文件',
        'Last updated': '最后更新',
        'No saved records to display.': '暂无已保存的记录。',
        'Editing changes record details only, not the attached file.': '编辑仅更改记录信息，不会修改附件。',
        'Date unavailable (older record)': '日期不可用（旧记录）',
        'Date unavailable': '日期不可用',
        'Recently Updated': '最近更新',
        'Just Added': '刚刚添加',
        'Duplicate filename': '文件名重复'},
 'ar': {'This prototype demonstrates how learners could organize educational records across schools and countries. It is currently a local demonstration using fictional documents only, without user accounts or secure storage.': 'يوضح '
                                                                                                                                                                                                                                    'هذا '
                                                                                                                                                                                                                                    'النموذج '
                                                                                                                                                                                                                                    'الأولي '
                                                                                                                                                                                                                                    'كيف '
                                                                                                                                                                                                                                    'يمكن '
                                                                                                                                                                                                                                    'للمتعلمين '
                                                                                                                                                                                                                                    'تنظيم '
                                                                                                                                                                                                                                    'سجلاتهم '
                                                                                                                                                                                                                                    'التعليمية '
                                                                                                                                                                                                                                    'من '
                                                                                                                                                                                                                                    'مدارس '
                                                                                                                                                                                                                                    'وبلدان '
                                                                                                                                                                                                                                    'مختلفة. '
                                                                                                                                                                                                                                    'وهو '
                                                                                                                                                                                                                                    'حاليًا '
                                                                                                                                                                                                                                    'عرض '
                                                                                                                                                                                                                                    'محلي '
                                                                                                                                                                                                                                    'يستخدم '
                                                                                                                                                                                                                                    'مستندات '
                                                                                                                                                                                                                                    'افتراضية '
                                                                                                                                                                                                                                    'فقط، '
                                                                                                                                                                                                                                    'من '
                                                                                                                                                                                                                                    'دون '
                                                                                                                                                                                                                                    'حسابات '
                                                                                                                                                                                                                                    'مستخدمين '
                                                                                                                                                                                                                                    'أو '
                                                                                                                                                                                                                                    'تخزين '
                                                                                                                                                                                                                                    'آمن.',
        'Educational records can help learners document their achievements and continue their education when moving between schools, regions, or countries. This page lets you organize copies of transcripts, report cards, diplomas, and certificates together with details about the institution, country, academic year, and grade level. Select the most accurate information available; if you do not know the grade level, choose Unknown. Hover over the question-mark icon beside each field label to see a definition. This is a local prototype: upload fictional test documents only, never real personal records.': 'تساعد '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'السجلات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'التعليمية '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المتعلمين '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'على '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'توثيق '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'إنجازاتهم '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ومتابعة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'دراستهم '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'عند '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الانتقال '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'بين '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المدارس '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'أو '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المناطق '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'أو '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'البلدان. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'تتيح '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'لك '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'هذه '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الصفحة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'تنظيم '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'نسخ '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'من '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'كشوف '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الدرجات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'والشهادات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'والتقارير '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الدراسية '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'مع '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'معلومات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'عن '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المؤسسة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'والبلد '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'والسنة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الدراسية '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'والمرحلة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'التعليمية. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'اختر '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'أدق '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المعلومات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المتاحة؛ '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'وإذا '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'لم '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'تعرف '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المستوى '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الدراسي، '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'فاختر '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 '«غير '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'معروف». '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'مرّر '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'المؤشر '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'فوق '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'علامة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'الاستفهام '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'بجانب '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'كل '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'حقل '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'لقراءة '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'تعريفه. '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'هذا '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'نموذج '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'أولي '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'محلي: '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ارفع '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'مستندات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'اختبار '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'افتراضية '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'فقط، '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ولا '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'ترفع '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'أي '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'مستندات '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'شخصية '
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 'حقيقية.',
        'Drag and drop a file into the upload area, or click Browse files. PDF, PNG, JPG or JPEG; maximum 5 MB.': 'اسحب '
                                                                                                                  'الملف '
                                                                                                                  'إلى '
                                                                                                                  'منطقة '
                                                                                                                  'الرفع '
                                                                                                                  'أو '
                                                                                                                  'انقر '
                                                                                                                  'على '
                                                                                                                  '«استعراض '
                                                                                                                  'الملفات». '
                                                                                                                  'الصيغ: '
                                                                                                                  'PDF '
                                                                                                                  'وPNG '
                                                                                                                  'وJPG '
                                                                                                                  'وJPEG؛ '
                                                                                                                  'الحد '
                                                                                                                  'الأقصى '
                                                                                                                  '5 '
                                                                                                                  'ميغابايت.',
        'Hover over the ? icon next to each field label for a definition. No dropdown is needed.': 'مرّر المؤشر '
                                                                                                   'فوق علامة «؟» '
                                                                                                   'بجانب كل حقل '
                                                                                                   'لقراءة '
                                                                                                   'تعريفه.',
        'Country': 'البلد',
        'Not specified': 'غير محدد',
        'File': 'الملف',
        'Last updated': 'آخر تحديث',
        'No saved records to display.': 'لا توجد سجلات محفوظة لعرضها.',
        'Editing changes record details only, not the attached file.': 'يغيّر التعديل تفاصيل السجل فقط، وليس '
                                                                       'الملف المرفق.',
        'Date unavailable (older record)': 'التاريخ غير متاح (سجل قديم)',
        'Date unavailable': 'التاريخ غير متاح',
        'Recently Updated': 'تم التحديث مؤخرًا',
        'Just Added': 'أُضيف للتو',
        'Duplicate filename': 'اسم ملف مكرر'}}
for _lang, _items in DESCRIPTION_TRANSLATIONS.items():
    TRANSLATIONS[_lang].update(_items)


PROFILE_TRANSLATIONS = {'fr': {'Education Profile': 'Parcours scolaire',
        'Your education history is organized automatically from the records you have saved. This is a demonstration using fictional records only.': 'Votre '
                                                                                                                                                    'parcours '
                                                                                                                                                    'scolaire '
                                                                                                                                                    'est '
                                                                                                                                                    'organisé '
                                                                                                                                                    'automatiquement '
                                                                                                                                                    'à '
                                                                                                                                                    'partir '
                                                                                                                                                    'des '
                                                                                                                                                    'documents '
                                                                                                                                                    'enregistrés. '
                                                                                                                                                    'Cette '
                                                                                                                                                    'démonstration '
                                                                                                                                                    'utilise '
                                                                                                                                                    'uniquement '
                                                                                                                                                    'des '
                                                                                                                                                    'documents '
                                                                                                                                                    'fictifs.',
        'No educational records yet. Add a record to start your education profile.': 'Aucun document scolaire pour le '
                                                                                     'moment. Ajoutez un document pour '
                                                                                     'commencer votre parcours.',
        'This timeline is based on the academic years entered for your documents. It does not verify enrollment or qualifications.': 'Cette '
                                                                                                                                     'chronologie '
                                                                                                                                     'repose '
                                                                                                                                     'sur '
                                                                                                                                     'les '
                                                                                                                                     'années '
                                                                                                                                     'scolaires '
                                                                                                                                     'indiquées. '
                                                                                                                                     'Elle '
                                                                                                                                     'ne '
                                                                                                                                     'vérifie '
                                                                                                                                     'ni '
                                                                                                                                     'les '
                                                                                                                                     'inscriptions '
                                                                                                                                     'ni '
                                                                                                                                     'les '
                                                                                                                                     'diplômes.',
        'Records could not be loaded.': 'Impossible de charger les documents.'},
 'es': {'Education Profile': 'Perfil educativo',
        'Your education history is organized automatically from the records you have saved. This is a demonstration using fictional records only.': 'Tu '
                                                                                                                                                    'historial '
                                                                                                                                                    'educativo '
                                                                                                                                                    'se '
                                                                                                                                                    'organiza '
                                                                                                                                                    'automáticamente '
                                                                                                                                                    'a '
                                                                                                                                                    'partir '
                                                                                                                                                    'de '
                                                                                                                                                    'los '
                                                                                                                                                    'documentos '
                                                                                                                                                    'guardados. '
                                                                                                                                                    'Esta '
                                                                                                                                                    'demostración '
                                                                                                                                                    'utiliza '
                                                                                                                                                    'solo '
                                                                                                                                                    'documentos '
                                                                                                                                                    'ficticios.',
        'No educational records yet. Add a record to start your education profile.': 'Aún no hay documentos '
                                                                                     'educativos. Agrega uno para '
                                                                                     'comenzar tu perfil.',
        'This timeline is based on the academic years entered for your documents. It does not verify enrollment or qualifications.': 'Esta '
                                                                                                                                     'cronología '
                                                                                                                                     'se '
                                                                                                                                     'basa '
                                                                                                                                     'en '
                                                                                                                                     'los '
                                                                                                                                     'años '
                                                                                                                                     'académicos '
                                                                                                                                     'introducidos. '
                                                                                                                                     'No '
                                                                                                                                     'verifica '
                                                                                                                                     'la '
                                                                                                                                     'matrícula '
                                                                                                                                     'ni '
                                                                                                                                     'las '
                                                                                                                                     'titulaciones.',
        'Records could not be loaded.': 'No se pudieron cargar los documentos.'},
 'zh': {'Education Profile': '教育档案',
        'Your education history is organized automatically from the records you have saved. This is a demonstration using fictional records only.': '系统会根据已保存的记录自动整理教育经历。本演示仅使用虚构记录。',
        'No educational records yet. Add a record to start your education profile.': '暂无教育记录。请添加记录以创建教育档案。',
        'This timeline is based on the academic years entered for your documents. It does not verify enrollment or qualifications.': '此时间线根据填写的学年生成，不验证入学情况或学历资格。',
        'Records could not be loaded.': '无法加载记录。'},
 'ar': {'Education Profile': 'الملف التعليمي',
        'Your education history is organized automatically from the records you have saved. This is a demonstration using fictional records only.': 'يُنظَّم '
                                                                                                                                                    'سجلك '
                                                                                                                                                    'التعليمي '
                                                                                                                                                    'تلقائيًا '
                                                                                                                                                    'من '
                                                                                                                                                    'المستندات '
                                                                                                                                                    'المحفوظة. '
                                                                                                                                                    'يستخدم '
                                                                                                                                                    'هذا '
                                                                                                                                                    'العرض '
                                                                                                                                                    'سجلات '
                                                                                                                                                    'افتراضية '
                                                                                                                                                    'فقط.',
        'No educational records yet. Add a record to start your education profile.': 'لا توجد سجلات تعليمية بعد. أضف '
                                                                                     'سجلًا لبدء ملفك التعليمي.',
        'This timeline is based on the academic years entered for your documents. It does not verify enrollment or qualifications.': 'يعتمد '
                                                                                                                                     'هذا '
                                                                                                                                     'التسلسل '
                                                                                                                                     'الزمني '
                                                                                                                                     'على '
                                                                                                                                     'السنوات '
                                                                                                                                     'الدراسية '
                                                                                                                                     'المُدخلة، '
                                                                                                                                     'ولا '
                                                                                                                                     'يتحقق '
                                                                                                                                     'من '
                                                                                                                                     'التسجيل '
                                                                                                                                     'أو '
                                                                                                                                     'المؤهلات.',
        'Records could not be loaded.': 'تعذر تحميل السجلات.'}}
for _lang, _items in PROFILE_TRANSLATIONS.items():
    TRANSLATIONS[_lang].update(_items)

HOME_TRANSLATIONS = {
    "fr": {"Home": "Accueil", "Welcome to the Education Platform": "Bienvenue sur la plateforme éducative"},
    "es": {"Home": "Inicio", "Welcome to the Education Platform": "Bienvenido a la plataforma educativa"},
    "zh": {"Home": "首页", "Welcome to the Education Platform": "欢迎使用教育平台"},
    "ar": {"Home": "الرئيسية", "Welcome to the Education Platform": "مرحبًا بك في المنصة التعليمية"},
}
for _lang, _items in HOME_TRANSLATIONS.items():
    TRANSLATIONS[_lang].update(_items)


PROFILE_VIEW_TRANSLATIONS = {'fr': {'Learner view': 'Vue apprenant',
        'Teacher transition view': 'Vue enseignant',
        'My educational journey': 'Mon parcours scolaire',
        'Add a school or learning period': 'Ajouter une école ou période d’études',
        'Start year': 'Année de début',
        'End year': 'Année de fin',
        'Curriculum (if known)': 'Programme scolaire (si connu)',
        'Language of instruction (if known)': 'Langue d’enseignement (si connue)',
        'Additional education notes (optional)': 'Notes scolaires supplémentaires (facultatif)',
        'Add to education history': 'Ajouter au parcours scolaire',
        'Schools entered': 'Écoles renseignées',
        'Countries entered': 'Pays renseignés',
        'Uploaded documents': 'Documents importés',
        'Edit school history': 'Modifier le parcours',
        'Remove school entry': 'Supprimer cette école',
        'School transition summary': 'Résumé de transition scolaire',
        'Transition details': 'Détails de la transition',
        'Languages spoken by learner (optional)': 'Langues parlées par l’apprenant (facultatif)',
        'Receiving country': 'Pays d’accueil',
        'Receiving school (optional)': 'École d’accueil (facultatif)',
        'Receiving school': 'École d’accueil',
        'Receiving curriculum (if known)': 'Programme d’accueil (si connu)',
        'Receiving curriculum': 'Programme d’accueil',
        'Notes to share with receiving teacher (optional)': 'Notes pour l’enseignant d’accueil (facultatif)',
        'Save transition details': 'Enregistrer les détails',
        'Last reported school': 'Dernière école déclarée',
        'Last reported grade': 'Dernier niveau déclaré',
        'Previous curriculum': 'Programme précédent',
        'Languages spoken by learner': 'Langues parlées',
        'Educational journey at a glance': 'Aperçu du parcours scolaire',
        'Suggested intake checklist': 'Liste de vérification pour l’accueil',
        'Curriculum': 'Programme scolaire',
        'Language of instruction': 'Langue d’enseignement',
        'Learner-provided notes': 'Notes de l’apprenant',
        'possible documents': 'documents possibles'},
 'es': {'Learner view': 'Vista del estudiante',
        'Teacher transition view': 'Vista del docente',
        'My educational journey': 'Mi trayectoria educativa',
        'Add a school or learning period': 'Agregar escuela o período de estudios',
        'Start year': 'Año de inicio',
        'End year': 'Año de finalización',
        'Curriculum (if known)': 'Plan de estudios (si se conoce)',
        'Language of instruction (if known)': 'Idioma de enseñanza (si se conoce)',
        'Additional education notes (optional)': 'Notas educativas adicionales (opcional)',
        'Add to education history': 'Agregar al historial educativo',
        'Schools entered': 'Escuelas registradas',
        'Countries entered': 'Países registrados',
        'Uploaded documents': 'Documentos cargados',
        'Edit school history': 'Editar historial escolar',
        'Remove school entry': 'Eliminar escuela',
        'School transition summary': 'Resumen de transición escolar',
        'Transition details': 'Detalles de la transición',
        'Languages spoken by learner (optional)': 'Idiomas del estudiante (opcional)',
        'Receiving country': 'País receptor',
        'Receiving school (optional)': 'Escuela receptora (opcional)',
        'Receiving school': 'Escuela receptora',
        'Receiving curriculum (if known)': 'Plan de estudios receptor (si se conoce)',
        'Receiving curriculum': 'Plan de estudios receptor',
        'Notes to share with receiving teacher (optional)': 'Notas para el docente receptor (opcional)',
        'Save transition details': 'Guardar detalles',
        'Last reported school': 'Última escuela declarada',
        'Last reported grade': 'Último grado declarado',
        'Previous curriculum': 'Plan de estudios anterior',
        'Languages spoken by learner': 'Idiomas del estudiante',
        'Educational journey at a glance': 'Trayectoria educativa de un vistazo',
        'Suggested intake checklist': 'Lista de verificación de ingreso',
        'Curriculum': 'Plan de estudios',
        'Language of instruction': 'Idioma de enseñanza',
        'Learner-provided notes': 'Notas del estudiante',
        'possible documents': 'documentos posibles'},
 'zh': {'Learner view': '学习者视图',
        'Teacher transition view': '教师转学视图',
        'My educational journey': '我的学习历程',
        'Add a school or learning period': '添加学校或学习阶段',
        'Start year': '开始年份',
        'End year': '结束年份',
        'Curriculum (if known)': '课程体系（如已知）',
        'Language of instruction (if known)': '授课语言（如已知）',
        'Additional education notes (optional)': '补充学习说明（可选）',
        'Add to education history': '添加至学习经历',
        'Schools entered': '已填写学校',
        'Countries entered': '已填写国家',
        'Uploaded documents': '已上传文件',
        'Edit school history': '编辑学习经历',
        'Remove school entry': '删除学校条目',
        'School transition summary': '转学情况摘要',
        'Transition details': '转学信息',
        'Languages spoken by learner (optional)': '学习者使用的语言（可选）',
        'Receiving country': '接收国家',
        'Receiving school (optional)': '接收学校（可选）',
        'Receiving school': '接收学校',
        'Receiving curriculum (if known)': '接收学校课程（如已知）',
        'Receiving curriculum': '接收学校课程',
        'Notes to share with receiving teacher (optional)': '给接收教师的说明（可选）',
        'Save transition details': '保存转学信息',
        'Last reported school': '最近就读学校（自报）',
        'Last reported grade': '最近年级（自报）',
        'Previous curriculum': '原课程体系',
        'Languages spoken by learner': '学习者使用的语言',
        'Educational journey at a glance': '学习历程概览',
        'Suggested intake checklist': '入学核对清单',
        'Curriculum': '课程体系',
        'Language of instruction': '授课语言',
        'Learner-provided notes': '学习者提供的说明',
        'possible documents': '份可能相关文件'},
 'ar': {'Learner view': 'عرض المتعلم',
        'Teacher transition view': 'عرض المعلم للانتقال',
        'My educational journey': 'رحلتي التعليمية',
        'Add a school or learning period': 'إضافة مدرسة أو فترة دراسية',
        'Start year': 'سنة البداية',
        'End year': 'سنة النهاية',
        'Curriculum (if known)': 'المنهج الدراسي (إن كان معروفًا)',
        'Language of instruction (if known)': 'لغة التدريس (إن كانت معروفة)',
        'Additional education notes (optional)': 'ملاحظات تعليمية إضافية (اختياري)',
        'Add to education history': 'إضافة إلى السجل التعليمي',
        'Schools entered': 'المدارس المدخلة',
        'Countries entered': 'البلدان المدخلة',
        'Uploaded documents': 'المستندات المرفوعة',
        'Edit school history': 'تعديل السجل المدرسي',
        'Remove school entry': 'حذف المدرسة',
        'School transition summary': 'ملخص الانتقال المدرسي',
        'Transition details': 'تفاصيل الانتقال',
        'Languages spoken by learner (optional)': 'لغات المتعلم (اختياري)',
        'Receiving country': 'بلد الاستقبال',
        'Receiving school (optional)': 'المدرسة المستقبلة (اختياري)',
        'Receiving school': 'المدرسة المستقبلة',
        'Receiving curriculum (if known)': 'منهج المدرسة المستقبلة (إن كان معروفًا)',
        'Receiving curriculum': 'منهج المدرسة المستقبلة',
        'Notes to share with receiving teacher (optional)': 'ملاحظات للمعلم المستقبِل (اختياري)',
        'Save transition details': 'حفظ تفاصيل الانتقال',
        'Last reported school': 'آخر مدرسة مُبلّغ عنها',
        'Last reported grade': 'آخر صف مُبلّغ عنه',
        'Previous curriculum': 'المنهج السابق',
        'Languages spoken by learner': 'لغات المتعلم',
        'Educational journey at a glance': 'لمحة عن المسار التعليمي',
        'Suggested intake checklist': 'قائمة التحقق عند الالتحاق',
        'Curriculum': 'المنهج الدراسي',
        'Language of instruction': 'لغة التدريس',
        'Learner-provided notes': 'ملاحظات المتعلم',
        'possible documents': 'مستندات محتملة'}}
for _lang, _items in PROFILE_VIEW_TRANSLATIONS.items():
    TRANSLATIONS[_lang].update(_items)

def t(message):
    return TRANSLATIONS.get(st.session_state.get("language", "en"), {}).get(message, message)

DOCUMENT_TYPES = ["Report Card", "Transcript", "Diploma", "Certificate", "Other"]
GRADE_LEVELS = ["Unknown", "Preschool", "Kindergarten"] + [f"Grade {n}" for n in range(1, 13)] + ["Undergraduate", "Graduate", "PhD", "Other"]

COUNTRIES = ['Not specified', 'Afghanistan', 'Albania', 'Algeria', 'American Samoa', 'Andorra', 'Angola', 'Anguilla', 'Antarctica', 'Antigua and Barbuda', 'Argentina', 'Armenia', 'Aruba', 'Australia', 'Austria', 'Azerbaijan', 'Bahamas', 'Bahrain', 'Bangladesh', 'Barbados', 'Belarus', 'Belgium', 'Belize', 'Benin', 'Bermuda', 'Bhutan', 'Bolivia, Plurinational State of', 'Bonaire, Sint Eustatius and Saba', 'Bosnia and Herzegovina', 'Botswana', 'Bouvet Island', 'Brazil', 'British Indian Ocean Territory', 'Brunei Darussalam', 'Bulgaria', 'Burkina Faso', 'Burundi', 'Cabo Verde', 'Cambodia', 'Cameroon', 'Canada', 'Cayman Islands', 'Central African Republic', 'Chad', 'Chile', 'China', 'Christmas Island', 'Cocos (Keeling) Islands', 'Colombia', 'Comoros', 'Congo', 'Congo, The Democratic Republic of the', 'Cook Islands', 'Costa Rica', 'Croatia', 'Cuba', 'Curaçao', 'Cyprus', 'Czechia', "Côte d'Ivoire", 'Denmark', 'Djibouti', 'Dominica', 'Dominican Republic', 'Ecuador', 'Egypt', 'El Salvador', 'Equatorial Guinea', 'Eritrea', 'Estonia', 'Eswatini', 'Ethiopia', 'Falkland Islands (Malvinas)', 'Faroe Islands', 'Fiji', 'Finland', 'France', 'French Guiana', 'French Polynesia', 'French Southern Territories', 'Gabon', 'Gambia', 'Georgia', 'Germany', 'Ghana', 'Gibraltar', 'Greece', 'Greenland', 'Grenada', 'Guadeloupe', 'Guam', 'Guatemala', 'Guernsey', 'Guinea', 'Guinea-Bissau', 'Guyana', 'Haiti', 'Heard Island and McDonald Islands', 'Holy See (Vatican City State)', 'Honduras', 'Hong Kong', 'Hungary', 'Iceland', 'India', 'Indonesia', 'Iran, Islamic Republic of', 'Iraq', 'Ireland', 'Isle of Man', 'Israel', 'Italy', 'Jamaica', 'Japan', 'Jersey', 'Jordan', 'Kazakhstan', 'Kenya', 'Kiribati', "Korea, Democratic People's Republic of", 'Korea, Republic of', 'Kuwait', 'Kyrgyzstan', "Lao People's Democratic Republic", 'Latvia', 'Lebanon', 'Lesotho', 'Liberia', 'Libya', 'Liechtenstein', 'Lithuania', 'Luxembourg', 'Macao', 'Madagascar', 'Malawi', 'Malaysia', 'Maldives', 'Mali', 'Malta', 'Marshall Islands', 'Martinique', 'Mauritania', 'Mauritius', 'Mayotte', 'Mexico', 'Micronesia, Federated States of', 'Moldova, Republic of', 'Monaco', 'Mongolia', 'Montenegro', 'Montserrat', 'Morocco', 'Mozambique', 'Myanmar', 'Namibia', 'Nauru', 'Nepal', 'Netherlands', 'New Caledonia', 'New Zealand', 'Nicaragua', 'Niger', 'Nigeria', 'Niue', 'Norfolk Island', 'North Macedonia', 'Northern Mariana Islands', 'Norway', 'Oman', 'Pakistan', 'Palau', 'Palestine, State of', 'Panama', 'Papua New Guinea', 'Paraguay', 'Peru', 'Philippines', 'Pitcairn', 'Poland', 'Portugal', 'Puerto Rico', 'Qatar', 'Romania', 'Russian Federation', 'Rwanda', 'Réunion', 'Saint Barthélemy', 'Saint Helena, Ascension and Tristan da Cunha', 'Saint Kitts and Nevis', 'Saint Lucia', 'Saint Martin (French part)', 'Saint Pierre and Miquelon', 'Saint Vincent and the Grenadines', 'Samoa', 'San Marino', 'Sao Tome and Principe', 'Saudi Arabia', 'Senegal', 'Serbia', 'Seychelles', 'Sierra Leone', 'Singapore', 'Sint Maarten (Dutch part)', 'Slovakia', 'Slovenia', 'Solomon Islands', 'Somalia', 'South Africa', 'South Georgia and the South Sandwich Islands', 'South Sudan', 'Spain', 'Sri Lanka', 'Sudan', 'Suriname', 'Svalbard and Jan Mayen', 'Sweden', 'Switzerland', 'Syrian Arab Republic', 'Taiwan, Province of China', 'Tajikistan', 'Tanzania, United Republic of', 'Thailand', 'Timor-Leste', 'Togo', 'Tokelau', 'Tonga', 'Trinidad and Tobago', 'Tunisia', 'Turkmenistan', 'Turks and Caicos Islands', 'Tuvalu', 'Türkiye', 'Uganda', 'Ukraine', 'United Arab Emirates', 'United Kingdom', 'United States', 'United States Minor Outlying Islands', 'Uruguay', 'Uzbekistan', 'Vanuatu', 'Venezuela, Bolivarian Republic of', 'Viet Nam', 'Virgin Islands, British', 'Virgin Islands, U.S.', 'Wallis and Futuna', 'Western Sahara', 'Yemen', 'Zambia', 'Zimbabwe', 'Åland Islands']

def grade_label(grade):
    if grade.startswith("Grade "):
        return grade.replace("Grade ", {"fr":"Classe ", "es":"Grado ", "zh":"年级 ", "ar":"الصف "}.get(st.session_state.get("language", "en"), "Grade "))
    return t(grade)


def combined_timeline(history, records):
    """Merge manual school periods and record-backed years without requiring manual entry."""
    events = []
    used = set()
    for entry in history:
        matches = related_documents(entry, records)
        for rec in matches:
            used.add(rec['id'])
        events.append({'school':entry['school'], 'country':entry['country'],
                       'period':f"{entry['start_year']}–{entry['end_year']}",
                       'sort_year':entry['start_year'] or 0, 'grade':entry['grade_level'],
                       'source':'School history + uploaded record(s)' if matches else 'Learner-entered school history',
                       'records':matches, 'curriculum':entry['curriculum'],
                       'language':entry['instruction_language']})
    for rec in records:
        if rec['id'] in used:
            continue
        import re
        year_match = re.search(r'(?:19|20)\d{2}',rec['year'] or '')
        year_num = int(year_match.group()) if year_match else 0
        key = ((rec['school'] or '').strip().casefold(), (rec['country'] or '').strip().casefold(), rec['year'],rec['grade_level'])
        existing = next((ev for ev in events if ev.get('record_group_key') == key),None)
        if existing:
            existing['records'].append(rec)
        else:
            events.append({'school':rec['school'], 'country':rec['country'] or 'Not specified',
                           'period':rec['year'], 'sort_year':year_num,
                           'grade':rec['grade_level'] or 'Unknown',
                           'source':'Uploaded record metadata', 'records':[rec],
                           'curriculum':'', 'language':'', 'record_group_key':key})
    return sorted(events,key=lambda e:(e['sort_year'],e['school'].casefold()))


def draw_timeline(events, prefix):
    if not events:
        st.info('No education history yet. Add an educational record or a school period to begin.')
        return
    st.markdown('**● Document-backed information**   ·   **○ Learner-entered information**')
    for i,event in enumerate(events):
        doc_based = bool(event['records'])
        left,right=st.columns([1,9])
        with left:
            st.markdown('### '+('●' if doc_based else '○'))
            st.caption(str(event['period']))
        with right:
            with st.container(border=True):
                st.markdown('**'+event['school']+'**')
                st.caption(f"{event['country']} · {grade_label(event['grade'] or 'Unknown')} · {event['source']}")
                if event['curriculum']:
                    st.caption('Curriculum: '+event['curriculum'])
                if event['language']:
                    st.caption('Language of instruction: '+event['language'])
                for rec in event['records']:
                    st.download_button('📄 '+rec['filename'], data=rec['file_data'],
                                       file_name=rec['filename'],key=f"{prefix}_timeline_{i}_{rec['id']}")


# Translations for the academic-history PDF download action.
for _language, _label in {
    "fr": "Télécharger le rapport de parcours scolaire (PDF)",
    "es": "Descargar informe del historial académico (PDF)",
    "zh": "下载学业经历报告（PDF）",
    "ar": "تنزيل تقرير السجل الدراسي (PDF)",
}.items():
    TRANSLATIONS[_language]["Download Academic History Report (PDF)"] = _label


def show_report_download(events, transition, prefix):
    """Show the same report action at the top of each view."""
    grade = transition.get("expected_grade", "Unknown")
    try:
        pdf_bytes = build_transition_pdf(events, transition, grade)
        st.download_button(
            "📥 " + t("Download Academic History Report (PDF)"),
            data=pdf_bytes,
            file_name="academic_history_transition_report.pdf",
            mime="application/pdf", key=f"{prefix}_report_pdf", type="primary",
        )
    except ImportError:
        st.warning("PDF export requires ReportLab: python -m pip install reportlab")
    except Exception:
        st.error("Could not generate the PDF report. Check the app console for details.")


def show_benchmark_readiness(events, transition, expected_grade):
    """Show evidence coverage and missing comparison inputs, not grade equivalence."""
    destination = transition.get("receiving_country") or "Not specified"
    curriculum = (transition.get("receiving_curriculum") or "").strip()
    last = events[-1] if events else None
    # Evidence presence is not evidence of competency. No file contents are read.
    evidence_count = sum(len(event.get("records", [])) for event in events)
    criteria = [
        ("Educational history", bool(events),
         "A school period or record is available" if events else "Add a school period or educational record"),
        ("Destination education system", destination != "Not specified",
         destination if destination != "Not specified" else "Select the receiving country in Learner view"),
        ("Proposed grade", expected_grade not in ("", "Unknown", None),
         expected_grade if expected_grade not in ("", "Unknown", None) else "Enter the proposed grade in Learner view"),
        ("Destination curriculum", bool(curriculum),
         curriculum or "Specify the receiving curriculum or school system"),
        ("Previous curriculum", any(bool(event.get("curriculum")) for event in events),
         "At least one prior curriculum was entered" if any(bool(event.get("curriculum")) for event in events) else "Previous curriculum has not been entered"),
        ("Uploaded academic documents", evidence_count > 0,
         f"{evidence_count} linked document reference(s)" if evidence_count else "No uploaded documents linked to timeline"),
    ]
    st.markdown("**Comparison readiness**")
    cols = st.columns(3)
    cols[0].metric("Information available", f"{sum(ready for _,ready,_ in criteria)}/{len(criteria)}")
    cols[1].metric("Timeline periods", len(events))
    cols[2].metric("Linked document references", evidence_count)
    with st.container(border=True):
        for label, ready, detail in criteria:
            a, b = st.columns([1, 2])
            a.markdown(("✓ " if ready else "○ ") + "**" + label + "**")
            b.caption(detail)
    st.caption("These indicators measure whether background information is present, not whether the learner meets any academic standard. Document contents have not been evaluated or verified.")
    if destination != "Not specified" and expected_grade not in ("", "Unknown", None) and curriculum:
        st.info(f"Comparison target: {curriculum} · {destination} · {expected_grade}. Official subject-specific standards for this combination have not been imported, so no learning-gap or placement result can be calculated.")
    else:
        st.info("Complete the destination country, proposed grade, and curriculum in Learner view to identify the reference standards needed for a future comparison.")
    if last:
        st.caption(f"Latest timeline entry: {last['school']} · {last['period']} · {last['grade']}. This is recorded history, not an academic assessment.")


def show_benchmark_preparation(events, transition, expected_grade):
    """Evidence-based readiness prompts, not fabricated national standards."""
    destination = transition['receiving_country']
    curriculum = transition['receiving_curriculum'].strip()
    latest = events[-1] if events else None
    st.caption("This section identifies information needed for a real comparison. It does not assign grades, calculate equivalence, or assess academic mastery.")
    a, b, c = st.columns(3)
    a.metric("Destination", destination if destination != 'Not specified' else 'Not selected')
    b.metric("Proposed grade", expected_grade)
    c.metric("Curriculum", curriculum or 'Not identified')
    if destination in ('', 'Not specified') or expected_grade == 'Unknown' or not curriculum:
        st.info("To prepare a curriculum-specific review, select the receiving country, proposed grade, and receiving curriculum or school system. These identify which official standards will be needed.")
    else:
        st.info(f"Reference standards for {curriculum} ({destination}, {expected_grade}) have not been loaded. No benchmark comparison is available yet.")
    if latest:
        st.markdown("**Most recent education evidence**")
        st.write(f"{latest['school']} · {latest['country']} · {latest['period']} · {latest['grade']}")
        st.caption(f"{len(latest['records'])} linked document(s); links use school metadata and are not independently verified. Document contents have not been assessed.")
    st.markdown("**Suggested intake assessments to consider**")
    for subject, explanation in [
        ("Language of instruction", "Determine whether the learner can access instruction in the receiving school's teaching language; consider language support without assuming subject knowledge is missing."),
        ("Mathematics", "Review the receiving grade's official prerequisite topics and use an age-appropriate diagnostic assessment to identify what the learner already knows."),
        ("Reading and writing", "Review literacy in the learner's strongest language as well as the receiving language, where suitable assessment tools are available."),
        ("Science and other subjects", "Compare documented prior coursework with the receiving school's actual topic sequence once both curricula are available."),
    ]:
        with st.expander(subject, expanded=False):
            st.write(explanation)
    st.caption("These are general assessment-planning prompts, not individualized findings or official country benchmarks.")


def transition_suggestions(events, transition, expected_grade):
    suggestions=[]
    destination=transition['receiving_country']
    latest=events[-1] if events else None
    if not latest:
        suggestions.append(('Education history needed','Add a school period or upload an educational record to establish the learner’s reported education history.'))
    else:
        suggestions.append(('Previous learning evidence',f"Latest recorded period: {latest['period']} at {latest['school']} ({latest['country']}); reported level: {latest['grade']}. This is not independently verified."))
        if latest['records']:
            suggestions.append(('Documents available',f"{len(latest['records'])} document(s) are linked to the latest timeline entry. Review their contents before drawing academic conclusions; filenames and metadata are not proof of mastery."))
        else:
            suggestions.append(('Evidence to request','No document is linked to the latest school period. Invite the learner to add a report card or transcript if available, without treating missing documentation as missing education.'))
    if destination in ('', 'Not specified'):
        suggestions.append(('Destination required','Choose the receiving country to prepare a destination-specific transition review.'))
    if expected_grade == 'Unknown':
        suggestions.append(('Expected grade required','Select the grade being considered by the receiving school to identify which destination benchmarks should be reviewed.'))
    if destination not in ('','Not specified') and expected_grade != 'Unknown':
        suggestions.append(('Destination benchmark status',f"{destination} · {expected_grade}: no validated destination curriculum benchmarks are installed in this prototype. Grade equivalence and subject readiness cannot yet be calculated. Use the receiving school’s official curriculum and placement procedures."))
        if latest and latest['country'] not in ('','Not specified',destination):
            suggestions.append(('Cross-border transition',f"The latest education entry is from {latest['country']}; the receiving country is {destination}. Review differences in curriculum sequence and language of instruction before planning support; country change alone does not imply a learning gap."))
    if not transition['receiving_curriculum']:
        suggestions.append(('Curriculum details','Enter the receiving curriculum or school system when known; national borders alone may not identify the curriculum used by the school.'))
    if not transition['learner_languages']:
        suggestions.append(('Language support','Ask the learner or caregiver which languages they use for learning and communication; avoid assuming language proficiency from country of origin.'))
    return suggestions

def build_transition_pdf(events, transition, expected_grade):
    """Create a shareable, metadata-only intake summary; uploaded files are excluded."""
    from io import BytesIO
    from xml.sax.saxutils import escape
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    buf = BytesIO()
    fonts = [Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/Library/Fonts/Arial Unicode.ttf')]
    font_path = next((f for f in fonts if f.exists()), None)
    if font_path and 'TransitionUnicode' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('TransitionUnicode', str(font_path)))
    face = 'TransitionUnicode' if font_path else 'Helvetica'
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='ProfileTitle', fontName=face, fontSize=17, leading=23, spaceAfter=10, textColor=colors.HexColor('#17324d')))
    styles.add(ParagraphStyle(name='ProfileSection', fontName=face, fontSize=11, leading=16, spaceBefore=14, spaceAfter=6, textColor=colors.HexColor('#17324d')))
    styles.add(ParagraphStyle(name='ProfileBody', fontName=face, fontSize=9, leading=15, spaceAfter=5))
    styles.add(ParagraphStyle(name='ProfileSmall', fontName=face, fontSize=8, leading=12, textColor=colors.HexColor('#526173'), spaceAfter=7))
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=43, rightMargin=43, topMargin=42, bottomMargin=42)
    story=[]
    def para(text, style='ProfileBody'):
        story.append(Paragraph(escape(str(text)).replace('\n','<br/>'), styles[style]))
    def field(label, value):
        para(f'{label}: {value or "Not specified"}')
    para('Student education transition summary', 'ProfileTitle')
    para('Learner-controlled preview | Fictional test data only | Not a verified transcript, assessment, or placement decision.', 'ProfileSmall')
    para('Receiving school', 'ProfileSection')
    field('School', transition['receiving_school'])
    field('Country', transition['receiving_country'])
    field('Curriculum', transition['receiving_curriculum'])
    field('Grade being considered', expected_grade)
    field('Languages reported', transition['learner_languages'])
    para('Education timeline (earliest to latest)', 'ProfileSection')
    if not events:
        para('No education history entered.')
    for ev in events:
        blocks=[]
        name=escape(str(ev['school']))
        period=escape(str(ev['period']))
        country=escape(str(ev['country']))
        grade=escape(str(ev['grade'] or 'Unknown'))
        blocks.append(Paragraph(f'<b>{period} | {name}</b>', styles['ProfileBody']))
        blocks.append(Paragraph(f'{country} | Grade/stage: {grade} | {escape(ev["source"])}',styles['ProfileSmall']))
        if ev['curriculum']:
            blocks.append(Paragraph('Previous curriculum: '+escape(ev['curriculum']),styles['ProfileSmall']))
        if ev['language']:
            blocks.append(Paragraph('Language of instruction: '+escape(ev['language']),styles['ProfileSmall']))
        if ev['records']:
            blocks.append(Paragraph('Associated uploaded records (not included): '+escape(', '.join(r['filename'] for r in ev['records'])),styles['ProfileSmall']))
        story.append(KeepTogether(blocks))
        story.append(Spacer(1,5))
    if transition['teacher_notes']:
        para('Learner-provided notes', 'ProfileSection')
        para(transition['teacher_notes'])
    para('Transition guidance', 'ProfileSection')
    for heading, detail in transition_suggestions(events, transition, expected_grade):
        story.append(KeepTogether([Paragraph(escape(heading),styles['ProfileBody']),Paragraph(escape(detail),styles['ProfileSmall'])]))
    para('Important limitations', 'ProfileSection')
    para('Timeline entries come from learner-entered information and document metadata. Uploaded files are not reviewed or verified. No validated destination curriculum benchmarks, academic equivalencies, or subject-level assessments are provided. The learner should review the report before sharing.', 'ProfileSmall')
    doc.build(story)
    return buf.getvalue()


st.set_page_config(page_title="Learning Records | Demo", page_icon="📘", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
/* Student-focused, restrained styling. Keep Streamlit's native responsive layout. */
:root { --student-ink:#24354b; --student-blue:#3566a6; --student-line:#e3e9e9; }
[data-testid="stAppViewContainer"] {background:#fcfcfa;}
[data-testid="stHeader"] {background:#fcfcfa;}
.block-container {max-width:1120px !important; padding-top:3.25rem !important; padding-bottom:3rem !important;}
[data-testid="stSidebar"] {background:#f6f8fc; border-right:1px solid #e1e7f0;}
[data-testid="stSidebar"] .stButton > button {min-height:39px; border:0; border-radius:8px; justify-content:flex-start; text-align:left; background:transparent; color:#3e5366; font-weight:500; font-size:.94rem;}
[data-testid="stSidebar"] .stButton > button:hover {background:#e9effa; color:#264d84;}
[data-testid="stSidebar"] .stButton > button[kind="primary"] {background:#e1eafb; color:#234e88; font-weight:650;}
[data-testid="stSidebar"] h1 {font-size:1.45rem !important; color:#24354b; letter-spacing:-.025em;}
h1 {font-size:clamp(1.75rem,2.8vw,2.25rem) !important; font-weight:680 !important; letter-spacing:-.025em; color:#24354b;}
h2 {font-size:1.45rem !important; color:#24354b;}
h3 {font-size:1.12rem !important; color:#24354b;}
[data-testid="stCaptionContainer"] {color:#71818d;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:12px; border-color:#e3e9e9;}
[data-testid="stForm"], [data-testid="stExpander"] {border-radius:10px; border-color:#e3e9e9;}
.stButton > button[kind="primary"] {background:#3566a6; border-color:#3566a6; color:white;}
.stButton > button[kind="primary"]:hover {background:#294f85; border-color:#294f85;}
.stButton > button {border-radius:9px;}
@media(max-width:768px){.block-container{padding-top:2.8rem !important;padding-left:1rem !important;padding-right:1rem !important;}}

/* Restrained student dashboard: no fixed widths, absolute positioning, or overlays. */
.student-hero {background:#e9f0fb; border:1px solid #d9e4f6; border-radius:15px;
  padding:clamp(20px,3vw,36px); margin:4px 0 26px;}
.student-hero .student-eyebrow {font-size:.72rem; font-weight:750; letter-spacing:.11em; color:#456da6; margin-bottom:12px;}
.student-hero h1 {font-size:clamp(1.7rem,2.7vw,2.35rem)!important; color:#203e68!important; margin:0 0 12px!important; line-height:1.2;}
.student-hero p {font-size:1rem; color:#4b6587; line-height:1.6; max-width:620px; margin:0;}
.dash-card-tag {display:inline-block; font-size:.69rem; letter-spacing:.08em; font-weight:750; border-radius:5px; padding:5px 9px; margin:0 0 8px;}
.dash-card-tag.teal {background:#e1f0e9;color:#267464;}
.dash-card-tag.blue {background:#e6eefb;color:#456eaa;}
.dash-card-tag.lavender {background:#f0e8f7;color:#79569a;}
.dash-card-tag.orange {background:#fff0df;color:#a26730;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag) {padding:5px;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.teal) {background:#f2f9f5;border-color:#d7e8de;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.blue) {background:#f3f7fd;border-color:#dbe5f4;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.lavender) {background:#f9f5fc;border-color:#e8dff1;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.orange) {background:#fff9f1;border-color:#f2e4d3;}

/* Section-specific button accents; not a global green wash. */
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.blue) .stButton > button[kind="primary"] {background:#3566a6;border-color:#3566a6;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.teal) .stButton > button[kind="primary"] {background:#247d82;border-color:#247d82;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.lavender) .stButton > button[kind="primary"] {background:#79569a;border-color:#79569a;}
[data-testid="stVerticalBlockBorderWrapper"]:has(.dash-card-tag.orange) .stButton > button[kind="primary"] {background:#a26730;border-color:#a26730;}
.records-summary-title {font-size:.73rem; font-weight:750; letter-spacing:.1em;color:#5b6f91; margin:14px 0 8px;}
[data-testid="stMetric"] {background:#f3f6fc; border:1px solid #e0e7f3; border-radius:12px; padding:14px 17px;}
[data-testid="stMetricValue"] {font-size:1.6rem;}
@media(max-width:700px){.student-hero{padding:20px}.student-hero h1{font-size:1.7rem!important}}
</style>
""", unsafe_allow_html=True)

initialize_database()
initialize_profile_database()
# Keep the language control and demo label in one compact row that can wrap.
left_header, right_header = st.columns([3, 2], gap="small")
with left_header:
    st.caption("PUBLIC DEMO · Fictional records only · Private temporary browser session · Not secure storage")
with right_header:
    current_language = st.session_state.get("language", "en")
    language_names = list(LANGUAGES)
    selected_language = st.selectbox("Language", language_names, index=list(LANGUAGES.values()).index(current_language), key="language_choice")
    st.session_state.language = LANGUAGES[selected_language]

# PUBLIC GRANT DEMO: research actions are simulated in browser session state.
# No Supabase/PostgreSQL connection, credential loading, or research writes.
def research_backend_enabled():
    return False


def research_persist_opt_in():
    raise RuntimeError("External research storage is disabled in the public demo.")


def research_persist_survey(participant_id, wave_id, answers):
    raise RuntimeError("External research storage is disabled in the public demo.")


def research_record_school_period(grade_level, change_type):
    return "session_only" if st.session_state.get("research_demo", {}).get("consent") else "off"


def research_after_history_change(grade_level, change_type):
    """Student history is saved locally even if optional research recording fails."""
    try:
        result = research_record_school_period(grade_level, change_type)
    except Exception:
        st.session_state["history_research_notice"] = (
            "Your school history was saved locally, but the optional research observation "
            "could not be saved. Check the database connection before retrying; "
            "do not re-add the school entry."
        )
    else:
        if result == "saved":
            st.session_state["history_research_notice"] = (
                "School history saved. A minimal, consent-approved research observation was also recorded."
            )
        elif result == "off":
            st.session_state["history_research_notice"] = "School history saved. No research observation was collected."
        else:
            st.session_state["history_research_notice"] = (
                "School history saved locally. Research database collection is not configured."
            )


def research_persist_withdrawal(participant_id):
    raise RuntimeError("External research storage is disabled in the public demo.")


# Demo-only research scheduling. Session state is NOT durable across browser sessions.
from datetime import date
import calendar

def research_demo_state():
    if "research_demo" not in st.session_state:
        st.session_state.research_demo = {"consent": False, "id": None, "events": [], "surveys": [], "consent_log": [], "enrolled_at": None, "dismissed_reminders": []}
    state = st.session_state.research_demo
    state.setdefault("enrolled_at", None)
    state.setdefault("dismissed_reminders", [])
    return state

def research_add_months(start, months):
    year = start.year + (start.month - 1 + months) // 12
    month = (start.month - 1 + months) % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))

def research_due_waves(state):
    if not state.get("consent") or not state.get("enrolled_at"):
        return []
    try:
        start = date.fromisoformat(state["enrolled_at"][:10])
    except (ValueError, TypeError):
        return []
    today = datetime.now(timezone.utc).date()
    if today < start:
        return []
    months = (today.year-start.year)*12 + today.month-start.month
    waves = [("baseline", "Baseline survey", start)]
    if months >= 5:
        waves.append(("month_6", "6-month follow-up", research_add_months(start, 6)))
    if months >= 11:
        waves.append(("month_12", "12-month follow-up", research_add_months(start, 12)))
    for m in range(24, min(months+1, 120)+1, 12):
        waves.append((f"month_{m}", f"{m//12}-year follow-up", research_add_months(start, m)))
    completed = {r.get("wave_id") for r in state.get("surveys", [])}
    return [(key, label, due) for key,label,due in waves if due <= today and key not in completed]

# Simple website-style navigation; one active page stored per browser session.
NAV_PAGES = [
    ("", "Home"), ("", "Add a Record"), ("", "My Records"),
    ("", "Education Profile"), ("", "Resume and Letters"), ("", "Employment"),
    ("", "Resources"), ("", "Research Participation"), ("", "Privacy and Consent"),
    ("", "Help Center"),
]
if st.session_state.get("active_page") not in [name for _, name in NAV_PAGES]:
    st.session_state.active_page = "Home"
with st.sidebar:
    st.markdown("**Learning Records**")
    st.caption("FICTIONAL DEMO · NOT SECURE STORAGE")
    research_state = research_demo_state()
    research_due = research_due_waves(research_state)
    research_alert = bool(research_due)
    for icon, name in NAV_PAGES:
        if name == "Research Participation" and research_alert:
            icon = "⭐"
        active = st.session_state.active_page == name
        if st.button(f"{icon}  {t(name)}" if icon else t(name), key=f"nav_{name}", use_container_width=True,
                     type="primary" if active else "secondary"):
            st.session_state.active_page = name
            st.rerun()
    st.divider()
    # Style only the reset control, without changing the navigation buttons.
    st.markdown("""<style>
    .st-key-reset_grant_demo button {
        background-color: #fff1f0 !important;
        color: #a52a32 !important;
        border: 1px solid #e7b8bc !important;
    }
    .st-key-reset_grant_demo button:hover {
        background-color: #ffe0df !important;
        border-color: #c65a62 !important;
    }
    </style>""", unsafe_allow_html=True)
    reset_col, info_col = st.columns([9, 1], gap="small", vertical_alignment="center")
    with reset_col:
        reset_clicked = st.button("Reset fictional data", key="reset_grant_demo", use_container_width=True)
    with info_col:
        st.markdown('''<span role="img" aria-label="Demo data information" tabindex="0"
            title="About your demo data: Fictional records may survive browser refreshes using the same URL, but expire after 24 hours of inactivity. Never upload real personal documents. This is not secure storage. Anyone with your demo-session URL may access its data. Resetting clears the current demo records."
            style="cursor:help;display:inline-block;font-size:19px;color:#61758c;line-height:1.5;">ⓘ</span>''', unsafe_allow_html=True)
    if reset_clicked:
        shutil.rmtree(DB_PATH.parent, ignore_errors=True)
        st.query_params["demo_session"] = secrets.token_hex(32)
        st.session_state.clear()
        st.rerun()
page = st.session_state.active_page

if page == "Home":
    st.markdown("""<div class="student-hero">
      <div class="student-eyebrow">YOUR LEARNING SPACE</div>
      <h1>Your education, your future.</h1>
      <p>Keep your records together, follow your learning journey, and prepare for what's next.</p>
      </div>""", unsafe_allow_html=True)
    _research = research_demo_state()
    _due = research_due_waves(_research)
    _not_dismissed = [w for w in _due if w[0] not in _research["dismissed_reminders"]]
    if _not_dismissed:
        _wave_id, _wave_label, _wave_date = _not_dismissed[0]
        st.info(f"Your optional {_wave_label.lower()} is ready in Research Participation.")
        _a, _b = st.columns(2)
        if _a.button("View research survey", key="home_research_view"):
            st.session_state.active_page = "Research Participation"
            st.rerun()
        if _b.button("Dismiss reminder for this session", key="home_research_dismiss"):
            _research["dismissed_reminders"].append(_wave_id)
            st.rerun()
    st.markdown("### What would you like to do today?")
    st.caption("Choose where to start. You can return to any section at any time.")
    action_left, action_right = st.columns(2, gap="medium")
    with action_left:
        with st.container(border=True):
            st.markdown('<span class="dash-card-tag blue">RECORDS</span>', unsafe_allow_html=True)
            st.markdown("#### Add a record")
            st.write("Save a transcript, report card, or certificate to your learning history.")
            if st.button("Add a record", key="home_add_card", use_container_width=True, type="primary"):
                st.session_state.active_page = "Add a Record"
                st.rerun()
        with st.container(border=True):
            st.markdown('<span class="dash-card-tag teal">YOUR JOURNEY</span>', unsafe_allow_html=True)
            st.markdown("#### My education journey")
            st.write("See your school history and prepare for your next school.")
            if st.button("View education profile", key="home_profile_card", use_container_width=True):
                st.session_state.active_page = "Education Profile"
                st.rerun()
    with action_right:
        with st.container(border=True):
            st.markdown('<span class="dash-card-tag lavender">DOCUMENTS</span>', unsafe_allow_html=True)
            st.markdown("#### My records")
            st.write("Find, review, and organize the documents you've added.")
            if st.button("Open my records", key="home_records_card", use_container_width=True):
                st.session_state.active_page = "My Records"
                st.rerun()
        with st.container(border=True):
            st.markdown('<span class="dash-card-tag orange">NEXT STEPS</span>', unsafe_allow_html=True)
            st.markdown("#### Create a resume or CV")
            st.write("Turn your educational history into an editable document.")
            if st.button("Create a document", key="home_resume_card", use_container_width=True):
                st.session_state.active_page = "Resume and Letters"
                st.rerun()
    st.caption("DEMONSTRATION ONLY · Use fictional test records. No user accounts or secure storage are available yet.")
elif page == "Add a Record":
    st.title(t("Add a Record"))
    st.write(t('Educational records can help learners document their achievements and continue their education when moving between schools, regions, or countries. This page lets you organize transcripts, report cards, diplomas, certificates, and school history together with details about the institution, country, academic year, and grade level. Select the most accurate information available; if you do not know the grade level, choose Unknown. Hover over the question-mark icon beside each field label to see a definition. This is a local prototype: use fictional test information only, never real personal records.'))
    st.info(t('A supporting document is optional. If attaching one, drag and drop a file or click Browse files. PDF, PNG, JPG or JPEG; maximum 5 MB.'))
    with st.form(f"upload_record_{st.session_state.get('upload_form_version', 0)}"):
        st.subheader(t("Add an Educational Record"))
        st.caption(t('Hover over the ? icon next to each field label for a definition. No dropdown is needed.'))
        document_type = st.selectbox(t("Document Type"), DOCUMENT_TYPES, format_func=t,
            help="Choose the category that best describes the educational record or supporting document.")
        school = st.text_input(t("School or Institution"), placeholder=t("School or Institution"),
            help="Name of the school, university, training provider, or organization associated with the record.")
        country = st.selectbox("Country", COUNTRIES, index=0,
            help="Country where the school or institution was located, not your nationality or current location.")
        year = st.text_input(t("Academic Year"), placeholder="YYYY",
            help="Year associated with this educational record. The upload date is tracked separately.")
        grade_level = st.selectbox(t("Grade Level"), GRADE_LEVELS, format_func=grade_label,
            help="Select the learner's grade or education stage. Choose Unknown if uncertain.")
        st.subheader(t("School or learning period"))
        st.caption("Optional additional details for your Education Profile. These are saved as a school-history entry when provided.")
        period_col1, period_col2 = st.columns(2)
        start_year = period_col1.number_input(t("Start year"), min_value=1900, max_value=2100, value=2020,
            help="The first year of this school or learning period.")
        end_year = period_col2.number_input(t("End year"), min_value=1900, max_value=2100, value=2024,
            help="The final year of this school or learning period.")
        curriculum = st.text_input(t("Curriculum (if known)"), help="Name of the curriculum, if known.")
        language = st.text_input(t("Language of instruction (if known)"), help="Language used for teaching, if known.")
        notes = st.text_area(t("Additional education notes (optional)"), help="Any additional information you want in your private education profile.")
        st.subheader("Supporting document (optional)")
        attach_choice = st.radio("Do you want to attach a document?", ["No document", "Attach a document"], horizontal=True,
            help="Your educational information is saved with either option. A file is never required.")
        uploaded_file = st.file_uploader("Drag and drop a document here, or browse files", type=["pdf", "png", "jpg", "jpeg"],
            help="Accepted formats: PDF, PNG, JPG, JPEG. Maximum 5 MB. Only used when Attach a document is selected.") if attach_choice == "Attach a document" else None
        submitted = st.form_submit_button(t("Save Record"))
    if submitted:
        st.session_state.upload_completed = False
        st.session_state.pop("pending_duplicate", None)
        school, year = school.strip(), year.strip()
        if not school or not year:
            st.warning("Please enter a school or institution and year.")
        elif len(school) > 200 or len(year) > 20 or end_year < start_year:
            st.warning("Please check the school name, year, and school-period dates.")
        elif attach_choice == "Attach a document" and uploaded_file is None:
            st.warning("Please upload a PDF, PNG, JPG, or JPEG document, or choose No document.")
        else:
            history_payload = dict(school=school, country=country, start_year=int(start_year),
                end_year=int(end_year), grade=grade_level, curriculum=curriculum.strip()[:200],
                language=language.strip()[:200], notes=notes.strip()[:1000], document_type=document_type)
            if attach_choice == "No document":
                try:
                    save_history(**history_payload)
                    research_after_history_change(grade_level, "added")
                    st.session_state.upload_completed = True
                    st.rerun()
                except (sqlite3.Error, ValueError):
                    st.error("The school history could not be saved. Please try again.")
            else:
                filename = safe_filename(uploaded_file.name)
                file_data = uploaded_file.getvalue()
                error = validate_document(filename, file_data)
                if error:
                    st.warning(error)
                else:
                    try:
                        existing = find_record_by_filename(filename)
                        if existing is None:
                            new_id = save_record_to_database(document_type, school, year, filename, file_data, grade_level, country)
                            save_history(**history_payload, linked_record_id=new_id)
                            research_after_history_change(grade_level, "added")
                            st.session_state.last_changed_id = new_id
                            st.session_state.last_change_type = "added"
                            st.session_state.upload_completed = True
                            st.rerun()
                        else:
                            st.session_state.pending_duplicate = dict(existing_id=existing[0], filename=filename, document_type=document_type, school=school, year=year, grade_level=grade_level, country=country, file_data=file_data, history_payload=history_payload)
                    except sqlite3.Error:
                        st.error("The record could not be checked or saved. Please try again.")

    pending = st.session_state.get("pending_duplicate")
    if pending:
        st.warning(f'A file named "{pending["filename"]}" already exists. Choose whether to replace it, rename it, or keep a duplicate.')
        # Cancel is a single button, not both a radio option and a button.
        action = st.radio("What would you like to do?", ["Replace existing record", "Rename new file", "Keep duplicate"], format_func=t, key="duplicate_action_v4")
        if action == "Replace existing record":
            st.write("Replacing will overwrite the existing document and its record details. This cannot be undone in the prototype.")
            if st.button("Confirm replacement", type="primary"):
                try:
                    existing = find_record_by_filename(pending["filename"])
                    if existing is None or existing[0] != pending["existing_id"]:
                        st.error("The existing record has changed. Please submit the file again.")
                    else:
                        replace_record_in_database(pending["existing_id"], pending["document_type"], pending["school"], pending["year"], pending["filename"], pending["file_data"], pending["grade_level"], pending["country"])
                        save_history(**pending["history_payload"])
                        research_after_history_change(pending["grade_level"], "added")
                        st.session_state.pop("pending_duplicate", None)
                        st.session_state.last_changed_id = pending["existing_id"]
                        st.session_state.last_change_type = "updated"
                        st.session_state.upload_completed = True
                        st.success("Existing record replaced.")
                        st.rerun()
                except (sqlite3.Error, ValueError):
                    st.error("The record could not be replaced. Please try again.")
        elif action == "Rename new file":
            original_stem = Path(pending["filename"]).stem
            new_stem = st.text_input("New filename (without the extension)", value=original_stem + "_new", key="duplicate_new_name_v4")
            proposed_name = rename_with_original_extension(pending["filename"], new_stem)
            if proposed_name:
                st.caption(f"New filename: {proposed_name}")
            if st.button("Save with new name", type="primary"):
                if proposed_name is None:
                    st.warning("Please enter a valid filename (up to 150 characters, without slashes or colons).")
                else:
                    try:
                        if find_record_by_filename(proposed_name) is not None:
                            st.warning("That filename already exists. Please choose another.")
                        else:
                            new_id = save_record_to_database(pending["document_type"], pending["school"], pending["year"], proposed_name, pending["file_data"], pending["grade_level"], pending["country"])
                            save_history(**pending["history_payload"])
                            research_after_history_change(pending["grade_level"], "added")
                            st.session_state.last_changed_id = new_id
                            st.session_state.last_change_type = "added"
                            st.session_state.pop("pending_duplicate", None)
                            st.session_state.upload_completed = True
                            st.success("New record saved with a different filename.")
                            st.rerun()
                    except sqlite3.Error:
                        st.error("The renamed record could not be saved. Please try again.")
        elif action == "Keep duplicate":
            if st.button("Save duplicate", type="primary"):
                try:
                    new_id = save_record_to_database(pending["document_type"], pending["school"], pending["year"], pending["filename"], pending["file_data"], pending["grade_level"], pending["country"])
                    save_history(**pending["history_payload"])
                    research_after_history_change(pending["grade_level"], "added")
                    st.session_state.last_changed_id = new_id
                    st.session_state.last_change_type = "added"
                    st.session_state.pop("pending_duplicate", None)
                    st.session_state.upload_completed = True
                    st.success("Duplicate saved as a separate record.")
                    st.rerun()
                except sqlite3.Error:
                    st.error("Could not save duplicate.")
        if st.button(t("Cancel upload"), key="cancel_upload_v4"):
            st.session_state.pop("pending_duplicate", None)
            st.info("Upload canceled. No record was changed.")
            st.rerun()

    if st.session_state.get("upload_completed"):
        st.success("Record saved. You can add another record or view your saved documents.")
        top_a, top_b = st.columns(2)
        with top_a:
            if st.button("➕ Add Another Record", use_container_width=True):
                st.session_state.upload_form_version = st.session_state.get("upload_form_version", 0) + 1
                st.session_state.upload_completed = False
                st.session_state.pop("pending_duplicate", None)
                st.rerun()
        with top_b:
            if st.button("📁 View My Records", use_container_width=True):
                st.session_state.active_page = "My Records"
                st.rerun()

    st.divider()
    st.subheader(t("School history"))
    st.caption("All saved school periods, with or without supporting documents, appear in Education Profile.")
    try:
        history = load_history()
        profile_records = load_records_from_database()
    except sqlite3.Error:
        st.error("Could not load school history.")
        st.stop()
    if st.session_state.get("history_research_notice"):
        st.info(st.session_state.pop("history_research_notice"))
    with st.expander(t("Edit manually entered school periods"), expanded=False):
        if not history:
            st.info("No manual school periods yet; the timeline above can still show uploaded records.")
        for idx, entry in enumerate(history):
            with st.container(border=True):
                st.markdown(f"### {entry['school']}")
                st.markdown("**Unverified**")
                st.caption("ℹ️ " + UNVERIFIED_HELP)
                st.caption(f"{entry['start_year'] or '?'}–{entry['end_year'] or '?'}  ·  {entry['country']}")
                st.write(f"**{t('Grade Level')}:** {grade_label(entry['grade_level'])}")
                if entry['curriculum']:
                    st.write(f"**{t('Curriculum')}:** {entry['curriculum']}")
                if entry['instruction_language']:
                    st.write(f"**{t('Language of instruction')}:** {entry['instruction_language']}")
                if entry['notes']:
                    st.caption(entry['notes'])
                matches = related_documents(entry, profile_records)
                st.caption(f"{len(matches)} {t('possible matching document(s) — matched by school and country, not verified')}")
                for record in matches:
                    st.download_button(f"📄 {record['filename']}", data=record['file_data'],
                                       file_name=record['filename'], key=f"hist_download_{entry['id']}_{record['id']}")
                with st.expander(t("Edit school history")):
                    with st.form(f"edit_history_{entry['id']}"):
                        eschool = st.text_input(t("School or Institution"), value=entry['school'])
                        countries = COUNTRIES if entry['country'] in COUNTRIES else COUNTRIES + [entry['country']]
                        ecountry = st.selectbox(t("Country"), countries, index=countries.index(entry['country']))
                        ec1, ec2 = st.columns(2)
                        e_start = ec1.number_input(t("Start year"), 1900, 2100, value=entry['start_year'] or 2020)
                        e_end = ec2.number_input(t("End year"), 1900, 2100, value=entry['end_year'] or 2024)
                        grade_options = GRADE_LEVELS if entry['grade_level'] in GRADE_LEVELS else GRADE_LEVELS + [entry['grade_level']]
                        egrade = st.selectbox(t("Grade Level"), grade_options, index=grade_options.index(entry['grade_level']), format_func=grade_label)
                        ecurriculum = st.text_input(t("Curriculum (if known)"), value=entry['curriculum'])
                        elanguage = st.text_input(t("Language of instruction (if known)"), value=entry['instruction_language'])
                        enotes = st.text_area(t("Additional education notes (optional)"), value=entry['notes'])
                        if st.form_submit_button(t("Save changes")):
                            if not eschool.strip() or len(eschool) > 200 or e_end < e_start:
                                st.warning(t("Enter a valid school name and a date range where the end year is not before the start year."))
                            else:
                                update_history(entry['id'], eschool.strip(), ecountry, int(e_start), int(e_end),
                                               egrade, ecurriculum.strip()[:200], elanguage.strip()[:200], enotes.strip()[:1000], entry["document_type"])
                                research_after_history_change(egrade, "updated")
                                st.rerun()
                    if st.checkbox(t("Confirm removal of this school history entry"), key=f"history_confirm_{entry['id']}"):
                        if st.button(t("Remove school entry"), key=f"history_delete_{entry['id']}"):
                            delete_history(entry['id'])
                            st.rerun()

elif page == "Education Profile":
    st.title(t("Education Profile"))
    st.caption(t("Build a learner-controlled education history and preview a separate teacher intake summary. Fictional data only."))
    try:
        history = load_history()
        profile_records = load_records_from_database()
        transition = load_transition()
    except sqlite3.Error:
        st.error(t("Could not load education profile."))
        st.stop()

    timeline_events = combined_timeline(history, profile_records)
    learner_tab, teacher_tab = st.tabs(["🎓 " + t("Learner view"), "🏫 " + t("Teacher transition view")])
    with learner_tab:
        show_report_download(timeline_events, transition, "learner")
        st.caption("The learner or caregiver manages the transition information below. The teacher view is read-only.")
        with st.expander(t("My school transition details"), expanded=False):
            with st.form("transition_details_form"):
                languages = st.text_input(t("Languages spoken by learner (optional)"), value=transition['learner_languages'])
                rcountry = st.selectbox(t("Receiving country"), COUNTRIES,
                    index=COUNTRIES.index(transition['receiving_country']) if transition['receiving_country'] in COUNTRIES else 0)
                rschool = st.text_input(t("Receiving school (optional)"), value=transition['receiving_school'])
                rcurriculum = st.text_input(t("Receiving curriculum (if known)"), value=transition['receiving_curriculum'])
                notes = st.text_area(t("Notes to share with receiving teacher (optional)"), value=transition['teacher_notes'])
                expected_grade = st.selectbox("Grade being considered by the receiving school", GRADE_LEVELS,
                    index=GRADE_LEVELS.index(transition.get('expected_grade', 'Unknown')) if transition.get('expected_grade', 'Unknown') in GRADE_LEVELS else 0,
                    format_func=grade_label)
                if st.form_submit_button(t("Save transition details")):
                    save_transition(languages.strip()[:200], rcountry, rschool.strip()[:200],
                                    rcurriculum.strip()[:200], notes.strip()[:1000], expected_grade)
                    st.rerun()
        with st.expander(t("My educational journey"), expanded=False):
            st.caption("Automatically assembled from the school, country, grade level, "
                       "and academic year saved with My Records, plus any optional school periods.")
            schools = {e["school"].strip().casefold() for e in timeline_events if e.get("school")}
            countries = {e["country"].strip().casefold() for e in timeline_events
                         if e.get("country") and e["country"] != "Not specified"}
            c1, c2, c3 = st.columns(3)
            c1.metric(t("Schools entered"), len(schools))
            c2.metric(t("Countries entered"), len(countries))
            c3.metric(t("Uploaded documents"), len(profile_records))
            if timeline_events:
                for event in sorted(timeline_events, key=lambda e: e["sort_year"], reverse=True):
                    with st.container(border=True):
                        st.markdown("**" + (event["school"] or "School not specified") + "**")
                        st.write(f'{event["country"]} · {event["period"] or "Year not specified"} '
                                 f'· {grade_label(event["grade"] or "Unknown")}')
                        st.caption(f'{event["source"]} · {len(event["records"])} linked document(s)')
                        for rec in event["records"]:
                            st.caption("Document: " + rec["filename"])
            else:
                st.info("No school information yet. Add a record with a school and country "
                        "to populate this section automatically.")
        with st.expander("Review grades from my latest report card or transcript", expanded=False):
            show_document_grade_review()
        with st.expander(t("Educational timeline"), expanded=False):
            st.caption("Built from uploaded record metadata and optional learner-entered school periods. No document contents are automatically interpreted.")
            draw_timeline(timeline_events, "learner")
        st.caption(t("School history is self-reported. Uploaded files are not independently verified."))

    # Teacher-view layout labels translated separately from stored field values.
    TRANSLATIONS["fr"].update({'Receiving school information': 'Établissement d’accueil', 'Previous education': 'Scolarité précédente', 'Proposed receiving grade': 'Niveau proposé à l’arrivée', 'Educational timeline': 'Parcours scolaire'})
    TRANSLATIONS["es"].update({'Receiving school information': 'Escuela de destino', 'Previous education': 'Educación anterior', 'Proposed receiving grade': 'Grado propuesto de ingreso', 'Educational timeline': 'Trayectoria educativa'})
    TRANSLATIONS["zh"].update({'Receiving school information': '接收学校信息', 'Previous education': '以往教育经历', 'Proposed receiving grade': '拟入学年级', 'Educational timeline': '教育时间线'})
    TRANSLATIONS["ar"].update({'Receiving school information': 'معلومات المدرسة المستقبلة', 'Previous education': 'التعليم السابق', 'Proposed receiving grade': 'الصف المقترح عند الالتحاق', 'Educational timeline': 'التسلسل الزمني للتعليم'})
    for _code, _labels in {
        "fr": {"Learner details": "Informations sur l’élève", "Grade and benchmark review": "Niveau scolaire et repères", "Curriculum comparison preparation": "Préparation de la comparaison des programmes"},
        "es": {"Learner details": "Datos del estudiante", "Grade and benchmark review": "Revisión del grado y referencias", "Curriculum comparison preparation": "Preparación de la comparación curricular"},
        "zh": {"Learner details": "学习者信息", "Grade and benchmark review": "年级与基准审查", "Curriculum comparison preparation": "课程比较准备"},
        "ar": {"Learner details": "معلومات المتعلم", "Grade and benchmark review": "مراجعة الصف والمعايير", "Curriculum comparison preparation": "التحضير لمقارنة المناهج"},
    }.items():
        TRANSLATIONS[_code].update(_labels)
    for _code, _labels in {
        "fr": {"Edit manually entered school periods": "Modifier les périodes scolaires ajoutées", "My educational journey": "Mon parcours scolaire"},
        "es": {"Edit manually entered school periods": "Editar períodos escolares añadidos", "My educational journey": "Mi trayectoria educativa"},
        "zh": {"Edit manually entered school periods": "编辑手动添加的就学经历", "My educational journey": "我的教育历程"},
        "ar": {"Edit manually entered school periods": "تعديل الفترات الدراسية المُضافة", "My educational journey": "رحلتي التعليمية"},
    }.items():
        TRANSLATIONS[_code].update(_labels)
    with teacher_tab:
        show_report_download(timeline_events, transition, "teacher")
        st.subheader(t("School transition summary"))
        st.caption(t("Preview only. The learner controls whether to share this information. This is not a verified school placement or academic assessment."))
        with st.expander(t("Learner details"), expanded=False):
            latest = max(timeline_events, key=lambda e: e["sort_year"]) if timeline_events else None
            # Display-only cards: all values come from the learner's saved information.
            with st.container(border=True):
                st.markdown("#### 🏫 " + t("Receiving school information"))
                left, right = st.columns(2, gap="large")
                with left:
                    st.caption(t("Receiving school"))
                    st.write(transition['receiving_school'] or t('Not specified'))
                    st.caption(t("Receiving country"))
                    st.write(transition['receiving_country'] or t('Not specified'))
                with right:
                    st.caption(t("Receiving curriculum"))
                    st.write(transition['receiving_curriculum'] or t('Not specified'))
                    st.caption(t("Proposed receiving grade"))
                    st.write(grade_label(transition.get('expected_grade') or 'Unknown'))
            with st.container(border=True):
                st.markdown("#### 📚 " + t("Previous education"))
                left, right = st.columns(2, gap="large")
                with left:
                    st.caption(t("Last reported school"))
                    st.write(latest['school'] if latest else t('Not specified'))
                    st.caption(t("Last reported grade"))
                    st.write(grade_label(latest['grade']) if latest else t('Unknown'))
                with right:
                    st.caption(t("Previous curriculum"))
                    st.write((latest['curriculum'] if latest else '') or t('Not specified'))
                    st.caption(t("Languages spoken by learner"))
                    st.write(transition['learner_languages'] or t('Not specified'))
            if transition['teacher_notes']:
                with st.container(border=True):
                    st.markdown("#### 📝 " + t("Learner-provided notes"))
                    st.write(transition['teacher_notes'])
        with st.expander(t("Educational timeline"), expanded=False):
            draw_timeline(timeline_events, "teacher")
        with st.expander(t("Grade and benchmark review"), expanded=False):
            expected_grade = transition.get('expected_grade', 'Unknown')
            st.caption(t("These details come from the learner view and cannot be changed here."))
            grade_left, grade_right = st.columns(2)
            with grade_left:
                st.caption(t("Last reported grade"))
                st.write(grade_label(latest['grade']) if latest else t('Unknown'))
            with grade_right:
                st.caption(t("Proposed receiving grade"))
                st.write(grade_label(expected_grade))
            show_benchmark_readiness(timeline_events, transition, expected_grade)
        with st.expander("Academic performance comparison", expanded=False):
            show_latest_document_comparison(transition.get("receiving_country", ""))
            st.divider()
            st.caption("Subject grades are sourced from confirmed document extraction, "
                       "not manually entered grade records.")
        with st.expander(t("Curriculum comparison preparation"), expanded=False):
            show_benchmark_preparation(timeline_events, transition, expected_grade)
        with st.expander("View automatically generated transition guidance", expanded=False):
            for heading,detail in transition_suggestions(timeline_events,transition,expected_grade):
                st.markdown("**"+heading+"**")
                st.write(detail)
        st.info("Subject-level benchmarks and placement recommendations are not available yet. This prototype does not calculate learning gaps or grade equivalence. A future curriculum comparison must use validated, jurisdiction- and grade-specific reference material.")

elif page == "My Records":
    st.title(t("My Records"))
    try:
        records = load_records_from_database()
        history_records = load_history()
    except sqlite3.Error:
        records, history_records = [], []
        st.error("Records could not be loaded.")
    st.markdown('<div class="records-summary-title">YOUR LIBRARY</div>', unsafe_allow_html=True)
    summary_a, summary_b, summary_c = st.columns(3)
    summary_a.metric("All records", len(records) + len([h for h in history_records if not h.get("linked_record_id")]))
    summary_b.metric("With documents", len(records))
    summary_c.metric("Without documents", len([h for h in history_records if not h.get("linked_record_id")]))
    st.caption("All educational records appear together. An attached document does not independently verify a record.")
    sort_options = ["Recently updated", "Oldest updated", "Document type (A–Z)", "School (A–Z)", "Academic year (newest first)"]
    filter_a, filter_b = st.columns([2, 1])
    with filter_a:
        sort_by = st.selectbox(t("Sort records"), sort_options, format_func=t)
    with filter_b:
        all_types = sorted({r["type"] for r in records} | {h.get("document_type") or "Other" for h in history_records})
        record_type_filter = st.selectbox("Document type", ["All types"] + all_types)
    # Existing uploaded records and their linked school periods are displayed as one card.
    unified = ([{"kind": "document", "value": r, "type": r["type"], "school": r["school"], "year": r["year"], "updated": r["updated_at"] or ""} for r in records]
        + [{"kind": "history", "value": h, "type": h.get("document_type") or "Other", "school": h["school"], "year": str(h.get("end_year") or h.get("start_year") or ""), "updated": ""} for h in history_records if not h.get("linked_record_id")])
    if record_type_filter != "All types":
        unified = [item for item in unified if item["type"] == record_type_filter]
    if sort_by == "Document type (A–Z)":
        unified.sort(key=lambda item: (item["type"].casefold(), item["school"].casefold()))
    elif sort_by == "School (A–Z)":
        unified.sort(key=lambda item: (item["school"].casefold(), item["type"].casefold()))
    elif sort_by == "Academic year (newest first)":
        unified.sort(key=lambda item: int(item["year"][:4]) if item["year"][:4].isdigit() else -1, reverse=True)
    elif sort_by == "Oldest updated":
        unified.sort(key=lambda item: (item["updated"], item["year"], item["value"]["id"]))
    else:
        unified.sort(key=lambda item: (item["updated"], item["year"], item["value"]["id"]), reverse=True)
    if not unified:
        st.info(t("No saved records to display."))
    from collections import Counter
    filename_counts = Counter(r["filename"].casefold() for r in records)
    for item in unified:
        if item["kind"] == "history":
            entry = item["value"]
            hid = entry["id"]
            with st.container(border=True):
                st.subheader(t(entry.get("document_type") or "Other"))
                left, right = st.columns([5, 2])
                left.markdown(f"**{entry['school']}**")
                right.markdown("**Unverified**", help="This record has not been independently verified.")
                st.caption("No document attached" if not entry.get("linked_record_id") else "Document attached")
                st.caption(f"{entry['country']} · "
                           f"{entry['start_year'] or '?'}–{entry['end_year'] or '?'} · "
                           f"{grade_label(entry['grade_level'] or 'Unknown')}")
                if entry.get("curriculum"):
                    st.write("Curriculum: " + entry["curriculum"])
                if entry.get("instruction_language"):
                    st.write("Language of instruction: " + entry["instruction_language"])
                if entry.get("notes"):
                    st.caption(entry["notes"])
                with st.expander(t("Edit record details")):
                    with st.form(f"my_records_edit_history_{hid}"):
                        types = DOCUMENT_TYPES.copy()
                        saved_type = entry.get("document_type") or "Other"
                        if saved_type not in types:
                            types.append(saved_type)
                        etype = st.selectbox(t("Document Type"), types, index=types.index(saved_type), format_func=t, key=f"history_type_{hid}")
                        eschool = st.text_input(t("School or Institution"), value=entry["school"])
                        countries = COUNTRIES if entry["country"] in COUNTRIES else COUNTRIES + [entry["country"]]
                        ecountry = st.selectbox(t("Country"), countries, index=countries.index(entry["country"]))
                        year_a, year_b = st.columns(2)
                        e_start = year_a.number_input(t("Start year"), min_value=1900, max_value=2100, value=entry["start_year"] or 2020)
                        e_end = year_b.number_input(t("End year"), min_value=1900, max_value=2100, value=entry["end_year"] or 2024)
                        saved_grade = entry["grade_level"] or "Unknown"
                        grades = GRADE_LEVELS if saved_grade in GRADE_LEVELS else GRADE_LEVELS + [saved_grade]
                        egrade = st.selectbox(t("Grade Level"), grades, index=grades.index(saved_grade), format_func=grade_label,
                            help="Select the learner's grade or education stage. Choose Unknown if uncertain.")
                        ecurriculum = st.text_input(t("Curriculum (if known)"), value=entry["curriculum"] or "")
                        elanguage = st.text_input(t("Language of instruction (if known)"), value=entry["instruction_language"] or "")
                        enotes = st.text_area(t("Additional education notes (optional)"), value=entry["notes"] or "")
                        if st.form_submit_button(t("Save changes")):
                            if not eschool.strip() or len(eschool.strip()) > 200 or e_end < e_start:
                                st.warning("Please enter a valid school name and date range.")
                            else:
                                try:
                                    update_history(hid, eschool.strip(), ecountry, int(e_start), int(e_end),
                                        egrade, ecurriculum.strip()[:200], elanguage.strip()[:200], enotes.strip()[:1000])
                                    research_after_history_change(egrade, "updated")
                                    st.rerun()
                                except (sqlite3.Error, ValueError):
                                    st.error("Could not update this school-history entry.")
                with st.expander(t("Delete record")):
                    st.warning("Deleting this school-history entry removes it from your Education Profile and future resume drafts. Any separately uploaded document remains saved. This cannot be undone.")
                    confirm = st.checkbox("I understand and want to delete this school-history entry", key=f"my_records_history_confirm_{hid}")
                    if st.button(t("Delete permanently"), key=f"my_records_history_delete_{hid}", disabled=not confirm):
                        try:
                            delete_history(hid)
                            st.rerun()
                        except (sqlite3.Error, ValueError):
                            st.error("Could not delete this school-history entry.")
        else:
            record = item["value"]
            rid = record["id"]
            with st.container(border=True):
                if filename_counts[record["filename"].casefold()] > 1:
                    st.caption("🔁 " + t("Duplicate filename"))
                if st.session_state.get("last_changed_id") == rid:
                    st.success(t("Just Added") if st.session_state.get("last_change_type") == "added" else t("Recently Updated"))
                st.subheader(t(record["type"]))
                st.write(f'**{record["school"]}** | {t("Academic Year")}: {record["year"]} | {t("Grade Level")}: {grade_label(record["grade_level"] or "Unknown")}')
                st.caption(f'{t("Country")}: {t(record["country"] or "Not specified")}')
                st.caption(f'{t("File")}: {record["filename"]} · {t("Last updated")}: {format_date(record["updated_at"])}')
                st.download_button(t("Download"), data=record["file_data"], file_name=record["filename"], key=f"download_{rid}")
                with st.expander(t("Edit record details")):
                    with st.form(f"edit_{rid}"):
                        options = DOCUMENT_TYPES.copy()
                        if record["type"] not in options:
                            options.append(record["type"])
                        new_type = st.selectbox(t("Document Type"), options, index=options.index(record["type"]), key=f"type_{rid}", format_func=t)
                        new_school = st.text_input(t("School or Institution"), value=record["school"], key=f"school_{rid}")
                        existing_country = record["country"] or "Not specified"
                        country_options = COUNTRIES if existing_country in COUNTRIES else COUNTRIES + [existing_country]
                        new_country = st.selectbox("Country", country_options, index=country_options.index(existing_country), key=f"country_{rid}", help="Country where the institution was located.")
                        new_year = st.text_input(t("Academic Year"), value=record["year"], key=f"year_{rid}", placeholder="YYYY")
                        saved_grade = record["grade_level"] or "Unknown"
                        if saved_grade not in GRADE_LEVELS:
                            saved_grade = "Unknown"
                        new_grade = st.selectbox(t("Grade Level"), GRADE_LEVELS, index=GRADE_LEVELS.index(saved_grade), key=f"grade_{rid}", format_func=grade_label)
                        if st.form_submit_button(t("Save changes")):
                            if not new_school.strip() or not new_year.strip():
                                st.warning("School and academic year are required.")
                            elif len(new_school.strip()) > 200 or len(new_year.strip()) > 20:
                                st.warning("School or year entry is too long.")
                            else:
                                try:
                                    edit_record_metadata(rid, new_type, new_school.strip(), new_year.strip(), new_grade, new_country)
                                    st.session_state.last_changed_id = rid
                                    st.session_state.last_change_type = "updated"
                                    st.rerun()
                                except (sqlite3.Error, ValueError):
                                    st.error("Could not update this record.")
                    st.caption(t("Editing changes record details only, not the attached file."))
                with st.expander(t("Delete record")):
                    st.warning("Deleting removes this record and its document from the local database. This cannot be undone.")
                    confirm = st.checkbox("I understand and want to delete this record", key=f"confirm_delete_{rid}")
                    if st.button(t("Delete permanently"), key=f"delete_{rid}", disabled=not confirm):
                        try:
                            delete_record(rid)
                            # Remove stale selections from the CV builder after deletion.
                            st.session_state.pop(f"builder_select_doc_{rid}", None)
                            if st.session_state.get("last_changed_id") == rid:
                                st.session_state.pop("last_changed_id", None)
                            st.rerun()
                        except (sqlite3.Error, ValueError):
                            st.error("Could not delete this record.")

elif page == "Resume and Letters":
    st.title("CV, Resume & Cover Letter Builder")
    st.caption("Local demo. Select records, edit every line, and download your document. "
               "Do not enter real personal information in this prototype.")
    import io
    from xml.sax.saxutils import escape as xml_escape

    ui = {
        "English": {"education": "Education", "experience": "Experience", "skills": "Skills",
                    "summary": "Professional Summary", "name": "[YOUR FULL NAME]",
                    "contact": "[EMAIL] · [PHONE]", "salutation": "[RECIPIENT NAME]",
                    "closing": "Sincerely,\n[YOUR FULL NAME]"},
        "French": {"education": "Formation", "experience": "Expérience", "skills": "Compétences",
                   "summary": "Profil professionnel", "name": "[VOTRE NOM COMPLET]",
                   "contact": "[E-MAIL] · [TÉLÉPHONE]", "salutation": "[NOM DU DESTINATAIRE]",
                   "closing": "Cordialement,\n[VOTRE NOM COMPLET]"},
        "Spanish": {"education": "Educación", "experience": "Experiencia", "skills": "Habilidades",
                    "summary": "Perfil profesional", "name": "[NOMBRE COMPLETO]",
                    "contact": "[CORREO] · [TELÉFONO]", "salutation": "[NOMBRE DEL DESTINATARIO]",
                    "closing": "Atentamente,\n[NOMBRE COMPLETO]"},
        "Arabic": {"education": "التعليم", "experience": "الخبرة", "skills": "المهارات",
                   "summary": "الملخص المهني", "name": "[الاسم الكامل]",
                   "contact": "[البريد الإلكتروني] · [الهاتف]", "salutation": "[اسم المستلم]",
                   "closing": "مع التحية،\n[الاسم الكامل]"},
        "Chinese (Simplified)": {"education": "教育经历", "experience": "工作经历", "skills": "技能",
                                 "summary": "个人简介", "name": "[姓名]",
                                 "contact": "[电子邮箱] · [电话号码]", "salutation": "[收件人姓名]",
                                 "closing": "此致\n[姓名]"},
    }
    st.subheader("1. Choose records")
    try:
        uploaded = load_records_from_database()
        periods = load_history()
    except sqlite3.Error:
        uploaded, periods = [], []
        st.error("Could not load educational records.")
    choices = []
    for record in uploaded:
        choices.append({"key": f'doc_{record["id"]}', "name": record["filename"],
                        "detail": f'{record["school"]} · {record["year"]} · {record["type"]}',
                        "entry": f'{record["school"]} — {record["type"]} ({record["year"]})',
                        "status": "Unverified (uploaded; not independently reviewed)"})
    for record in periods:
        choices.append({"key": f'history_{record["id"]}', "name": record["school"],
                        "detail": f'{record["country"]} · {record["start_year"] or "?"}–{record["end_year"] or "?"} · School history',
                        "entry": f'{record["school"]} — attended {record["start_year"] or "?"}–{record["end_year"] or "?"}',
                        "status": "Unverified (self-reported)"})
    if not choices:
        st.info("No records yet. Add a document or school period under Add a Record.")
    chosen = []
    for record in choices:
        with st.container(border=True):
            a, b = st.columns([1, 8])
            selected = a.checkbox("Include", key="builder_select_" + record["key"],
                                  label_visibility="collapsed")
            b.markdown(f'**{record["name"]}**')
            b.caption(record["detail"] + " · " + record["status"])
            if selected:
                chosen.append(record)
    st.caption(f"{len(chosen)} selected. All records are treated as unverified until an authorized verification workflow exists.")
    st.subheader("2. Customize document")
    a,b,c,d,e = st.columns(5)
    kind = a.selectbox("Document", ["Resume", "CV", "Cover letter"])
    lang = b.selectbox("Language", list(ui))
    layout = c.selectbox("Layout", ["Modern", "Classic", "Academic", "Minimal"])
    font = d.selectbox("Font", ["Arial", "Calibri", "Times New Roman", "Georgia"])
    length = e.selectbox("Target pages", ["1", "2", "3", "No limit"])
    labels = ui[lang]
    st.caption("Language changes headings and placeholders; source records are not machine-translated. "
               "Page count is a target, not an enforced limit.")
    chosen_ids = tuple(x["key"] for x in chosen)
    seed = (kind, lang, chosen_ids)
    if st.session_state.get("builder_seed") != seed:
        st.session_state.builder_seed = seed
        st.session_state.builder_name = labels["name"]
        st.session_state.builder_contact = labels["contact"]
        st.session_state.builder_summary = ("[WRITE YOUR PROFESSIONAL SUMMARY]" if lang == "English"
                                             else "[" + labels["summary"] + "]")
        st.session_state.builder_education = "\n".join(x["entry"] for x in chosen)
        st.session_state.builder_experience = "[ADD RELEVANT EXPERIENCE]"
        st.session_state.builder_skills = "[ADD YOUR SKILLS]"
        st.session_state.builder_recipient = labels["salutation"]
        st.session_state.builder_job = "[JOB TITLE / ORGANIZATION]"
        st.session_state.builder_letter = ("[WRITE WHY YOU ARE APPLYING AND HOW YOUR EXPERIENCE FITS]"
                                            if lang == "English" else "[ " + labels["summary"] + " ]")
    st.subheader("3. Edit your draft")
    st.info("Yellow-highlighted bracketed text needs your input. Nothing entered here is saved to My Records.")
    st.text_input("Name", key="builder_name")
    st.text_input("Contact details", key="builder_contact")
    if kind == "Cover letter":
        st.text_input("Recipient", key="builder_recipient")
        st.text_input("Job / organization", key="builder_job")
        st.text_area("Letter body", key="builder_letter", height=230)
    else:
        st.text_area(labels["summary"], key="builder_summary", height=100)
        st.text_area(labels["education"], key="builder_education", height=150)
        st.text_area(labels["experience"], key="builder_experience", height=130)
        st.text_area(labels["skills"], key="builder_skills", height=100)
    if kind == "Cover letter":
        blocks = [(None, st.session_state.builder_name),
                  (None, st.session_state.builder_contact),
                  (None, st.session_state.builder_recipient),
                  (None, st.session_state.builder_job),
                  (None, st.session_state.builder_letter),
                  (None, labels["closing"])]
    else:
        blocks = [(None, st.session_state.builder_name),
                  (None, st.session_state.builder_contact),
                  (labels["summary"], st.session_state.builder_summary),
                  (labels["education"], st.session_state.builder_education),
                  (labels["experience"], st.session_state.builder_experience),
                  (labels["skills"], st.session_state.builder_skills)]
    st.subheader("4. Preview & download")
    def highlight_placeholders(value):
        import re
        return re.sub(r"(\[[^\]\n]+\])", r'<mark style="background:#fff176;color:#111">\1</mark>',
                      xml_escape(value).replace("\n", "<br>"))
    direction = "rtl" if lang == "Arabic" else "ltr"
    html = [f'<div dir="{direction}" style="font-family:{font};background:white;color:#111;'
            'padding:26px;border:1px solid #ddd;border-radius:8px">']
    for heading, value in blocks:
        if heading:
            html.append(f'<h3 style="margin-bottom:6px">{xml_escape(heading)}</h3>')
        html.append(f'<div style="white-space:normal;margin-bottom:14px">{highlight_placeholders(value)}</div>')
    html.append("</div>")
    st.markdown("\n".join(html), unsafe_allow_html=True)
    has_unverified = bool(chosen)
    acknowledged = st.checkbox(
        "I have reviewed the selected records and will not present unverified education "
        "or credentials as independently verified.",
        disabled=not has_unverified) if has_unverified else True

    def make_word():
        from docx import Document
        from docx.shared import Pt, Inches, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        import re
        doc = Document()
        sec = doc.sections[0]
        sec.top_margin = Inches(.75 if layout != "Academic" else 1)
        sec.bottom_margin = Inches(.75)
        sec.left_margin = sec.right_margin = Inches(.8)
        normal = doc.styles["Normal"]
        normal.font.name = font
        normal.font.size = Pt(10 if layout == "Minimal" else 11)
        for idx,(heading, value) in enumerate(blocks):
            if heading:
                doc.add_heading(heading, level=2)
            par = doc.add_paragraph()
            if idx == 0:
                par.style = doc.styles["Title"]
            for line_no,line in enumerate(value.split("\n")):
                if line_no:
                    par.add_run().add_break()
                for chunk in re.split(r"(\[[^\]\n]+\])", line):
                    if not chunk: continue
                    run = par.add_run(chunk)
                    run.font.name = font
                    if chunk.startswith("[") and chunk.endswith("]"):
                        from docx.enum.text import WD_COLOR_INDEX
                        run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    def make_pdf():
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.colors import HexColor
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.enums import TA_RIGHT, TA_LEFT
        import re
        font_file = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        if lang == "Arabic":
            font_file = "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"
        elif lang == "Chinese (Simplified)":
            font_file = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        registered = "BuilderUnicode"
        try:
            if lang == "Chinese (Simplified)":
                from reportlab.pdfbase.cidfonts import UnicodeCIDFont
                registered = "STSong-Light"
                pdfmetrics.registerFont(UnicodeCIDFont(registered))
            else:
                pdfmetrics.registerFont(TTFont(registered, font_file))
        except Exception:
            registered = "Helvetica"
        alignment = TA_RIGHT if lang == "Arabic" else TA_LEFT
        body = ParagraphStyle("body", fontName=registered, fontSize=10, leading=16,
                              spaceAfter=12, alignment=alignment)
        heading_style = ParagraphStyle("heading", parent=body, fontSize=13, leading=18,
                                       spaceBefore=12, spaceAfter=6)
        title_style = ParagraphStyle("title", parent=body, fontSize=18, leading=24)
        buff = io.BytesIO()
        doc = SimpleDocTemplate(buff, pagesize=(595.28,841.89),
                                leftMargin=50,rightMargin=50,topMargin=50,bottomMargin=50)
        story = []
        for idx,(heading,value) in enumerate(blocks):
            if heading:
                story.append(Paragraph(xml_escape(heading), heading_style))
            safe = xml_escape(value).replace("\n","<br/>")
            safe = re.sub(r"(\[[^\]\n]+\])",
                          r'<font backColor="#fff176">\1</font>', safe)
            story.append(Paragraph(safe, title_style if idx == 0 else body))
        doc.build(story)
        return buff.getvalue()

    if not acknowledged:
        st.warning("Confirm the verification notice before exporting.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            try:
                word_data = make_word()
                st.download_button("Download Word (.docx)", word_data,
                                   file_name=kind.lower().replace(" ", "_") + ".docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   use_container_width=True)
            except ImportError as exc:
                st.error(f"Word export needs a missing library: {exc}. Install with: python -m pip install python-docx")
            except Exception as exc:
                st.error(f"Word export failed ({type(exc).__name__}): {exc}")
        with col2:
            try:
                pdf_data = make_pdf()
                st.download_button("Download PDF", pdf_data,
                                   file_name=kind.lower().replace(" ", "_") + ".pdf",
                                   mime="application/pdf", use_container_width=True)
            except ImportError as exc:
                st.error(f"PDF export needs a missing library: {exc}. Install with: python -m pip install reportlab")
            except Exception as exc:
                st.error(f"PDF export failed ({type(exc).__name__}): {exc}")
    st.caption("Drafts are not saved to the database. PDF rendering of complex Arabic "
               "right-to-left text may differ from Word; review exports before use.")

elif page == "Research Participation":
    st.title("Research Participation")
    st.warning("FICTIONAL-DATA DEMONSTRATION ONLY. Not approved or secured for real research participants.")
    st.write("Research participation is voluntary and separate from your educational records. You can withdraw at any time; surveys are always optional.")
    state = research_demo_state()
    backend_on = research_backend_enabled()
    if backend_on:
        st.caption("Database mode: research actions are sent to the configured PostgreSQL research schema. "
                   "This is still a fictional-data prototype; browser session identity is not persistent authentication.")
    else:
        st.info("Grant-reviewer demonstration: research consent and surveys are simulated in this browser session only. No research data is sent to Supabase or another external database.")
    st.subheader("Research consent")
    with st.expander("What participation means and what data may be used", expanded=True):
        st.markdown("""**Proposed research fields:** broad educational milestones, interruptions, and voluntary survey responses. Survey answers remain in temporary session state and are not sent to a research database. Adding or editing school history does not send observations to a research database. Enrollment status and education level are not inferred from school history.

**Purpose:** understand educational continuity over time. **Choice:** you may decline or withdraw without losing access to educational services. **Withdrawal:** clears simulated research participation and survey responses from this browser session. It cannot recall previously downloaded files. **Future real-world use:** requires reviewed consent, safeguarding and ethics review, and independently secured data storage.""")
    if not state["consent"]:
        st.info("Research participation is OFF. You can continue using the educational platform without joining research.")
        confirm = st.checkbox("I understand that research is optional and consent to simulated research participation with fictional information only.", key="research_demo_confirm")
        if st.button("Opt in to fictional research demo", disabled=not confirm, type="primary"):
            if backend_on:
                try:
                    pid, enrolled_at = research_persist_opt_in()
                except Exception as exc:
                    st.error("External research storage is disabled for this public demonstration.")
                else:
                    state.update(consent=True, id=pid, enrolled_at=enrolled_at)
                    state["consent_log"].append({"action": "opt_in", "at": enrolled_at, "scope": "fictional_demo_v1"})
                    st.rerun()
            else:
                state["consent"] = True
                state["id"] = "demo-" + secrets.token_hex(12)
                state["enrolled_at"] = datetime.now(timezone.utc).isoformat()
                state["consent_log"].append({"action": "opt_in", "at": state["enrolled_at"], "scope": "fictional_demo_v1"})
                st.rerun()
    else:
        st.success("Research participation is ON. Surveys are optional and do not affect educational services.")
        if st.button("Withdraw research consent", type="secondary"):
            withdrawal_ok = True
            if backend_on:
                try:
                    research_persist_withdrawal(state["id"])
                except Exception as exc:
                    withdrawal_ok = False
                    st.error(f"Database withdrawal failed ({type(exc).__name__}). Please retry; withdrawal has NOT been confirmed.")
            if withdrawal_ok:
                st.session_state.research_demo = {"consent": False, "id": None, "events": [], "surveys": [], "consent_log": [], "enrolled_at": None, "dismissed_reminders": []}
                st.session_state.pop("research_demo_confirm", None)
                st.rerun()
        st.subheader("Optional research surveys")
        st.caption("The platform assigns follow-up dates automatically from the consent date. Participants cannot choose or change their follow-up wave. Times use UTC in this prototype.")
        due = research_due_waves(state)
        if due:
            key, label, due_date = due[0]
            st.info(f"⭐ {label} is ready (scheduled for {due_date.isoformat()}). You may complete it now or later.")
        else:
            st.caption("No scheduled survey is currently due.")
        st.markdown("**Take a survey whenever you want**")
        survey_choices = {"Anytime feedback survey": "anytime"}
        if due:
            survey_choices[f"Scheduled: {due[0][1]}"] = due[0][0]
        selected = st.radio("Available surveys", list(survey_choices), key="research_survey_choice")
        wave_id = survey_choices[selected]
        with st.form("research_demo_survey", clear_on_submit=True):
            enrolled = st.selectbox("Current education status", ["Prefer not to answer", "Currently enrolled", "Not currently enrolled", "Completed a program", "Unknown"])
            interruption = st.selectbox("Any interruption in schooling since your last check-in?", ["Prefer not to answer", "Yes", "No", "Unsure"])
            barrier = st.selectbox("Main education barrier", ["Prefer not to answer", "None", "Language", "Missing records", "Cost", "School access", "Other"])
            pathway = st.selectbox("Current pathway", ["Prefer not to answer", "School", "Higher education", "Vocational training", "Employment", "Other"])
            submitted = st.form_submit_button("Submit optional survey")
        if submitted:
            # Never relabel an anytime response as a scheduled follow-up.
            answers = {"education_status": enrolled, "school_interruption": interruption,
                       "main_barrier": barrier, "pathway": pathway}
            saved = True
            if backend_on:
                try:
                    research_persist_survey(state["id"], wave_id, answers)
                except Exception as exc:
                    saved = False
                    st.error(f"Survey was NOT saved to the database ({type(exc).__name__}). Please retry.")
            if saved:
                state["surveys"].append({"submitted_at": datetime.now(timezone.utc).isoformat(), "wave_id": wave_id, **answers})
                # Render feedback immediately beneath the form's Submit button; no rerun/scroll reset.
                st.success("Survey submitted successfully! Your responses have been saved to the research database."
                           if backend_on else "Survey submitted in this fictional demo session.")
        st.caption("No research dataset, participant ID, or collected-data dashboard is displayed here.")

else:
    st.title(t(page))
    if page == "Help Center":
        st.write("This prototype is for fictional documents only. Upload a PDF or image under Add a Record, then use My Records to download, edit, or delete it. Hover over the ? icons for field definitions.")
    else:
        st.info("This area is planned for a future release. The current demonstration focuses on educational records, transitions, documents, and optional research surveys.")
