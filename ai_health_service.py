import json
import os
import re

from openai import OpenAI


# ============================================================
# OPENROUTER CONFIGURATION
# ============================================================

OPENROUTER_API_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    ""
).strip()


# You can override this in Render without changing the code.
#
# Gemma 4 31B currently has a free OpenRouter variant and supports
# JSON output. We use JSON mode rather than strict JSON-schema mode
# because free-model provider support for strict schemas can vary.
#
# Alternative:
#   openrouter/free
#
# The explicit model gives more predictable behavior.
AI_MODEL = os.environ.get(
    "OPENROUTER_MODEL",
    "google/gemma-4-31b-it:free"
).strip()


OPENROUTER_BASE_URL = (
    "https://openrouter.ai/api/v1"
)


# ============================================================
# AI HEALTH PLAN SCHEMA
# ============================================================

AI_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {
            "type": "string"
        },

        "executive_summary": {
            "type": "string"
        },

        "priority_focus": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "nutrition_goals": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "recommended_foods": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "type": "string"
                    },
                    "examples": {
                        "type": "string"
                    },
                    "reason": {
                        "type": "string"
                    }
                },
                "required": [
                    "category",
                    "examples",
                    "reason"
                ]
            }
        },

        "foods_to_avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "item": {
                        "type": "string"
                    },
                    "reason": {
                        "type": "string"
                    }
                },
                "required": [
                    "item",
                    "reason"
                ]
            }
        },

        "meal_framework": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "meal": {
                        "type": "string"
                    },
                    "options": {
                        "type": "string"
                    }
                },
                "required": [
                    "meal",
                    "options"
                ]
            }
        },

        "hydration_guidance": {
            "type": "string"
        },

        "activity_and_lifestyle": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "allergy_safety": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "lab_considerations": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "clinician_review": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },

        "disclaimer": {
            "type": "string"
        }
    },

    "required": [
        "title",
        "executive_summary",
        "priority_focus",
        "nutrition_goals",
        "recommended_foods",
        "foods_to_avoid",
        "meal_framework",
        "hydration_guidance",
        "activity_and_lifestyle",
        "allergy_safety",
        "lab_considerations",
        "clinician_review",
        "disclaimer"
    ]
}


# ============================================================
# SYSTEM INSTRUCTIONS
# ============================================================

SYSTEM_INSTRUCTIONS = """
You are the ViQtor Health Personalized Nutrition & Wellness Engine.

Your task is to create a cautious, personalized nutrition and wellness
plan from the structured health information supplied by the application.

This is decision support, NOT diagnosis, treatment, medication prescribing,
or a replacement for a qualified clinician or dietitian.

RULES:

1. Use only the information supplied in the patient context.
   Do not invent missing values.

2. Do not diagnose a disease.

3. Do not claim to cure, treat, reverse, or prevent a disease.

4. Do not prescribe medication.

5. Do not change medication doses.

6. Do not recommend stopping medication.

7. Do not declare a vitamin or mineral deficiency solely from supplied
   laboratory values unless a documented deficiency is explicitly present
   in the supplied record.

8. Give practical, general nutrition and lifestyle guidance using ordinary
   foods.

9. Respect the ACTIVE ALLERGEN EXCLUSION LIST as a hard safety constraint.

10. NEVER recommend an excluded allergen.

11. Do not include an excluded allergen as:
    - a meal example
    - snack
    - ingredient
    - garnish
    - supplement
    - optional substitute

12. Distinguish recorded allergies from other clinical information.

13. A drug allergy is NOT automatically a food restriction.

14. If a supplied value may warrant professional review, state that
    clinician review is appropriate instead of making a diagnosis.

15. If data is missing, explicitly state that it is missing.

16. Do not provide exact therapeutic dosing.

17. Do not provide medical treatment protocols.

18. Keep recommendations culturally flexible and realistic for an
    Indian adult unless the supplied context says otherwise.

19. Keep the output concise enough for a professional patient report.

20. The disclaimer must clearly state that the report is AI-generated
    decision support and does not replace professional medical or
    dietetic advice.

IMPORTANT OUTPUT RULE:

Return ONLY one valid JSON object.

Do NOT use Markdown.

Do NOT use ```json.

Do NOT write an introduction.

Do NOT write an explanation outside the JSON object.

The JSON object MUST contain exactly these top-level fields:

title
executive_summary
priority_focus
nutrition_goals
recommended_foods
foods_to_avoid
meal_framework
hydration_guidance
activity_and_lifestyle
allergy_safety
lab_considerations
clinician_review
disclaimer

The structure must be:

{
  "title": "string",
  "executive_summary": "string",
  "priority_focus": ["string"],
  "nutrition_goals": ["string"],
  "recommended_foods": [
    {
      "category": "string",
      "examples": "string",
      "reason": "string"
    }
  ],
  "foods_to_avoid": [
    {
      "item": "string",
      "reason": "string"
    }
  ],
  "meal_framework": [
    {
      "meal": "string",
      "options": "string"
    }
  ],
  "hydration_guidance": "string",
  "activity_and_lifestyle": ["string"],
  "allergy_safety": ["string"],
  "lab_considerations": ["string"],
  "clinician_review": ["string"],
  "disclaimer": "string"
}

Do not add additional top-level fields.
"""


