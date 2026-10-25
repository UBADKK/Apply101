from fastapi import APIRouter

from ..app import schemas
from ..app.openai_client import FAKE_ANALYSIS_PROVIDER, selected_analysis_provider


router = APIRouter(tags=["Meta"])


# Public (no auth): lets the web client show a demo banner when analyses
# come from the deterministic fake provider.
@router.get("/meta", response_model=schemas.MetaResponse)
def get_meta():
    provider = selected_analysis_provider()
    return {
        "analysis_provider": provider,
        "demo": provider == FAKE_ANALYSIS_PROVIDER,
    }
