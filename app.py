from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    send_file
)

import firebase_admin
from firebase_admin import credentials, firestore, auth

import qrcode
import os
import secrets

from datetime import datetime
from functools import wraps
from io import BytesIO
from html import escape

from ai_health_service import (
    generate_ai_health_plan,
    AI_MODEL
)

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak


# ============================================================
# FLASK CONFIGURATION
# ============================================================

app = Flask(__name__)

# Production-safe Flask configuration.
# On Render, FLASK_SECRET_KEY must be supplied as an environment variable.
IS_RENDER = os.environ.get("RENDER", "").lower() == "true"
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")

if IS_RENDER and not FLASK_SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not configured. "
        "Add it in Render > Environment before deploying."
    )

app.secret_key = FLASK_SECRET_KEY or "viqtor-health-development-only-secret"

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = IS_RENDER
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_NAME"] = "viqtor_health_session"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


# ============================================================
# FIREBASE INITIALIZATION
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

firebase_config_path = os.path.join(
    BASE_DIR,
    "firebase_config.json"
)


if not firebase_admin._apps:

    cred = credentials.Certificate(
        firebase_config_path
    )

    firebase_admin.initialize_app(
        cred,
        {
            "projectId": "qr-health-monitoring-system"
        }
    )


db = firestore.client()


# ============================================================
# QR CODE FOLDER
# ============================================================

# QR_FOLDER can point to a Render Persistent Disk, e.g. /var/data/qr_codes.
# Locally it falls back to the project's qr_codes folder.
QR_FOLDER = os.environ.get(
    "QR_FOLDER",
    os.path.join(BASE_DIR, "qr_codes")
)

os.makedirs(
    QR_FOLDER,
    exist_ok=True
)


# ============================================================
# LOGIN REQUIRED
# ============================================================

def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("logged_in"):

            return redirect(
                url_for("login")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


# ============================================================
# ROLE REQUIRED
# ============================================================

def role_required(*allowed_roles):

    def decorator(function):

        @wraps(function)
        def wrapper(*args, **kwargs):

            if not session.get("logged_in"):

                return redirect(
                    url_for("login")
                )

            current_role = session.get(
                "role"
            )

            if current_role not in allowed_roles:

                return (
                    render_template(
                        "access_denied.html",
                        role=current_role
                    ),
                    403
                )

            return function(
                *args,
                **kwargs
            )

        return wrapper

    return decorator


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


# ============================================================
# FIREBASE LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if session.get("logged_in"):

        return redirect(
            url_for("admin")
        )


    if request.method == "GET":

        return render_template(
            "login.html"
        )


    try:

        data = request.get_json(
            silent=True
        )

        if not data:

            return {
                "success": False,
                "error": "Invalid login request."
            }, 400


        id_token = data.get(
            "idToken"
        )


        if not id_token:

            return {
                "success": False,
                "error": "Firebase authentication token is missing."
            }, 400


        # ====================================================
        # VERIFY FIREBASE ID TOKEN
        # ====================================================

        decoded_token = auth.verify_id_token(
            id_token
        )


        firebase_uid = decoded_token.get(
            "uid"
        )

        email = decoded_token.get(
            "email"
        )


        if not firebase_uid or not email:

            return {
                "success": False,
                "error": "Unable to identify Firebase user."
            }, 401


        email = email.strip().lower()


        # ====================================================
        # GET ROLE FROM FIRESTORE
        # ====================================================

        user_doc = (
            db.collection("users")
            .document(email)
            .get()
        )


        if not user_doc.exists:

            return {
                "success": False,
                "error": (
                    "Your Firebase account exists, "
                    "but no system role has been assigned."
                )
            }, 403


        user_data = user_doc.to_dict()

        role = (
            user_data.get("role", "")
            .strip()
            .lower()
        )


        # ====================================================
        # VALIDATE ROLE
        # ====================================================

        if role not in ["admin", "doctor"]:

            return {
                "success": False,
                "error": "Invalid system role."
            }, 403


        # ====================================================
        # CREATE FLASK SESSION
        # ====================================================

        session.clear()

        session["logged_in"] = True

        session["username"] = email

        session["email"] = email

        session["firebase_uid"] = firebase_uid

        session["role"] = role


        # ====================================================
        # LOGIN SUCCESS
        # ====================================================

        return {
            "success": True,
            "redirect": url_for("admin")
        }


    except auth.InvalidIdTokenError as error:

        print(
            "FIREBASE TOKEN ERROR:",
            str(error)
        )

        return {
            "success": False,
            "error": "Invalid Firebase authentication token. Please sign in again."
        }, 401


    except auth.ExpiredIdTokenError:

        return {
            "success": False,
            "error": (
                "Your login session has expired. "
                "Please sign in again."
            )
        }, 401


    except Exception as error:

        print(
            "LOGIN ERROR:",
            error
        )

        return {
            "success": False,
            "error": "Unable to authenticate. Please try again."
        }, 500


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# ADMIN / DOCTOR DASHBOARD
# ============================================================

@app.route("/admin")
@login_required
def admin():

    personnel_list = []


    # ========================================================
    # GET PERSONNEL
    # ========================================================

    personnel_docs = (
        db.collection("personnel")
        .stream()
    )


    for doc in personnel_docs:

        person = doc.to_dict()


        if not person.get(
            "personnel_id"
        ):

            person["personnel_id"] = doc.id


        if not person.get(
            "full_name"
        ):

            first_name = person.get(
                "first_name",
                ""
            )

            last_name = person.get(
                "last_name",
                ""
            )

            person["full_name"] = (
                first_name
                + " "
                + last_name
            ).strip()


        personnel_list.append(
            person
        )


    # ========================================================
    # SORT PERSONNEL
    # ========================================================

    personnel_list.sort(
        key=lambda x: x.get(
            "full_name",
            ""
        ).lower()
    )


    # ========================================================
    # DASHBOARD
    #
    # IMPORTANT:
    # username now displays ADMIN / DOCTOR
    # instead of the email address.
    # ========================================================

    current_role = session.get(
        "role",
        ""
    ).upper()


    return render_template(
        "admin.html",

        personnel_list=personnel_list,

        username=current_role,

        role=session.get(
            "role"
        )
    )


# ============================================================
# DOCTOR QR SCANNER
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/doctor/scanner"
)
@role_required(
    "admin",
    "doctor"
)
def doctor_scanner():

    return render_template(
        "doctor_scanner.html"
    )