# ============================================================
# REQUIRED FIELDS
# ============================================================

REQUIRED_TOP_LEVEL_FIELDS = [
    "title",
    "executive_summary",
    "priority_focus",
    "nutrition_goals",
    "recommended_foods",
    "foods_to_avoid",
    "meal_framework",
    "hydration_guidance",
    "activity_and_lifestyle",
    "allergy_safety",
    "lab_considerations",
    "clinician_review",
    "disclaimer"
]


# ============================================================
# JSON EXTRACTION
# ============================================================

def _extract_json_object(text):
    """
    Extract the first complete JSON object from the model response.

    This protects the application from models returning things such as:

        ```json
        {...}
        ```

    or:

        Here is the plan:
        {...}
    """

    if not text:
        raise RuntimeError(
            "The OpenRouter AI service returned an empty response."
        )

    cleaned = text.strip()

    # --------------------------------------------------------
    # Remove Markdown code fences if the model added them.
    # --------------------------------------------------------

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE
    )

    cleaned = re.sub(
        r"\s*```$",
        "",
        cleaned
    )

    cleaned = cleaned.strip()

    # --------------------------------------------------------
    # First attempt: entire response is JSON.
    # --------------------------------------------------------

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # --------------------------------------------------------
    # Second attempt: find the first balanced JSON object.
    # --------------------------------------------------------

    start = cleaned.find("{")

    if start == -1:
        raise RuntimeError(
            "The OpenRouter AI service did not return a JSON object."
        )

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(cleaned)):

        character = cleaned[index]

        if escaped:
            escaped = False
            continue

        if character == "\\" and in_string:
            escaped = True
            continue

        if character == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if character == "{":
            depth += 1

        elif character == "}":
            depth -= 1

            if depth == 0:

                candidate = cleaned[
                    start:index + 1
                ]

                try:
                    return json.loads(candidate)

                except json.JSONDecodeError:
                    break

    raise RuntimeError(
        "The OpenRouter AI service returned invalid JSON."
    )


# ============================================================
# RESPONSE VALIDATION
# ============================================================

def _validate_plan(plan):
    """
    Validate the returned plan before it is stored in Firestore.

    We intentionally validate the application contract ourselves
    because free OpenRouter models can differ in how strictly they
    enforce JSON schemas.
    """

    if not isinstance(plan, dict):
        raise RuntimeError(
            "The AI service returned a JSON value that is not an object."
        )

    missing_fields = [
        field
        for field in REQUIRED_TOP_LEVEL_FIELDS
        if field not in plan
    ]

    if missing_fields:
        raise RuntimeError(
            "The AI service returned an incomplete health plan. "
            "Missing fields: "
            + ", ".join(missing_fields)
        )

    # --------------------------------------------------------
    # Basic type validation
    # --------------------------------------------------------

    string_fields = [
        "title",
        "executive_summary",
        "hydration_guidance",
        "disclaimer"
    ]

    for field in string_fields:

        if not isinstance(plan[field], str):

            raise RuntimeError(
                f"Invalid AI health plan field: {field}"
            )

    list_fields = [
        "priority_focus",
        "nutrition_goals",
        "activity_and_lifestyle",
        "allergy_safety",
        "lab_considerations",
        "clinician_review"
    ]

    for field in list_fields:

        if not isinstance(plan[field], list):

            raise RuntimeError(
                f"Invalid AI health plan field: {field}"
            )

        if not all(
            isinstance(item, str)
            for item in plan[field]
        ):

            raise RuntimeError(
                f"Invalid values returned for AI health plan field: {field}"
            )

    # --------------------------------------------------------
    # recommended_foods
    # --------------------------------------------------------

    if not isinstance(
        plan["recommended_foods"],
        list
    ):
        raise RuntimeError(
            "Invalid recommended_foods returned by the AI service."
        )

    for item in plan["recommended_foods"]:

        if not isinstance(item, dict):
            raise RuntimeError(
                "Invalid recommended_foods item returned by the AI service."
            )

        for key in [
            "category",
            "examples",
            "reason"
        ]:

            if not isinstance(
                item.get(key),
                str
            ):

                raise RuntimeError(
                    "Invalid recommended_foods structure returned by the AI service."
                )

    # --------------------------------------------------------
    # foods_to_avoid
    # --------------------------------------------------------

    if not isinstance(
        plan["foods_to_avoid"],
        list
    ):
        raise RuntimeError(
            "Invalid foods_to_avoid returned by the AI service."
        )

    for item in plan["foods_to_avoid"]:

        if not isinstance(item, dict):
            raise RuntimeError(
                "Invalid foods_to_avoid item returned by the AI service."
            )

        for key in [
            "item",
            "reason"
        ]:

            if not isinstance(
                item.get(key),
                str
            ):

                raise RuntimeError(
                    "Invalid foods_to_avoid structure returned by the AI service."
                )

    # --------------------------------------------------------
    # meal_framework
    # --------------------------------------------------------

    if not isinstance(
        plan["meal_framework"],
        list
    ):
        raise RuntimeError(
            "Invalid meal_framework returned by the AI service."
        )

    for item in plan["meal_framework"]:

        if not isinstance(item, dict):
            raise RuntimeError(
                "Invalid meal_framework item returned by the AI service."
            )

        for key in [
            "meal",
            "options"
        ]:

            if not isinstance(
                item.get(key),
                str
            ):

                raise RuntimeError(
                    "Invalid meal_framework structure returned by the AI service."
                )

    # --------------------------------------------------------
    # Remove unexpected top-level fields.
    #
    # This keeps the stored Firestore object aligned with the
    # ViQtor report structure.
    # --------------------------------------------------------

    cleaned_plan = {
        field: plan[field]
        for field in REQUIRED_TOP_LEVEL_FIELDS
    }

    return cleaned_plan


