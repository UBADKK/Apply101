import json
from backend.app import schemas
print(
    json.dumps(
        schemas.ProfileAnalysisStructured.model_json_schema(),
        indent=2
    )
)