# ============================================================
# PERSONNEL REGISTRATION
# ADMIN ONLY
# ============================================================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
@role_required("admin")
def register():

    if request.method == "POST":

        # ====================================================
        # FORM DATA
        # ====================================================

        first_name = request.form.get(
            "first_name",
            ""
        ).strip()

        last_name = request.form.get(
            "last_name",
            ""
        ).strip()

        phone = request.form.get(
            "phone",
            ""
        ).strip()

        dob = request.form.get(
            "dob",
            ""
        ).strip()

        blood_group = request.form.get(
            "blood_group",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()


        # ====================================================
        # VALIDATION
        # ====================================================

        if not first_name or not last_name:

            return render_template(
                "register.html",
                error=(
                    "First name and last name "
                    "are required."
                )
            )


        if (
            not phone.isdigit()
            or len(phone) != 10
        ):

            return render_template(
                "register.html",
                error=(
                    "Enter a valid 10-digit "
                    "mobile number."
                )
            )


        if not dob or len(dob) < 4:

            return render_template(
                "register.html",
                error=(
                    "Please enter a valid "
                    "date of birth."
                )
            )


        # ====================================================
        # STRUCTURED PERSONNEL ID
        # ====================================================

        mobile_last4 = phone[-4:]

        birth_year = dob[:4]

        surname_initial = (
            last_name[0].upper()
        )


        personnel_id = (
            "47"
            + mobile_last4
            + birth_year
            + surname_initial
        )


        # ====================================================
        # DUPLICATE CHECK
        # ====================================================

        existing_personnel = (
            db.collection("personnel")
            .document(personnel_id)
            .get()
        )


        if existing_personnel.exists:

            return render_template(
                "register.html",
                error=(
                    "A personnel record with "
                    "this generated Personnel ID "
                    "already exists."
                )
            )


        # ====================================================
        # SECURE RANDOM QR TOKEN
        # ====================================================

        qr_token = secrets.token_urlsafe(
            32
        )


        # ====================================================
        # PROFILE URL
        # ====================================================

        PUBLIC_BASE_URL = os.environ.get(
            "PUBLIC_BASE_URL",
            "https://viqtor-health.onrender.com"
        ).rstrip("/")

        profile_url = (
            PUBLIC_BASE_URL
            + "/profile/"
            + qr_token
        )


        # ====================================================
        # QR FILENAME
        # ====================================================

        qr_filename = (
            personnel_id
            + ".png"
        )


        qr_path = os.path.join(
            QR_FOLDER,
            qr_filename
        )


        # ====================================================
        # GENERATE QR
        # ====================================================

        qr = qrcode.QRCode(

            version=1,

            error_correction=(
                qrcode.constants
                .ERROR_CORRECT_H
            ),

            box_size=10,

            border=4
        )


        qr.add_data(
            profile_url
        )

        qr.make(
            fit=True
        )


        qr_image = qr.make_image()


        qr_image.save(
            qr_path
        )


        # ====================================================
        # PERSONNEL DATA
        # ====================================================

        personnel_data = {

            "personnel_id":
                personnel_id,

            "first_name":
                first_name,

            "last_name":
                last_name,

            "full_name":
                (
                    first_name
                    + " "
                    + last_name
                ).strip(),

            "phone":
                phone,

            "dob":
                dob,

            "blood_group":
                blood_group,

            "email":
                email,

            "qr_token":
                qr_token,

            "profile_url":
                profile_url,

            "qr_filename":
                qr_filename,

            "created_at":
                datetime.now().isoformat(),

            "created_by":
                session.get(
                    "email"
                )
        }


        # ====================================================
        # SAVE PERSONNEL
        # ====================================================

        db.collection(
            "personnel"
        ).document(
            personnel_id
        ).set(
            personnel_data
        )


        # ====================================================
        # REGISTRATION SUCCESS
        # ====================================================

        return render_template(
            "registration_success.html",
            personnel=personnel_data
        )


    return render_template(
        "register.html"
    )


# ============================================================
# MEDICAL PROFILE / QR ACCESS
# ============================================================

@app.route(
    "/profile/<qr_token>"
)
def profile(qr_token):

    # ========================================================
    # FIND PERSONNEL USING SECURE QR TOKEN
    # ========================================================

    personnel_query = (
        db.collection("personnel")
        .where(
            "qr_token",
            "==",
            qr_token
        )
        .limit(1)
        .stream()
    )

    personnel = None

    for doc in personnel_query:

        personnel = doc.to_dict()

        break

    if not personnel:

        return (
            "Invalid or expired QR code.",
            404
        )

    personnel_id = personnel[
        "personnel_id"
    ]

    # ========================================================
    # LATEST HEALTH RECORD
    # ========================================================

    health_query = (
        db.collection("health_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    health_records = []

    for doc in health_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        health_records.append(
            record
        )

    health_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )

    latest_health = (
        health_records[0]
        if health_records
        else None
    )

    # ========================================================
    # LATEST CBC PROFILE
    # ========================================================

    cbc_query = (
        db.collection("cbc_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    cbc_records = []

    for doc in cbc_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        cbc_records.append(
            record
        )

    cbc_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )

    latest_cbc = (
        cbc_records[0]
        if cbc_records
        else None
    )

    # ========================================================
    # LATEST LIPID PROFILE
    # ========================================================

    lipid_query = (
        db.collection("lipid_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    lipid_records = []

    for doc in lipid_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        lipid_records.append(
            record
        )

    lipid_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )

    latest_lipid = (
        lipid_records[0]
        if lipid_records
        else None
    )

    # ========================================================
    # LATEST ALLERGY PROFILE
    # ========================================================

    allergy_query = (
        db.collection("allergy_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    allergy_records = []

    for doc in allergy_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        allergy_records.append(
            record
        )

    allergy_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )

    latest_allergy = (
        allergy_records[0]
        if allergy_records
        else None
    )

    # ========================================================
    # LATEST THYROID PROFILE
    # ========================================================

    thyroid_query = (
        db.collection("thyroid_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    thyroid_records = []

    for doc in thyroid_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        thyroid_records.append(
            record
        )

    thyroid_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )

    latest_thyroid = (
        thyroid_records[0]
        if thyroid_records
        else None
    )

    # ========================================================
    # LATEST AI HEALTH REPORT
    # ========================================================

    ai_query = (
        db.collection("ai_health_reports")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    ai_reports = []

    for doc in ai_query:

        report = doc.to_dict()

        report["document_id"] = doc.id

        ai_reports.append(report)

    ai_reports.sort(
        key=lambda x: x.get(
            "generated_at",
            ""
        ),
        reverse=True
    )

    latest_ai_report = (
        ai_reports[0]
        if ai_reports
        else None
    )

    # ========================================================
    # AUTHENTICATED ACCESS
    #
    # Admin/doctor users continue to receive the existing
    # detailed medical profile.
    # ========================================================

    if session.get("logged_in"):

        current_role = session.get(
            "role"
        )

        if current_role in ["admin", "doctor"]:

            return render_template(
                "profile.html",
                personnel=personnel,
                latest_health=latest_health,
                health_records=health_records,
                latest_cbc=latest_cbc,
                latest_lipid=latest_lipid,
                latest_allergy=latest_allergy,
                latest_thyroid=latest_thyroid,
                latest_ai_report=latest_ai_report,
                username=(
                    current_role.upper()
                    if current_role
                    else "Medical Officer"
                ),
                role=current_role
            )

    # ========================================================
    # PUBLIC QR ACCESS
    #
    # No login is required for this page.
    # Only the deliberately public health snapshot is shown.
    # Phone, email, detailed history, doctor notes and other
    # confidential fields are NOT sent to the public template.
    # ========================================================

    public_personnel = {
        "first_name": personnel.get(
            "first_name",
            ""
        ),
        "last_name": personnel.get(
            "last_name",
            ""
        ),
        "full_name": personnel.get(
            "full_name",
            ""
        ),
        "personnel_id": personnel.get(
            "personnel_id",
            ""
        ),
        "dob": personnel.get(
            "dob",
            ""
        ),
        "blood_group": personnel.get(
            "blood_group",
            ""
        )
    }

    return render_template(
        "qr_access.html",
        personnel=public_personnel,
        latest_health=latest_health,
        latest_cbc=latest_cbc,
        latest_lipid=latest_lipid
    )


# ============================================================
# ADD MEDICAL RECORD
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/health/<personnel_id>",
    methods=["GET", "POST"]
)
@role_required(
    "admin",
    "doctor"
)
def health_record(personnel_id):

    # ========================================================
    # FIND PERSONNEL
    # ========================================================

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )


    if not personnel_doc.exists:

        return (
            "Personnel not found.",
            404
        )


    personnel = personnel_doc.to_dict()


    # ========================================================
    # POST
    # ========================================================

    if request.method == "POST":

        try:

            # =================================================
            # INTEGER HELPER
            # =================================================

            def get_int(
                field_name,
                required=False
            ):

                value = request.form.get(
                    field_name,
                    ""
                ).strip()


                if value == "":

                    if required:

                        raise ValueError(
                            field_name
                            + " is required."
                        )

                    return None


                return int(value)


            # =================================================
            # FLOAT HELPER
            # =================================================

            def get_float(
                field_name,
                required=False
            ):

                value = request.form.get(
                    field_name,
                    ""
                ).strip()


                if value == "":

                    if required:

                        raise ValueError(
                            field_name
                            + " is required."
                        )

                    return None


                return float(value)


            # =================================================
            # REQUIRED VITALS
            # =================================================

            heart_rate = get_int(
                "heart_rate",
                True
            )


            systolic_bp = get_int(
                "systolic_bp",
                True
            )


            diastolic_bp = get_int(
                "diastolic_bp",
                True
            )


            spo2 = get_float(
                "spo2",
                True
            )


            temperature = get_float(
                "temperature",
                True
            )


            respiratory_rate = get_int(
                "respiratory_rate",
                True
            )


            # =================================================
            # OPTIONAL PARAMETERS
            # =================================================

            weight = get_float(
                "weight"
            )


            height = get_float(
                "height"
            )


            bmi = get_float(
                "bmi"
            )


            body_fat = get_float(
                "body_fat"
            )


            muscle_mass = get_float(
                "muscle_mass"
            )


            hydration = get_float(
                "hydration"
            )


            steps = get_int(
                "steps"
            )


            calories_burned = get_float(
                "calories_burned"
            )


            sleep_duration = get_float(
                "sleep_duration"
            )


            activity_duration = get_float(
                "activity_duration"
            )


            blood_glucose = get_float(
                "blood_glucose"
            )


            resting_heart_rate = get_int(
                "resting_heart_rate"
            )


            sleep_quality = request.form.get(
                "sleep_quality",
                ""
            ).strip()


            stress_level = request.form.get(
                "stress_level",
                ""
            ).strip()


            ecg_status = request.form.get(
                "ecg_status",
                ""
            ).strip()


            fatigue_level = request.form.get(
                "fatigue_level",
                ""
            ).strip()


            pain_level = request.form.get(
                "pain_level",
                ""
            ).strip()


            medication_status = request.form.get(
                "medication_status",
                ""
            ).strip()


            overall_health_status = (
                request.form.get(
                    "overall_health_status",
                    ""
                ).strip()
            )


            # =================================================
            # ADDITIONAL HEALTH INFORMATION
            # =================================================

            allergic_conditions = (
                request.form.get(
                    "allergic_conditions",
                    ""
                ).strip()
            )

            critical_hospital_admission = (
                request.form.get(
                    "critical_hospital_admission",
                    ""
                ).strip()
            )

            thyroid_tsh = get_float(
                "thyroid_tsh"
            )

            thyroid_t3 = get_float(
                "thyroid_t3"
            )

            thyroid_t4 = get_float(
                "thyroid_t4"
            )

            thyroid_free_t3 = get_float(
                "thyroid_free_t3"
            )

            thyroid_free_t4 = get_float(
                "thyroid_free_t4"
            )


            # =================================================
            # HEALTH DATA
            # =================================================

            health_data = {

                "personnel_id":
                    personnel_id,

                "heart_rate":
                    heart_rate,

                "systolic_bp":
                    systolic_bp,

                "diastolic_bp":
                    diastolic_bp,

                "spo2":
                    spo2,

                "temperature":
                    temperature,

                "respiratory_rate":
                    respiratory_rate,

                "weight":
                    weight,

                "height":
                    height,

                "bmi":
                    bmi,

                "body_fat":
                    body_fat,

                "muscle_mass":
                    muscle_mass,

                "hydration":
                    hydration,

                "steps":
                    steps,

                "calories_burned":
                    calories_burned,

                "sleep_duration":
                    sleep_duration,

                "sleep_quality":
                    sleep_quality,

                "activity_duration":
                    activity_duration,

                "blood_glucose":
                    blood_glucose,

                "stress_level":
                    stress_level,

                "resting_heart_rate":
                    resting_heart_rate,

                "ecg_status":
                    ecg_status,

                "fatigue_level":
                    fatigue_level,

                "pain_level":
                    pain_level,

                "medication_status":
                    medication_status,

                "overall_health_status":
                    overall_health_status,

                "allergic_conditions":
                    allergic_conditions,

                "critical_hospital_admission":
                    critical_hospital_admission,

                "thyroid_tsh":
                    thyroid_tsh,

                "thyroid_t3":
                    thyroid_t3,

                "thyroid_t4":
                    thyroid_t4,

                "thyroid_free_t3":
                    thyroid_free_t3,

                "thyroid_free_t4":
                    thyroid_free_t4,

                "recorded_at":
                    datetime.now().isoformat(),

                "recorded_by":
                    session.get(
                        "email"
                    ),

                "recorded_by_role":
                    session.get(
                        "role"
                    )
            }


            # =================================================
            # SAVE TO FIRESTORE
            # =================================================

            db.collection(
                "health_records"
            ).add(
                health_data
            )


            # =================================================
            # RETURN TO PROFILE
            # =================================================

            return redirect(
                url_for(
                    "profile",
                    qr_token=personnel[
                        "qr_token"
                    ]
                )
            )


        except ValueError as error:

            return render_template(
                "health_record.html",

                personnel=personnel,

                error=str(error)
            )


        except Exception as error:

            print(
                "HEALTH RECORD ERROR:",
                error
            )


            return render_template(
                "health_record.html",

                personnel=personnel,

                error=(
                    "Unable to save the "
                    "health record."
                )
            )


    # ========================================================
    # GET HEALTH RECORD PAGE
    # ========================================================

    return render_template(
        "health_record.html",
        personnel=personnel
    )


# ============================================================
# CBC PROFILE
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/cbc/<personnel_id>",
    methods=["GET", "POST"]
)
@role_required(
    "admin",
    "doctor"
)
def cbc_profile(personnel_id):

    # ========================================================
    # FIND PERSONNEL
    # ========================================================

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:

        return (
            "Patient not found.",
            404
        )

    personnel = personnel_doc.to_dict()


    # ========================================================
    # POST CBC RECORD
    # ========================================================

    if request.method == "POST":

        try:

            cbc_data = {
                "personnel_id":
                    personnel_id,

                "recorded_at":
                    datetime.now().isoformat(),

                "recorded_by":
                    session.get(
                        "email"
                    ),

                "recorded_by_role":
                    session.get(
                        "role"
                    )
            }


            # ------------------------------------------------
            # Store all CBC form fields.
            # Numeric values are saved as numbers where
            # possible; text/select fields remain strings.
            # ------------------------------------------------

            for field_name, field_value in request.form.items():

                value = field_value.strip()

                if value == "":
                    cbc_data[field_name] = None
                    continue

                try:
                    if "." in value:
                        cbc_data[field_name] = float(value)
                    else:
                        cbc_data[field_name] = int(value)

                except ValueError:
                    cbc_data[field_name] = value


            db.collection(
                "cbc_records"
            ).add(
                cbc_data
            )


            return redirect(
                url_for(
                    "profile",
                    qr_token=personnel[
                        "qr_token"
                    ]
                )
            )


        except Exception as error:

            print(
                "CBC PROFILE ERROR:",
                error
            )

            return render_template(
                "cbc_profile.html",
                personnel=personnel,
                error=(
                    "Unable to save the CBC profile. "
                    "Please check the entered values."
                )
            )


    # ========================================================
    # GET PREVIOUS CBC RECORDS
    # ========================================================

    cbc_query = (
        db.collection("cbc_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    cbc_records = []

    for doc in cbc_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        cbc_records.append(
            record
        )


    cbc_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )


    latest_cbc = (
        cbc_records[0]
        if cbc_records
        else None
    )


    return render_template(
        "cbc_profile.html",
        personnel=personnel,
        cbc_records=cbc_records,
        latest_cbc=latest_cbc
    )


# ============================================================
# LIPID PROFILE
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/lipid/<personnel_id>",
    methods=["GET", "POST"]
)
@role_required(
    "admin",
    "doctor"
)
def lipid_profile(personnel_id):

    # ========================================================
    # FIND PERSONNEL
    # ========================================================

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:

        return (
            "Patient not found.",
            404
        )

    personnel = personnel_doc.to_dict()


    # ========================================================
    # POST LIPID RECORD
    # ========================================================

    if request.method == "POST":

        try:

            lipid_data = {
                "personnel_id":
                    personnel_id,

                "recorded_at":
                    datetime.now().isoformat(),

                "recorded_by":
                    session.get(
                        "email"
                    ),

                "recorded_by_role":
                    session.get(
                        "role"
                    )
            }


            # ------------------------------------------------
            # Store all lipid form fields.
            # Numeric values are saved as numbers where
            # possible; text/select fields remain strings.
            # ------------------------------------------------

            for field_name, field_value in request.form.items():

                value = field_value.strip()

                if value == "":
                    lipid_data[field_name] = None
                    continue

                try:
                    if "." in value:
                        lipid_data[field_name] = float(value)
                    else:
                        lipid_data[field_name] = int(value)

                except ValueError:
                    lipid_data[field_name] = value


            db.collection(
                "lipid_records"
            ).add(
                lipid_data
            )


            return redirect(
                url_for(
                    "profile",
                    qr_token=personnel[
                        "qr_token"
                    ]
                )
            )


        except Exception as error:

            print(
                "LIPID PROFILE ERROR:",
                error
            )

            return render_template(
                "lipid_profile.html",
                personnel=personnel,
                error=(
                    "Unable to save the lipid profile. "
                    "Please check the entered values."
                )
            )


    # ========================================================
    # GET PREVIOUS LIPID RECORDS
    # ========================================================

    lipid_query = (
        db.collection("lipid_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    lipid_records = []

    for doc in lipid_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        lipid_records.append(
            record
        )


    lipid_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )


    latest_lipid = (
        lipid_records[0]
        if lipid_records
        else None
    )


    return render_template(
        "lipid_profile.html",
        personnel=personnel,
        lipid_records=lipid_records,
        latest_lipid=latest_lipid
    )


# ============================================================
# DOWNLOAD MEDICAL PROFILE PDF
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/profile/<qr_token>/pdf"
)
@role_required(
    "admin",
    "doctor"
)
def medical_profile_pdf(qr_token):

    personnel_query = (
        db.collection("personnel")
        .where("qr_token", "==", qr_token)
        .limit(1)
        .stream()
    )

    personnel = None
    for doc in personnel_query:
        personnel = doc.to_dict()
        break

    if not personnel:
        return "Patient not found.", 404

    personnel_id = personnel.get("personnel_id", "")

    def records_for(collection_name):
        records = []
        for doc in db.collection(collection_name).where(
            "personnel_id", "==", personnel_id
        ).stream():
            item = doc.to_dict()
            records.append(item)
        records.sort(key=lambda x: x.get("recorded_at", ""), reverse=True)
        return records

    health_records = records_for("health_records")
    cbc_records = records_for("cbc_records")
    lipid_records = records_for("lipid_records")

    latest_health = health_records[0] if health_records else None
    latest_cbc = cbc_records[0] if cbc_records else None
    latest_lipid = lipid_records[0] if lipid_records else None

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=16*mm, leftMargin=16*mm,
        topMargin=16*mm, bottomMargin=16*mm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ViQTitle", parent=styles["Title"], fontSize=20,
        leading=24, textColor=colors.HexColor("#17324d"), spaceAfter=4
    )
    section_style = ParagraphStyle(
        "ViQSection", parent=styles["Heading2"], fontSize=12,
        leading=15, textColor=colors.HexColor("#26736a"), spaceBefore=10, spaceAfter=6
    )
    body_style = ParagraphStyle(
        "ViQBody", parent=styles["BodyText"], fontSize=9, leading=12, textColor=colors.HexColor("#263238")
    )

    story = [
        Paragraph("ViQtor Health", title_style),
        Paragraph("Smart Digital Health & Medical Record Platform", body_style),
        Spacer(1, 8),
        Paragraph("Medical Profile", section_style)
    ]

    patient_rows = [
        ["Patient", personnel.get("full_name", "")],
        ["Patient ID", personnel.get("personnel_id", "")],
        ["Date of Birth", personnel.get("dob", "")],
        ["Blood Group", personnel.get("blood_group", "")],
    ]
    story.append(Table(patient_rows, colWidths=[42*mm, 125*mm], style=TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#eef6f4")),
        ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#d8e4e1")),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 7), ("RIGHTPADDING", (0,0), (-1,-1), 7),
        ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6)
    ])))

    def add_kv_section(title, pairs):
        story.append(Paragraph(title, section_style))
        rows = [[str(k), "" if v is None else str(v)] for k,v in pairs]
        story.append(Table(rows, colWidths=[60*mm, 107*mm], style=TableStyle([
            ("GRID", (0,0), (-1,-1), 0.35, colors.HexColor("#dfe7e5")),
            ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#f7faf9")),
            ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 8.5),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 6), ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5)
        ])))

    if latest_health:
        add_kv_section("Health Snapshot", [
            ("Heart Rate", latest_health.get("heart_rate")),
            ("Blood Pressure", f"{latest_health.get('systolic_bp','')} / {latest_health.get('diastolic_bp','')}"),
            ("SpO₂", latest_health.get("spo2")),
            ("Temperature", latest_health.get("temperature")),
            ("Respiratory Rate", latest_health.get("respiratory_rate")),
            ("Weight", latest_health.get("weight")),
            ("Height", latest_health.get("height")),
            ("BMI", latest_health.get("bmi")),
            ("Blood Glucose", latest_health.get("blood_glucose")),
            ("Allergic Conditions", latest_health.get("allergic_conditions")),
            ("Critical / Last Hospital Admission", latest_health.get("critical_hospital_admission")),
            ("Overall Health Status", latest_health.get("overall_health_status")),
        ])
        add_kv_section("Thyroid Profile", [
            ("TSH", latest_health.get("thyroid_tsh")),
            ("T3", latest_health.get("thyroid_t3")),
            ("T4", latest_health.get("thyroid_t4")),
            ("Free T3", latest_health.get("thyroid_free_t3")),
            ("Free T4", latest_health.get("thyroid_free_t4")),
        ])

    if latest_cbc:
        add_kv_section("Latest CBC Profile", [(k.replace("_", " ").title(), v) for k,v in latest_cbc.items() if k not in {"personnel_id", "recorded_by", "recorded_by_role", "document_id"}])

    if latest_lipid:
        add_kv_section("Latest Lipid Profile", [(k.replace("_", " ").title(), v) for k,v in latest_lipid.items() if k not in {"personnel_id", "recorded_by", "recorded_by_role", "document_id"}])

    story.append(Spacer(1, 10))
    story.append(Paragraph("Generated from ViQtor Health. This document reflects the records available at the time of generation.", body_style))
    doc.build(story)
    buffer.seek(0)

    safe_id = personnel_id.replace("/", "-")
    return send_file(
        buffer, mimetype="application/pdf", as_attachment=True,
        download_name=f"ViQtor_Health_Medical_Profile_{safe_id}.pdf"
    )


