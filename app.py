from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory
)

import firebase_admin
from firebase_admin import credentials, firestore, auth

import qrcode
import os
import secrets

from datetime import datetime
from functools import wraps


# ============================================================
# FLASK CONFIGURATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "viqtor-health-development-secret-key"
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


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

QR_FOLDER = os.path.join(
    BASE_DIR,
    "qr_codes"
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
# RUN APPLICATION
# ============================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )