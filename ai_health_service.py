import json
import os
from openai import OpenAI


AI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")


AI_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "executive_summary": {"type": "string"},
        "priority_focus": {
            "type": "array",
            "items": {"type": "string"}
        },
        "nutrition_goals": {
            "type": "array",
            "items": {"type": "string"}
        },
        "recommended_foods": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {"type": "string"},
                    "examples": {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["category", "examples", "reason"]
            }
        },
        "foods_to_avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "item": {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["item", "reason"]
            }
        },
        "meal_framework": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "meal": {"type": "string"},
                    "options": {"type": "string"}
                },
                "required": ["meal", "options"]
            }
        },
        "hydration_guidance": {"type": "string"},
        "activity_and_lifestyle": {
            "type": "array",
            "items": {"type": "string"}
        },
        "allergy_safety": {
            "type": "array",
            "items": {"type": "string"}
        },
        "lab_considerations": {
            "type": "array",
            "items": {"type": "string"}
        },
        "clinician_review": {
            "type": "array",
            "items": {"type": "string"}
        },
        "disclaimer": {"type": "string"}
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


SYSTEM_INSTRUCTIONS = """
You are the ViQtor Health Personalized Nutrition & Wellness Engine.

Your task is to create a cautious, personalized nutrition and wellness plan from the
structured health information supplied by the application.

This is decision support, NOT diagnosis, treatment, medication prescribing, or a
replacement for a qualified clinician or dietitian.

RULES:
1. Use only the information supplied in the patient context. Do not invent missing values.
2. Do not diagnose a disease or claim to cure, treat, reverse, or prevent a disease.
3. Do not prescribe medication, change medication doses, or recommend stopping medication.
4. Do not declare a vitamin/mineral deficiency solely from the supplied labs unless a
   documented deficiency is explicitly present in the supplied record.
5. Give practical, general nutrition and lifestyle guidance using ordinary foods.
6. Respect the ACTIVE ALLERGEN EXCLUSION LIST as a hard safety constraint. Never
   recommend an excluded allergen, including it as a meal example, snack, ingredient,
   garnish, supplement or optional substitute.
7. Distinguish recorded allergies from other clinical information. A drug allergy is
   not a reason to invent a food restriction.
8. If a supplied value may warrant professional review, say that clinician review is
   appropriate rather than making a diagnosis.
9. If data is missing, say that it is missing rather than guessing.
10. Do not provide exact therapeutic dosing or medical treatment protocols.
11. Keep recommendations culturally flexible and realistic for an Indian adult unless
    the supplied context says otherwise.
12. The output must be concise enough to be useful on a professional patient report.
13. The disclaimer must clearly state that the report is AI-generated decision support
    and does not replace professional medical or dietetic advice.
"""


def generate_ai_health_plan(patient_context):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured. Add it to the local environment or Render Environment."
        )

    client = OpenAI(api_key=api_key)

    payload = json.dumps(
        patient_context,
        ensure_ascii=False,
        separators=(",", ":")
    )

    response = client.responses.create(
        model=AI_MODEL,
        store=False,
        instructions=SYSTEM_INSTRUCTIONS,
        input=(
            "Create the personalized nutrition and wellness plan from this patient context. "
            "Return only the requested structured object.\n\n"
            + payload
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "viqtor_health_plan",
                "description": "Structured personalized nutrition and wellness plan",
                "strict": True,
                "schema": AI_PLAN_SCHEMA
            }
        }
    )

    output_text = response.output_text

    if not output_text:
        raise RuntimeError("The AI service returned an empty response.")

    try:
        return json.loads(output_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("The AI service returned an invalid structured response.") from error