# ============================================================
# OPENROUTER CLIENT
# ============================================================

def _get_openrouter_client():

    if not OPENROUTER_API_KEY:

        raise RuntimeError(
            "OPENROUTER_API_KEY is not configured. "
            "Add OPENROUTER_API_KEY to the local environment "
            "or Render Environment."
        )

    return OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL
    )


# ============================================================
# AI HEALTH PLAN GENERATION
# ============================================================

def generate_ai_health_plan(patient_context):

    client = _get_openrouter_client()

    payload = json.dumps(
        patient_context,
        ensure_ascii=False,
        separators=(",", ":")
    )

    # --------------------------------------------------------
    # Explicit schema instructions are included in the prompt
    # because the selected free model may support JSON mode
    # without strict JSON-schema enforcement.
    # --------------------------------------------------------

    schema_description = json.dumps(
        AI_PLAN_SCHEMA,
        ensure_ascii=False,
        separators=(",", ":")
    )

    user_prompt = (
        "Create the personalized nutrition and wellness plan "
        "from the following patient context.\n\n"

        "PATIENT CONTEXT:\n"
        + payload
        + "\n\n"

        "REQUIRED JSON SCHEMA:\n"
        + schema_description
        + "\n\n"

        "Return ONLY the JSON object. "
        "Do not include Markdown or any text before or after it."
    )

    try:

        response = client.chat.completions.create(
            model=AI_MODEL,

            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_INSTRUCTIONS
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],

            # JSON mode is supported by the selected model.
            # We intentionally do NOT use strict json_schema here
            # because free providers do not all enforce it.
            response_format={
                "type": "json_object"
            },

            temperature=0.2,

            max_tokens=5000,

            extra_headers={
                "HTTP-Referer": os.environ.get(
                    "PUBLIC_BASE_URL",
                    "https://viqtor-health.onrender.com"
                ),

                "X-Title": "ViQtor Health"
            }
        )

    except Exception as error:

        error_text = str(error)

        # ----------------------------------------------------
        # Make OpenRouter errors easier to understand.
        # ----------------------------------------------------

        if "401" in error_text:
            raise RuntimeError(
                "OpenRouter authentication failed. "
                "Check OPENROUTER_API_KEY in Render/local environment."
            ) from error

        if "403" in error_text:
            raise RuntimeError(
                "OpenRouter rejected the request. "
                "Check the API key permissions and selected model."
            ) from error

        if "429" in error_text:
            raise RuntimeError(
                "OpenRouter rate limit reached. "
                "Please wait and try generating the health plan again."
            ) from error

        if "402" in error_text:
            raise RuntimeError(
                "OpenRouter requires credits for the selected model. "
                "Use a model ending in :free."
            ) from error

        raise RuntimeError(
            "OpenRouter AI request failed: "
            + error_text
        ) from error

    # ========================================================
    # READ RESPONSE
    # ========================================================

    if not response.choices:

        raise RuntimeError(
            "OpenRouter returned no choices."
        )

    message = response.choices[0].message

    output_text = (
        message.content
        if message and message.content
        else ""
    )

    if not output_text:

        # Some reasoning models can return content in unusual
        # structures. Give a useful error rather than storing
        # an empty report.
        raise RuntimeError(
            "OpenRouter returned an empty AI health plan."
        )

    # ========================================================
    # PARSE JSON
    # ========================================================

    plan = _extract_json_object(
        output_text
    )

    # ========================================================
    # VALIDATE JSON
    # ========================================================

    return _validate_plan(
        plan
    )