# ============================================================
# QR IMAGE
# ============================================================

@app.route(
    "/qr_codes/<filename>"
)
def qr_code(filename):

    return send_from_directory(
        QR_FOLDER,
        filename
    )


# ============================================================
# ALLERGY PROFILE
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/allergy/<personnel_id>",
    methods=["GET", "POST"]
)
@role_required(
    "admin",
    "doctor"
)
def allergy_profile(personnel_id):

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:
        return "Patient not found.", 404

    personnel = personnel_doc.to_dict()

    if request.method == "POST":

        try:

            allergy_data = {
                "personnel_id":
                    personnel_id,

                "allergy_type":
                    request.form.get(
                        "allergy_type",
                        ""
                    ).strip(),

                "allergen":
                    request.form.get(
                        "allergen",
                        ""
                    ).strip(),

                "reaction":
                    request.form.get(
                        "reaction",
                        ""
                    ).strip(),

                "severity":
                    request.form.get(
                        "severity",
                        ""
                    ).strip(),

                "first_identified":
                    request.form.get(
                        "first_identified",
                        ""
                    ).strip(),

                "status":
                    request.form.get(
                        "status",
                        "Active"
                    ).strip(),

                "emergency_allergy":
                    (
                        "Yes"
                        if request.form.get(
                            "emergency_allergy"
                        )
                        else "No"
                    ),

                "notes":
                    request.form.get(
                        "notes",
                        ""
                    ).strip(),

                "recorded_at":
                    datetime.now().isoformat(),

                "recorded_by":
                    session.get("email"),

                "recorded_by_role":
                    session.get("role")
            }

            db.collection(
                "allergy_records"
            ).add(
                allergy_data
            )

            return redirect(
                url_for(
                    "allergy_profile",
                    personnel_id=personnel_id
                )
            )

        except Exception as error:

            print(
                "ALLERGY PROFILE ERROR:",
                error
            )

            return render_template(
                "allergy_profile.html",
                personnel=personnel,
                allergy_records=[],
                error=(
                    "Unable to save the allergy profile."
                )
            )

    allergy_query = (
        db.collection("allergy_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    allergy_records = []

    for doc in allergy_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        allergy_records.append(record)

    allergy_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )

    return render_template(
        "allergy_profile.html",
        personnel=personnel,
        allergy_records=allergy_records
    )


# ============================================================
# THYROID PROFILE
# ADMIN + DOCTOR
# ============================================================

@app.route(
    "/thyroid/<personnel_id>",
    methods=["GET", "POST"]
)
@role_required(
    "admin",
    "doctor"
)
def thyroid_profile(personnel_id):

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:
        return "Patient not found.", 404

    personnel = personnel_doc.to_dict()

    if request.method == "POST":

        try:

            def get_float(field_name):

                value = request.form.get(
                    field_name,
                    ""
                ).strip()

                if value == "":
                    return None

                return float(value)


            thyroid_data = {

                "personnel_id":
                    personnel_id,

                "tsh":
                    get_float("tsh"),

                "t3":
                    get_float("t3"),

                "t4":
                    get_float("t4"),

                "free_t3":
                    get_float("free_t3"),

                "free_t4":
                    get_float("free_t4"),

                "investigation_date":
                    request.form.get(
                        "investigation_date",
                        ""
                    ).strip(),

                "thyroid_condition":
                    request.form.get(
                        "thyroid_condition",
                        ""
                    ).strip(),

                "medication":
                    request.form.get(
                        "medication",
                        ""
                    ).strip(),

                "clinical_notes":
                    request.form.get(
                        "clinical_notes",
                        ""
                    ).strip(),

                "recorded_at":
                    datetime.now().isoformat(),

                "recorded_by":
                    session.get("email"),

                "recorded_by_role":
                    session.get("role")
            }

            db.collection(
                "thyroid_records"
            ).add(
                thyroid_data
            )

            return redirect(
                url_for(
                    "thyroid_profile",
                    personnel_id=personnel_id
                )
            )

        except ValueError:

            return render_template(
                "thyroid_profile.html",
                personnel=personnel,
                thyroid_records=[],
                latest_thyroid=None,
                error=(
                    "Please enter valid numeric thyroid values."
                )
            )

        except Exception as error:

            print(
                "THYROID PROFILE ERROR:",
                error
            )

            return render_template(
                "thyroid_profile.html",
                personnel=personnel,
                thyroid_records=[],
                latest_thyroid=None,
                error=(
                    "Unable to save the thyroid profile."
                )
            )


    thyroid_query = (
        db.collection("thyroid_records")
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    thyroid_records = []

    for doc in thyroid_query:

        record = doc.to_dict()

        record["document_id"] = doc.id

        thyroid_records.append(record)


    thyroid_records.sort(
        key=lambda x: x.get(
            "recorded_at",
            ""
        ),
        reverse=True
    )


    latest_thyroid = (
        thyroid_records[0]
        if thyroid_records
        else None
    )


    return render_template(
        "thyroid_profile.html",
        personnel=personnel,
        thyroid_records=thyroid_records,
        latest_thyroid=latest_thyroid
    )

# ============================================================
# AI PERSONALIZED NUTRITION & WELLNESS
# ADMIN + DOCTOR
# ============================================================


def _latest_collection_record(collection_name, personnel_id):

    records = []

    query = (
        db.collection(collection_name)
        .where(
            "personnel_id",
            "==",
            personnel_id
        )
        .stream()
    )

    for doc in query:
        record = doc.to_dict()
        record["document_id"] = doc.id
        records.append(record)

    records.sort(
        key=lambda x: x.get("recorded_at", ""),
        reverse=True
    )

    return records


@app.route(
    "/ai-health-plan/<personnel_id>",
    methods=["POST"]
)
@role_required(
    "admin",
    "doctor"
)
def generate_ai_health_plan_route(personnel_id):

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:
        return "Patient not found.", 404

    personnel = personnel_doc.to_dict()

    try:
        health_records = _latest_collection_record(
            "health_records",
            personnel_id
        )
        cbc_records = _latest_collection_record(
            "cbc_records",
            personnel_id
        )
        lipid_records = _latest_collection_record(
            "lipid_records",
            personnel_id
        )
        allergy_records = _latest_collection_record(
            "allergy_records",
            personnel_id
        )
        thyroid_records = _latest_collection_record(
            "thyroid_records",
            personnel_id
        )

        latest_health = health_records[0] if health_records else None
        latest_cbc = cbc_records[0] if cbc_records else None
        latest_lipid = lipid_records[0] if lipid_records else None
        latest_thyroid = thyroid_records[0] if thyroid_records else None

        active_allergies = []
        for allergy in allergy_records:
            status = str(allergy.get("status", "")).strip().lower()
            if status in ["active", "ongoing", "current"]:
                active_allergies.append({
                    "allergy_type": allergy.get("allergy_type"),
                    "allergen": allergy.get("allergen"),
                    "reaction": allergy.get("reaction"),
                    "severity": allergy.get("severity"),
                    "emergency_allergy": allergy.get("emergency_allergy")
                })

        hard_exclusions = []
        for allergy in active_allergies:
            allergen = str(allergy.get("allergen") or "").strip()
            if allergen:
                hard_exclusions.append(allergen)

        dob = str(personnel.get("dob") or "").strip()
        age = None
        if dob:
            try:
                birth_date = datetime.strptime(dob, "%Y-%m-%d").date()
                today = datetime.now().date()
                age = today.year - birth_date.year - (
                    (today.month, today.day) <
                    (birth_date.month, birth_date.day)
                )
            except ValueError:
                age = None

        patient_context = {
            "age_years": age,
            "blood_group": personnel.get("blood_group"),
            "latest_vitals": latest_health,
            "latest_cbc": latest_cbc,
            "latest_lipid": latest_lipid,
            "latest_thyroid": latest_thyroid,
            "active_allergies": active_allergies,
            "hard_food_exclusions": hard_exclusions,
            "data_availability": {
                "health_record": bool(latest_health),
                "cbc": bool(latest_cbc),
                "lipid": bool(latest_lipid),
                "allergy": bool(allergy_records),
                "thyroid": bool(latest_thyroid)
            }
        }

        plan = generate_ai_health_plan(
            patient_context
        )

        generated_at = datetime.now().isoformat()

        report_data = {
            "personnel_id": personnel_id,
            "generated_at": generated_at,
            "generated_by": session.get("email"),
            "generated_by_role": session.get("role"),
            "model": AI_MODEL,
            "plan": plan,
            "source_timestamps": {
                "health": (latest_health or {}).get("recorded_at"),
                "cbc": (latest_cbc or {}).get("recorded_at"),
                "lipid": (latest_lipid or {}).get("recorded_at"),
                "thyroid": (latest_thyroid or {}).get("recorded_at")
            },
            "active_allergy_count": len(active_allergies)
        }

        report_ref = db.collection(
            "ai_health_reports"
        ).add(report_data)[1]

        return redirect(
            url_for(
                "view_ai_health_plan",
                report_id=report_ref.id
            )
        )

    except Exception as error:
        print(
            "AI HEALTH PLAN ERROR:",
            repr(error)
        )

        return render_template(
            "ai_health_plan.html",
            personnel=personnel,
            report=None,
            error=(
                "Unable to generate the AI health plan. "
                + str(error)
            )
        ), 500


@app.route(
    "/ai-health-plan/<report_id>",
    methods=["GET"]
)
@role_required(
    "admin",
    "doctor"
)
def view_ai_health_plan(report_id):

    report_doc = (
        db.collection("ai_health_reports")
        .document(report_id)
        .get()
    )

    if not report_doc.exists:
        return "AI report not found.", 404

    report = report_doc.to_dict()
    report["document_id"] = report_doc.id

    personnel_id = report.get("personnel_id")

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:
        return "Patient not found.", 404

    personnel = personnel_doc.to_dict()

    return render_template(
        "ai_health_plan.html",
        personnel=personnel,
        report=report,
        error=None
    )


@app.route(
    "/ai-health-plan/<report_id>/pdf",
    methods=["GET"]
)
@role_required(
    "admin",
    "doctor"
)
def ai_health_plan_pdf(report_id):

    report_doc = (
        db.collection("ai_health_reports")
        .document(report_id)
        .get()
    )

    if not report_doc.exists:
        return "AI report not found.", 404

    report = report_doc.to_dict()

    personnel_id = report.get("personnel_id")

    personnel_doc = (
        db.collection("personnel")
        .document(personnel_id)
        .get()
    )

    if not personnel_doc.exists:
        return "Patient not found.", 404

    personnel = personnel_doc.to_dict()
    plan = report.get("plan") or {}

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "AIPlanTitle",
        parent=styles["Title"],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#17324d"),
        spaceAfter=5
    )

    subtitle_style = ParagraphStyle(
        "AIPlanSubtitle",
        parent=styles["BodyText"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#667575"),
        spaceAfter=10
    )

    section_style = ParagraphStyle(
        "AIPlanSection",
        parent=styles["Heading2"],
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#26736a"),
        spaceBefore=10,
        spaceAfter=6
    )

    body_style = ParagraphStyle(
        "AIPlanBody",
        parent=styles["BodyText"],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#263238"),
        spaceAfter=5
    )

    bullet_style = ParagraphStyle(
        "AIPlanBullet",
        parent=body_style,
        leftIndent=10,
        firstLineIndent=-6,
        spaceAfter=4
    )

    story = [
        Paragraph("ViQtor Health", title_style),
        Paragraph(
            "AI Personalized Nutrition & Wellness Report",
            subtitle_style
        ),
        Paragraph(
            "Patient: " + escape(str(personnel.get("full_name", "Patient"))),
            body_style
        ),
        Paragraph(
            "Patient ID: " + escape(str(personnel_id)),
            body_style
        ),
        Paragraph(
            "Generated: " + escape(str(report.get("generated_at", ""))),
            body_style
        )
    ]

    def add_section(title, text=None, bullets=None):
        story.append(Paragraph(escape(title), section_style))
        if text:
            story.append(Paragraph(escape(str(text)), body_style))
        for item in bullets or []:
            story.append(Paragraph("• " + escape(str(item)), bullet_style))

    add_section(
        "Executive Summary",
        plan.get("executive_summary")
    )

    add_section(
        "Priority Focus",
        bullets=plan.get("priority_focus", [])
    )

    add_section(
        "Nutrition Goals",
        bullets=plan.get("nutrition_goals", [])
    )

    story.append(Paragraph("Recommended Foods", section_style))
    for item in plan.get("recommended_foods", []):
        story.append(
            Paragraph(
                "<b>" + escape(str(item.get("category", ""))) + "</b>: "
                + escape(str(item.get("examples", "")))
                + " — "
                + escape(str(item.get("reason", ""))),
                body_style
            )
        )

    story.append(Paragraph("Foods to Avoid", section_style))
    for item in plan.get("foods_to_avoid", []):
        story.append(
            Paragraph(
                "<b>" + escape(str(item.get("item", ""))) + "</b>: "
                + escape(str(item.get("reason", ""))),
                body_style
            )
        )

    story.append(Paragraph("Daily Meal Framework", section_style))
    for item in plan.get("meal_framework", []):
        story.append(
            Paragraph(
                "<b>" + escape(str(item.get("meal", ""))) + "</b>: "
                + escape(str(item.get("options", ""))),
                body_style
            )
        )

    add_section(
        "Hydration Guidance",
        plan.get("hydration_guidance")
    )

    add_section(
        "Activity & Lifestyle",
        bullets=plan.get("activity_and_lifestyle", [])
    )

    add_section(
        "Allergy Safety",
        bullets=plan.get("allergy_safety", [])
    )

    add_section(
        "Laboratory Considerations",
        bullets=plan.get("lab_considerations", [])
    )

    add_section(
        "Professional Review",
        bullets=plan.get("clinician_review", [])
    )

    add_section(
        "Important Disclaimer",
        plan.get("disclaimer")
    )

    story.append(Spacer(1, 8))
    story.append(
        Paragraph(
            "Generated by ViQtor Health AI. This report should be reviewed by a qualified healthcare professional or dietitian when individualized medical or nutrition advice is required.",
            subtitle_style
        )
    )

    document.build(story)
    buffer.seek(0)

    safe_id = str(personnel_id).replace("/", "-")

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            "ViQtor_AI_Nutrition_Wellness_"
            + safe_id
            + ".pdf"
        )
    )



# ============================================================
# 50-50-50 DAILY WELLNESS CHALLENGE
# ============================================================

FIFTY_COLLECTION = "fifty_fifty_fifty"


def india_today():
    """Return the current calendar date in India for the daily challenge."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


@app.route("/505050", methods=["GET", "POST"])
def fifty_fifty_fifty():
    """Public 50-50-50 daily wellness entry page. No login is required."""

    error = None
    success = None
    today = india_today()

    if request.method == "POST":
        personnel_id = request.form.get("personnel_id", "").strip()
        pushups_raw = request.form.get("pushups", "").strip()
        squats_raw = request.form.get("squats", "").strip()
        steps_raw = request.form.get("steps", "").strip()

        if not personnel_id:
            error = "Please enter your Unique ID."
        else:
            person_doc = db.collection("personnel").document(personnel_id).get()

            if not person_doc.exists:
                error = "Unique ID not found. Please enter a valid ViQtor Health Unique ID."
            else:
                try:
                    pushups = int(pushups_raw)
                    squats = int(squats_raw)
                    steps = int(steps_raw)

                    if pushups < 0 or squats < 0 or steps < 0:
                        raise ValueError

                    if pushups > 100000 or squats > 100000 or steps > 1000000:
                        raise ValueError

                except (TypeError, ValueError):
                    error = "Please enter valid non-negative numbers for all three activities."
                else:
                    person = person_doc.to_dict() or {}
                    full_name = person.get("full_name", "").strip()

                    if not full_name:
                        full_name = (
                            str(person.get("first_name", "")).strip()
                            + " "
                            + str(person.get("last_name", "")).strip()
                        ).strip()

                    entry_id = f"{personnel_id}_{today}"
                    entry_ref = db.collection(FIFTY_COLLECTION).document(entry_id)
                    transaction = db.transaction()

                    @firestore.transactional
                    def create_daily_entry(transaction):
                        existing = entry_ref.get(transaction=transaction)

                        if existing.exists:
                            return False

                        transaction.set(
                            entry_ref,
                            {
                                "personnel_id": personnel_id,
                                "full_name": full_name,
                                "pushups": pushups,
                                "squats": squats,
                                "steps": steps,
                                "entry_date": today,
                                "submitted_at": datetime.now().isoformat(),
                            }
                        )
                        return True

                    try:
                        created = create_daily_entry(transaction)
                    except Exception as exc:
                        print("50-50-50 ENTRY ERROR:", str(exc))
                        created = None

                    if created is True:
                        success = (
                            "Your 50-50-50 entry has been recorded for today."
                        )
                    elif created is False:
                        error = (
                            "You have already submitted your 50-50-50 entry for today. "
                            "Only one entry per day is allowed."
                        )
                    else:
                        error = "Unable to save your entry right now. Please try again."

    return render_template(
        "505050.html",
        error=error,
        success=success,
        today=today,
    )


@app.route("/admin/505050")
@role_required("admin")
def fifty_fifty_fifty_admin():
    """Admin-only 50-50-50 analytics and leaderboard."""

    selected_date = request.args.get("date", "").strip()
    today = india_today()

    if not selected_date:
        selected_date = today

    records = []
    query = db.collection(FIFTY_COLLECTION)

    if selected_date:
        query = query.where("entry_date", "==", selected_date)

    for doc in query.stream():
        record = doc.to_dict() or {}
        record["document_id"] = doc.id
        records.append(record)

    records.sort(
        key=lambda item: (
            int(item.get("pushups", 0) or 0),
            int(item.get("squats", 0) or 0),
            int(item.get("steps", 0) or 0),
        ),
        reverse=True,
    )

    total_pushups = sum(int(item.get("pushups", 0) or 0) for item in records)
    total_squats = sum(int(item.get("squats", 0) or 0) for item in records)
    total_steps = sum(int(item.get("steps", 0) or 0) for item in records)

    return render_template(
        "505050_admin.html",
        records=records,
        selected_date=selected_date,
        today=today,
        total_entries=len(records),
        total_pushups=total_pushups,
        total_squats=total_squats,
        total_steps=total_steps,
    )

# ============================================================
# RUN APPLICATION
# ============================================================

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(self), microphone=(), geolocation=()"
    )

    if IS_RENDER:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains"
        )

    return response


if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=not IS_RENDER
    )

